"""会话状态与持久化（从 driver.py 拆出，作为 mixin 混入 DeepSeekWebDriver）。

包含 ``SessionState`` 数据类、按桶读写状态文件、默认桶的属性代理、
以及轮转 / 恢复等会话生命周期操作。依赖宿主提供 ``page`` / ``_sessions`` /
``_page_for`` / ``_wait_ready`` / ``_current_session_url`` / ``_last_prompts``。
"""

import contextlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from . import config
from .errors import DEFAULT_SESSION_KEY, HOME_URL
from .token_speed import SpeedAggregate


@dataclass
class SessionState:
    """单个会话桶的状态。会话的“是否新开 / 能否复用”都由它决定。

    除会话生命周期外，还承载 ``/bridge`` 命令的按桶状态：

    * ``linked_url``：``/bridge session link`` 绑定的网页会话（页面（重）建、启动恢复
      都优先落到它上面）；
    * ``pending_reseed``：``/bridge session reseed`` 排队的一次性标记——下一轮把完整
      历史再播种一遍（落盘，重启 / 并发都不会丢，发送成功后清除）；
    """

    url: str | None = None
    linked_url: str | None = None
    has_history: bool = False
    turns: int = 0
    est_tokens: int = 0
    cap_hit: bool = False
    pending_rotation: bool = False
    pending_reseed: bool = False
    last_error: str | None = None
    updated_at: int = 0
    # ---- token 速度统计（仅数值和，不含正文；见 deepseek_web/token_speed.py）----
    # ``speed_samples`` / ``speed_*_sum`` 是 /bridge token 的桶平均值来源；
    # ``last_speed`` 保存最近一轮的完整指标（同样是纯数值）。
    speed_samples: int = 0
    # 平均单次速度用（速度和）
    speed_prompt_rate_sum: float = 0.0
    speed_decode_rate_sum: float = 0.0
    # 整体聚合吞吐率用（token 总和与时长总和来自同一组有效样本）
    speed_reply_tokens_sum: int = 0
    speed_growth_seconds_sum: float = 0.0
    speed_prompt_tokens_sum: int = 0
    speed_ttft_s_sum: float = 0.0
    last_speed: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SessionState":
        state = cls()
        if not isinstance(payload, dict):
            return state
        url = payload.get("url")
        if isinstance(url, str):
            state.url = url
        linked = payload.get("linked_url")
        if isinstance(linked, str) and linked:
            state.linked_url = linked
        for name in ("has_history", "cap_hit", "pending_rotation", "pending_reseed"):
            if name in payload:
                setattr(state, name, bool(payload.get(name)))
        for name in (
            "turns", "est_tokens", "updated_at", "speed_samples",
            "speed_reply_tokens_sum", "speed_prompt_tokens_sum",
        ):
            try:
                setattr(state, name, int(payload.get(name) or 0))
            except (TypeError, ValueError):
                setattr(state, name, 0)
        for name in (
            "speed_prompt_rate_sum",
            "speed_decode_rate_sum",
            "speed_growth_seconds_sum",
            "speed_ttft_s_sum",
        ):
            try:
                setattr(state, name, float(payload.get(name) or 0.0))
            except (TypeError, ValueError):
                setattr(state, name, 0.0)
        last_error = payload.get("last_error")
        state.last_error = last_error if isinstance(last_error, str) else None
        last_speed = payload.get("last_speed")
        state.last_speed = last_speed if isinstance(last_speed, dict) else None
        return state


