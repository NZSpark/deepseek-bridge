"""把一次上游对话编码成 OpenAI 兼容的 SSE 流。"""

import asyncio
import json
import time
import traceback
import uuid
from typing import Any

from . import config
from .driver import (
    DeepSeekBusyError,
    DeepSeekContextLimitError,
    DeepSeekTimeoutError,
    MalformedToolCallError,
)
from .models import ChatCompletionRequest
from .prompting import estimate_tokens, extract_images
from .token_speed import TokenSpeed, apply_stream_timing
from .toolcall_retry import complete_with_toolcall_retry
from .toolcalls import _tool_names, parse_tool_calls

#: SSE 合成流的分帧大小：客户端看到的「增量」就是这个粒度的切片。
#: 短回复只有 1 帧是正常的；长回复的帧数 = ceil(len/CHUNK_CHARS)。
CHUNK_CHARS = 64


def _chunk_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _collect_images(messages):
    """抽取本轮要粘贴的图片并按上限裁剪（见 doc/image_upload.md）。

    开关关闭时直接返回空列表（全部降级为 prompt 里的 ``[图片]`` 文本引用）。
    """
    if not config.IMAGE_PASTE_ENABLED:
        return []
    images = extract_images(messages, only_delta=True)
    if len(images) > config.IMAGE_PASTE_MAX:
        images = images[: config.IMAGE_PASTE_MAX]
    return images


