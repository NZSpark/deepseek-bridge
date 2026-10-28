"""OpenAI Responses API（Codex CLI 专用）兼容层。

Codex CLI 只发送 ``POST /v1/responses``（Responses API），不再支持 chat。
这里把 Responses 请求翻译成本项目内部的 Chat 语义，复用现有 driver / 会话分桶 /
工具解析，再把结果包装回 Responses 对象或命名 SSE 事件。

设计依据：doc/codex_support.md（§2 请求映射 / §3 响应映射 / §9 Pi 兼容性保障）。
硬约束：不改动 /v1/chat/completions，不修改共享组件的签名与行为。
"""

import json
import time
import traceback
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from . import bridge_commands, config, tasks
from .driver import (
    DEFAULT_SESSION_KEY,
    DeepSeekBusyError,
    DeepSeekContextLimitError,
    DeepSeekTimeoutError,
)
from .markdown_tools import effective_tools, run_local_tool_round
from .models import ChatCompletionRequest, ChatMessage, ToolCall
from .prompting import build_prompt, estimate_tokens, extract_images
from .toolcall_retry import complete_with_toolcall_retry
from .toolcalls import _tool_names, parse_tool_calls

# ==================== 请求模型（宽松接收）====================


class ResponsesRequest(BaseModel):
    """Responses API 请求。用 extra="allow" 吞掉未知字段，绝不因 422 打断 Codex。"""

    model_config = ConfigDict(extra="allow")

    model: str = "deepseek-chat"
    input: Any | None = None
    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any | None = None
    max_output_tokens: int | None = None
    stream: bool | None = False
    temperature: float | None = None
    top_p: float | None = None


# ==================== 请求转换：Responses -> Chat ====================


def _tool_to_chat(tool: dict[str, Any]) -> dict[str, Any] | None:
    """Responses 的扁平工具 -> Chat 的嵌套 function 工具。strict 丢弃。"""
    if not isinstance(tool, dict):
        return None
    if tool.get("type") == "function" or "name" in tool:
        name = tool.get("name")
        if not name:
            return None
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            },
        }
    # 已是嵌套格式，直接透传
    if "function" in tool:
        return tool
    return None


def _content_parts_to_text(content: Any) -> str:
    """把 Responses 的 content（string / item 数组）里的文本抽出为纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict) and part.get("type") in (
                "input_text",
                "output_text",
                "text",
            ):
                    text = part.get("text")
                    if isinstance(text, str):
                        pieces.append(text)
        return "\n".join(pieces)
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return str(content)


def _item_to_message(item: dict[str, Any]) -> ChatMessage | None:
    """单个 Responses input item -> ChatMessage。未知类型返回 None（宽松跳过）。"""
    if not isinstance(item, dict):
        return None
    itype = item.get("type")
    if itype == "message" or ("role" in item and "content" in item):
        role = item.get("role", "user")
        text = _content_parts_to_text(item.get("content"))
        return ChatMessage(role=role, content=text)
    if itype == "function_call":
        call = ToolCall(
            id=item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:16]}",
            function={"name": item.get("name", ""), "arguments": item.get("arguments", "{}")},
        )
        return ChatMessage(role="assistant", content=None, tool_calls=[call])
    if itype == "function_call_output":
        return ChatMessage(
            role="tool",
            content=item.get("output", ""),
            tool_call_id=item.get("call_id"),
        )
    return None


def to_chat_request(req: ResponsesRequest) -> ChatCompletionRequest:
    """Responses 请求 -> 内部 ChatCompletionRequest。"""
    messages: list[ChatMessage] = []
    if req.instructions:
        messages.append(ChatMessage(role="system", content=req.instructions))

    raw_input = req.input
    if isinstance(raw_input, str):
        messages.append(ChatMessage(role="user", content=raw_input))
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append(ChatMessage(role="user", content=item))
                continue
            converted = _item_to_message(item)
            if converted is not None:
                messages.append(converted)
            else:
                print(f"[responses] 跳过未知 input item: {item!r}")
    elif raw_input is None:
        pass
    else:
        messages.append(ChatMessage(role="user", content=str(raw_input)))

    tools = None
    if req.tools:
        tools = [t for t in (_tool_to_chat(x) for x in req.tools) if t]
    # 本地 Markdown 工具由桥接层拥有 canonical schema；开关关闭时保持原样。
    tools = effective_tools(tools, req.tool_choice)

    return ChatCompletionRequest(
        model=req.model,
        messages=messages,
        stream=req.stream,
        tools=tools,
        tool_choice=req.tool_choice,
        max_tokens=req.max_output_tokens,
    )


# ==================== 响应构造：Chat -> Responses ====================


def _usage_dict(prompt_tokens: int, completion_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def from_chat_response(
    reply: str,
    model: str,
    prompt_tokens: int,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把 driver 的回复构造为 Responses 非流式响应。"""
    rid = f"resp_{uuid.uuid4().hex[:16]}"
    output: list[dict[str, Any]] = []

    if tool_calls:
        output.extend(
            {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:12]}",
                "call_id": f"call_{uuid.uuid4().hex[:16]}",
                "name": call["name"],
                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                "status": "completed",
            }
            for call in tool_calls
        )
    else:
        output.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:12]}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": reply, "annotations": []}],
        })

    completion_tokens = estimate_tokens(reply)
    return {
        "id": rid,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": _usage_dict(prompt_tokens, completion_tokens),
    }


