"""坏结构检测：网页端把 tool_call 生成坏时的判据（设计见 doc/toolcall_retry.md）。

判据：出现了 tool_call 标记，但包裹的内容不是合法 JSON 调用 -> 判为损坏，需要重发。
合法 = 那段内容能被 json.loads 解析成 JSON 对象，且含非空字符串 name 与对象型 arguments。
判据刻意不看普通 python/bash/json 等其它语言围栏；不把 DSML 标签当损坏（解析层能救）。
"""
from __future__ import annotations

import json
import re

# 显式围栏：开围栏到闭合。body 可以为空（损坏形态）；未闭合以 Z 兜底。
_FENCED = re.compile(r"```[ \t]*(tool_call)[ \t]*\n(.*?)(?:```|\Z)", re.DOTALL | re.IGNORECASE)

# 只匹配开围栏本身，判断是否闭合。语言标记后不要求必须换行（同行/CRLF 都要能命中）。
TOOL_CALL_OPEN = re.compile(r"```[ \t]*(tool_call)\b(?![0-9A-Za-z_])", re.IGNORECASE)

# DSML 结构化开标签（原生 DSL）。出现即「声称要调用」；解析层救不出来就要重发。
_DSML_BAR = r"[｜|]{1,4}"
DSML_CLAIM = re.compile(
    rf"<\s*/?\s*{_DSML_BAR}\s*DSML\s*{_DSML_BAR}\s*(?:calls|invoke)\b",
    re.IGNORECASE,
)

# 裸标记：只在行首/空白后出现才算。
BARE = re.compile(r"^[ \t]*tool_call\b(?![0-9A-Za-z_])[ \t]*[:：]?[ \t]*(.*)$", re.MULTILINE | re.IGNORECASE)  # noqa: E501 正则单行更易读

RETRY_PROMPT = '调用失败，重新调用。需要调用工具时，只输出一个如下格式的代码块（arguments 必须是合法 JSON）：\n```tool_call\n"{"name": "工具名", "arguments": {参数对象}}"\n```\n一次只发一条指令，不要有多余解释的话或标记、标号。代码块要用```完成闭合。'  # noqa: E501 提示词单行

def has_unclosed_tool_fence(text):
    if not text:
        return False
    pos = 0
    while True:
        m = TOOL_CALL_OPEN.search(text, pos)
        if not m:
            break
        close = text.find("```", m.end())
        if close < 0:
            return True
        pos = close + 3
    return False


def _loads_lenient(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    try:
        from .toolcalls import _normalize_json_text
        fixed = _normalize_json_text(text)
    except Exception:  # noqa: BLE001 解析层缺席/异常时退回「只认原样」
        return None
    if fixed == text:
        return None
    try:
        return json.loads(fixed)
    except (ValueError, TypeError):
        return None


def _is_valid_call(body):
    text = (body or "").strip()
    if not text:
        return False
    obj = _loads_lenient(text)
    if not isinstance(obj, dict):
        return False
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        return False
    return isinstance(obj.get("arguments"), dict)


def _iter_bodies(text):
    fenced = False
    for match in _FENCED.finditer(text):
        fenced = True
        yield match.group(2)
    if fenced:
        return
    for match in BARE.finditer(text):
        yield match.group(1)


def has_no_usable_call(text, valid_names=None):
    if not text:
        return False
    try:
        from .toolcalls import parse_tool_calls
        if parse_tool_calls(text, valid_names):
            return False
    except Exception:  # noqa: BLE001, S110 解析层缺席/异常不影响判据本身
        pass
    if has_unclosed_tool_fence(text):
        return True
    saw_marker = False
    for b in _iter_bodies(text):
        saw_marker = True
        if _is_valid_call(b):
            return False
    if saw_marker:
        return True
    # DSML 形态：出现 calls / invoke 标签（声称要调用），但解析层一个调用都救不出来
    # （参数被换行截断、JSON 不合法、标签对不上……）→ 同样必须重发。
    # 解析层能救出调用时上面已 return False，所以不会误伤合法的 DSML。
    return bool(DSML_CLAIM.search(text))


def _parse_any(text):
    """解析层能否从这段文本里救出至少一个调用。"""
    try:
        from .toolcalls import parse_tool_calls
        return bool(parse_tool_calls(text))
    except Exception:  # noqa: BLE001
        return False


def has_malformed_tool_call(text):
    if not text:
        return False
    if has_unclosed_tool_fence(text):
        return True
    # DSML 声称要调用却救不出任何调用 → 这段调用本身不可用
    if DSML_CLAIM.search(text) and not _parse_any(text):
        return True
    if any(not _is_valid_call(m.group(2)) for m in _FENCED.finditer(text)):
        return True
    if _FENCED.search(text):
        return False
    return any(not _is_valid_call(m.group(1)) for m in BARE.finditer(text))


def has_empty_fence_triple(text):
    return has_malformed_tool_call(text)
