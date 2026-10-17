"""Token 速度估算：只保存计数与时长，不保留提示词或回复正文。

口径诚实优先（见 doc/tokenspeed.md）：
  * 所有 token 数来自 ``estimate_tokens`` 的字符估算，字段名带 ``_est``；
  * 时间点是网页快照的**观测值**，不是模型原生时间戳；
  * 分母不可用时速度返回 ``None``（不可用），**绝不用 epsilon 硬凑出伪精确值**；
  * 完成判定等待单独记录（``settle_observed_s``），不并入文本增长窗口；
  * 短回复 / 短窗口 / 重写等样本带 ``quality_flags``，聚合时剔除。
"""

from dataclasses import asdict, dataclass, field

from .prompting import estimate_tokens

#: 回复估算 token 低于此值时，输出速度可信度低（标记 short_reply）。
SHORT_REPLY_TOKENS = 50
#: 文本增长窗口短于「一个轮询周期」时，速度不可靠（标记 short_growth_window）。
#: 具体阈值由调用方按实际轮询间隔判断，这里仅作默认下限（秒）。
MIN_GROWTH_WINDOW_S = 0.0


@dataclass(slots=True)
class TokenSpeed:
    """单轮生成的可观测速度指标。

    字段名严格区分「观测时间」与「估算 token」与「派生速度」；不可用一律 ``None``。
    """

    # ---- 计数（估算）----
    prompt_chars: int = 0
    reply_chars: int = 0
    prompt_tokens_est: int = 0
    reply_tokens_est: int = 0
    token_count_source: str = "char_estimate"

    # ---- 观测时长（秒；None = 不可用）----
    page_ttft_observed_s: float | None = None
    page_growth_window_s: float | None = None
    page_generation_to_finish_s: float | None = None
    settle_observed_s: float | None = None
    request_e2e_s: float | None = None

    # ---- 派生速度（估算；None = 不可用）----
    prompt_rate_est_tps: float | None = None
    page_decode_tps_est: float | None = None

    # ---- 流式端到端（仅流式路径；服务端发送口径）----
    stream_first_content_latency_s: float | None = None
    stream_completion_window_s: float | None = None
    stream_effective_output_tps: float | None = None

    # ---- 采样上下文 ----
    poll_interval_observed_s: float | None = None
    saw_generating: bool = False
    streamed: bool = False
    rewritten: bool = False
    #: 稳定、可测试的质量标记（short_reply / short_growth_window / rewritten /
    #: no_generation_signal / token_estimate / invalid_timing）。
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        """转换成可安全落盘的数值字典，不含任何对话正文。"""
        data = asdict(self)
        # 落盘时把 tuple 转成 list，兼容 JSON。
        data["quality_flags"] = list(self.quality_flags)
        return data


@dataclass(slots=True, frozen=True)
class SpeedTiming:
    """仅包含一轮测速所需的时间点与采样标志（都是单调时钟秒）。"""

    submitted_at: float
    first_text_at: float | None
    finished_at: float
    last_change_at: float | None = None  # t7：最后一次观察到正文变化
    poll_interval_s: float = 0.0
    saw_generating: bool = False
    streamed: bool = False
    rewritten: bool = False


@dataclass(slots=True)
class SpeedAggregate:
    """单个会话桶的滚动聚合。只存数值和，不存每次明细、不存正文。

    区分两种口径（见 doc/tokenspeed.md §6.1）：
      * 平均单次速度 = Σ(单次速度) / 样本数，每请求权重相同；
      * 整体聚合吞吐率 = Σ(token) / Σ(时长)，长请求权重更大。
    """

    samples: int = 0
    # 平均单次速度用（速度和）
    prompt_rate_sum: float = 0.0
    decode_rate_sum: float = 0.0
    # 整体吞吐率用（token 总和与时长总和来自同一组有效样本）
    reply_tokens_sum: int = 0
    growth_seconds_sum: float = 0.0
    prompt_tokens_sum: int = 0
    ttft_seconds_sum: float = 0.0

    @property
    def mean_prompt_rate_est_tps(self) -> float | None:
        return self.prompt_rate_sum / self.samples if self.samples else None

    @property
    def mean_decode_tps_est(self) -> float | None:
        return self.decode_rate_sum / self.samples if self.samples else None

    @property
    def aggregate_decode_tps_est(self) -> float | None:
        if self.growth_seconds_sum <= 0:
            return None
        return self.reply_tokens_sum / self.growth_seconds_sum

    @property
    def aggregate_prompt_rate_est_tps(self) -> float | None:
        if self.ttft_seconds_sum <= 0:
            return None
        return self.prompt_tokens_sum / self.ttft_seconds_sum

    @property
    def ttft_s_avg(self) -> float | None:
        return self.ttft_seconds_sum / self.samples if self.samples else None

    @property
    def decode_s_avg(self) -> float | None:
        if self.samples <= 0:
            return None
        return self.growth_seconds_sum / self.samples


#: 有效样本：速度可用、非重写、无致命质量标记（short_* 允许进聚合但打标）。
_INVALID_FOR_SPEED = {"rewritten", "no_generation_signal", "invalid_timing"}


def _speed_usable(speed: TokenSpeed) -> bool:
    """该样本能否进入速度聚合（token 与时长都有效、未被判定为不可信）。"""
    if speed.page_decode_tps_est is None:
        return False
    if any(flag in _INVALID_FOR_SPEED for flag in speed.quality_flags):
        return False
    return True