# ==================== 错误映射 ====================


def _error_payload(message: str, err_type: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": err_type}}


def _map_exception(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, DeepSeekContextLimitError):
        return 400, "context_length_exceeded"
    if isinstance(exc, DeepSeekBusyError):
        return 503, "upstream_busy"
    if isinstance(exc, DeepSeekTimeoutError):
        return 504, "timeout"
    if isinstance(exc, RuntimeError):
        return 502, "upstream_error"
    return 500, "server_error"


# ==================== 共享执行（流式/非流式都走这里）====================


async def run_chat(
    request: ChatCompletionRequest,
    driver,
    session_key: str | None,
    on_delta=None,
):
    """执行一次上游对话，返回 (reply, tool_calls, sent_prompt)。

    与 server.py 的 chat 路径共用 driver / prompting / toolcalls，但不改其代码。

    桥内命令（``/bridge ...``）在这里被识别：整条消息就是那一行命令时直接返回桥自己的
    应答，**不发给网页版**（流式 / 非流式两条路径都经过本函数）。
    """
    command_reply = await bridge_commands.handle_command(request.messages, driver, session_key)
    if command_reply is not None:
        # sent_prompt 记空串：命令没有上游 prompt，不能算成网页版用量
        return command_reply, [], ""

    # 任务快照：记录本轮 messages，轮转播种时用它续接任务（不丢任务目标）。
    bucket = session_key or DEFAULT_SESSION_KEY
    tasks.record(bucket, request.messages)
    task_block = tasks.resume_block(bucket)

    delta_prompt = build_prompt(request.messages, request.tools, request.tool_choice)
    seeded_prompt = build_prompt(
        request.messages,
        request.tools,
        request.tool_choice,
        seed=True,
        seed_max_chars=config.SEED_MAX_CHARS,
        task_block=task_block,
    )
    prompt = seeded_prompt if driver.needs_seed(session_key) else delta_prompt
    if not prompt:
        raise ValueError("需要包含至少一条 user / tool 消息")

    wants_tools = bool(request.tools) and request.tool_choice != "none"
    # 图片：抽取真实字节（base64）或远程 URL，提交前粘贴进网页输入框；失败降级为
    # prompt 里的 ``[图片]`` 文本引用。开关关闭 / 无图片时为空列表。
    images = []
    if config.IMAGE_PASTE_ENABLED:
        images = extract_images(request.messages, only_delta=True)[: config.IMAGE_PASTE_MAX]
    valid_names = _tool_names(request.tools)
    reply = await complete_with_toolcall_retry(
        driver,
        prompt,
        on_delta=on_delta,
        seeded_prompt=seeded_prompt,
        key=session_key,
        images=images,
        wants_tools=wants_tools,
        valid_names=valid_names,
    )
    tool_calls = parse_tool_calls(reply, valid_names) if wants_tools else []
    # 本地 Markdown 工具：执行一轮把真实结果回喂网页模型，再决定最终形态。
    # 多轮闭环由 chat 路径的 run_local_edit_markdown_loop 负责；这里只做一轮，
    # 避免把整条多轮循环塞进一次缓冲的上游调用里。
    reply, tool_calls, _handled = await run_local_tool_round(
        driver,
        reply,
        request.tools,
        request.tool_choice,
        seeded_prompt,
        session_key,
    )
    sent_prompt = driver.sent_prompt(session_key) or prompt
    return reply, tool_calls, sent_prompt


# ==================== 非流式入口 ====================


async def handle_responses(
    req: ResponsesRequest,
    session_key: str | None,
    driver,
) -> Any:
    """处理 /v1/responses。返回 dict 或 StreamingResponse。"""
    from fastapi.responses import JSONResponse

    chat_req = to_chat_request(req)
    if not chat_req.messages:
        return JSONResponse(
            status_code=400,
            content=_error_payload("input 不能为空", "invalid_request_error"),
        )

    # 桥内命令（``/bridge ...``）不经过网页版，因此**浏览器没起来也要答**：页面失效 /
    # 初始化失败时它们正是用户唯一的排障入口。普通请求仍按下面的检查 503。
    if driver.page is None and not bridge_commands.is_command(chat_req.messages):
        return JSONResponse(
            status_code=503,
            content=_error_payload(
                "浏览器尚未就绪，请确认已完成登录。", "upstream_error"
            ),
        )

    if req.stream:
        from fastapi.responses import StreamingResponse

        return StreamingResponse(
            stream_responses(chat_req, driver, session_key),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        reply, tool_calls, sent_prompt = await run_chat(chat_req, driver, session_key)
    except DeepSeekContextLimitError as exc:
        print("\n[ERR] responses: 上下文超限:")
        traceback.print_exc()
        return JSONResponse(
            status_code=400, content=_error_payload(str(exc), "context_length_exceeded")
        )
    except DeepSeekBusyError as exc:
        return JSONResponse(status_code=503, content=_error_payload(str(exc), "upstream_busy"))
    except DeepSeekTimeoutError as exc:
        return JSONResponse(status_code=504, content=_error_payload(str(exc), "timeout"))
    except ValueError as exc:
        return JSONResponse(
            status_code=400, content=_error_payload(str(exc), "invalid_request_error")
        )
    except RuntimeError as exc:
        return JSONResponse(status_code=502, content=_error_payload(str(exc), "upstream_error"))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return JSONResponse(status_code=500, content=_error_payload(str(exc), "server_error"))

    return from_chat_response(
        reply, req.model, estimate_tokens(sent_prompt), tool_calls or None
    )


# ==================== 流式 SSE（命名事件）====================


def _sse(event_type: str, payload: dict[str, Any]) -> str:
    """命名 SSE 事件：event 名 + data（data 载荷自带 type，Codex 反序列化要求）。"""
    data = dict(payload)
    data["type"] = event_type
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_responses(
    request: ChatCompletionRequest,
    driver,
    session_key: str | None,
):
    """以 Responses 命名 SSE 事件流输出。"""
    import asyncio

    rid = f"resp_{uuid.uuid4().hex[:16]}"
    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = request.model

    base_response = {
        "id": rid,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "model": model,
        "output": [],
    }

    seq = 0

    def evt(event_type: str, payload: dict[str, Any]) -> str:
        nonlocal seq
        payload = dict(payload)
        payload["sequence_number"] = seq
        seq += 1
        return _sse(event_type, payload)

    yield evt("response.created", {"response": base_response})

    wants_tools = bool(request.tools) and request.tool_choice != "none"

    # 工具模式默认先缓冲（要判断是不是 tool_calls）。可用 RESPONSES_TOOL_BUFFER=false
    # 关闭缓冲：此时工具模式下也会实时吐字，但若最终解析出 tool_calls，已发的文本事件
    # 不会撤回（Codex 会以 function_call 为准）。
    buffer_tools = wants_tools and config.RESPONSES_TOOL_BUFFER
    queue: asyncio.Queue[tuple] = asyncio.Queue()

    async def on_delta(piece: str):
        await queue.put(("delta", piece))

    async def runner():
        try:
            reply, tool_calls, sent_prompt = await run_chat(
                request, driver, session_key,
                on_delta=None if buffer_tools else on_delta,
            )
            await queue.put(("done", (reply, tool_calls, sent_prompt, None, None)))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            _status, err_type = _map_exception(exc)
            await queue.put(("done", (None, [], None, str(exc), err_type)))

    task = asyncio.create_task(runner())

    # 文本消息的结构（先声明 output_item / content_part）
    yield evt("response.output_item.added", {
        "output_index": 0,
        "item": {
            "type": "message",
            "id": msg_id,
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        },
    })
    yield evt("response.content_part.added", {
        "item_id": msg_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })

    streamed = ""
    result = None
    keepalive_s = config.RESPONSES_KEEPALIVE_S
    while result is None:
        try:
            kind, payload = await asyncio.wait_for(
                queue.get(),
                timeout=keepalive_s if keepalive_s and keepalive_s > 0 else None,
            )
        except TimeoutError:
            yield ": keep-alive\n\n"
            continue
        if kind == "delta":
            streamed += payload
            yield evt("response.output_text.delta", {
                "item_id": msg_id,
                "output_index": 0,
                "content_index": 0,
                "delta": payload,
            })
        else:
            result = payload

    await task
    reply, tool_calls, sent_prompt, error, err_type = result

    if error:
        yield evt("response.failed", {
            "response": {
                **base_response,
                "status": "failed",
                "error": {"message": error, "type": err_type},
            }
        })
        return

    if tool_calls:
        # 工具调用：跳过文本事件，改发 function_call 事件
        for index, call in enumerate(tool_calls):
            call_id = f"call_{uuid.uuid4().hex[:16]}"
            yield evt("response.output_item.added", {
                "output_index": index,
                "item": {
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex[:12]}",
                    "call_id": call_id,
                    "name": call["name"],
                    "arguments": "",
                    "status": "in_progress",
                },
            })
            args_str = json.dumps(call["arguments"], ensure_ascii=False)
            yield evt("response.function_call_arguments.delta", {
                "item_id": call_id,
                "output_index": index,
                "delta": args_str,
            })
            yield evt("response.function_call_arguments.done", {
                "item_id": call_id,
                "output_index": index,
                "arguments": args_str,
            })
        final_output: list[dict[str, Any]] = [
            {
                "type": "function_call",
                "call_id": f"call_{uuid.uuid4().hex[:16]}",
                "name": c["name"],
                "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                "status": "completed",
            }
            for c in tool_calls
        ]
    else:
        full = reply if reply is not None else streamed
        if not streamed and full:
            # 工具模式缓冲后未流式吐字，这里补发
            for i in range(0, len(full), 64):
                yield evt("response.output_text.delta", {
                    "item_id": msg_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": full[i:i + 64],
                })
        yield evt("response.output_text.done", {
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "text": full,
        })
        yield evt("response.content_part.done", {
            "item_id": msg_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": full, "annotations": []},
        })
        final_output = [{
            "type": "message",
            "id": msg_id,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": full, "annotations": []}],
        }]

    yield evt("response.output_item.done", {
        "output_index": 0,
        "item": final_output[0],
    })

    completed = {
        **base_response,
        "status": "completed",
        "output": final_output,
        "usage": _usage_dict(
            estimate_tokens(sent_prompt or ""), estimate_tokens(reply or streamed or "")
        ),
    }
    yield evt("response.completed", {"response": completed})
