"""页面池与并发锁（从 driver.py 拆出，作为 mixin 混入 DeepSeekWebDriver）。

管理「每个会话桶一条页面」的生命周期：惰性创建、判活重建、空闲回收、LRU 淘汰，
以及按桶加锁（串行或并发）。依赖宿主提供 ``context`` / ``page`` /
``_page_lock`` / ``_pages`` / ``_page_last_used`` / ``_locks`` /
``_active_buckets`` / ``_active_counts``。
"""

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from . import config
from .errors import DEFAULT_SESSION_KEY, HOME_URL, DeepSeekBusyError


class PagePoolMixin:
    """会话桶页面的创建、回收、淘汰与加锁。"""

    #: 判断「页面已经不可用」的异常线索（小写子串匹配）。
    #: 不 import playwright 的异常类，是为了让测试替身 / 不同版本都能命中。
    _PAGE_LOST_HINTS = (
        "target closed",
        "page closed",
        "has been closed",
        "browser has been closed",
        "context closed",
        "session closed",
        "target crashed",
        "page crashed",
        "browser has disconnected",
    )

    def _page_for(self, key: Optional[str] = None):
        """取出某个会话桶的页面；默认桶就是 ``self.page``。

        注意：返回的对象**可能已经失效**（标签被关掉 / 渲染进程崩溃）。
        使用前请用 ``_page_is_alive`` 判活，或直接走 ``_ensure_page`` /
        ``_rebuild_page`` 让它自愈。
        """
        bucket = key or DEFAULT_SESSION_KEY
        if bucket == DEFAULT_SESSION_KEY:
            return self.page
        return self._pages.get(bucket)

    # ---------- 页面存活性（真机根因：标签被关闭后页面池里留着一具尸体）----------
    @staticmethod
    def _page_is_alive(page) -> bool:
        """页面对象是否仍可驱动（best-effort，任何情况下都不抛错）。

        为什么必须显式判活：``_pages`` 只记录「这条页面是我们创建的」，不保证标签
        还活着。用户在浏览器里关掉标签、渲染进程 OOM 崩溃、或浏览器回收后台标签后，
        页面对象仍留在 ``_pages`` 里；此后该会话桶的**每一次**请求都会在
        ``wait_for_selector`` 上立刻抛错，被误报成「找不到输入框，请检查是否登录」，
        并且永远不会自愈——直到进程重启。

        没有 ``is_closed`` 的实现（测试替身 / 老版本）按「存活」处理：
        宁可沿用旧行为，也不要因为探测不到就把好页面判死。
        """
        if page is None:
            return False
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed):
            try:
                return not is_closed()
            except Exception:  # noqa: BLE001 连判活都抛错 -> 视为不可用
                return False
        return True

    @classmethod
    def _looks_like_page_lost(cls, exc: BaseException) -> bool:
        """异常内容是否说明「页面已经没了」（而不是「选择器没命中」）。"""
        text = f"{type(exc).__name__}: {exc}".lower()
        return any(hint in text for hint in cls._PAGE_LOST_HINTS)

    def _mark_bucket_active(self, bucket: str) -> None:
        """标记该桶有请求在飞（可重入计数，见 ``_bucket_busy``）。"""
        self._active_counts[bucket] = self._active_counts.get(bucket, 0) + 1
        self._active_buckets.add(bucket)

    def _unmark_bucket_active(self, bucket: str) -> None:
        """撤销一次 ``_mark_bucket_active``（计数归零才不再算活跃）。"""
        left = self._active_counts.get(bucket, 1) - 1
        if left > 0:
            self._active_counts[bucket] = left
        else:
            self._active_counts.pop(bucket, None)
            self._active_buckets.discard(bucket)

    def sent_prompt(self, key: Optional[str] = None) -> Optional[str]:
        """某个会话桶最近一次真正发给网页版的 prompt（可能因轮转由增量改选播种版）。"""
        return self._last_prompts.get(key or DEFAULT_SESSION_KEY)

    def busy_keys(self) -> List[str]:
        """当前正在处理请求（已拿到锁、正在生成）的会话桶，供多 Agent 场景观察占用。"""
        return sorted(self._active_buckets)

    def cluster_stats(self) -> Dict[str, Any]:
        """多会话 / 多 Agent 运行概况（并发开关、桶上限、占用、已开页面数）。"""
        return {
            "parallel": config.PARALLEL_BUCKETS,
            "max_buckets": config.MAX_SESSION_BUCKETS,
            "open_pages": len(self._pages),
            "bucket_lock_timeout_s": config.BUCKET_LOCK_TIMEOUT_S,
            "busy": self.busy_keys(),
            "keys": self.session_keys(),
        }

    def _lock_for(self, key: Optional[str] = None) -> asyncio.Lock:
        """取某个会话桶的锁。

        默认（``PARALLEL_BUCKETS=false``）所有桶共用 ``self.lock``，即**串行**：
        分桶只是上下文隔离，不是并发能力。只有显式打开开关才会按桶各持一把锁。
        """
        bucket = key or DEFAULT_SESSION_KEY
        if not config.PARALLEL_BUCKETS or bucket == DEFAULT_SESSION_KEY:
            return self.lock
        lock = self._locks.get(bucket)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[bucket] = lock
        return lock

    @asynccontextmanager
    async def _session_lock(self, key: Optional[str] = None):
        """获取某个会话桶的锁；超过 ``BUCKET_LOCK_TIMEOUT_S`` 则抛 ``DeepSeekBusyError``。

        ``BUCKET_LOCK_TIMEOUT_S=0``（默认）表示一直等，保持旧行为；
        设成正数后，同一会话桶的请求堆叠时会快速失败，而不是排到客户端超时之后。
        """
        lock = self._lock_for(key)
        timeout = config.BUCKET_LOCK_TIMEOUT_S
        if timeout and timeout > 0:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                bucket = key or DEFAULT_SESSION_KEY
                raise DeepSeekBusyError(
                    f"会话桶 {bucket} 正在处理另一个请求（等待超过 {timeout:g}s）。"
                    "请稍后重试；若要并发访问，请为每个 Agent 使用不同的会话标识。"
                ) from None
        else:
            await lock.acquire()
        bucket = key or DEFAULT_SESSION_KEY
        self._mark_bucket_active(bucket)
        try:
            yield
        finally:
            self._unmark_bucket_active(bucket)
            lock.release()

    def _touch_page(self, key: Optional[str] = None) -> None:
        self._page_last_used[key or DEFAULT_SESSION_KEY] = time.monotonic()

    def _bucket_busy(self, bucket: str) -> bool:
        """该桶是否有请求在飞（正在生成，或已进入 send_chat 但还没拿到锁）。

        为什么不能只看锁（真机根因）：``send_chat`` 在拿锁**之前**就要确定页面
        （``_ensure_page``），这中间有一个窗口。另一个桶的 ``_ensure_page`` 恰好
        在这个窗口里按 LRU 淘汰，就会把**一条正在处理请求的页面**关掉，该请求随即
        报「找不到输入框」——表现为随机的 502。所以「有请求在飞」必须以
        ``_active_counts`` 为准，而不是只看锁是否被持有。
        """
        return bucket in self._active_counts or self._lock_for(bucket).locked()

    async def _close_bucket_page(self, bucket: str, reason: str) -> bool:
        """关闭某个会话桶的页面（**只关页面，状态保留**）。

        返回是否真的关掉了一个页面。状态里的 ``url`` / ``turns`` 不动，
        因此下次用到该桶时会重新打开同一个会话并按需播种上下文。
        """
        page = self._pages.pop(bucket, None)
        self._page_last_used.pop(bucket, None)
        if page is None:
            return False
        if not self._page_is_alive(page):
            # 已经是一具尸体（标签被关闭 / 崩溃）：不必再关，直接从池里移除即可
            print(f"[回收] key={bucket} 的页面已失效（{reason}），已移出页面池。")
            return True
        try:
            await page.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[回收] 关闭 key={bucket} 的页面时出错（已忽略）：{exc}")
        else:
            print(f"[回收] 已关闭 key={bucket} 的页面（{reason}），会话状态保留。")
        return True

    async def _recycle_idle_pages(self, exclude: Optional[str] = None) -> int:
        """关闭空闲超过 ``BUCKET_IDLE_TTL_S`` 的桶页面，返回关闭数量。"""
        ttl = config.BUCKET_IDLE_TTL_S
        if ttl <= 0:
            return 0
        now = time.monotonic()
        closed = 0
        for bucket in list(self._pages):
            if bucket == exclude or self._bucket_busy(bucket):
                continue
            last_used = self._page_last_used.get(bucket, now)
            if now - last_used > ttl:
                closed += 1 if await self._close_bucket_page(bucket, f"空闲超过 {int(ttl)}s") else 0
        return closed

    async def _evict_lru_page(self, exclude: Optional[str] = None) -> bool:
        """淘汰最久未用的桶页面（状态保留），腾出一个位置。

        正在生成回复的桶与 ``exclude`` 永不淘汰：淘汰它们会直接中断正在进行的一轮对话。
        """
        candidates = [
            bucket for bucket in self._pages
            if bucket != exclude and not self._bucket_busy(bucket)
        ]
        if not candidates:
            return False
        oldest = min(candidates, key=lambda b: self._page_last_used.get(b, 0.0))
        return await self._close_bucket_page(oldest, "超出会话桶上限，按 LRU 淘汰")

    async def _wait_ready(self, page) -> bool:
        """等页面的输入框就绪；超时只警告，不抛错（调用方还有自己的等待）。"""
        try:
            await page.wait_for_selector(
                config.READY_SELECTOR, timeout=config.READY_TIMEOUT_MS, state="visible"
            )
            return True
        except Exception:
            print("[会话] 页面已打开，但未检测到输入框，请检查登录状态。")
            return False

    def _bucket_target(self, bucket: str) -> str:
        """该桶下次打开页面时应落到的地址（上次的会话；已到顶则回首页开新会话）。"""
        state = self._state(bucket)
        return HOME_URL if state.cap_hit else (state.url or HOME_URL)

    async def _create_bucket_page(self, bucket: str) -> None:
        """创建某个会话桶的页面并回到它上次的会话。

        **调用方必须已持有 ``_page_lock``**（本方法内部会回收 / 淘汰别的桶页面）。
        """
        if self.context is None:
            raise RuntimeError("浏览器尚未初始化，无法创建新的会话页面。")
        limit = config.MAX_SESSION_BUCKETS
        if limit <= 0:
            raise RuntimeError(
                "MAX_SESSION_BUCKETS=0 表示不允许额外的会话桶（所有请求共用默认会话）。"
                "如需按任务隔离，请把它设为 >=1；想彻底关闭分桶请用 SESSION_SCOPING=false。"
            )
        # 先回收空闲页面，仍不够就按 LRU 淘汰最久未用的（两者都不丢会话状态）
        await self._recycle_idle_pages(exclude=bucket)
        while len(self._pages) >= limit:
            if not await self._evict_lru_page(exclude=bucket):
                raise RuntimeError(
                    f"会话桶数量已达上限（{limit}），且当前没有可回收的页面"
                    "（正在生成回复的会话不会被淘汰）。请稍后重试。"
                )
        page = await self.context.new_page()
        self._pages[bucket] = page
        self._touch_page(bucket)
        target = self._bucket_target(bucket)
        await page.goto(target, wait_until="domcontentloaded")
        await self._wait_ready(page)
        state = self._state(bucket)
        # 只有页面确实停在某个会话上才算“有历史”，否则本轮必须播种
        state.has_history = (
            not state.cap_hit and self._current_session_url(bucket) is not None
        )
        print(f"[会话] 已为 key={bucket} 创建独立会话页面（{target}）")

    async def _ensure_default_page(self, force: bool = False) -> None:
        """确保默认桶（``self.page``）的页面可用；失效则**就地重建**。

        默认桶是所有不参与分桶的客户端的载体，它一旦是一具尸体就**没有任何退路**：
        以前整桥会永久 502（每次请求都瞬间报「找不到输入框」），只能重启进程恢复。
        这里把它当普通桶一样处理：回到状态里保存的会话（没有就回首页），
        并按真实落点重算 ``has_history``（决定本轮是否需要播种）。
        """
        if not force and self._page_is_alive(self.page):
            return
        async with self._page_lock:
            if not force and self._page_is_alive(self.page):
                return
            if self.context is None:
                raise RuntimeError("浏览器尚未初始化，无法重建默认会话页面。")
            old = self.page
            target = self._bucket_target(DEFAULT_SESSION_KEY)
            page = await self.context.new_page()
            await page.goto(target, wait_until="domcontentloaded")
            await self._wait_ready(page)
            self.page = page
            state = self._state(DEFAULT_SESSION_KEY)
            state.has_history = (
                not state.cap_hit
                and self._current_session_url(DEFAULT_SESSION_KEY) is not None
            )
            if old is not None and old is not page:
                try:
                    await old.close()
                except Exception:  # noqa: BLE001 旧页面多半已经死了，关不掉也无所谓
                    pass
            print(f"[会话] 默认会话页面已失效，已重建（{target}）。")

    async def _rebuild_page(self, key: Optional[str], reason: str) -> None:
        """**强制**重建某个桶的页面（不信任 ``is_closed()`` 的判断）。

        用于「页面已经不可用」的恢复：崩溃的渲染进程往往仍报 ``is_closed() == False``，
        却让每一次调用都失败，所以恢复路径必须能无条件换一条新标签——否则同一个桶
        会一直失败下去。会话状态（url / turns）保留，重建后仍回到同一条会话。
        """
        bucket = key or DEFAULT_SESSION_KEY
        if bucket == DEFAULT_SESSION_KEY:
            await self._ensure_default_page(force=True)
            return
        async with self._page_lock:
            await self._close_bucket_page(bucket, reason)
            await self._create_bucket_page(bucket)

    async def _ensure_page(self, key: Optional[str]) -> None:
        """确保某个会话桶有一条**可驱动**的页面（惰性创建 + 失效重建）。

        桶数量达到 ``MAX_SESSION_BUCKETS`` 时**不再直接报错**：先回收空闲页面，
        再按 LRU 淘汰最久未用的页面（**只关页面、状态保留**，下次会自动重开同一会话
        并按需播种）。只有显式把 ``MAX_SESSION_BUCKETS=0`` 设成“不允许额外桶”时才拒绝。

        这里同时也是「页面已经死了」的统一入口：``_pages`` 里登记过的页面**不等于**
        可用的页面，标签被用户关掉 / 渲染进程崩溃后必须重建，否则该会话桶会一直
        瞬间失败（真机根因：表现为随机的 502「无法找到对话输入框」）。
        """
        bucket = key or DEFAULT_SESSION_KEY
        if bucket == DEFAULT_SESSION_KEY:
            await self._ensure_default_page()
            return
        page = self._pages.get(bucket)
        if page is not None and self._page_is_alive(page):
            return
        async with self._page_lock:
            page = self._pages.get(bucket)  # 并发请求可能刚建好 / 刚重建过
            if page is not None and self._page_is_alive(page):
                return
            if page is not None:
                # 页面已失效：先移出池子再重建（状态保留，仍回到同一条会话）
                await self._close_bucket_page(bucket, "页面已失效，重建")
            await self._create_bucket_page(bucket)

    # ---- 默认桶的状态：保留为属性，兼容既有调用与测试 ----
