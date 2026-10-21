import unittest

from deepseek_web.malformed import (
    RETRY_PROMPT,
    has_malformed_tool_call,
    has_no_usable_call,
    has_unclosed_tool_fence,
)

F = "```"
N = chr(10)


def fence(body):
    return F + "tool_call" + N + body + N + F


class HasNoUsableCallTests(unittest.TestCase):
    def test_empty_body_is_bad(self):
        self.assertTrue(has_no_usable_call(fence("")))

    def test_truncated_json_is_bad(self):
        self.assertTrue(has_no_usable_call(fence('{"name":"a","arguments":{')))

    def test_trailing_comma_is_bad(self):
        self.assertTrue(has_no_usable_call(fence('{"name":"a","arguments":{},}')))

    def test_bare_marker_empty_is_bad(self):
        self.assertTrue(has_no_usable_call("tool_call"))

    def test_good_call_is_ok(self):
        self.assertFalse(has_no_usable_call(fence('{"name":"bash","arguments":{}}')))

    def test_mixed_good_and_bad_is_ok(self):
        text = fence('{"name":"a","arguments":{}}') + N + fence("{bad")
        self.assertFalse(has_no_usable_call(text))

    def test_python_fence_is_ok(self):
        self.assertFalse(has_no_usable_call(F + "python" + N + "x=1" + N + F))

    def test_no_marker_is_ok(self):
        self.assertFalse(has_no_usable_call("just a normal answer"))


class UnclosedFenceTests(unittest.TestCase):
    def test_unclosed_is_bad(self):
        self.assertTrue(has_unclosed_tool_fence(F + "tool_call" + N + "{}"))

    def test_same_line_open_is_bad(self):
        self.assertTrue(has_unclosed_tool_fence(F + 'tool_call{"name": "a"}'))

    def test_closed_is_ok(self):
        self.assertFalse(has_unclosed_tool_fence(fence("{}")))

    def test_python_fence_not_counted(self):
        self.assertFalse(has_unclosed_tool_fence(F + "python" + N + "x=1" + N + F))


BAR = "\uff5c\uff5c"


def dsml(name, extra=""):
    return f"<{BAR}DSML{BAR} {name}{extra}>"


def dsml_close(name):
    return f"</{BAR}DSML{BAR} {name}>"


def dsml_call(name="bash", command="ls"):
    return N.join([
        dsml("calls"),
        dsml("invoke", ' name="bash"'),
        dsml("parameter", ' name="command" string="true"') + command + dsml_close("parameter"),
        dsml_close("invoke"),
        dsml_close("calls"),
    ])


class DsmlMalformedTests(unittest.TestCase):
    """解析失败的 DSML 也必须要求重发（不能当正常回答交回客户端）。"""

    def test_parsable_dsml_is_ok(self):
        self.assertFalse(has_no_usable_call(dsml_call()))

    def test_dsml_empty_calls_is_bad(self):
        text = dsml("calls") + N + dsml_close("calls")
        self.assertTrue(has_no_usable_call(text))

    def test_dsml_invoke_without_name_is_bad(self):
        text = dsml("invoke") + N + dsml_close("invoke")
        self.assertTrue(has_no_usable_call(text))

    def test_dsml_bad_json_body_is_bad(self):
        # 参数被换行截断：JSON 非法，解析层救不出任何调用
        body = '{"name": "bash", "arguments": {"command": "cd /x' + N + '&& ls"}}'
        text = dsml("calls") + N + body + N + dsml_close("calls")
        self.assertTrue(has_no_usable_call(text))

    def test_dsml_bad_is_malformed_per_segment(self):
        text = dsml("calls") + N + dsml_close("calls")
        self.assertTrue(has_malformed_tool_call(text))

    def test_plain_text_mentioning_dsml_is_ok(self):
        self.assertFalse(has_no_usable_call("这段文本提到 DSML 这个词，但没有标签"))

    def test_truncated_but_parsable_dsml_is_ok(self):
        # 闭标签缺失，但 invoke/parameter 结构完整，解析层能救 -> 不重发
        text = N.join([
            dsml("invoke", ' name="bash"'),
            dsml("parameter", ' name="command" string="true"') + "ls" + dsml_close("parameter"),
        ])
        self.assertFalse(has_no_usable_call(text))


class MalformedPerSegmentTests(unittest.TestCase):
    def test_mixed_is_malformed_per_segment(self):
        text = fence('{"name":"a","arguments":{}}') + N + fence("{bad")
        self.assertTrue(has_malformed_tool_call(text))

    def test_good_is_not_malformed(self):
        self.assertFalse(has_malformed_tool_call(fence('{"name":"a","arguments":{}}')))


class RetryPromptTests(unittest.TestCase):
    def test_has_tool_call_example(self):
        self.assertIn("tool_call", RETRY_PROMPT)

    def test_does_not_show_dsml_shape(self):
        self.assertNotIn("DSML", RETRY_PROMPT)


if __name__ == "__main__":
    unittest.main()
