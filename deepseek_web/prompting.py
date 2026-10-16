"""把客户端的消息数组转换成网页输入框里的一整段文本，以及若干纯文本工具函数。

图片支持（见 doc/image_upload.md）：网页输入框只接受文本，图片分片先被渲染成
一条引用行（``[图片] …``）作为兜底锚点；真正的图片字节由 :mod:`deepseek_web.chat_io`
在填写文本之后、提交之前**合成 paste 事件**粘贴进输入框（见 ``_paste_images``）。
"""

import base64
from dataclasses import dataclass
from typing import Any

from .models import ChatMessage
from .toolcalls import format_tool_call_emphasis, format_tools_instruction

#: 图片在文本 prompt 里的引用标记（兜底锚点，绝不包含完整 base64 字节）。
MARK_IMAGE = "[图片]"


@dataclass(slots=True)
class ImageAttachment:
    """一张待粘贴进网页输入框的图片。

    ``data`` 为已知原始字节（来自 base64 data URI）；``url`` 为远程地址，
    由调用方惰性下载后再填回 ``data``。``name`` / ``mime`` 用于构造浏览器里的
    ``File`` 对象（合成 paste 事件时使用）。
    """

    name: str
    mime: str
    data: bytes | None = None
    url: str | None = None


def _parse_data_uri(url: str) -> tuple[str, bytes] | None:
    """把 ``data:<mime>;base64,<payload>`` 解析为 (mime, bytes)；失败返回 None。"""
    header, _, payload = url.partition(",")
    if ";base64" not in header or not payload:
        return None
    mime = header.removeprefix("data:").split(";", 1)[0] or "application/octet-stream"
    try:
        return mime, base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return None


def _image_url(part: dict[str, Any]) -> str | None:
    """从图片分片里取出 URL（兼容 image_url 为 dict / str、以及裸 url 字段）。"""
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        url = image_url.get("url")
    elif isinstance(image_url, str):
        url = image_url
    else:
        url = part.get("url")
    return url.strip() if isinstance(url, str) and url.strip() else None


def _is_image_part(part: dict[str, Any]) -> bool:
    """该分片是否是图片（兼容 OpenAI / Responses API 的多种写法）。"""
    part_type = str(part.get("type", "")).lower()
    if part_type in {"image_url", "input_image", "image"}:
        return True
    return "image_url" in part or "image" in part


