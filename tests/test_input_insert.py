"""输入框写入 / 提交（长 prompt 防卡死）的回归测试，用假 page 驱动，不开浏览器。

覆盖的都是真实踩过的坑（参照姊妹项目 gemini-bridge 的 chat_io.py）：

  * 一次性 ``fill()`` 长 prompt 会把网页主线程排成长任务，Playwright 报
    ``waiting for element to be visible, enabled and editable`` —— 用户侧看到
    的就是「输入框卡死」；
  * ``fill()`` 报超时 ≠ 没写进去：文本可能已经整条落地，必须读回确认后继续提交，
    绝不能清空重写；
  * ``fill()`` 确实写不进去时要退回**分块插入**，支持断点续写、不重复写入；
  * 富文本编辑器会把换行规范化，读回比较不能按逐字节；
  * 按了 Enter ≠ 消息发出去了：必须验证输入框已清空 / 页面进入生成中，
    否则退到发送按钮，仍验证不到就立刻抛可操作的错误；
  * 超长 prompt 必须在发送前按 ``PROMPT_MAX_CHARS``（默认 48000 < 50K）截断。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv  # noqa: E402
from deepseek_web import config  # noqa: E402
from deepseek_web.chat_io import ChunkedInsertUnavailable  # noqa: E402


class FillTimeout(Exception):
    """模拟 Playwright 的 TimeoutError（只用于假 page）。"""


class FakeComposer:
    """模拟网页输入框：可配置 fill 是否成功 / 是否真的写进去。"""

    def __init__(self, text="", fill_ok=True, fill_lands=True, exec_insert_ok=True):
        self.text = text
        self.fill_ok = fill_ok          # False -> fill 抛超时
        self.fill_lands = fill_lands    # fill 抛超时时，文本是否其实已经落地
        self.exec_insert_ok = exec_insert_ok  # False -> execCommand 插入原语也失效
        self.detached = False           # True -> 模拟 React 重挂载后的断连旧节点
        self.fill_calls = []
        self.click_calls = 0

    async def fill(self, text, **kwargs):
        self.fill_calls.append(text)
        if self.fill_ok:
            self.text = text
            return
        if self.fill_lands:
            # 真机签名：fill 报超时，但输入框同时长出了整条文本
            self.text = text
        raise FillTimeout("waiting for element to be visible, enabled and editable")

    async def evaluate(self, script, *args):
        if "typeof el.value === 'string'" in script:
            # 断连的旧节点读不到文本（返回 None 而不是旧值）
            return None if self.detached else self.text
        if "!!el.isConnected" in script:
            return not self.detached
        if "execCommand('insertText'" in script:
            if not self.exec_insert_ok:
                return False
            self.text += args[0]
            return True
        if "execCommand('delete'" in script:
            self.text = ""
            return True
        if "range.collapse(false)" in script:
            return True
        if "document.activeElement === el" in script:
            return True
        if "caret_in_composer" in script:
            return "{}"
        if "KeyboardEvent" in script:
            return True  # 合成 Enter：派发成功但不代表网页接住了
        return True

    async def click(self, **kwargs):
        self.click_calls += 1


class FakeButton:
    def __init__(self, page):
        self.page = page

    async def click(self, **kwargs):
        self.page.button_clicks += 1
        if self.page.button_submits:
            self.page.composer.text = ""

    async def dispatch_event(self, name):
        self.page.button_dispatches += 1
        if self.page.button_submits:
            self.page.composer.text = ""

    async def evaluate(self, script):
        return "{}"


class FakeKeyboard:
    def __init__(self, page):
        self.page = page
        self.inserted = []
        self.insert_text_fails = False

    async def press(self, key):
        if key != "Enter":
            return
        page = self.page
        if page.enter_reveals_prompt:
            # 网页接到消息后把它渲染进对话列表（输入框不一定同步清空）
            page.page_text = page.enter_reveals_prompt
        if page.remount_on_submit:
            # 真机：提交后 React 重挂载编辑器，旧节点断连、新节点为空
            page.composer.detached = True
            page.composer = FakeComposer()
        elif page.enter_submits:
            page.composer.text = ""

    async def insert_text(self, text):
        if self.insert_text_fails:
            raise RuntimeError("CDP 通道卡住")
        self.inserted.append(text)
        self.page.composer.text += text


class FakeNode:
    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text

    async def query_selector_all(self, selector):
        return []

    async def query_selector(self, selector):
        return None

    async def get_attribute(self, name):
        return None


class FakePage:
    """假页面：``query_selector_all`` 第一次是发送前的 baseline，之后按脚本返回回复。"""

    def __init__(self, composer=None, baseline=(), script=(), generating=(False,),
                 enter_submits=True, button_submits=True, button_present=False,
                 page_text="", enter_reveals_prompt=None, remount_on_submit=False):
        self.composer = composer if composer is not None else FakeComposer()
        self.baseline = list(baseline)
        self.script = list(script) or [[None]]
        self.generating = list(generating)
        self.enter_submits = enter_submits
        self.button_submits = button_submits
        self.button_present = button_present
        self.button_clicks = 0
        self.button_dispatches = 0
        self.query_calls = 0
        self.eval_calls = 0
        self.page_text = page_text          # 页面对话区文本（提交验证用）
        self.enter_reveals_prompt = enter_reveals_prompt  # 按下 Enter 后页面出现的消息
        self.remount_on_submit = remount_on_submit        # 提交后是否重挂载编辑器
        self.keyboard = FakeKeyboard(self)
        self.url = "https://chat.deepseek.com/"

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        return self.composer

    async def query_selector_all(self, selector):
        index = self.query_calls
        self.query_calls += 1
        if index == 0:
            return [FakeNode(t) for t in self.baseline]
        texts = self.script[min(index - 1, len(self.script) - 1)]
        return [FakeNode(t) for t in texts if t is not None]

    async def query_selector(self, selector):
        return FakeButton(self) if self.button_present else None

    async def evaluate(self, script, *args):
        # 页面级 evaluate：用脚本内容区分「消息是否出现在对话区」「继续按钮」
        # 「到顶检测」「生成中」
        if "style.display = 'none'" in script:
            return self.page_text
        if "clicked" in script:
            return {"clicked": False}
        if "innerText" in script and "replace" in script:
            return ""
        index = self.eval_calls
        self.eval_calls += 1
        return self.generating[min(index, len(self.generating) - 1)]


class InputTestCase(unittest.TestCase):
    def setUp(self):
        # 固定所有与写入 / 提交相关的配置，避免依赖开发机上的 .env 取值
        self._tmp = Path(self.id().replace(".", "_") + ".session")
        self.addCleanup(lambda: self._tmp.exists() and self._tmp.unlink())
        self._patches = [
            unittest.mock.patch.object(config, "SESSION_FILE", self._tmp),
            unittest.mock.patch.object(config, "POLL_INTERVAL_S", 0),
            unittest.mock.patch.object(config, "RESPONSE_TIMEOUT_S", 5.0),
            unittest.mock.patch.object(config, "RETRY_BACKOFF_S", 0),
            unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 1),
            unittest.mock.patch.object(config, "SESSION_MAX_TURNS", 0),
            unittest.mock.patch.object(config, "SESSION_MAX_TOKENS", 0),
            unittest.mock.patch.object(config, "PARALLEL_BUCKETS", False),
            unittest.mock.patch.object(config, "BUCKET_LOCK_TIMEOUT_S", 0),
            unittest.mock.patch.object(config, "FILL_TIMEOUT_MS", 1000),
            unittest.mock.patch.object(config, "FILL_RETRIES", 3),
            unittest.mock.patch.object(config, "FILL_CHUNK_CHARS", 1000),
            unittest.mock.patch.object(config, "SUBMIT_VERIFY_MS", 100),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def driver_for(page):
        driver = srv.DeepSeekWebDriver()
        driver.page = page
        return driver


class ClampTests(InputTestCase):
    """PROMPT_MAX_CHARS：发送前把超长 prompt 压到预算内（默认 48000 < 50K）。"""

    def test_short_prompt_is_untouched(self):
        prompt = "x" * 100
        self.assertEqual(srv.DeepSeekWebDriver._clamp_prompt(prompt), prompt)

    def test_long_prompt_keeps_head_and_tail(self):
        prompt = "A" * 30000 + "MIDDLE" + "B" * 30000
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 1000):
            clamped = srv.DeepSeekWebDriver._clamp_prompt(prompt)
        self.assertTrue(clamped.startswith("A" * 400))
        self.assertTrue(clamped.endswith("B" * 400))
        self.assertNotIn("MIDDLE", clamped)
        self.assertIn("已省略中间 59006 字符", clamped)
        # 含提示语在内也不超过预算，且重复调用是幂等的
        self.assertEqual(len(clamped), 1000)
        self.assertEqual(srv.DeepSeekWebDriver._clamp_prompt(clamped), clamped)

    def test_send_chat_records_and_fills_the_clamped_prompt(self):
        page = FakePage(script=[["答案"], ["答案"]])
        driver = self.driver_for(page)
        driver.session_has_history = True
        with unittest.mock.patch.object(config, "PROMPT_MAX_CHARS", 500):
            asyncio.run(driver.send_chat("Z" * 2000))
        sent = driver.sent_prompt()
        self.assertTrue(sent.startswith("Z" * 200))
        self.assertEqual(len(sent), 500)
        # 真正写进输入框的就是截断后的那份（usage / 日志与事实一致）
        self.assertEqual(page.composer.fill_calls[-1], sent)


class FillTests(InputTestCase):
    """写入输入框：整段 fill 优先，写不进去才分块，可断点续写。"""

    def test_fill_success_uses_whole_fill(self):
        composer = FakeComposer()
        page = FakePage(composer=composer)
        driver = self.driver_for(page)
        handle = asyncio.run(driver._fill_prompt(page, "hello"))
        self.assertEqual(composer.text, "hello")
        self.assertIs(handle, composer)
        self.assertEqual(page.keyboard.inserted, [])

    def test_fill_timeout_with_text_landed_is_not_rewritten(self):
        composer = FakeComposer(fill_ok=False, fill_lands=True)
        page = FakePage(composer=composer)
        driver = self.driver_for(page)
        asyncio.run(driver._fill_prompt(page, "long prompt"))
        # fill 报错但文本已在输入框中：按成功继续，绝不能清空重写
        self.assertEqual(composer.text, "long prompt")
        self.assertEqual(page.keyboard.inserted, [])
        self.assertEqual(len(composer.fill_calls), 1)

    def test_chunked_fallback_when_fill_never_lands(self):
        composer = FakeComposer(fill_ok=False, fill_lands=False)
        page = FakePage(composer=composer)
        driver = self.driver_for(page)
        prompt = "0123456789" * 300  # 3000 字符 -> 1000/块 -> 3 块
        asyncio.run(driver._fill_prompt(page, prompt))
        self.assertEqual(composer.text, prompt)
        self.assertGreater(len(page.keyboard.inserted), 1)
        self.assertTrue(all(len(part) <= 1000 for part in page.keyboard.inserted))
        self.assertEqual("".join(page.keyboard.inserted), prompt)

    def test_chunked_insert_resumes_from_partial_prefix(self):
        prompt = "Q" * 2500
        composer = FakeComposer(text=prompt[:400], fill_ok=False, fill_lands=False)
        page = FakePage(composer=composer)
        driver = self.driver_for(page)
        asyncio.run(driver._fill_prompt(page, prompt))
        self.assertEqual(composer.text, prompt)
        # 已存在的 400 字符不会被重复写入
        self.assertEqual("".join(page.keyboard.inserted), prompt[400:])

    def test_leftover_draft_is_cleared_before_writing(self):
        composer = FakeComposer(text="上一次没发出去的草稿", fill_ok=False, fill_lands=False)
        page = FakePage(composer=composer)
        driver = self.driver_for(page)
        prompt = "NEW" * 500
        asyncio.run(driver._fill_prompt(page, prompt))
        self.assertEqual(composer.text, prompt)
        self.assertNotIn("草稿", composer.text)

    def test_unwritable_page_raises_actionable_error(self):
        composer = FakeComposer(fill_ok=False, fill_lands=False, exec_insert_ok=False)
        page = FakePage(composer=composer)
        page.keyboard.insert_text_fails = True
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "FILL_RETRIES", 1):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(driver._fill_prompt(page, "X" * 3000))
        message = str(ctx.exception)
        self.assertIn("写入输入框连续失败", message)
        self.assertIn("分块插入", message)
        self.assertIn("PROMPT_MAX_CHARS", message)

    def test_chunked_insert_reports_when_page_cannot_read_back(self):
        # 读不到输入框文本时不要假装成功，也不该反复清空：直接抛 fill 的原始错误
        class UnreadableComposer(FakeComposer):
            async def evaluate(self, script, *args):
                if "typeof el.value === 'string'" in script:
                    return None
                return await super().evaluate(script, *args)

        composer = UnreadableComposer(fill_ok=False, fill_lands=False)
        page = FakePage(composer=composer)
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "FILL_RETRIES", 1):
            with self.assertRaises(RuntimeError):
                asyncio.run(driver._fill_prompt(page, "hello"))
        self.assertEqual(composer.fill_calls, ["hello"])


class SubmitTests(InputTestCase):
    """提交：真实键盘 Enter -> 发送按钮 -> 合成 Enter，每步都验证真的发出去了。"""

    def test_keyboard_enter_submits_and_clears_composer(self):
        composer = FakeComposer(text="hi")
        page = FakePage(composer=composer)
        driver = self.driver_for(page)
        asyncio.run(driver._submit_prompt(page, composer))
        self.assertEqual(composer.text, "")

    def test_falls_back_to_send_button_when_enter_is_ignored(self):
        composer = FakeComposer(text="hi")
        page = FakePage(composer=composer, enter_submits=False, button_submits=True,
                        button_present=True)
        driver = self.driver_for(page)
        asyncio.run(driver._submit_prompt(page, composer))
        self.assertGreaterEqual(page.button_clicks, 1)
        self.assertEqual(composer.text, "")

    def test_remounted_composer_is_relocated_before_verdict(self):
        # 真机故障：提交后 React 重挂载编辑器，旧句柄断连却仍保留 fill 的文本。
        # 必须重新定位当前输入框再读，不能拿旧节点判“没提交”。
        old = FakeComposer(text="prompt text")
        page = FakePage(composer=old, remount_on_submit=True)
        driver = self.driver_for(page)
        asyncio.run(driver._submit_prompt(page, old))
        self.assertTrue(old.detached)
        self.assertEqual(page.composer.text, "")

    def test_message_appearing_in_conversation_counts_as_submitted(self):
        # 输入框文本还在、也没观测到生成中，但消息已经渲染进对话区：
        # 说明网页收下了消息，不得误报失败（网页可能还没清空输入框）。
        prompt = "hello world, please fix the login bug"
        composer = FakeComposer(text=prompt)
        page = FakePage(composer=composer, enter_submits=False, button_submits=False,
                        button_present=False)
        page.enter_reveals_prompt = f"**{prompt}**"  # 渲染成 markdown 后仍可匹配
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "SUBMIT_VERIFY_MS", 50):
            asyncio.run(driver._submit_prompt(page, composer, prompt=prompt))
        self.assertEqual(composer.text, prompt)

    def test_raises_actionable_error_when_never_submitted(self):
        composer = FakeComposer(text="hi")
        page = FakePage(composer=composer, enter_submits=False, button_submits=False,
                        button_present=False)
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "SUBMIT_VERIFY_MS", 50):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(driver._submit_prompt(page, composer))
        message = str(ctx.exception)
        self.assertIn("未能提交", message)
        self.assertIn("输入框仍有 2 字符", message)
        self.assertIn("SUBMIT_VERIFY_MS", message)


class PromptPresenceTests(InputTestCase):
    """读回比较：容忍编辑器的空白规范化，但不容忍被截断。"""

    def test_whitespace_normalization_is_tolerated(self):
        prompt = "line1\n\nline2\nline3"
        self.assertTrue(
            srv.DeepSeekWebDriver._prompt_present(prompt, "line1 line2 line3")
        )

    def test_truncated_readback_is_not_treated_as_present(self):
        self.assertFalse(
            srv.DeepSeekWebDriver._prompt_present("A" * 500, "A" * 200)
        )

    def test_different_text_is_not_present(self):
        self.assertFalse(
            srv.DeepSeekWebDriver._prompt_present("hello world", "hello there")
        )

    def test_presence_check_is_markdown_tolerant(self):
        prompt = "修复登录 bug 的步骤"
        self.assertTrue(
            srv.DeepSeekWebDriver._text_shows_prompt("说明\n\n**修复登录 bug 的步骤**", prompt)
        )

    def test_presence_check_ignores_too_short_head(self):
        self.assertFalse(srv.DeepSeekWebDriver._text_shows_prompt("短", "短"))


class SendPathTests(InputTestCase):
    """端到端（假 page）：长 prompt 分块写入 + 提交验证 + 正常收尾。"""

    def test_long_prompt_sent_in_chunks_and_verified(self):
        composer = FakeComposer(fill_ok=False, fill_lands=False)
        page = FakePage(composer=composer, script=[["答案"], ["答案"]])
        driver = self.driver_for(page)
        prompt = "Q" * 2500
        text, _ = asyncio.run(driver._send_chat_locked(prompt))
        self.assertEqual(text, "答案")
        self.assertEqual("".join(page.keyboard.inserted), prompt)
        # 提交成功后输入框必须已清空（Enter 被网页接住）
        self.assertEqual(composer.text, "")

    def test_send_chat_locked_raises_without_polling_when_submit_fails(self):
        composer = FakeComposer(text="已写入但发不出去")
        page = FakePage(composer=composer, enter_submits=False, button_submits=False,
                        button_present=False, script=[["不该被读到的回复"]])
        driver = self.driver_for(page)
        with unittest.mock.patch.object(config, "SUBMIT_VERIFY_MS", 50):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(driver._send_chat_locked("hi"))
        self.assertIn("未能提交", str(ctx.exception))
        # 没有静默等到轮询超时：连一轮回复都没有读
        self.assertEqual(page.query_calls, 1)  # 只有发送前的 baseline


if __name__ == "__main__":
    unittest.main()
