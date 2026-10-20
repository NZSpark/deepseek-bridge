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