async def _stream_bridge_reply(request: ChatCompletionRequest, reply: str):
    """把桥内命令（``/bridge ...``）的应答编码成普通回复同形的 SSE 流。

    命令由桥本地应答、**不经过网页版**：页面失效 / 浏览器未初始化时它也必须能用，
    所以这里不发任何上游请求。usage 的 ``prompt_tokens`` 记 0（桥的应答不是网页版用量）。
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = request.model

    def encode(delta: dict[str, Any] | None, finish: str | None = None) -> str:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield encode({"role": "assistant"})
    for piece in _chunk_text(reply):
        yield encode({"content": piece})
    yield encode(None, finish="stop")

    include_usage = isinstance(request.stream_options, dict) and bool(
        request.stream_options.get("include_usage")
    )
    if include_usage:
        completion_tokens = estimate_tokens(reply)
        usage_payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": completion_tokens,
                "total_tokens": completion_tokens,
            },
        }
        yield f"data: {json.dumps(usage_payload, ensure_ascii=False)}\n\n"

    yield "data: [DONE]\n\n"


def _patch_stream_timing(driver, session_key, started_at, first_content_at, done_at):
    """把流式端到端计时并入该桶最近一轮 speed（服务端发送口径）。

    只更新已有的最近一轮样本，不新增样本；driver 不支持时静默跳过。
    """
    patch = getattr(driver, "patch_last_speed", None)
    stats_fn = getattr(driver, "speed_stats", None)
    if not callable(patch) or not callable(stats_fn):
        return
    try:
        last = (stats_fn(session_key) or {}).get("last") or {}
        if not last:
            return
        known = {f: last[f] for f in TokenSpeed.__dataclass_fields__ if f in last}
        base = TokenSpeed(**known)
        updated = apply_stream_timing(
            base,
            started_at=started_at,
            first_content_at=first_content_at,
            done_at=done_at,
        )
        patch(
            {
                "stream_first_content_latency_s": updated.stream_first_content_latency_s,
                "stream_completion_window_s": updated.stream_completion_window_s,
                "stream_effective_output_tps": updated.stream_effective_output_tps,
                "request_e2e_s": updated.request_e2e_s,
            },
            key=session_key,
        )
    except Exception:  # noqa: BLE001 计时补丁失败不得影响正常响应
        pass


async def _stream_chat_completion(
    request: ChatCompletionRequest,
    prompt: str,
    driver,
    seeded_prompt: str | None = None,
    session_key: str | None = None,
):
    """以 OpenAI SSE 格式输出 chunk，兼容 Pi 的 openai-completions 流式解析。

    ``driver`` 由调用方（server）注入，避免与本模块形成循环依赖。
    ``seeded_prompt`` 在需要轮转到新会话时使用（重放历史）。
    ``session_key`` 为按任务隔离会话的桶（None = 默认桶）。
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = request.model
    wants_tools = bool(request.tools) and request.tool_choice != "none"
    # 图片：从消息里抽取真实字节（base64）或远程 URL，提交前粘贴进网页输入框。
    images = _collect_images(request.messages)
    # 流式服务端计时：t0 = 进入流式处理；t10/t11 在真正写出正文 / [DONE] 时记录。
    stream_started_at = time.monotonic()
    first_content_at: float | None = None
    done_at: float | None = None

    def encode(delta: dict[str, Any] | None, finish: str | None = None,
               choices: list | None = None) -> str:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": choices if choices is not None else [
                {"index": 0, "delta": delta or {}, "finish_reason": finish}
            ],
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    # 先发 role 头
    yield encode({"role": "assistant"})

    queue: asyncio.Queue[tuple] = asyncio.Queue()

    async def on_delta(piece: str):
        await queue.put(("delta", piece))

    async def runner():
        try:
            # 需要工具时先缓冲（等解析出 tool_calls 再决定输出形态），因此不实时吐字。
            # 坏结构重发在本函数内完成：工具模式下 on_delta 为空、尚未吐出任何增量，
            # 因此重发对客户端不可见（不会看到两段内容）。
            reply, blocks = await complete_with_toolcall_retry(
                driver,
                prompt,
                on_delta=None if wants_tools else on_delta,
                seeded_prompt=seeded_prompt,
                key=session_key,
                images=images,
                wants_tools=wants_tools,
                valid_names=_tool_names(request.tools),
            )
            await queue.put(("done", (reply, blocks, None, None)))
        except DeepSeekContextLimitError as exc:
            # 给客户端一个可区分的类型，而不是笼统的 server_error
            print("\n[ERR] 网页会话已达上下文长度上限:")
            traceback.print_exc()
            await queue.put(("done", (None, [], str(exc), "context_length_exceeded")))
        except DeepSeekBusyError as exc:
            # 本地排队保护：同一会话桶已有请求在跑且等锁超时，对应 HTTP 503 / upstream_busy
            print(f"\n[繁忙] {exc}")
            await queue.put(("done", (None, [], str(exc), "upstream_busy")))
        except DeepSeekTimeoutError as exc:
            # 与 server.py 的非流式分支保持一致：超时是 504/timeout，而不是 500
            print("\n[ERR] 等待 DeepSeek 回复超时（已重试）:")
            traceback.print_exc()
            await queue.put(("done", (None, [], str(exc), "timeout")))
        except MalformedToolCallError as exc:
            # 上游给了回复但 tool_call 坏且重发耗尽：上游质量问题，客户端可重试。
            print(f"\n[重发] 坏结构重发耗尽: {exc}")
            await queue.put(("done", (None, [], str(exc), "upstream_error")))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            await queue.put(("done", (None, [], str(exc), "server_error")))

    task = asyncio.create_task(runner())

    streamed = False
    reply_content = ""
    error: str | None = None
    error_type = "server_error"
    keepalives = 0

    while True:
        try:
            kind, payload = await asyncio.wait_for(queue.get(), timeout=10.0)
        except TimeoutError:
            # 网页版生成较慢，发送 SSE 注释保活，避免 Pi 侧超时断连。
            # 带 tools 时回复必须先完整缓冲才能判断是不是 tool_calls，
            # 因此这段时间客户端看不到内容 —— 用注释保活 + 日志保持可观测。
            keepalives += 1
            if config.DEBUG:
                print(
                    f"[debug] 等待上游回复中（已发 {keepalives} 次 keep-alive，"
                    f"工具模式={wants_tools}）"
                )
            yield ": keep-alive\n\n"
            continue

        if kind == "delta":
            streamed = True
            if first_content_at is None:
                first_content_at = time.monotonic()
            yield encode({"content": payload})
        else:
            reply_content, _blocks, error, error_type = payload
            break

    await task

    if error:
        payload = json.dumps(
            {"error": {"message": error, "type": error_type}}, ensure_ascii=False
        )
        yield f"data: {payload}\n\n"
        yield "data: [DONE]\n\n"
        return

    tool_calls = parse_tool_calls(reply_content, _tool_names(request.tools)) if wants_tools else []

    if tool_calls:
        for index, call in enumerate(tool_calls):
            yield encode({
                "tool_calls": [{
                    "index": index,
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {"name": call["name"], "arguments": ""},
                }]
            })
            arguments_str = json.dumps(call["arguments"], ensure_ascii=False)
            for piece in _chunk_text(arguments_str):
                yield encode({"tool_calls": [{"index": index, "function": {"arguments": piece}}]})
        yield encode(None, finish="tool_calls")
    else:
        if not streamed and reply_content:
            for piece in _chunk_text(reply_content):
                if first_content_at is None:
                    first_content_at = time.monotonic()
                yield encode({"content": piece})
        yield encode(None, finish="stop")

    include_usage = isinstance(request.stream_options, dict) and bool(
        request.stream_options.get("include_usage")
    )
    if include_usage:
        # usage 用真正发出去的 prompt 估算（driver 可能选了播种版 / 中途轮转过）。
        # 按会话桶读取，并发时不会拿到别的 Agent 的 prompt；回退到入参 prompt 兼容假 driver。
        sent_prompt = driver.sent_prompt(session_key) or prompt
        completion_tokens = estimate_tokens(reply_content)
        usage_payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": estimate_tokens(sent_prompt),
                "completion_tokens": completion_tokens,
                "total_tokens": estimate_tokens(sent_prompt) + completion_tokens,
            },
        }
        yield f"data: {json.dumps(usage_payload, ensure_ascii=False)}\n\n"

    done_at = time.monotonic()
    _patch_stream_timing(driver, session_key, stream_started_at, first_content_at, done_at)
    yield "data: [DONE]\n\n"
