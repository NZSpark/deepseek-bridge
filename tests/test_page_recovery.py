"""页面失效自愈的回归测试（用假 page 驱动，不开浏览器）。

真实故障（本文件覆盖的就是它）：

* 会话页面的**浏览器标签被关掉 / 渲染进程崩溃**后，页面对象仍留在页面池 ``_pages``
  里。此后该会话桶的每一次请求都在 ``wait_for_selector`` 上**立刻**抛错，被当成
  「选择器都没命中」，对外只报一句误导性的「无法找到对话输入框，请检查是否登录」，
  而且永远不会自愈——同一会话桶会持续 502 到进程重启（用 OpenAI SDK 调用 bridge
  时表现为连片的 502，而其他会话桶的请求却正常）。
* 页面池的回收 / LRU 淘汰只看「桶锁是否被持有」，``send_chat`` 在**拿锁之前**就
  已经确定了页面，这个窗口里页面可能被别的桶顺手关掉。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv  # noqa: E402
from deepseek_web import config  # noqa: E402
from deepseek_web.driver import (  # noqa: E402
    DEFAULT_SESSION_KEY,
    DeepSeekPageLostError,
)

HOME = "https://chat.deepseek.com/"
URL_A = "https://chat.deepseek.com/a/chat/s/aaaaaaaa-1111-2222-3333-444444444444"
PAGE_GONE = "Target page, context or browser has been closed"


class FakeInput:
    def __init__(self):
        self.text = ""

    async def fill(self, text, **kwargs):
        self.text = text


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


class HealthyPage:
    """行为正常的假页面：能找到输入框，回复内容稳定（走内容稳定结束判定）。"""

    def __init__(self, url=HOME):
        self.url = url
        self.gotos = []
        self.closed = False
        self.keyboard = self
        self.query_calls = 0
        self.ready_waits = 0
        self.reply = "收到"

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url

    async def reload(self, **kwargs):
        self.gotos.append("__reload__")

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        self.ready_waits += 1
        return FakeInput()

    async def close(self):
        self.closed = True

    async def press(self, key):
        return None

    async def query_selector(self, selector):
        return None

    async def query_selector_all(self, selector):
        index = self.query_calls
        self.query_calls += 1
        # 第 0 次是发送前的 baseline（还没有回复），之后一直是同一条回复
        return [] if index == 0 else [FakeNode(self.reply)]

    async def evaluate(self, script, *args):
        if "style.display = 'none'" in script:
            return ""
        if "document.readyState" in script:  # _PAGE_DIAG_JS
            return '{"url": "%s"}' % self.url
        return False


class ClosedPage(HealthyPage):
    """标签已经被关掉：``is_closed()`` 为真，所有操作立刻失败。"""

    def is_closed(self):
        return True

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        raise RuntimeError(PAGE_GONE)

    async def query_selector_all(self, selector):
        raise RuntimeError(PAGE_GONE)


class CrashedPage(HealthyPage):
    """渲染进程崩溃：``is_closed()`` 仍报 False，但每次操作都失败。

    这正是「只看 is_closed 就以为页面还活着」会踩到的坑，恢复路径必须能无条件重建。
    """

    def __init__(self, url=HOME):
        super().__init__(url)
        self.crash_calls = 0

    def is_closed(self):
        return False

    async def _crash(self):
        self.crash_calls += 1
        raise RuntimeError(PAGE_GONE)

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        await self._crash()

    async def query_selector_all(self, selector):
        await self._crash()


class BlockingPage(HealthyPage):
    """第一次轮询卡住不返回，用来制造「请求正在进行中」的窗口。"""

    def __init__(self, url=HOME):
        super().__init__(url)
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def query_selector_all(self, selector):
        index = self.query_calls
        self.query_calls += 1
        if index == 0:
            return []
        if not self.release.is_set():
            self.entered.set()
            await self.release.wait()
        return [FakeNode(self.reply)]


class FakeContext:
    def __init__(self, page_factory=None):
        self.pages = []
        self.page_factory = page_factory or HealthyPage

    async def new_page(self):
        page = self.page_factory()
        self.pages.append(page)
        return page


class PageRecoveryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(self.id().replace(".", "_") + ".session")
        self.addCleanup(lambda: self._tmp.exists() and self._tmp.unlink())
        patches = [
            unittest.mock.patch.object(config, "SESSION_FILE", self._tmp),
            unittest.mock.patch.object(config, "POLL_INTERVAL_S", 0),
            unittest.mock.patch.object(config, "RETRY_BACKOFF_S", 0),
            unittest.mock.patch.object(config, "RESPONSE_TIMEOUT_S", 5.0),
            unittest.mock.patch.object(config, "MAX_UPSTREAM_RETRIES", 2),
            unittest.mock.patch.object(config, "SESSION_MAX_TURNS", 0),
            unittest.mock.patch.object(config, "SESSION_MAX_TOKENS", 0),
            # 固定并发相关配置，避免依赖开发机上的 .env 取值
            unittest.mock.patch.object(config, "PARALLEL_BUCKETS", True),
            unittest.mock.patch.object(config, "BUCKET_LOCK_TIMEOUT_S", 0),
            unittest.mock.patch.object(config, "BUCKET_IDLE_TTL_S", 0),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def driver_for(self, page=None, page_factory=None):
        driver = srv.DeepSeekWebDriver()
        driver.page = page or HealthyPage()
        driver.context = FakeContext(page_factory)
        return driver


class LocateInputTests(PageRecoveryTestCase):
    def test_closed_page_raises_page_lost_instead_of_returning_none(self):
        driver = self.driver_for()
        with self.assertRaises(DeepSeekPageLostError) as ctx:
            asyncio.run(driver._locate_input(ClosedPage()))
        # 文案必须指向真正的原因与出路，而不是「请检查是否登录」
        self.assertIn("标签已失效", str(ctx.exception))

    def test_crashed_page_raises_page_lost_even_though_is_closed_is_false(self):
        driver = self.driver_for()
        with self.assertRaises(DeepSeekPageLostError):
            asyncio.run(driver._locate_input(CrashedPage()))

    def test_missing_input_on_a_live_page_still_returns_none(self):
        """页面活着、只是选择器都没命中：保持旧行为（由调用方给出诊断）。"""

        class EmptyPage(HealthyPage):
            async def wait_for_selector(self, selector, timeout=0, **kwargs):
                raise TimeoutError("selector not found")

        driver = self.driver_for()
        self.assertIsNone(asyncio.run(driver._locate_input(EmptyPage())))

    def test_page_diagnostics_report_the_scene_without_conversation_text(self):
        driver = self.driver_for()

        class DiagPage(HealthyPage):
            async def evaluate(self, script, *args):
                self.script = script
                return '{"url": "x", "textarea": 0}'

        page = DiagPage()
        diag = asyncio.run(driver._page_diag(page))
        self.assertIn("textarea", diag)
        # 现场信息必须包含 URL / 就绪状态 / 登录墙标记，且**不回显正文**
        for field in ("document.readyState", "login_like", "body_chars"):
            self.assertIn(field, srv.DeepSeekWebDriver._PAGE_DIAG_JS)
        self.assertIn("readyState", page.script)

    def test_dead_page_diagnosis_says_the_tab_is_gone(self):
        driver = self.driver_for()
        self.assertIn("已失效", asyncio.run(driver._page_diag(ClosedPage())))


class EnsurePageTests(PageRecoveryTestCase):
    def test_dead_registered_page_is_rebuilt_and_session_is_kept(self):
        driver = self.driver_for()
        dead = ClosedPage(url=URL_A)
        driver._pages["task-a"] = dead
        driver._state("task-a").url = URL_A
        driver._state("task-a").turns = 5

        asyncio.run(driver._ensure_page("task-a"))

        page = driver._page_for("task-a")
        self.assertIsNot(page, dead)
        self.assertTrue(driver._page_is_alive(page))
        self.assertEqual(page.gotos, [URL_A])  # 仍回到同一条网页会话
        self.assertEqual(driver._state("task-a").turns, 5)  # 状态保留
        self.assertFalse(driver.needs_seed("task-a"))

    def test_default_bucket_page_is_rebuilt_when_dead(self):
        old = ClosedPage(url=URL_A)
        driver = self.driver_for(page=old)
        driver._state(DEFAULT_SESSION_KEY).url = URL_A

        asyncio.run(driver._ensure_page(None))

        self.assertIsNot(driver.page, old)
        self.assertTrue(driver._page_is_alive(driver.page))
        self.assertEqual(driver.page.gotos, [URL_A])

    def test_live_page_is_reused_without_rebuilding(self):
        driver = self.driver_for()
        page = HealthyPage(url=URL_A)
        driver._pages["task-a"] = page
        asyncio.run(driver._ensure_page("task-a"))
        self.assertIs(driver._page_for("task-a"), page)
        self.assertEqual(driver.context.pages, [])


class SendChatRecoveryTests(PageRecoveryTestCase):
    def test_send_chat_rebuilds_a_crashed_page_and_retries(self):
        driver = self.driver_for()
        crashed = CrashedPage(url=URL_A)
        driver._pages["task-a"] = crashed
        driver._state("task-a").url = URL_A
        driver._state("task-a").has_history = True

        reply, blocks = asyncio.run(driver.send_chat("继续", key="task-a"))

        self.assertEqual(reply.strip(), "收到")
        self.assertEqual(blocks, [])
        self.assertTrue(crashed.closed)  # 死页面已被移出并关闭
        self.assertIsNot(driver._page_for("task-a"), crashed)
        self.assertTrue(driver._page_is_alive(driver._page_for("task-a")))
        self.assertEqual(driver._page_for("task-a").gotos, [URL_A])
        # 会话本身没有丢：turns 照常累计，且不需要播种
        self.assertEqual(driver._state("task-a").turns, 1)
        self.assertFalse(driver.needs_seed("task-a"))

    def test_page_lost_error_surfaces_when_the_page_keeps_dying(self):
        # 每重建一次都还是崩溃的页面：重试用尽后必须把**真实原因**报出来
        driver = self.driver_for(page_factory=CrashedPage)
        driver._pages["task-a"] = CrashedPage(url=URL_A)

        with self.assertRaises(DeepSeekPageLostError):
            asyncio.run(driver.send_chat("继续", key="task-a"))
        # 最后一次失败的原因必须是「页面失效」，而不是「找不到输入框 / 请检查登录」
        self.assertIn("标签已失效", driver._state("task-a").last_error or "")

    def test_default_bucket_recovers_through_send_chat(self):
        driver = self.driver_for(page=ClosedPage(url=URL_A))
        driver._state(DEFAULT_SESSION_KEY).url = URL_A
        driver._state(DEFAULT_SESSION_KEY).has_history = True

        reply, _ = asyncio.run(driver.send_chat("继续"))

        self.assertEqual(reply.strip(), "收到")
        self.assertTrue(driver._page_is_alive(driver.page))


class PageReclaimProtectionTests(PageRecoveryTestCase):
    """请求进行中的桶，其页面绝不能被空闲回收 / LRU 淘汰关掉。"""

    def test_in_flight_request_keeps_its_page_from_idle_recycle(self):
        driver = self.driver_for(page_factory=BlockingPage)
        page = BlockingPage(url=URL_A)
        driver._pages["task-a"] = page
        driver._state("task-a").url = URL_A
        driver._state("task-a").has_history = True
        driver._page_last_used["task-a"] = time.monotonic() - 10_000  # 看起来“很空闲”

        async def scenario():
            with unittest.mock.patch.object(config, "BUCKET_IDLE_TTL_S", 60):
                task = asyncio.create_task(driver.send_chat("继续", key="task-a"))
                await asyncio.wait_for(page.entered.wait(), timeout=5)
                # 请求正在生成中：另一个桶的惰性建页会顺手回收空闲页面
                recycled = await driver._recycle_idle_pages()
                page.release.set()
                reply, _ = await task
                return recycled, reply

        recycled, reply = asyncio.run(scenario())
        self.assertEqual(recycled, 0)
        self.assertFalse(page.closed)
        self.assertEqual(reply.strip(), "收到")

    def test_lru_eviction_skips_a_bucket_with_an_in_flight_request(self):
        driver = self.driver_for()
        active = HealthyPage(url=URL_A)
        idle = HealthyPage(url=URL_A)
        driver._pages["task-a"] = active
        driver._pages["task-b"] = idle
        driver._page_last_used["task-a"] = 0.0            # 最久未用 -> 第一个被淘汰
        driver._page_last_used["task-b"] = time.monotonic()

        driver._mark_bucket_active("task-a")  # send_chat 在拿锁之前就会打上这个标记
        try:
            evicted = asyncio.run(driver._evict_lru_page(exclude="task-c"))
        finally:
            driver._unmark_bucket_active("task-a")

        self.assertTrue(evicted)
        self.assertIn("task-a", driver._pages)  # 请求进行中：不能被淘汰
        self.assertNotIn("task-b", driver._pages)
        self.assertTrue(idle.closed)
        self.assertFalse(active.closed)

    def test_active_marking_is_reentrant_until_the_request_finishes(self):
        """send_chat 与桶锁都会标记活跃：内层先退出不能提前撤掉外层的保护。"""
        driver = self.driver_for(page_factory=BlockingPage)
        page = BlockingPage(url=URL_A)
        driver._pages["task-a"] = page
        driver._state("task-a").url = URL_A
        driver._state("task-a").has_history = True

        async def scenario():
            task = asyncio.create_task(driver.send_chat("继续", key="task-a"))
            await asyncio.wait_for(page.entered.wait(), timeout=5)
            busy_during = driver._bucket_busy("task-a")
            page.release.set()
            await task
            return busy_during, driver._bucket_busy("task-a")

        busy_during, busy_after = asyncio.run(scenario())
        self.assertTrue(busy_during)
        self.assertFalse(busy_after)


if __name__ == "__main__":
    unittest.main()
