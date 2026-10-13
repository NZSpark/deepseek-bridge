"""Token 速度估算：只保存计数与时长，不保留提示词或回复正文。"""

from dataclasses import asdict, dataclass

from .prompting import estimate_tokens

_EPSILON_S = 1e-6


@dataclass(slots=True)
class TokenSpeed:
    """单轮生成的可观测速度指标。token 数沿用项目现有估算口径。"""

    prompt_chars: int = 0
    reply_chars: int = 0
    prompt_tokens: int = 0
    reply_tokens: int = 0
    ttft_s: float = 0.0
    decode_s: float = 0.0
    total_s: float = 0.0
    ttft_adj_s: float = 0.0
    decode_adj_s: float = 0.0
    prefill_tps: float = 0.0
    decode_tps: float = 0.0
    settle_credit_s: float = 0.0
    poll_credit_s: float = 0.0
    bridge_overhead_ratio: float | None = None
    saw_generating: bool = False
    streamed: bool = False

    def to_dict(self) -> dict[str, int | float | bool | None]:
        """转换成可安全落盘的数值字典，不含任何对话正文。"""
        return asdict(self)


@dataclass(slots=True, frozen=True)
class SpeedTiming:
    """仅包含一轮测速所需的时间点与采样标志。"""

    submitted_at: float
    first_text_at: float | None
    finished_at: float
    poll_interval_s: float
    settle_credit_s: float = 0.0
    saw_generating: bool = False
    streamed: bool = False


@dataclass(slots=True)
class SpeedAggregate:
    """单个会话桶的滚动平均。只存数值和，不存每次明细、不存正文。"""

    samples: int = 0
    prefill_tps_sum: float = 0.0
    decode_tps_sum: float = 0.0
    ttft_s_sum: float = 0.0
    decode_s_sum: float = 0.0

    def updated(self, speed: TokenSpeed) -> SpeedAggregate:
        """返回累加一轮样本后的新聚合（不改自身，便于纯函数式测试）。"""
        return SpeedAggregate(
            samples=self.samples + 1,
            prefill_tps_sum=self.prefill_tps_sum + speed.prefill_tps,
            decode_tps_sum=self.decode_tps_sum + speed.decode_tps,
            ttft_s_sum=self.ttft_s_sum + speed.ttft_s,
            decode_s_sum=self.decode_s_sum + speed.decode_s,
        )

    @property
    def prefill_tps_avg(self) -> float:
        return self.prefill_tps_sum / self.samples if self.samples else 0.0

    @property
    def decode_tps_avg(self) -> float:
        return self.decode_tps_sum / self.samples if self.samples else 0.0

    @property
    def ttft_s_avg(self) -> float:
        return self.ttft_s_sum / self.samples if self.samples else 0.0

    @property
    def decode_s_avg(self) -> float:
        return self.decode_s_sum / self.samples if self.samples else 0.0


def compute_speed(prompt: str, reply: str, timing: SpeedTiming) -> TokenSpeed:
    """按单调时钟时间戳计算速度；纯函数便于测试，不读取系统状态。

    TTFT 是提交后网页首次呈现非空回复文本的延迟代理，并非模型原生 TTFT。
    只扣除半个轮询周期作为保守修正；稳定轮询修正不代表经过了真实模型时延校准。
    """
    total_s = max(0.0, timing.finished_at - timing.submitted_at)
    has_first_text = timing.first_text_at is not None
    first_at = timing.first_text_at if has_first_text else timing.finished_at
    ttft_s = max(0.0, first_at - timing.submitted_at)
    decode_s = max(0.0, timing.finished_at - first_at) if has_first_text else 0.0

    prompt_tokens = estimate_tokens(prompt)
    reply_tokens = estimate_tokens(reply)
    poll_credit = min(ttft_s, max(0.0, timing.poll_interval_s) / 2)
    settle_credit = min(decode_s, max(0.0, timing.settle_credit_s))
    ttft_adj = max(_EPSILON_S, ttft_s - poll_credit)
    decode_adj = max(_EPSILON_S, decode_s - settle_credit)

    overhead = None
    if total_s > 0:
        overhead = max(0.0, min(1.0, 1.0 - (ttft_adj + decode_adj) / total_s))

    return TokenSpeed(
        prompt_chars=len(prompt),
        reply_chars=len(reply),
        prompt_tokens=prompt_tokens,
        reply_tokens=reply_tokens,
        ttft_s=ttft_s,
        decode_s=decode_s,
        total_s=total_s,
        ttft_adj_s=ttft_adj,
        decode_adj_s=decode_adj,
        prefill_tps=prompt_tokens / ttft_adj,
        decode_tps=reply_tokens / decode_adj if reply_tokens else 0.0,
        settle_credit_s=settle_credit,
        poll_credit_s=poll_credit,
        bridge_overhead_ratio=overhead,
        saw_generating=timing.saw_generating,
        streamed=timing.streamed,
    )
