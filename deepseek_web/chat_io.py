"""发送消息 / 轮询回复 / 提取代码块（从 driver.py 拆出，作为 mixin 混入 DeepSeekWebDriver）。

输入框写入与提交的「长 prompt 防卡死」逻辑也在本模块：``_fill_prompt`` /
``_insert_prompt_in_chunks``（整段 fill 优先，写不进去则分块插入、可断点续写）、
``_submit_prompt``（提交并验证真的发出去了），``_clamp_prompt`` 是最后一道字符预算护栏。

依赖页面池（``_ensure_page`` / ``_page_for`` / ``_session_lock`` / ``_touch_page`` /
``_remember_session`` / ``_start_new_session`` / ``_recover_session`` / ``_state`` /
``_session_over_budget``）与生成检测（``_page_is_generating`` /
``_click_continue_if_present`` / ``_page_shows_context_limit`` /
``_mark_context_limit`` / ``_context_limit_error``）。
"""

import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import List, Optional

from . import config
from .errors import DEFAULT_SESSION_KEY, DeepSeekContextLimitError, DeepSeekTimeoutError
from .prompting import _delta_piece, estimate_tokens


class ChunkedInsertUnavailable(RuntimeError):
    """分块插入在本页面**一个字都写不进去**：调用方应退回整段 ``fill()``。

    与「写了但慢」区分开：只有 ``written == 0`` 才抛这个，表示两种插入原语
    在本页面上完全无效（真机故障），继续分块只会白等到报错。
    """