def aggregate_speed(agg: SpeedAggregate, speed: TokenSpeed) -> SpeedAggregate:
    """把一轮样本并入聚合，返回新对象（纯函数，便于测试）。

    只有有效样本才进入；无效样本不改变聚合（调用方可另外计数）。
    """
    if not _speed_usable(speed):
        return SpeedAggregate(**{f: getattr(agg, f) for f in agg.__dataclass_fields__})
    samples = agg.samples + 1
    return SpeedAggregate(
        samples=samples,
        prompt_rate_sum=agg.prompt_rate_sum + (speed.prompt_rate_est_tps or 0.0),
        decode_rate_sum=agg.decode_rate_sum + (speed.page_decode_tps_est or 0.0),
        reply_tokens_sum=agg.reply_tokens_sum + speed.reply_tokens_est,
        growth_seconds_sum=agg.growth_seconds_sum + (speed.page_growth_window_s or 0.0),
        prompt_tokens_sum=agg.prompt_tokens_sum + speed.prompt_tokens_est,
        ttft_seconds_sum=agg.ttft_seconds_sum + (speed.page_ttft_observed_s or 0.0),
    )


def apply_stream_timing(
    speed: TokenSpeed,
    *,
    started_at: float | None,
    first_content_at: float | None,
    done_at: float | None,
) -> TokenSpeed:
    """把流式路径的服务端发送时间点并入单轮指标（纯函数）。

    ``t10`` = 首个含正文的 SSE chunk 实际生成时刻；``t11`` = ``[DONE]`` 实际发出时刻；
    ``t0`` = 进入流式处理的时刻。任一缺失则对应指标保持 ``None``，不做替代估计。
    """
    first_latency = None
    window = None
    effective = None
    e2e = None
    if started_at is not None and first_content_at is not None:
        delta = first_content_at - started_at
        if delta == delta and delta >= 0:
            first_latency = delta
    if first_content_at is not None and done_at is not None:
        delta = done_at - first_content_at
        if delta == delta and delta >= 0:
            window = delta
            if speed.reply_tokens_est > 0 and window > 0:
                effective = speed.reply_tokens_est / window
    if started_at is not None and done_at is not None:
        delta = done_at - started_at
        if delta == delta and delta >= 0:
            e2e = delta
    return TokenSpeed(
        **{
            **{f: getattr(speed, f) for f in speed.__dataclass_fields__},
            "stream_first_content_latency_s": first_latency,
            "stream_completion_window_s": window,
            "stream_effective_output_tps": effective,
            "request_e2e_s": e2e if e2e is not None else speed.request_e2e_s,
        }
    )


def compute_speed(prompt: str, reply: str, timing: SpeedTiming) -> TokenSpeed:
    """按单调时钟时间戳计算速度；纯函数，不读取系统状态。

    口径（对齐 doc/tokenspeed.md）：
      * 页面首字延迟 = t6 - t4（提交到首次观察到非空正文），观测代理，非模型 TTFT；
      * 文本增长窗口 = t7 - t6（首次正文到最后一次变化），不含完成判定等待；
      * 完成判定等待 = t8 - t7，单独记录；
      * 输入速率 = 输入 token / 首字延迟（分母含网页等待，非真实 Prefill TPS）；
      * 输出速度 = 输出 token / 增长窗口（快照观测，非真实 Decode TPS）；
      * 分母不可用 / 过短时速度返回 None，并打质量标记，绝不用 epsilon 硬凑。
    """
    flags: list[str] = ["token_estimate"]

    prompt_tokens = estimate_tokens(prompt)
    reply_tokens = estimate_tokens(reply)

    # ---- 时间点校验 ----
    def _valid(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        delta = a - b
        if not (delta == delta) or delta < 0:  # NaN 或逆序
            return None
        return delta

    ttft = _valid(timing.first_text_at, timing.submitted_at)
    growth = _valid(timing.last_change_at, timing.first_text_at)
    settle = _valid(timing.finished_at, timing.last_change_at)
    gen_to_finish = _valid(timing.finished_at, timing.first_text_at)
    e2e = _valid(timing.finished_at, timing.submitted_at)

    if timing.first_text_at is None:
        flags.append("no_generation_signal")
    if ttft is None and timing.first_text_at is not None:
        flags.append("invalid_timing")
    if growth is None and timing.last_change_at is not None:
        flags.append("invalid_timing")
    if timing.rewritten:
        flags.append("rewritten")

    # ---- 输入速率（分母含网页等待，非真实 Prefill TPS）----
    prompt_rate = None
    if ttft is not None and ttft > 0 and prompt_tokens > 0:
        prompt_rate = prompt_tokens / ttft

    # ---- 输出速度（快照观测窗口，非真实 Decode TPS）----
    decode_rate = None
    if growth is not None and growth > 0 and reply_tokens > 0:
        # 增长窗口短于一个轮询周期：速度快照不可信，打标并置为不可用。
        if timing.poll_interval_s > 0 and growth < timing.poll_interval_s:
            flags.append("short_growth_window")
        else:
            decode_rate = reply_tokens / growth
    if reply_tokens and reply_tokens < SHORT_REPLY_TOKENS:
        flags.append("short_reply")

    return TokenSpeed(
        prompt_chars=len(prompt),
        reply_chars=len(reply),
        prompt_tokens_est=prompt_tokens,
        reply_tokens_est=reply_tokens,
        page_ttft_observed_s=ttft,
        page_growth_window_s=growth,
        page_generation_to_finish_s=gen_to_finish,
        settle_observed_s=settle,
        request_e2e_s=e2e,
        prompt_rate_est_tps=prompt_rate,
        page_decode_tps_est=decode_rate,
        poll_interval_observed_s=timing.poll_interval_s,
        saw_generating=timing.saw_generating,
        streamed=timing.streamed,
        rewritten=timing.rewritten,
        quality_flags=tuple(dict.fromkeys(flags)),
    )