class SessionStoreMixin:
    """会话桶状态的读写、属性代理与轮转 / 恢复。"""

    def _state(self, key: str | None = None) -> SessionState:
        """取出某个会话桶的状态；首次访问时从磁盘恢复。"""
        bucket = key or DEFAULT_SESSION_KEY
        state = self._sessions.get(bucket)
        if state is None:
            state = SessionState.from_payload(self._load_session_state(bucket))
            self._sessions[bucket] = state
        return state

    def sent_prompt(self, key: str | None = None) -> str | None:
        """某个会话桶最近一次真正发给网页版的 prompt（可能因轮转由增量改选播种版）。"""
        return self._last_prompts.get(key or DEFAULT_SESSION_KEY)

    @property
    def session_has_history(self) -> bool:
        return self._state().has_history

    @session_has_history.setter
    def session_has_history(self, value: bool) -> None:
        self._state().has_history = bool(value)

    @property
    def session_turns(self) -> int:
        return self._state().turns

    @session_turns.setter
    def session_turns(self, value: int) -> None:
        self._state().turns = int(value)

    @property
    def session_est_tokens(self) -> int:
        return self._state().est_tokens

    @session_est_tokens.setter
    def session_est_tokens(self, value: int) -> None:
        self._state().est_tokens = int(value)

    @property
    def session_cap_hit(self) -> bool:
        return self._state().cap_hit

    @session_cap_hit.setter
    def session_cap_hit(self, value: bool) -> None:
        self._state().cap_hit = bool(value)

    @property
    def last_error(self) -> str | None:
        return self._state().last_error

    @last_error.setter
    def last_error(self, value: str | None) -> None:
        self._state().last_error = value

    @property
    def _pending_rotation(self) -> bool:
        return self._state().pending_rotation

    @_pending_rotation.setter
    def _pending_rotation(self, value: bool) -> None:
        self._state().pending_rotation = bool(value)

    def _current_session_url(self, key: str | None = None) -> str | None:
        """某个会话桶的页面若处于会话中，返回其规范化会话地址。"""
        page = self._page_for(key)
        try:
            url = page.url if page else ""
        except Exception:  # noqa: BLE001 页面随时可能被关掉；这里只求「拿不到就当空」，
            # 且刻意不 import playwright 的异常类（测试替身 / 版本差异都要能命中，见 page_pool.py）
            url = ""
        match = config.SESSION_URL_RE.search(url or "")
        return match.group(0) if match else None

    # ---------- 会话状态（url + 轮数 / 体积 / 是否到顶）----------
    def _read_state_file(self) -> dict[str, Any]:
        """读取原始状态文件（解析失败或非 JSON 时返回空字典）。"""
        try:
            if not config.SESSION_FILE.exists():
                return {}
            raw = config.SESSION_FILE.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return {}
        if not raw.startswith("{"):
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _load_session_state(self, key: str | None = None) -> dict[str, Any]:
        """读取某个会话桶的状态。

        兼容两种旧格式：文件内容是单独的会话 URL 纯文本、或顶层直接放单会话对象。
        """
        bucket = key or DEFAULT_SESSION_KEY
        raw = ""
        try:
            if config.SESSION_FILE.exists():
                raw = config.SESSION_FILE.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return {}
        if not raw:
            return {}
        if not raw.startswith("{"):
            # 旧格式：单独的会话 URL（只可能对应默认桶）
            if bucket == DEFAULT_SESSION_KEY and config.SESSION_URL_RE.fullmatch(raw):
                return {"url": raw}
            return {}
        data = self._read_state_file()
        if bucket == DEFAULT_SESSION_KEY:
            # 历史格式：默认桶的状态字段直接放在文件顶层
            return {k: v for k, v in data.items() if k not in ("sessions", "version")}
        extra = data.get("sessions")
        own = extra.get(bucket) if isinstance(extra, dict) else None
        return own if isinstance(own, dict) else {}

    def _save_session_state(self, clear_url: bool = False, key: str | None = None) -> None:
        """落盘某个会话桶的状态，供后续恢复 / 轮转决策使用。

        文件格式（同时兼容历史格式）：

        * **默认桶**的状态字段直接放在顶层（与历史文件完全一致）；
        * 其余桶放在 ``sessions`` 下，各任务互不影响。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        # 优先取页面当前地址；页面不存在或停在首页时，保留状态里已知的 url，不覆盖
        url = None if clear_url else (self._current_session_url(bucket) or state.url)
        state.url = url
        state.updated_at = int(time.time())
        payload = state.to_payload()

        data = self._read_state_file()
        extra = data.get("sessions")
        extra = dict(extra) if isinstance(extra, dict) else {}
        if bucket == DEFAULT_SESSION_KEY:
            payload["sessions"] = extra
        else:
            extra[bucket] = payload
            # 顶层的默认桶状态原样保留（缺 url 时补 null，便于直接查看默认桶）
            data = {k: v for k, v in data.items() if k != "sessions"}
            data.setdefault("url", None)
            data["sessions"] = extra
            payload = data
        with contextlib.suppress(OSError):
            config.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.SESSION_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def _saved_session_url(self, key: str | None = None) -> str | None:
        """读取已保存的会话地址（若存在且合法）。

        ``/bridge session link`` 绑定过时优先返回绑定地址：绑定就是「这条桶必须回到
        这条会话」的意思，页面丢失重建、启动恢复都不得漂到别的会话去。
        """
        data = self._load_session_state(key)
        linked = data.get("linked_url")
        if isinstance(linked, str) and config.SESSION_URL_RE.fullmatch(linked):
            return linked
        url = data.get("url")
        if isinstance(url, str) and config.SESSION_URL_RE.fullmatch(url):
            return url
        return None

    async def _remember_session(self, key: str | None = None) -> None:
        """刷新落盘的会话状态（保留此名字，兼容既有调用）。"""
        self._save_session_state(key=key)

    def session_keys(self) -> list[str]:
        """当前在用的会话桶（至少包含默认桶）。"""
        return sorted({DEFAULT_SESSION_KEY, *self._sessions})

    def needs_seed(self, key: str | None = None) -> bool:
        """某个会话桶的网页会话里没有可用上下文时，需要把完整历史播种进去。

        两种情形都要播种：网页会话本来就没有历史（新开 / 轮转后），或用户用
        ``/bridge session reseed`` 明确要求下一轮把完整历史再发一遍（一次性标记）。
        """
        state = self._state(key)
        return state.pending_reseed or not state.has_history

    def note_speed(self, speed: dict[str, Any], key: str | None = None) -> None:
        """把一轮成功生成的测速指标累加进该桶，并落盘。

        只累加**数值**（sum 与样本数）与最近一轮的数值快照，不含 prompt / reply 正文。
        取消 / 失败轮不调用本方法，因此不会计入。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        # 有效样本判定与 token_speed._speed_usable 一致：速度可用、非重写、无致命标记。
        flags = set(speed.get("quality_flags") or ())
        invalid = {"rewritten", "no_generation_signal", "invalid_timing"}
        usable = (
            speed.get("page_decode_tps_est") is not None and not (flags & invalid)
        )
        if usable:
            state.speed_samples += 1
            state.speed_prompt_rate_sum += float(speed.get("prompt_rate_est_tps") or 0.0)
            state.speed_decode_rate_sum += float(speed.get("page_decode_tps_est") or 0.0)
            state.speed_reply_tokens_sum += int(speed.get("reply_tokens_est") or 0)
            state.speed_growth_seconds_sum += float(speed.get("page_growth_window_s") or 0.0)
            state.speed_prompt_tokens_sum += int(speed.get("prompt_tokens_est") or 0)
            state.speed_ttft_s_sum += float(speed.get("page_ttft_observed_s") or 0.0)
        state.last_speed = dict(speed)
        self._save_session_state(key=bucket)

    def patch_last_speed(self, updates: dict[str, Any], key: str | None = None) -> None:
        """把额外的数值字段并入最近一轮 speed 快照并落盘（不新增样本）。

        用于流式路径：生成完成后才知道 ``t10``/``t11``，此时把流式端到端指标
        补进**同一轮**的 ``last_speed``，而不是再计一个样本。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        if not isinstance(state.last_speed, dict):
            return
        merged = dict(state.last_speed)
        merged.update(updates)
        state.last_speed = merged
        self._save_session_state(key=bucket)

    def speed_stats(self, key: str | None = None) -> dict[str, Any]:
        """该桶的测速汇总：本轮（最近一次）与桶平均。只读，不导航页面。"""
        state = self._state(key)
        # 复用纯函数的平均口径，避免 store 与 token_speed 各写一份除式而悄悄漂移。
        agg = SpeedAggregate(
            samples=state.speed_samples,
            prompt_rate_sum=state.speed_prompt_rate_sum,
            decode_rate_sum=state.speed_decode_rate_sum,
            reply_tokens_sum=state.speed_reply_tokens_sum,
            growth_seconds_sum=state.speed_growth_seconds_sum,
            prompt_tokens_sum=state.speed_prompt_tokens_sum,
            ttft_seconds_sum=state.speed_ttft_s_sum,
        )
        return {
            "samples": agg.samples,
            "last": state.last_speed,
            # 平均单次速度（每请求等权）
            "mean_prompt_rate_est_tps": agg.mean_prompt_rate_est_tps,
            "mean_decode_tps_est": agg.mean_decode_tps_est,
            # 整体聚合吞吐率（长请求加权）
            "aggregate_decode_tps_est": agg.aggregate_decode_tps_est,
            "aggregate_prompt_rate_est_tps": agg.aggregate_prompt_rate_est_tps,
            "ttft_s_avg": agg.ttft_s_avg,
            "decode_s_avg": agg.decode_s_avg,
        }

    def session_stats(self, key: str | None = None) -> dict[str, Any]:
        """供 /healthz 与 ``/bridge`` 命令观察会话增长情况。"""
        state = self._state(key)
        return {
            "url": self._current_session_url(key),
            "has_history": state.has_history,
            "needs_seed": self.needs_seed(key),
            "turns": state.turns,
            "est_tokens": state.est_tokens,
            "cap_hit": state.cap_hit,
            "pending_rotation": state.pending_rotation,
            "pending_reseed": state.pending_reseed,
            "linked_url": state.linked_url,
            "last_error": state.last_error,
            "speed": self.speed_stats(key),
            "buckets": self.session_keys(),
        }

    # ---------- /bridge 命令用到的按桶状态 ----------
    def linked_session_url(self, key: str | None = None) -> str | None:
        """该桶通过 ``/bridge session link`` 绑定的网页会话（未绑定 = None）。"""
        return self._state(key).linked_url

    def reseed_session(self, key: str | None = None) -> None:
        """排队「下一轮重新播种完整历史」：一次性标记，落盘后可跨重启 / 并发。"""
        bucket = key or DEFAULT_SESSION_KEY
        self._state(bucket).pending_reseed = True
        self._save_session_state(key=bucket)
        print(f"[会话] 已排队 key={bucket} 的重新播种，下一轮将重发完整历史。")

    def _session_over_budget(self, key: str | None = None) -> bool:
        """会话体积是否已达到轮转阈值（0 表示禁用该维度）。"""
        state = self._state(key)
        if config.SESSION_MAX_TURNS and state.turns >= config.SESSION_MAX_TURNS:
            return True
        return bool(
            config.SESSION_MAX_TOKENS and state.est_tokens >= config.SESSION_MAX_TOKENS
        )

    async def _start_new_session(self, key: str | None = None) -> None:
        """轮转到新会话，并重置会话状态（调用方必须使用“播种”prompt）。"""
        page = self._page_for(key)
        if page is None:
            return
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        await self._wait_ready(page)
        state = self._state(key)
        state.has_history = False
        state.turns = 0
        state.est_tokens = 0
        state.cap_hit = False
        state.pending_rotation = False
        state.pending_reseed = False
        state.last_error = None
        if state.linked_url:
            # 轮转 = 换一条全新的网页会话；绑定的旧会话（多半已到顶 / 已失效）留着
            # 只会让下一次页面重建又漂回去，因此明确解除并告知。
            state.linked_url = None
            print("[轮转] 会话绑定已解除（轮转需要全新的网页会话）。")
        self._save_session_state(clear_url=True, key=key)
        print("[轮转] 已开启新的网页会话（本轮会用完整历史播种上下文）。")

    def reset_session(self, key: str | None = None) -> None:
        """把某个会话桶标记为“下一轮开新会话”（手动逃生口）。

        只改状态、不碰页面：下一轮的 ``send_chat`` 会先轮转，并用“播种”
        prompt 重放历史，所以不会丢上下文。
        """
        bucket = key or DEFAULT_SESSION_KEY
        state = self._state(bucket)
        state.pending_rotation = True
        state.cap_hit = False
        state.has_history = False
        state.pending_reseed = False
        # 重置的语义就是「开一条全新的网页会话」，与「绑定到某条指定会话」互斥
        state.linked_url = None
        self._save_session_state(clear_url=True, key=bucket)
        print(f"[会话] 已请求重置 key={bucket} 的会话，下一轮将开启新会话并播种上下文。")