class ChatIOMixin:
    """发送 prompt、轮询至生成结束、提取回复与代码块。"""

    async def send_chat(
        self,
        prompt: str,
        on_delta=None,
        seeded_prompt: Optional[str] = None,
        key: Optional[str] = None,
    ) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应。

        :param prompt: 增量 prompt（网页会话已有上下文时使用）
        :param seeded_prompt: 带完整历史的“播种”prompt（需要新开会话时使用，
                              未提供则退回 ``prompt``）
        :param key: 会话桶标识（按任务隔离会话）。不同 key 各自持有一条独立
                    的网页会话与页面，互不污染上下文；None 表示默认桶。

        **重试阶梯**（避免在同一个已失效的会话上反复超时）：

        1. 首级：直接用现有会话（若已达体积预算或上次到顶，先轮转到新会话）；
        2. 中间级：按保存的会话链接恢复**同一个**会话（只重开页面）；
        3. 最高一级：**换新会话 + 用播种 prompt 重放历史**。

        为什么把“换新会话”当作最后手段，而不是一直恢复同一个会话：
        页面完全没有新回复（超时）最常见的原因就是会话已到顶 / 已失效，
        重复打开同一个会话注定再次超时。而换新会话只有在有“播种”能力时才安全。
        """
        bucket = key or DEFAULT_SESSION_KEY
        seeded = seeded_prompt or prompt
        max_attempts = max(1, config.MAX_UPSTREAM_RETRIES)
        last_error: Optional[RuntimeError] = None

        # 额外的会话桶需要自己的页面（默认桶就是 self.page，不涉及创建）
        await self._ensure_page(bucket)

        for attempt in range(1, max_attempts + 1):
            state = self._state(bucket)
            if state.pending_rotation:
                # 体积超预算或上次检测到“到顶”：先轮转，再播种
                await self._start_new_session(bucket)
            elif attempt == 1:
                pass
            elif attempt < max_attempts:
                print(f"[恢复] 第 {attempt}/{max_attempts} 次重试：恢复同一个会话……")
                if not await self._recover_session(bucket):
                    break
                await asyncio.sleep(config.RETRY_BACKOFF_S * attempt)
            else:
                print("[恢复] 恢复同一会话无效，改为开启新会话并重放历史……")
                await self._start_new_session(bucket)

            # 会话是新开的（或被轮转过）-> 必须播种，否则模型收不到任何上下文
            active_prompt = seeded if not self._state(bucket).has_history else prompt
            # 先按字符预算截断再记录：sent_prompt / usage / 日志必须反映真正发出去的那份
            active_prompt = self._clamp_prompt(active_prompt)
            # 记录真正要发出的那份 prompt，供上层估算 usage（按桶隔离，避免并发串台）
            self._last_prompts[bucket] = active_prompt

            await self._remember_session(bucket)
            try:
                return await self._send_chat_locked(active_prompt, on_delta, key=bucket)
            except DeepSeekContextLimitError as exc:
                # 到顶了：下次不要再恢复同一个会话，直接轮转
                last_error = exc
                self._state(bucket).pending_rotation = True
                print(f"[恢复] 第 {attempt}/{max_attempts} 次失败：会话已达上下文上限。")
            except DeepSeekTimeoutError as exc:
                # 只有「超时 / 到顶」才可重试；找不到输入框、profile 被占用等不可重试
                last_error = exc
                self._state(bucket).last_error = str(exc)
                print(f"[恢复] 第 {attempt}/{max_attempts} 次失败：等待回复超时。")

        if last_error is not None:
            raise last_error
        raise RuntimeError("上游请求未能发送")

    # ==================== 输入框写入与提交（长 prompt 防卡死）====================
    # 这一节参照姊妹项目 gemini-bridge 的 chat_io.py：一次性 fill() 长文本会在网页
    # 主线程上排成一个长任务（React 重渲染 + 富文本编辑器同步），期间 Playwright
    # 连“元素是否可见/可编辑”都探测不到，直接抛
    # ``waiting for element to be visible, enabled and editable``；网页本身也卡住，
    # 用户侧看到的就是「输入框卡死」。做法：整段 fill 优先（失败后读回救援），
    # 写不进去才退回分块插入（逐块让出主线程、读回校验、可断点续写）；
    # 提交后再验证「真的发出去了」，而不是把按 Enter 当作一定成功。

    # 读取输入框当前文本：textarea 用 value，contenteditable 用 textContent
    # （不用 innerText：它遵循“渲染后可见性”，窗口不可见时会读到空串，
    # 会让「输入框已清空」的判定变成假阳性）。
    # 节点已从页面断开（React 重挂载换了新节点）时返回 null 而不是旧值：
    # 旧节点的残留文本不能被当成“提交后输入框还有字”。
    _COMPOSER_TEXT_JS = """
    (el) => {
      if (!el.isConnected) return null;
      if (typeof el.value === 'string') return el.value;
      return el.textContent || el.innerText || '';
    }
    """

    _COMPOSER_CONNECTED_JS = "(el) => !!el.isConnected"

    # 判断“这条 prompt 是否已经出现在页面对话区”：textarea 的 value 不进
    # innerText，但 contenteditable 的正文会。这里把输入框**临时隐藏**再读整页
    # innerText（读完立即恢复）：既排除了草稿，也不受“对话里的消息文本与草稿
    # 完全相同”影响（按字符串剔除会把对话里的那一份也误删）。
    _PROMPT_IN_PAGE_JS = """
    (el) => {
      let hidden = null;
      try {
        if (el && el.isConnected && el.style) {
          hidden = el.style.display;
          el.style.display = 'none';
        }
        return document.body ? (document.body.innerText || '') : '';
      } finally {
        if (hidden !== null) { el.style.display = hidden; }
      }
    }
    """

    # 把光标（**折叠**选区）显式放进输入框内容末尾。
    # 这一段等于 Playwright `fill()` 内部对 contenteditable 做的前半段
    # （`selectText`：focus + range.selectNodeContents + addRange），
    # 只是折叠到末尾而不是全选——分块追加不能覆盖已有内容。
    _SET_CARET_JS = """
    (el) => {
      el.focus();
      const sel = window.getSelection();
      if (!sel) return false;
      const range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
      return true;
    }
    """

    # 分块写入用的两种插入原语（都触发真实编辑事件，且不碰 OS 焦点）。
    _INSERT_TEXT_JS = """
    (el, text) => {
      el.focus();
      return document.execCommand('insertText', false, text) === true;
    }
    """
    _CLEAR_COMPOSER_JS = """
    (el) => {
      el.focus();
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
      return document.execCommand('delete') === true;
    }
    """

    # 合成 Enter（纯 DOM，不碰 OS 焦点）：只在真实键盘 Enter 之后兜底。
    _ENTER_JS = """
    (el) => {
      const opts = {key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
                    bubbles: true, cancelable: true};
      el.dispatchEvent(new KeyboardEvent('keydown', opts));
      el.dispatchEvent(new KeyboardEvent('keypress', opts));
      el.dispatchEvent(new KeyboardEvent('keyup', opts));
      return true;
    }
    """

    _IS_ACTIVE_JS = "(el) => document.activeElement === el"

    # 发送按钮的可用性（best-effort 诊断，失败时写进错误信息）。
    _BUTTON_STATE_JS = """
    (el) => JSON.stringify({
      aria_label: el.getAttribute('aria-label'),
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      connected: el.isConnected,
      size: (() => { const r = el.getBoundingClientRect();
        return Math.round(r.width) + 'x' + Math.round(r.height); })(),
    })
    """

    # 输入框写入失败时的诊断脚本：把“为什么不可编辑”留下来，而不是只丢一个
    # Playwright 超时。`caret_in_composer` 是最关键的一条：插入原语只在**当前
    # 选区**处生效，选区不在编辑器内时它们会静默空操作。
    _COMPOSER_DIAG_JS = """
    (el) => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      const active = document.activeElement;
      let caret_in_composer = false;
      try {
        const sel = window.getSelection();
        if (sel && sel.rangeCount) {
          const node = sel.getRangeAt(0).startContainer;
          caret_in_composer = (node === el || el.contains(node));
        }
      } catch (e) { caret_in_composer = false; }
      return JSON.stringify({
        tag: el.tagName,
        ce: el.getAttribute('contenteditable'),
        aria_disabled: el.getAttribute('aria-disabled'),
        disabled: el.hasAttribute('disabled'),
        connected: el.isConnected,
        display: style.display,
        visibility: style.visibility,
        size: Math.round(rect.width) + 'x' + Math.round(rect.height),
        active: active ? active.tagName + (active === el ? '(self)' : '') : null,
        caret_in_composer: caret_in_composer,
        child_nodes: el.childNodes.length,
      });
    }
    """

    def _fill_timeout_s(self) -> float:
        return max(0.1, (config.FILL_TIMEOUT_MS or 10000) / 1000.0)

    async def _locate_input(self, page, rounds: int = 3, timeout_ms: int = 2000):
        """定位对话输入框：最多 ``rounds`` 轮 × 逐个候选选择器，只做 DOM 查询。

        绝不 bring_to_front / 抢 OS 焦点：提交走页面内事件（见 ``_submit_prompt``），
        本就不依赖窗口是否在前台。验证提交时用更小的 ``rounds`` / ``timeout_ms``
        （页面在那里必须立即答复，不能把轮询拖成长任务）。
        """
        for _round in range(max(1, rounds)):
            for selector in config.INPUT_SELECTORS:
                try:
                    node = await page.wait_for_selector(selector, timeout=timeout_ms)
                    if node:
                        return node
                except Exception:  # noqa: BLE001 选择器未命中 / 超时
                    continue
        return None

    async def _composer_text(self, handle) -> Optional[str]:
        """读取输入框当前文本（best-effort；读不到返回 None，表示“无法判断”）。

        句柄已从页面断开（React 重挂载）时返回 None：旧节点上的文本是残留，
        不能拿它当“还没提交”的判据（真机误报的根因）。
        """
        if handle is None:
            return None
        try:
            text = await handle.evaluate(self._COMPOSER_TEXT_JS)
        except Exception:  # noqa: BLE001
            return None
        return text if isinstance(text, str) else None

    async def _handle_connected(self, handle) -> Optional[bool]:
        """句柄是否仍挂在页面上（best-effort；提交误报时写进错误信息供排查）。"""
        if handle is None:
            return None
        try:
            return bool(await handle.evaluate(self._COMPOSER_CONNECTED_JS))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_for_compare(text: str) -> str:
        """把文本压成可比较的形态：所有空白序列（含换行）折成单个空格。

        为什么必须这么做（真机根因）：富文本编辑器把每条换行渲染成**独立的块级
        节点**，读回来的 ``textContent`` **不含换行**。于是 ``prompt.startswith(current)``
        对任何多行 prompt 都判 False，而 False 会触发清空——把**刚写好的整条 prompt
        清掉**再从头写（自我毁灭的循环）。
        """
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _prompt_present(cls, prompt: str, current: str) -> bool:
        """输入框里已经是我们这条 prompt 吗？（**容忍**编辑器对空白/换行的规范化）

        不做逐字节比较：读回的开头一段与长度都按折叠后的形态比。判据取保守的两条：
        开头 120 字符一致 + 长度不短于 prompt（防被截断后误判）。
        """
        if not current:
            return False
        want = cls._normalize_for_compare(prompt)
        have = cls._normalize_for_compare(current)
        if not want or not have:
            return False
        head = want[: min(120, len(want))]
        return have.startswith(head) and len(have) >= len(want)

    # 「消息是否已经出现在对话区」判断用的归一化：网页把消息渲染成 markdown 后会
    # 吞掉 `*` / 反引号 / `[]` 这类标记，双方都去掉这些字符再折叠空白才能可靠比对。
    _PRESENCE_STRIP_RE = re.compile(r"[`*_#>~\[\]()|]+")

    @classmethod
    def _prompt_head_for_presence(cls, prompt: str) -> str:
        """取 prompt 开头的可比较片段；太短（<12 字符）则放弃这条证据。"""
        sample = cls._PRESENCE_STRIP_RE.sub(" ", prompt[:2000])
        sample = re.sub(r"\s+", " ", sample).strip()
        return sample[:120] if len(sample) >= 12 else ""

    @classmethod
    def _text_shows_prompt(cls, page_text: str, prompt: str) -> bool:
        """页面对话区文本里是否已经出现这条 prompt 的开头。"""
        head = cls._prompt_head_for_presence(prompt)
        if not head or not page_text:
            return False
        normalized = re.sub(r"\s+", " ", cls._PRESENCE_STRIP_RE.sub(" ", page_text))
        return head in normalized

    async def _page_shows_prompt(self, page, prompt: str, handle=None) -> bool:
        """这条 prompt 是否已经渲染进页面对话区（确认消息真的被网页收下）。

        textarea 的 value 不进 innerText，但 contenteditable 的正文会；先把输入框
        自身的文本从页面文本里剔除，避免把“还在框里的草稿”当成“已经发出的消息”。
        失败一律返回 False（无法判断不作为证据）。
        """
        if page is None or not prompt:
            return False
        try:
            page_text = await page.evaluate(self._PROMPT_IN_PAGE_JS, handle)
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(page_text, str):
            return False
        return self._text_shows_prompt(page_text, prompt)

    async def _composer_diag(self, handle) -> str:
        """收集输入框状态（best-effort：诊断本身绝不抛错）。"""
        if handle is None:
            return "（输入框句柄为空）"
        try:
            return str(await handle.evaluate(self._COMPOSER_DIAG_JS))
        except Exception as exc:  # noqa: BLE001
            return f"（诊断不可用：{exc}）"

    async def _focus_composer(self, page, handle) -> None:
        """让输入框真正获得焦点：**真实 click() 优先**，失败再退 JS ``el.focus()``。

        为什么不能只调 JS focus：受控编辑器只把**真实交互**后的焦点当成“激活”，
        否则后续插入的文本可能不进它的内部模型；而且 ``page.keyboard`` 走的是
        页面内焦点，不聚焦就等于把按键送到别处。
        """
        if handle is None:
            return
        try:
            await asyncio.wait_for(
                handle.click(timeout=config.FILL_TIMEOUT_MS or 5000),
                timeout=self._fill_timeout_s(),
            )
            return
        except Exception as exc:  # noqa: BLE001 不可见 / 被遮挡 / 超时
            if config.DEBUG:
                print(f"[debug] 聚焦输入框的 click 失败（{exc}），退回 JS focus")
        try:
            await asyncio.wait_for(
                handle.evaluate("(el) => el.focus()"), timeout=self._fill_timeout_s()
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("无法聚焦输入框：click 与 JS focus 均失败") from exc

    async def _set_caret(self, handle) -> bool:
        """把光标显式放进输入框（最佳努力；失败返回 False，由调用方继续尝试插入）。

        为什么必须显式设置（真机根因）：``page.keyboard.insert_text()`` 发的是
        CDP ``Input.insertText``，**只在当前选区处插入**；``execCommand('insertText')``
        同理。而输入框往往**已经是** ``document.activeElement``——此时 ``el.focus()``
        是空操作，页面里没有任何落在编辑器内的选区，两种插入随之变成**静默空操作**：
        不抛错、一个字也不进。
        """
        if handle is None:
            return False
        try:
            return bool(
                await asyncio.wait_for(
                    handle.evaluate(self._SET_CARET_JS), timeout=self._fill_timeout_s()
                )
            )
        except Exception as exc:  # noqa: BLE001
            if config.DEBUG:
                print(f"[debug] 设置输入框光标失败：{exc}")
            return False

    async def _insert_chunk(self, page, handle, text: str, prefer_keyboard: bool = True):
        """把**一块**文本插入输入框；返回真正生效的原语名，两种都失败时返回 None。

        原语顺序：``page.keyboard.insert_text()``（CDP，走浏览器真实编辑管线，会触发
        beforeinput/input，受控编辑器才会同步内部模型）**优先**；
        ``document.execCommand('insertText')``（同样触发真实编辑事件）退路。
        插入之前先显式放置光标（见 ``_set_caret``）：两种原语都只在当前选区处生效，
        没有选区它们既不报错也不写入。
        真正“写进去了没有”由调用方逐块读回校验，所以这里不把“没报错”当成成功。
        """
        timeout = self._fill_timeout_s()
        insert_text = getattr(getattr(page, "keyboard", None), "insert_text", None)

        async def via_keyboard() -> bool:
            if insert_text is None:
                return False
            try:
                await asyncio.wait_for(insert_text(text), timeout=timeout)
                return True
            except Exception as exc:  # noqa: BLE001
                if config.DEBUG:
                    print(f"[debug] keyboard.insert_text 失败：{exc}")
                return False

        async def via_exec_command() -> bool:
            try:
                return bool(
                    await asyncio.wait_for(
                        handle.evaluate(self._INSERT_TEXT_JS, text), timeout=timeout
                    )
                )
            except Exception as exc:  # noqa: BLE001 句柄失效 / 页面卡住 / 超时
                if config.DEBUG:
                    print(f"[debug] execCommand 插入失败：{exc}")
                return False

        await self._set_caret(handle)
        if prefer_keyboard:
            primitives = (("keyboard", via_keyboard), ("execCommand", via_exec_command))
        else:
            primitives = (("execCommand", via_exec_command), ("keyboard", via_keyboard))
        for name, primitive in primitives:
            if await primitive():
                return name
        return None

    async def _clear_composer(self, page, handle) -> None:
        """把输入框清到「读回来是空的」为止（多手法 + 循环校验）。

        编辑器会把草稿持久化，页面上可能残留上一次没发出去的内容；不清空就会与
        新 prompt **拼接**后一起发出去。单一手法都不可靠（Ctrl+A 在 macOS 未必
        生效、直接改 DOM 会被编辑器回滚），所以组合使用并循环校验。
        """
        keyboard = getattr(page, "keyboard", None)
        press = getattr(keyboard, "press", None)
        for _round in range(3):
            current = await self._composer_text(handle)
            if current is None or not current.strip():
                return
            if press is not None:
                for modifier in ("Control+A", "Meta+A"):
                    try:
                        await press(modifier)
                        await press("Backspace")
                    except Exception:  # noqa: BLE001
                        pass
            try:
                await handle.fill("", timeout=config.FILL_TIMEOUT_MS or 5000)
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(
                    handle.evaluate(self._CLEAR_COMPOSER_JS), timeout=self._fill_timeout_s()
                )
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.1)
        leftover = await self._composer_text(handle)
        if leftover:
            print(f"[输入] 清空输入框后仍读到 {len(leftover)} 字符残留，新 prompt 可能被拼接")

    async def _insert_prompt_in_chunks(self, page, prompt: str, handle=None) -> Optional[int]:
        """**分块**把 prompt 写进输入框，返回已写入的字符数；读不到输入框文本时返回 None。

        为什么必须分块（真机复现）：客户端的 find / read 结果很长时，一次性
        ``fill()`` 会在网页主线程上排成一个长任务（React 重渲染 + 富文本编辑器同步），
        期间 Playwright 连“元素是否可见/可编辑”都探测不到，直接抛
        ``waiting for element to be visible, enabled and editable``；网页本身也卡住，
        于是整轮请求失败、客户端拿不到任何回复（用户侧看到的就是「输入框卡死」）。

        做法：按 ``FILL_CHUNK_CHARS`` 逐块插入；每块之前**重新读一遍输入框文本**，
        只补写缺的那一段——因此可重挂载后续写、不会重复写入，也能从上次失败处继续。
        每块之间 ``await asyncio.sleep(0)`` 让出主线程。

        :param handle: 已经定位好的输入框句柄（复用，避免重复定位）；None 表示自己定位。
        """
        chunk = max(200, config.FILL_CHUNK_CHARS or 4000)
        max_stalls = max(1, config.FILL_RETRIES)
        cleared = False   # 残留只清一次：读回是有损的，反复清会删掉刚写进去的内容
        blind = False     # 读回对不上且已清过一次 -> 改用“本轮自己插入了多少”推进
        written = 0
        stalls = 0
        last_len = -1
        last_source: Optional[str] = None
        focused = False
        while True:
            if handle is None:
                handle = await self._locate_input(page)
                if handle is None:
                    raise RuntimeError("分块写入时找不到输入框")
                focused = False  # 重挂载后的新节点要重新聚焦
            if not blind:
                current = await self._composer_text(handle)
                if current is None:
                    return None  # 读不到文本的页面：改用整段 fill 兜底
                if self._prompt_present(prompt, current):
                    # 已经是我们这条 prompt（可能被编辑器规范化过空白/换行）：直接成功
                    return len(prompt)
                if prompt.startswith(current):
                    written = len(current)  # 精确前缀：断点续写
                elif cleared:
                    # 已清过一次还是对不上：说明读回**有损**（换行被渲染成块级节点、
                    # markdown 标记变成格式）。**再去清就会把刚写进去的内容删掉**，
                    # 所以改为不再清空、按“自己插入了多少”推进。
                    print(f"[输入] 读回与 prompt 对不上（{len(current)} 字符）：不再清空，改为按已插入字符数推进")
                    blind = True
                    written = 0
                else:
                    # 首次遇到残留草稿：清一次，之后绝不再清
                    await self._clear_composer(page, handle)
                    cleared = True
                    after = await self._composer_text(handle)
                    if after is None:
                        return None
                    written = len(after) if prompt.startswith(after) else 0
            if written >= len(prompt):
                return written
            if not blind:
                # 进度判据：读回长度必须增长。有损读回只改变绝对值，不改变增减。
                cur_len = len(await self._composer_text(handle) or "")
                if 0 <= last_len and cur_len <= last_len:
                    stalls += 1
                else:
                    stalls = 0
                last_len = cur_len
                if stalls > max_stalls:
                    detail = await self._composer_diag(handle)
                    if written == 0:
                        print(
                            f"[输入] 分块插入完全无效（已写入 0/{len(prompt)} 字符，"
                            f"最后原语={last_source or '无'}）：{detail}"
                        )
                        raise ChunkedInsertUnavailable(
                            "分块插入在本页面写不进去（已写入 0 字符）"
                        )
                    print(
                        f"[输入] 分块写入无进展：读回 {cur_len} 字符"
                        f"（已写入 {written}/{len(prompt)}，最后原语={last_source or '无'}）：{detail}"
                    )
                    raise RuntimeError(
                        f"分块写入无进展：已写入 {written}/{len(prompt)} 字符"
                    )
            if not focused:
                # 真实 click 聚焦（优先）：page.keyboard.insert_text 走页面内焦点，
                # 不聚焦就会把文本送到别处。
                await self._focus_composer(page, handle)
                focused = True
            piece = prompt[written:written + chunk]
            try:
                # 上一次“没进展”说明这个原语可能被编辑器忽略：换另一个原语再试
                last_source = await self._insert_chunk(
                    page, handle, piece, prefer_keyboard=(stalls == 0)
                )
            except Exception as exc:  # noqa: BLE001 句柄失效 / 页面卡住
                print(f"[输入] 分块写入失败（已写入 {written}/{len(prompt)} 字符）：{exc}；重新定位后继续写")
                handle = None
                last_source = None
                stalls += 1
                if stalls > max_stalls * 2:
                    raise
                await asyncio.sleep(0)
                continue
            if last_source is None:
                # 两种插入原语都失败：这块没写进去，不能推进 written
                stalls += 1
                if stalls > max_stalls * 2:
                    detail = await self._composer_diag(handle)
                    raise RuntimeError(
                        f"分块写入无进展：已写入 {written}/{len(prompt)} 字符"
                        f"（两种插入原语均失败）；输入框状态={detail}"
                    )
            else:
                written += len(piece)
                if blind:
                    stalls = 0
            await asyncio.sleep(0)

    async def _write_prompt(self, page, prompt: str, chat_input, timeout):
        """把 prompt 真正写进输入框，返回可提交的句柄（整段 fill 优先，分块兜底）。

        三条防线：
        * 写之前先读回：**已经在框里**就跳过写入（重试时最常见的浪费与风险）；
        * ``fill()`` 之后读回：“成功”或“报错但文本已落地”都算成功；
        * ``fill()`` 确实没落地 -> 分块插入；分块也写不进 -> 抛回 **fill 的原始错误**
          （那才是根因，分块失败只是它的衍生现象）。
        """
        current = await self._composer_text(chat_input)
        if current and self._prompt_present(prompt, current):
            print(f"[输入] 输入框已有本条 prompt（{len(current)} 字符）：跳过写入")
            return await self._locate_input(page) or chat_input
        fill_error: Optional[Exception] = None
        try:
            await chat_input.fill(prompt, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 Playwright 超时等
            fill_error = exc
        else:
            # fill 没报错就信任它：读回差异只记日志，绝不据此判失败——富文本编辑器的
            # 读回是**有损**的（换行渲染成块级节点、markdown 标记变成格式），拿差值
            # 当失败判据会把本来已经写好的请求判死，再清空重写反而把它弄坏。
            landed = await self._composer_text(chat_input)
            if landed is not None and len(landed) != len(prompt):
                print(
                    f"[输入] fill 完成：读回 {len(landed)} 字符 / prompt {len(prompt)} 字符"
                    "（编辑器有损渲染，属正常）"
                )
            else:
                print(f"[输入] 写入输入框完成：{len(prompt)} 字符（整段 fill）")
            return chat_input
        # 只有 fill **报错**时才需要读回救援：真机见过 fill 报超时但文本已落地
        landed = await self._composer_text(chat_input)
        if landed and self._prompt_present(prompt, landed):
            print(f"[输入] fill 报错（{fill_error}）但 {len(landed)} 字符已在输入框中：按成功继续提交")
            return await self._locate_input(page) or chat_input
        try:
            written = await self._insert_prompt_in_chunks(page, prompt, chat_input)
        except ChunkedInsertUnavailable:
            raise fill_error
        if written is None:
            # 读不到输入框文本、分块无法判断，而 fill 也已经失败：抛 fill 的错误，
            # 让上层重试（会重新定位句柄），不做无谓的第二次整段 fill。
            raise fill_error
        print(f"[输入] 写入输入框完成：{written} 字符（分块）")
        # 分块过程中节点可能被重挂载，重新定位一个可用的句柄再提交
        return await self._locate_input(page) or chat_input

    async def _fill_prompt(self, page, prompt: str):
        """把 prompt 写进输入框，返回可提交的句柄；每次尝试都**重新定位**。

        两条路径：
        1. **整段 ``fill()`` 优先**：Playwright 对 contenteditable 自带选区
           （focus + selectNodeContents + addRange），多数页面仍然最快；
           **``fill()`` 报超时 ≠ 没写进去**——读回确认后可能直接继续提交。
        2. **分块插入**只在 ``fill()`` 确实没把文本写进去时才用——把长任务切碎、
           逐块读回校验、可续写，这正是「长 prompt 卡死输入框」的正解。

        每次尝试都重新定位（拿到重挂载后的新节点）；失败按 ``RETRY_BACKOFF_S``
        退避再试，最多 ``FILL_RETRIES`` 次；仍不行才报错，并附输入框诊断。
        """
        attempts = max(1, config.FILL_RETRIES)
        timeout = config.FILL_TIMEOUT_MS or None
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            chat_input = await self._locate_input(page)
            if chat_input is None:
                last_error = RuntimeError("选择器均未命中输入框")
                print(f"[输入] 写入输入框失败（第 {attempt}/{attempts} 次）：找不到输入框")
            else:
                try:
                    return await self._write_prompt(page, prompt, chat_input, timeout)
                except Exception as exc:  # noqa: BLE001 Playwright TimeoutError 等
                    last_error = exc
                    print(
                        f"[输入] 写入输入框失败（第 {attempt}/{attempts} 次，prompt {len(prompt)} 字符）："
                        f"{exc}；输入框状态={await self._composer_diag(chat_input)}"
                    )
            if attempt < attempts:
                await asyncio.sleep(max(0.0, config.RETRY_BACKOFF_S))
        raise RuntimeError(
            f"写入输入框连续失败 {attempts} 次（单次超时 {config.FILL_TIMEOUT_MS}ms）："
            "整段 fill 与分块插入都没能把 prompt 写进去。常见原因：登录态失效或被风控"
            "拦住、页面停在非对话视图、有头模式下窗口失焦后输入框被懒卸载（可试 HEADLESS=1），"
            "或单个 prompt 超出输入框字符上限（可调小 PROMPT_MAX_CHARS / FILL_CHUNK_CHARS）。"
        ) from last_error

    async def _keyboard_enter(self, page, chat_input) -> bool:
        """用**真实键盘事件**提交（首选）。

        为什么优先于合成事件：``dispatchEvent(new KeyboardEvent(...))`` 的
        ``isTrusted=false``，受控编辑器常常直接忽略它——这正是「文字在输入框里、
        消息没发出去」的根源之一；只有 CDP 通道的真实按键才会走编辑器的提交 handler。
        不依赖窗口是否在前台：Playwright 的键盘事件走 CDP，直接投递给页面内当前
        焦点元素；这里先确认输入框仍是 activeElement，不是就先真实 click 聚焦。
        """
        if page is None or chat_input is None:
            return False
        try:
            focused = bool(await chat_input.evaluate(self._IS_ACTIVE_JS))
        except Exception:  # noqa: BLE001
            focused = False
        if not focused:
            try:
                await self._focus_composer(page, chat_input)
            except Exception as exc:  # noqa: BLE001
                if config.DEBUG:
                    print(f"[debug] 聚焦输入框失败：{exc}")
        try:
            await page.keyboard.press("Enter")
            return True
        except Exception as exc:  # noqa: BLE001
            if config.DEBUG:
                print(f"[debug] 真实键盘 Enter 失败：{exc}")
            return False

    async def _dispatch_enter(self, chat_input) -> bool:
        """在页面内对输入框派发 Enter 键事件（纯 DOM，不碰 OS 焦点）。"""
        if chat_input is None:
            return False
        try:
            return bool(await chat_input.evaluate(self._ENTER_JS))
        except Exception:  # noqa: BLE001
            return False

    async def _send_button(self, page):
        """定位发送按钮（多个候选选择器依次尝试），找不到返回 None。"""
        if page is None:
            return None
        for selector in config.SEND_BUTTON_SELECTORS:
            try:
                button = await page.query_selector(selector)
                if button:
                    return button
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _click_send_button(self, page) -> bool:
        """点发送按钮：先 Playwright 原生 ``click()``（真实鼠标事件，React 才认），
        失败再退化为 DOM ``dispatch_event("click")``（同样不碰 OS 焦点）。

        ``click()`` 需要按钮处于 enabled 状态；按钮被禁用时它会超时——那正是
        「编辑器还在处理长文本」的信号，在这里会被记成失败并进入下一次尝试。
        """
        button = await self._send_button(page)
        if button is None:
            return False
        timeout = config.FILL_TIMEOUT_MS or 5000
        try:
            await button.click(timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001 含禁用/不可见导致的超时
            if config.DEBUG:
                print(f"[debug] 发送按钮原生 click 失败（{exc}），改用 DOM 事件")
        try:
            await button.dispatch_event("click")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[提交] 发送按钮 DOM click 也失败：{exc}")
            return False

    async def _send_button_state(self, page) -> str:
        """发送按钮的可用性（best-effort 诊断）。"""
        button = await self._send_button(page)
        if button is None:
            return "未找到发送按钮"
        try:
            return str(await button.evaluate(self._BUTTON_STATE_JS))
        except Exception as exc:  # noqa: BLE001
            return f"（按钮状态不可读：{exc}）"

    async def _prompt_submitted(self, page, chat_input, bucket: Optional[str] = None):
        """刚才那次提交是否真的生效？返回 ``(判定, 输入框句柄)``；None = 无法判断。

        判据（满足其一即为已提交）：
        * **输入框已清空**——网页接受了这条消息，最直接的证据；
        * 页面进入「生成中」（停止按钮出现）。
        读不到输入框内容时**先重新定位一次输入框**再读：真机故障里 React 会在提交后
        重挂载编辑器，旧节点已 disconnected 却仍保留着 fill 进去的文本；拿它当判据
        就会把“已经发出去的消息”误判成没提交（用户侧看到的就是提交误报）。重新定位后
        仍读不到才算无法判断，返回 None。
        """
        text = await self._composer_text(chat_input)
        if text is not None and not text.strip():
            return True, chat_input
        if text is None:
            # 旧句柄已断连 / 不可读：换成当前页面上的输入框再读一次
            fresh = await self._locate_input(page, rounds=1, timeout_ms=1000)
            fresh_text = await self._composer_text(fresh) if fresh is not None else None
            if fresh_text is not None:
                chat_input, text = fresh, fresh_text
                if not text.strip():
                    return True, chat_input
        # 读不到输入框内容时返回 None（无法判断）——此时**不**去探测「生成中」：
        # 无法判断就不该继续做额外探测，也不该据此报错。
        if text is None:
            return None, chat_input
        if await self._page_is_generating(bucket or DEFAULT_SESSION_KEY):
            return True, chat_input
        return False, chat_input

    async def _wait_submitted(self, page, chat_input, bucket: Optional[str] = None):
        """等待「已提交」的迹象，最长 ``SUBMIT_VERIFY_MS``；返回 ``(判定, 句柄)``。

        为什么不能只读一次：Enter 派发后，网页要先更新内部状态、清空输入框、
        再开始生成，都有延迟；立即读会看到「输入框里还有字」而误判成没提交。
        若**根本读不到**输入框内容（None），立即返回 None：无法验证就不该空等。
        """
        deadline = asyncio.get_event_loop().time() + max(0.0, config.SUBMIT_VERIFY_MS / 1000.0)
        while True:
            submitted, chat_input = await self._prompt_submitted(page, chat_input, bucket)
            if submitted is None:
                return None, chat_input
            if submitted or asyncio.get_event_loop().time() >= deadline:
                return submitted, chat_input
            await asyncio.sleep(0.1)

    async def _submit_prompt(self, page, chat_input, bucket: Optional[str] = None,
                             prompt: Optional[str] = None) -> None:
        """提交 prompt，并**确认它真的发出去了**（全程无窗口焦点依赖）。

        为什么必须确认（真实故障）：旧实现把 ``keyboard.press("Enter")`` 当作一定成功，
        从来不验证、也没有兜底；当网页没接住按键时（长文本刚 fill 进去、编辑器还没
        接管，或发送按钮处于 disabled），输入框里就是“文字在、消息没发”，网页不产生
        任何回复，客户端只能干等到超时。

        为什么还要看“消息出现在对话区”（第二个真实故障）：网页明明已经收下消息并开始
        生成，但 React 会在提交后重挂载编辑器——旧句柄已断开却仍留着 fill 进去的文本，
        只看旧句柄就会把“已发出”误判成“未提交”并直接抛错（客户端拿到 server_error）。
        因此：① 读输入框前旧句柄不可读就重新定位；② 阶梯全部失败后，再看这条消息是否
        **在提交之后新出现**在页面对话区里——出现即按已提交继续轮询。

        阶梯（每次尝试后都用 ``_wait_submitted`` 验证）：
        1. **真实键盘 Enter**（CDP，受控编辑器才认；合成事件常被忽略）；
        2. 点真正的发送按钮（先原生 click，再 DOM click）；
        3. 再派发一次合成 Enter（给编辑器消化大文本留出时间）。
        以上都拿不到“已提交”证据才抛可行动错误（附发送按钮与输入框状态）。
        """
        plan = ("真实键盘 Enter", "发送按钮", "合成 Enter")
        last_detail = "（未知）"
        # 提交前先记基线：任务重发同一段内容时，页面里可能**本来就有**这条消息，
        # 只有“提交后才新出现”才能算本轮发出的证据。
        prompt_was_visible: Optional[bool] = None
        if prompt:
            prompt_was_visible = await self._page_shows_prompt(page, prompt, chat_input)
        for index, action in enumerate(plan, start=1):
            try:
                if action == "真实键盘 Enter":
                    await self._keyboard_enter(page, chat_input)
                elif action == "合成 Enter":
                    await self._dispatch_enter(chat_input)
                else:
                    await self._click_send_button(page)
            except Exception as exc:  # noqa: BLE001 兜底本身出错不能中断阶梯
                print(f"[提交] 第 {index}/{len(plan)} 次尝试（{action}）出错（已忽略）：{exc}")
            submitted, chat_input = await self._wait_submitted(page, chat_input, bucket)
            if submitted is not False:
                if index > 1:
                    print(f"[提交] 第 {index} 次尝试（{action}）成功。")
                return
            remaining = await self._composer_text(chat_input)
            last_detail = (
                f"输入框仍有 {len(remaining)} 字符" if remaining is not None else "无法读取输入框"
            )
            print(
                f"[提交] 第 {index}/{len(plan)} 次尝试（{action}）无效：{last_detail}；"
                f"发送按钮={await self._send_button_state(page)}"
            )
        # 最终证据：消息在提交后新出现在对话区 -> 网页已经收下，输入框里的文本是
        # 旧节点残留（或受控组件还没清空）。按已提交继续轮询，不要给客户端误报。
        if prompt and prompt_was_visible is False:
            if await self._page_shows_prompt(page, prompt, chat_input):
                print("[提交] 页面对话区已出现本条消息：按已提交继续（输入框残留的是旧文本）。")
                return
            detail = "未在页面对话区检测到本条消息"
        else:
            detail = "页面侧无更多证据"
        attached = await self._handle_connected(chat_input)
        raise RuntimeError(
            f"prompt 已写入输入框但未能提交（尝试 {len(plan)} 次：键盘 Enter -> 发送按钮 -> 合成 Enter）；"
            f"{last_detail}；{detail}（输入框句柄仍挂载={attached}）。"
            "常见原因：发送按钮处于 disabled（编辑器还在消化长文本）、页面停在非对话视图，"
            "或按键被网页忽略。可调大 SUBMIT_VERIFY_MS；若与长 prompt 相关，"
            "可调小 PROMPT_MAX_CHARS / FILL_CHUNK_CHARS。"
        )

    @staticmethod
    def _clamp_prompt(prompt: str) -> str:
        """发送侧最后一道护栏：把整段 prompt 压到字符预算内（默认 48000 < 50K）。

        ``PROMPT_MAX_CHARS`` 不是「输入框物理上限」，而是**我们主动设的预算**：
        超长 prompt 会在网页主线程排成长任务，把输入框写卡死。触发时**必定留一条
        警告**：它意味着中间有一段内容真的被切掉了，工具结果可能被拦腰截断，
        模型可能因此答非所问——不看日志就无从察觉。

        截断后的总长度（含提示语）不超过 ``limit``，因此可安全重复调用。
        """
        limit = config.PROMPT_MAX_CHARS
        if not limit or len(prompt) <= limit:
            return prompt
        dropped = len(prompt) - limit
        notice = f"\n\n…（prompt 过长，已省略中间 {dropped} 字符）\n\n"
        # 提示语本身也占字符：把它算进预算，保证截断后的最终长度 ≤ limit。
        # 这让截断是**幂等**的——send_chat 与 _send_chat_locked 各调一次也不会
        # 把已经截好的内容再截一刀。
        budget = max(2, limit - len(notice))
        head = budget // 2
        tail = budget - head
        print(
            f"[截断] prompt {len(prompt)} 字符超出 PROMPT_MAX_CHARS={limit}："
            f"已省略中间 {dropped} 字符（保留头部 {head} + 尾部 {tail}）。"
            "工具结果可能被拦腰截断，模型可能答非所问。"
        )
        return prompt[:head] + notice + prompt[-tail:]

    async def _extract_code_blocks(self, element) -> List[dict]:
        """从某条回复的 DOM 节点中提取代码块（语言 + 纯代码文本）。"""
        extracted: List[dict] = []
        if element is None:
            return extracted
        code_elements = await element.query_selector_all(config.CODE_BLOCK_SELECTOR)
        for code_el in code_elements:
            code_tag = await code_el.query_selector(config.CODE_TAG_SELECTOR)
            lang = "txt"
            if code_tag:
                class_attr = await code_tag.get_attribute('class') or ""
                lang_match = re.search(r'language-(\w+)', class_attr)
                if lang_match:
                    lang = lang_match.group(1)

            code_content = await (code_tag or code_el).inner_text()
            clean_code = re.sub(
                r'^(?:' + lang + r'|bash|python|json|html|javascript)?\s*(?:Copy|Download)\s*\n',
                '', code_content, flags=re.IGNORECASE
            ).strip()

            extracted.append({"lang": lang, "code": clean_code})
        return extracted

    async def _send_chat_locked(self, prompt: str, on_delta=None,
                                key: Optional[str] = None) -> tuple[str, List[dict]]:
        """发送单条消息并获取响应及提取的代码块。

        :param on_delta: 可选异步回调，生成过程中实时吐出增量文本（用于 SSE 流式）。
        :param key: 会话桶标识（决定使用哪一条页面）。
        """
        bucket = key or DEFAULT_SESSION_KEY
        page = self._page_for(bucket)
        state = self._state(bucket)
        # 默认所有桶共用 self.lock（串行）；只有 PARALLEL_BUCKETS=true 才按桶各持一把锁
        async with self._session_lock(bucket):
            if page is None:
                raise RuntimeError("浏览器尚未初始化：找不到可用于发送的会话页面。")
            self._touch_page(bucket)  # 正在用的页面不会被空闲回收 / LRU 淘汰
            # 1. 先确认输入框存在（快速失败，给出明确的“没登录/没打开”提示）；
            #    真正写入时还会在 _fill_prompt 里**每次尝试重新定位一次**。
            if not await self._locate_input(page):
                raise RuntimeError("无法找到对话输入框，请检查 DeepSeek 网页是否打开或处于登录状态。")

            # 记录发送前最后一条回复的文本，用来判断“新回复是否已经出现”。
            # 注意：绝不能用“回复节点数量变多”来判断。
            # DeepSeek 的消息列表会回收/替换节点，长会话下节点数可能恒为 2，
            # 新回复只会把旧节点内容改掉而不会让数量增长，
            # 那样会导致永远读不到本轮回复直接等到超时。
            before_text = ""
            before_count = 0
            try:
                before_nodes = await page.query_selector_all(config.RESPONSE_SELECTORS)
                before_count = len(before_nodes)
                if before_nodes:
                    before_text = (await before_nodes[-1].inner_text()).strip()
            except Exception:
                before_text = ""

            # 1.5 发送前最后一道护栏：把超长 prompt 压到字符预算内（默认 48000 < 50K）。
            #     必须发生在写入之前——一次性写超长文本正是「输入框卡死」的成因。
            prompt = self._clamp_prompt(prompt)
            if config.DEBUG:
                print(f"[debug] 发送 prompt={len(prompt)} 字符")

            # 1.6 写入（整段 fill 优先，长文本自动退回分块插入）+ 提交并验证。
            #     不再假设「按了 Enter 就发出去了」：写不进去 / 发不出去会立刻抛
            #     可操作的错误，而不是让客户端干等到 RESPONSE_TIMEOUT_S。
            chat_input = await self._fill_prompt(page, prompt)
            await self._submit_prompt(page, chat_input, bucket, prompt=prompt)

            # 2. 轮询等待回复完成
            await asyncio.sleep(config.POLL_INTERVAL_S)
            last_text = ""
            last_normalized = ""
            last_len = -1
            streamed = ""            # 已经通过 on_delta 发给客户端的内容
            stable_count = 0
            saw_generating = False      # 本轮是否观测到过页面「生成中」状态
            latest_node = None          # 本轮最新的回复节点
            poll = 0
            deadline = asyncio.get_event_loop().time() + config.RESPONSE_TIMEOUT_S

            cap_check_every = max(1, config.CAP_CHECK_EVERY)
            no_node_fail_polls = max(2, config.NO_NODE_FAIL_POLLS)
            no_progress_fail_polls = max(
                no_node_fail_polls + 1, config.NO_PROGRESS_FAIL_POLLS
            )
            no_node_polls = 0           # 连续「一个回复节点都没匹配到」的轮数
            no_progress_polls = 0       # 连续「既无新回复又无生成中」的轮数
            continue_used = 0           # 本轮已自动点击「继续生成」的次数

            async def _maybe_continue() -> bool:
                """判定本轮结束时页面上是否有「继续」按钮；有则点击并继续收集。

                返回 True 表示已点击、调用方应**不**结束本轮，而是重置分段状态
                继续轮询（把后续内容拼进同一条回复）。返回 False 表示可以结束。
                超过 ``CONTINUE_BUTTON_MAX`` 次后不再点击，避免无限续接。
                """
                nonlocal continue_used, stable_count, last_normalized, last_len, saw_generating
                if continue_used >= max(0, config.CONTINUE_BUTTON_MAX):
                    return False
                label = await self._click_continue_if_present(bucket)
                if not label:
                    return False
                continue_used += 1
                # 分段重新开始：上一段的“稳定 / 长度 / 生成中”判定不能带到下一段，
                # 否则新一段刚出现就会被判成“没变化”而立刻结束。
                stable_count = 0
                last_normalized = ""
                last_len = -1
                saw_generating = False
                no_progress_polls = 0
                if config.DEBUG:
                    print(f"[debug] 检测到「{label}」按钮，点击后继续收集（第 {continue_used} 次）")
                return True

            while True:
                poll += 1
                responses = await page.query_selector_all(config.RESPONSE_SELECTORS)
                current_text = ""
                generating = None
                if responses:
                    latest_node = responses[-1]
                    current_text = await latest_node.inner_text()
                normalized = current_text.strip()

                # 0. 快速失败：连续多轮一个回复节点都没有。
                #    与「真的在生成但选择器没命中」不同，这里连旧回复都不存在，
                #    基本可断定是选择器失效 / 消息压根没发出去，再等只是浪费超时时间。
                if not responses:
                    no_node_polls += 1
                    if no_node_polls >= no_node_fail_polls:
                        raise DeepSeekTimeoutError(
                            "连续 {} 轮未匹配到任何回复节点（RESPONSE_SELECTORS={!r}），"
                            "疑似选择器失效或消息未成功发送。".format(
                                no_node_polls, config.RESPONSE_SELECTORS
                            )
                        )
                else:
                    no_node_polls = 0

                # 1. 本轮回复是否已经出现。判据（满足其一即可）：
                #    a) 末节点文本 != 发送前文本；
                #    b) 节点数变多（短会话常见）；
                #    c) 已经观测到过「生成中」——这说明本轮确已开始，
                #       此时即使文本暂时等于 before_text（首帧还没渲染完）也算已出现。
                #    注意：不能只看节点数——长会话下新回复会原地替换旧节点，数量不增长。
                reply_seen = (
                    (bool(normalized) and normalized != before_text)
                    or (len(responses) > before_count)
                    or saw_generating
                )

                # 1.1 还没有新回复时，周期性检查是否“会话到顶”。
                #     到顶与“真的卡住”在外表上完全一样（页面不再产生新回复），
                #     不主动看提示语就只能等到超时，而那时已经分不清原因了。
                if not reply_seen:
                    # 1.1 周期性检查是否「会话到顶」。
                    #     到顶与「真的卡住」在外表上完全一样（页面不再产生新回复），
                    #     不主动看提示语就只能等到超时，而那时已经分不清原因了。
                    if poll % cap_check_every == 0:
                        if await self._page_shows_context_limit(bucket):
                            self._mark_context_limit(bucket)
                            raise self._context_limit_error()
                    # 1.2 兜底：新回复迟迟不出现（页面既没生成中、文本也没变）。
                    #     可能是 DOM 选择器没命中新回复、或模型直接复用旧节点。
                    #     观察「是否生成中」一旦变 True 就交给下面的主逻辑。
                    generating = await self._page_is_generating(bucket)
                    if generating:
                        saw_generating = True

                    # 1.3 看门狗：连续多轮既无新回复也无生成中状态，提前结束，
                    #     不再干等到总超时（超时后错误信息也帮不上排查）。
                    no_progress_polls += 1
                    if no_progress_polls >= no_progress_fail_polls:
                        await self._remember_session(bucket)
                        raise DeepSeekTimeoutError(
                            "连续 {} 轮既未出现新回复、也未观测到生成中状态"
                            "（nodes={}，before_len={}），已放弃等待。".format(
                                no_progress_polls, len(responses), len(before_text)
                            )
                        )
                else:
                    no_progress_polls = 0

                if reply_seen:
                    # 2.1 主判定：页面「生成中」状态。一旦观测到过「停止生成」
                    #     控件、又发现它消失，就说明生成真正结束，可立即收尾
                    generating = await self._page_is_generating(bucket)
                    if generating:
                        saw_generating = True
                    elif generating is False and saw_generating:
                        last_text = current_text
                        if await _maybe_continue():
                            await asyncio.sleep(config.POLL_INTERVAL_S)
                            continue
                        if config.DEBUG:
                            print(f"[debug] poll={poll} 停止按钮已消失，判定结束")
                        break

                    # 2.2 兜底判定：文本一模一样算一轮不变；
                    #     仅长度不再增长也算，但要更保守（多等几轮），
                    #     以免尾部重排 / 工具栏插入导致永远等不到逐字相等
                    same_text = bool(normalized) and normalized == last_normalized
                    same_len = bool(normalized) and len(normalized) == last_len
                    if same_text or same_len:
                        stable_count += 1
                        threshold = config.STABLE_POLLS if same_text else config.LEN_STABLE_POLLS
                        if stable_count >= threshold:
                            last_text = current_text
                            if await _maybe_continue():
                                await asyncio.sleep(config.POLL_INTERVAL_S)
                                continue
                            if config.DEBUG:
                                print(
                                    f"[debug] poll={poll} 内容稳定 {stable_count} 次"
                                    f"（same_text={same_text}），判定结束"
                                )
                            break
                    else:
                        stable_count = 0

                    # 2.3 生成过程中吐出增量，供 SSE 使用。
                    #     用「已发送内容」的公共前缀做 diff，即使节点中途重排也不会漏字
                    if on_delta is not None:
                        piece, streamed = _delta_piece(streamed, current_text)
                        if piece:
                            await on_delta(piece)

                    last_text = current_text
                    last_normalized = normalized
                    last_len = len(normalized)

                if config.DEBUG:
                    print(
                        f"[debug] poll={poll} nodes={len(responses)} len={len(normalized)} "
                        f"stable={stable_count} generating={generating} saw={saw_generating} "
                        f"before_len={len(before_text)}"
                    )

                # 总超时判定：若这期间其实已经读到实质回复，就直接返回已产生的内容，
                # 绝不再把同一句 prompt 重发一遍（避免网页多出一轮、与客户端状态错位）
                if asyncio.get_event_loop().time() > deadline:
                    await self._remember_session(bucket)
                    if last_text:
                        # 超时时若还有「继续」按钮，说明只是被截断而非到顶：先续接一轮
                        if await _maybe_continue():
                            deadline = (
                                asyncio.get_event_loop().time()
                                + config.RESPONSE_TIMEOUT_S
                            )
                            await asyncio.sleep(config.POLL_INTERVAL_S)
                            continue
                        print("[超时] 已读取到回复内容，直接返回，不重发。")
                        break
                    # 超时前最后确认一次是否“到顶”，否则错误信息会误导排查方向
                    if await self._page_shows_context_limit(bucket):
                        self._mark_context_limit(bucket)
                        raise self._context_limit_error()
                    raise DeepSeekTimeoutError(
                        "等待 DeepSeek 响应超时（{}s）：poll={} nodes={} "
                        "saw_generating={} before_len={}（选择器 {}）。".format(
                            int(config.RESPONSE_TIMEOUT_S), poll, len(responses),
                            saw_generating, len(before_text), config.RESPONSE_SELECTORS,
                        )
                    )

                await asyncio.sleep(config.POLL_INTERVAL_S)

            # 3. 从最新回复节点中提取代码块
            extracted_blocks = await self._extract_code_blocks(latest_node)

            # 4. 更新会话状态：已建立历史，并累计体积；超预算则下一轮轮转
            state.has_history = True
            state.turns += 1
            state.est_tokens += estimate_tokens(prompt) + estimate_tokens(last_text)
            state.last_error = None
            if self._session_over_budget(bucket):
                state.pending_rotation = True
                print(
                    f"[轮转] 会话已达预算（轮数={state.turns}，"
                    f"估算 token={state.est_tokens}），"
                    "下一轮将开启新会话并播种上下文。"
                )

            # 成功产生回复后：刷新会话状态（可能刚创建了新会话）并续期页面使用时间
            self._touch_page(bucket)
            await self._remember_session(bucket)
            return last_text, extracted_blocks

    def save_extracted_files(raw_text: str, code_blocks: List[dict], output_dir: str) -> List[str]:
        """将提取的代码落地为对应格式的文件"""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        saved = []

        ext_map = {
            "python": "py", "py": "py", "javascript": "js", "js": "js",
            "html": "html", "css": "css", "json": "json", "cpp": "cpp",
            "c": "c", "bash": "sh", "shell": "sh", "sql": "sql", "markdown": "md"
        }

        # 同一秒内的多个请求会拿到同样的 timestamp，必须再加一段随机后缀，
        # 否则 code_<ts>_1.py / response_<ts>.md 会互相覆盖（多任务并行后很常见）
        unique = uuid.uuid4().hex[:6]

        if code_blocks:
            for idx, block in enumerate(code_blocks, start=1):
                lang = block["lang"].lower().strip()
                code = block["code"]
                ext = ext_map.get(lang, "py" if "import " in code or "def " in code else "txt")

                timestamp = int(time.time())
                filename = f"code_{timestamp}_{idx}_{unique}.{ext}"
                filepath = Path(output_dir) / filename

                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(code)
                saved.append(str(filepath))
                print(f"[已保存文件] {filepath}")
        else:
            filename = f"response_{int(time.time())}_{unique}.md"
            filepath = Path(output_dir) / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw_text)
            saved.append(str(filepath))

        return saved

