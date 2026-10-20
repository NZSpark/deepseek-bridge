import unittest

from deepseek_web import config
from deepseek_web.errors import MalformedToolCallError
from deepseek_web.toolcall_retry import complete_with_toolcall_retry

F = "```"
N = chr(10)


def good():
        return F + "tool_call" + N + '{"name":"bash","arguments":{}}' + N + F


def bad():
        return F + "tool_call" + N + F


class FakeDriver:
        def __init__(self, replies):
                self.replies = list(replies)
                self.calls = []
        async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None, images=None):
                self.calls.append({"prompt": prompt, "images": images})
                r = self.replies.pop(0)
                return r, []


class RetryFlowTests(unittest.IsolatedAsyncioTestCase):
        async def test_bad_then_good_retries_once(self):
                d = FakeDriver([bad(), good()])
                text, _blocks = await complete_with_toolcall_retry(d, "p", wants_tools=True)
                self.assertEqual(len(d.calls), 2)
                self.assertFalse(has_bad(text))
        async def test_retry_has_no_images(self):
                d = FakeDriver([bad(), good()])
                await complete_with_toolcall_retry(d, "p", images=["img"], wants_tools=True)
                self.assertEqual(d.calls[0]["images"], ["img"])
                self.assertIn(d.calls[1]["images"], (None, []))
        async def test_not_wants_tools_no_retry(self):
                d = FakeDriver([bad()])
                await complete_with_toolcall_retry(d, "p", wants_tools=False)
                self.assertEqual(len(d.calls), 1)
        async def test_exhausted_raises(self):
                d = FakeDriver([bad(), bad(), bad(), bad()])
                old = config.MALFORMED_RETRIES
                config.MALFORMED_RETRIES = 1
                try:
                        with self.assertRaises(MalformedToolCallError):
                                await complete_with_toolcall_retry(d, "p", wants_tools=True)
                finally:
                        config.MALFORMED_RETRIES = old


def has_bad(text):
        from deepseek_web.malformed import has_no_usable_call
        return has_no_usable_call(text)


if __name__ == "__main__":
        unittest.main()
