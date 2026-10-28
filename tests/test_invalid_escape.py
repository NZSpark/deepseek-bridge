"""JSON 里的非法转义（典型：shell 的 `grep 'A\\|B'`）导致调用被静默丢弃。

真实故障：模型把 `grep 'A\\|B'` 原样写进 JSON 字符串，`\\|` 不是合法 JSON 转义，
`json.loads` 抛错 -> 调用被丢弃 -> 既没解析出 tool_call，也没被判坏重发 ->
整段原文作为文本回给客户端，任务终止。
"""

from __future__ import annotations

import unittest

from deepseek_web.malformed import has_malformed_tool_call, has_no_usable_call
from deepseek_web.toolcalls import _normalize_json_text, parse_tool_calls

BS = chr(92)  # 反斜杠
F = chr(96) * 3  # ```
N = chr(10)
SQ = chr(39)  # 单引号


def fence(body: str) -> str:
    return F + "tool_call" + N + body + N + F


# 模型输出：JSON 字符串里含 \|（非法转义）
BAD_JSON = (
    '{"name": "bash", "arguments": {"command": "grep -n ' + SQ + "A" + BS + "|B" + SQ
    + ' x.kt | head -20"}}'
)


class InvalidEscapeTests(unittest.TestCase):
    def test_normalize_fixes_invalid_escape(self):
        src = '{"a": "x' + BS + '|y"}'
        fixed = _normalize_json_text(src)
        self.assertIn(BS + BS + "|", fixed)

    def test_invalid_escape_call_is_parsed(self):
        calls = parse_tool_calls(fence(BAD_JSON), {"bash"})
        self.assertEqual([c["name"] for c in calls], ["bash"])
        self.assertIn(BS + "|", calls[0]["arguments"]["command"])

    def test_invalid_escape_is_not_malformed(self):
        self.assertFalse(has_malformed_tool_call(fence(BAD_JSON)))
        self.assertFalse(has_no_usable_call(fence(BAD_JSON), {"bash"}))

    def test_xml_tag_with_invalid_escape_is_parsed(self):
        text = "<tool_call>" + N + BAD_JSON + N + "</tool_call>"
        calls = parse_tool_calls(text, {"bash"})
        self.assertEqual([c["name"] for c in calls], ["bash"])

    def test_xml_tag_unparsable_is_malformed(self):
        text = "<tool_call>" + N + '{"name": "bash", "arguments": {' + N + "</tool_call>"
        self.assertEqual(parse_tool_calls(text, {"bash"}), [])
        self.assertTrue(has_no_usable_call(text, {"bash"}))
        self.assertTrue(has_malformed_tool_call(text))


if __name__ == "__main__":
    unittest.main()