def _image_reference(part: dict[str, Any]) -> str | None:
    """把图片分片转成网页版可读的文本引用行。

    远程 URL 原样保留；base64 data URI 仅记录类型与估算大小，**不外泄完整字节**
    （否则 prompt 会爆炸且会泄漏图片内容）。无法识别时返回 None。
    """
    url = _image_url(part)
    if url is None:
        return None
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        media = header.removeprefix("data:").split(";", 1)[0] or "image"
        approx_kb = max(1, len(payload) * 3 // 4 // 1024)
        return f"{MARK_IMAGE} ({media}, ~{approx_kb}KB base64)"
    return f"{MARK_IMAGE} {url}"


def _messages_since_last_assistant(messages: list[Any]) -> list[Any]:
    """返回最后一条 assistant 消息之后的“本轮新增”消息（兼容 dict / 对象）。

    没有 assistant 或没有新增时返回原列表——宁多贴一张图，也不漏掉本该粘贴的图片。
    """
    last = -1
    for index, message in enumerate(messages):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role == "assistant":
            last = index
    if last < 0 or last + 1 >= len(messages):
        return messages
    return messages[last + 1:]


def extract_images(messages: list[Any], only_delta: bool = False) -> list[ImageAttachment]:
    """从消息里收集图片分片，供浏览器真实粘贴。

    base64 data URI 直接解码为字节；远程 URL 保留 ``url``（``data=None``）由调用方
    惰性下载。顺序保留；数量上限由调用方裁剪。

    :param only_delta: 只收集「本轮新增消息」（最后一条 assistant 之后）里的图片。
        图片只需在它**首次出现的那一轮**粘贴一次——它已经进入网页会话上下文后，
        后续轮次重发整段 messages 时不应再重复粘贴（历史图片保留 ``[图片]`` 文本
        引用行即可）。默认 False（收集全部）以兼容既有调用方。
    """
    if only_delta:
        messages = _messages_since_last_assistant(messages)
    attachments: list[ImageAttachment] = []
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for part in content:
            if not (isinstance(part, dict) and _is_image_part(part)):
                continue
            url = _image_url(part)
            if not url:
                continue
            if url.startswith("data:"):
                parsed = _parse_data_uri(url)
                if parsed is None:
                    continue
                mime, data = parsed
                if not data:
                    # 非法 / 空的 base64 解出来是 b""：不算有效图片，直接跳过，
                    # 免得下游粘贴一个 0 字节的 File。
                    continue
                ext = mime.split("/")[-1] or "png"
                attachments.append(ImageAttachment(name=f"image.{ext}", mime=mime, data=data))
            else:
                name = url.rsplit("/", 1)[-1].split("?", 1)[0] or "image.png"
                attachments.append(ImageAttachment(name=name, mime="image/png", url=url))
    return attachments


def _content_to_text(content: Any) -> str:
    """把 OpenAI 的 content 归一化为纯文本。

    content 可能是：
      - None
      - 字符串
      - 内容分片数组，例如 [{"type": "text", "text": "hi"}]
    图片分片被转成文本引用行（``[图片] …``），不影响文本分片的解析。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                if _is_image_part(part):
                    ref = _image_reference(part)
                    if ref:
                        pieces.append(ref)
                    continue
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(pieces)
    if isinstance(content, dict):
        if _is_image_part(content):
            return _image_reference(content) or ""
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return str(content)


def estimate_tokens(text: str) -> int:
    """估算 token 数（仅用于填充 OpenAI 的 usage 字段，不是精确值）。

    CJK 字符约 1 char/token，其余字符约 4 char/token。
    不引入 tiktoken：那是 OpenAI 的分词器，算 DeepSeek 的 token 只会
    得到一个“看起来很精确但其实是错的”数字，反而更容易误导客户端做上下文裁剪。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u30ff")
    other = len(text) - cjk
    return max(1, cjk + other // 4)


def _delta_piece(streamed: str, current: str) -> tuple[str | None, str]:
    """计算 ``current`` 相对「已经发给客户端的内容」真正新增的部分。

    网页版在生成中可能重排 / 替换回复节点，导致 ``current`` 不再以之前的内容为前缀。
    此时不能用简单的 ``startswith`` 判定（会静默丢字），也不应从头发一遍
    （会重复）。这里退回「公共前缀之后的部分」。

    :return: (需要补发的内容, 客户端补发后实际拥有的内容)；无新增时第一项为 None。
    """
    if current == streamed:
        return None, streamed
    if current.startswith(streamed):
        return current[len(streamed):], current
    limit = min(len(streamed), len(current))
    index = 0
    while index < limit and streamed[index] == current[index]:
        index += 1
    piece = current[index:] or None
    return piece, streamed[:index] + (piece or "")


# 播种时的默认字符预算（server 会传入 config.SEED_MAX_CHARS 覆盖）
DEFAULT_SEED_MAX_CHARS = 12000


def _render_message(message: ChatMessage) -> str:
    """把单条消息渲染成喂给网页版的一段文本。"""
    content = _content_to_text(message.content).strip()
    if message.role == "system":
        return f"[系统指令]\n{content}"
    if message.role == "tool":
        tag = f" {message.tool_call_id}" if message.tool_call_id else ""
        return f"[工具执行结果{tag}]\n{content}"
    if message.role == "assistant":
        return f"[你之前的回复]\n{content}"
    return content


def _last_assistant_index(messages: list[ChatMessage]) -> int:
    last = -1
    for index, message in enumerate(messages):
        if message.role == "assistant":
            last = index
    return last


def _run_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """取出“最后一条 assistant 之后”的新增消息。"""
    last_assistant = _last_assistant_index(messages)
    delta = messages[last_assistant + 1:] if last_assistant >= 0 else messages
    if not delta:
        # 兜底：没有新消息时，退回最后一条 user 消息
        delta = [m for m in messages if m.role == "user"][-1:]
    return delta


def _seed_messages(messages: list[ChatMessage], max_chars: int):
    """为新会话准备“播种”内容：尽量带上完整上下文，超出预算时保留最近的。

    网页会话一旦轮转（新开会话），后端的上下文就清空了。此时如果还只发增量，
    模型会收到一条“没有前因”的孤立消息——不报错，但会胡编。

    :return: (system 消息, 保留的其余消息, 是否发生了截断)
    """
    systems = [m for m in messages if m.role == "system"]
    rest = [m for m in messages if m.role != "system"]

    kept: list[ChatMessage] = []
    used = sum(len(_content_to_text(m.content)) for m in systems)
    truncated = False
    for message in reversed(rest):
        size = len(_content_to_text(message.content))
        if kept and used + size > max_chars:
            truncated = True
            break
        kept.append(message)
        used += size
    kept.reverse()
    return systems, kept, truncated


def build_prompt(  # noqa: PLR0913 扁平签名是刻意的：prompt 的全部输入，拆成 dataclass 只会让 9 个调用点更绕
    messages: list[ChatMessage],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any | None = None,
    *,
    seed: bool = False,
    seed_max_chars: int | None = None,
    task_block: str | None = None,
) -> str:
    """把客户端发来的完整 OpenAI 消息数组，转换成要发给网页输入框的文本。

    * ``seed=False``（默认）：网页版是一个持续存在的会话，无需每轮重发全部历史，
      只发送“最后一条 assistant 消息之后”的新增消息（新的 user 指令或 tool 结果）。
    * ``seed=True``：当前会话是**新开的**，必须把既有上下文一次性播种进去，
      否则模型会收到一条没有前因的孤立消息。
    * ``task_block``：任务快照（见 ``tasks.resume_block``）。仅在 ``seed=True`` 时
      生效，会被放在**历史之前、上下文重建头之后**，因此**不会**被
      ``seed_max_chars`` 的尾部截断逻辑丢掉——这是“轮转不丢任务”的关键。
    """
    if seed:
        systems, kept, truncated = _seed_messages(
            messages, seed_max_chars or DEFAULT_SEED_MAX_CHARS
        )
        parts: list[str] = [
            "[上下文重建] 这是一个新会话。以下是本次任务此前的对话记录，请据此继续，不要从头重做。"
        ]
        if task_block:
            parts.append(task_block)
        if truncated:
            parts.append("（更早的部分因长度限制已省略，如需可向我确认。）")
        parts.extend(_render_message(m) for m in systems + kept)
    else:
        parts = [_render_message(m) for m in _run_messages(messages)]

    use_tools = bool(tools) and tool_choice != "none"
    if seed and use_tools:
        # 新 bucket / 重置后的第一轮：把带围栏示例的格式强调块放在播种开头，
        # 模型最容易在这种时候退回原生 DSML 标记，双保险。
        parts.insert(1, format_tool_call_emphasis())

    prompt = "\n\n".join(part for part in parts if part).strip()

    if use_tools:
        prompt = (prompt + "\n\n" + format_tools_instruction(tools)).strip()

    return prompt
