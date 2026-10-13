"""「继续生成」分段续接的回归测试（假 page 驱动，不启浏览器）。

真实故障类别：网页版把一条回复截断成多段，点击「继续生成」后桥必须把后续内容**拼进同一条回复**。
`_maybe_continue()` 里的分段重置（注释原话：「上一段的稳定 / 长度 / 生成中判定不能带到下一段」）
就是为此存在的 —— 少重置任何一个，新一段刚出现就会被判成「没变化」而提前收尾，
用户拿到的是**半截回复**，而且错误看起来完全像「正常结束」。

所以本文件钉的是**可观察行为**，而不是实现细节：
  * 接通第二段后返回的必须是完整文本（`test_second_segment_is_appended`）；
  * 「继续」按钮必须真的被点过（否则上面那条可能是「压根没触发续接」而侥幸通过）；
  * 超过 `CONTINUE_BUTTON_MAX` 后不再续接，避免无限循环。

区分力：把 `_maybe_continue()` 的 nonlocal 列表里去掉 `stable_count`（等价于让分段重置失效），
`test_second_segment_is_appended` 必须变红 —— 它断言的是完整文本，半截回复会直接对不上。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv  # noqa: E402


class FakeInput:
    async def fill(self, text, **kwargs):
        return None


class FakeKeyboard:
    async def press(self, key):
        return None


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


class FakeContinuePage:
    """按脚本驱动的假 page：回复节点文本 + 「生成中」信号 + 「继续」按钮随时可用。

    `evaluate` 的分派依据是**脚本正文里的特征串**，不能用「有没有第二个参数」判断：
    提交验证也会带参数调用（`evaluate(_PROMPT_IN_PAGE_JS, handle)`），按参数分派会把那次
    调用误当成「继续」探测，凭空多出一次点击、并错乱后面的时序（实测踩过）。
      * `class*="continue"` ＝「继续生成」按钮探测（`_CONTINUE_BUTTON_JS_TEMPLATE`）；
      * `style.display = 'none'` ＝提交验证扫描（`_PROMPT_IN_PAGE_JS`）；
      * 其余 ＝「生成中」探测。
    """

    def __init__(self, baseline, script, generating, continue_labels):
        self.baseline = list(baseline)
        self.script = list(script)
        self.generating = list(generating)
        self.continue_labels = list(continue_labels)
        self.query_calls = 0
        self.eval_calls = 0
        self.continue_checks = 0
        self.clicks = 0
        self.url = "https://chat.deepseek.com/"
        self.keyboard = FakeKeyboard()

    async def wait_for_selector(self, selector, timeout=0, **kwargs):
        return FakeInput()

    async def query_selector_all(self, selector):
        index = self.query_calls
        self.query_calls += 1
        if index == 0:
            return [FakeNode(text) for text in self.baseline]
        texts = self.script[min(index - 1, len(self.script) - 1)]
        return [FakeNode(text) for text in texts]

    async def evaluate(self, script, *args):
        if 'class*="continue"' in script:  # 「继续生成」按钮探测
            index = self.continue_checks
            self.continue_checks += 1
            label = self.continue_labels[min(index, len(self.continue_labels) - 1)]
            if label is None:
                return {"clicked": False, "text": ""}
            self.clicks += 1
            return {"clicked": True, "text": label}
        if "style.display = 'none'" in script:  # 提交验证扫描（把输入框临时隐藏后取全页文本）
            return ""
        index = self.eval_calls
        self.eval_calls += 1
        return self.generating[min(index, len(self.generating) - 1)]


class ContinueSegmentTestCase(unittest.TestCase):
    def setUp(self):
        # 会话文件放进**临时目录**：只按名字删单个文件不够（驱动可能按分桶再写别的名字，
        # 实测就会在仓库根目录留下一个 .session），整个目录一起收掉才不会污染工作区。
        self._tmpdir = Path(tempfile.mkdtemp(prefix="continue-segments-"))
        self.addCleanup(shutil.rmtree, self._tmpdir, ignore_errors=True)
        self._tmp_session = self._tmpdir / "session"
        for name, value in (
            ("SESSION_FILE", self._tmp_session),
            ("POLL_INTERVAL_S", 0),
            ("RESPONSE_TIMEOUT_S", 5.0),
            ("PARALLEL_BUCKETS", False),
            ("BUCKET_LOCK_TIMEOUT_S", 0),
            ("CAP_CHECK_EVERY", 10_000),  # 让到顶检测不参与本用例的 evaluate 时序
            # 结束阈值必须由用例钉住：开发机上的 .env 会改写它们（本仓库实测 STABLE_POLLS=5），
            # 不 patch 就会变成「测的是本机配置」，换台机器结论就变。
            ("STABLE_POLLS", 2),
            ("LEN_STABLE_POLLS", 2),
            ("CONTINUE_BUTTON_ENABLED", True),
        ):
            patch = unittest.mock.patch.object(srv.config, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def driver_for(page):
        driver = srv.DeepSeekWebDriver()
        driver.page = page
        return driver

    def run_chat(self, driver, prompt="go"):
        return asyncio.run(driver.send_chat(prompt))


class SegmentResetTests(ContinueSegmentTestCase):
    """接通「继续」后，上一段的判定参数不得带到下一段。"""

    def test_second_segment_is_appended(self):
        # before_text = "u"；第 1 轮已出现新回复且生成中 → 第 2 轮停止按钮消失，此时应触发续接。
        page = FakeContinuePage(
            baseline=["u"],
            script=[
                ["u", "seg1"],        # poll1：第一段内容 + 生成中
                ["u", "seg1"],        # poll2：停止按钮消失 → 点「继续」（第 1 次探测命中）
                ["u", "seg1"],        # poll3：刚点完，下一段还没吐字
                ["u", "seg1"],        # poll4：仍未吐字（分段重置失效时这里就会被判“稳定”而收尾）
                ["u", "seg1+seg2"],   # poll5：第二段内容到达
                ["u", "seg1+seg2"],
                ["u", "seg1+seg2"],   # poll7：稳定 2 次 → 正常收尾
            ],
            generating=[True, False, False, False, False, False, False],
            continue_labels=["继续", None],
        )
        text = self.run_chat(self.driver_for(page))
        self.assertEqual(page.clicks, 1, "「继续」按钮应当被点击恰好一次")
        self.assertEqual(
            text[0] if isinstance(text, tuple) else text,
            "seg1+seg2",
            "续接后的第二段必须拼进同一条回复（半截回复＝分段重置失效）",
        )

    def test_continue_is_bounded_by_max(self):
        # 每轮都能探测到「继续」：必须受 CONTINUE_BUTTON_MAX 限制，不能无限续接。
        page = FakeContinuePage(
            baseline=["u"],
            script=[["u", "seg1"], ["u", "seg1"], ["u", "seg1"]],
            generating=[True, False, False, False, False, False],
            continue_labels=["继续"],  # 永远可点
        )
        with unittest.mock.patch.object(srv.config, "CONTINUE_BUTTON_MAX", 2):
            text = self.run_chat(self.driver_for(page))
        self.assertEqual(page.clicks, 2, "点击次数必须被 CONTINUE_BUTTON_MAX 封顶")
        self.assertEqual(text[0] if isinstance(text, tuple) else text, "seg1")
