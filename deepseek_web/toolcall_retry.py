"""坏结构重发循环（设计见 doc/toolcall_retry.md §5）。

与 ``chat_io.send_chat`` 的上游重试阶梯**正交**：那套处理「网页没给出回复」
（超时 / 页面失效 / 会话到顶）；这里处理「网页给了回复，但回复里的 tool_call
结构坏了」。因此本模块**不换会话、不重放历史**——上下文没丢，只需在同一会话里
补一句纯提示词让模型重发。

只对声明了 tools 且 ``tool_choice != none`` 的请求启用；纯文本请求里出现
``tool_call`` 字样是正常讨论，不能重发。
"""

from __future__ import annotations

import asyncio

from . import config
from .errors import MalformedToolCallError
from .malformed import RETRY_PROMPT, has_no_usable_call


async def complete_with_toolcall_retry(  # noqa: PLR0913 扁平签名是刻意的：与 send_chat 对齐，避免多一层 dataclass
    driver,
    prompt: str,
    *,
    seeded_prompt: str | None = None,
    key: str | None = None,
    images: list | None = None,
    on_delta=None,
    wants_tools: bool = False,
    valid_names: set | None = None,
):
    """发一轮并（必要时）重发坏结构，返回 ``(reply_text, code_blocks)``。

    :param driver: 具备 ``send_chat(prompt, on_delta=, seeded_prompt=, key=, images=)``
                   的对象（真实 driver 或测试替身）。
    :param wants_tools: 是否声明了 tools。为假时直接返回首轮结果，不判定、不重发。
    :param valid_names: 合法工具名集合，传给判据做 DSML 短路 / 解析过滤。

    **重试轮的两条硬约束**（设计 §5.1）：
      1. 不带 ``images``：同一批图再粘一次会在会话里出现两份，干扰模型；
      2. 不换会话：仍用同一 ``key``，交给 ``send_chat`` 自己的阶梯处理上游问题。

    重发耗尽仍坏 → 抛 :class:`MalformedToolCallError`（如实失败，不静默吞掉）。
    """
    result = await driver.send_chat(
        prompt,
        on_delta=on_delta,
        seeded_prompt=seeded_prompt,
        key=key,
        images=images,
    )
    text, blocks = result

    if not wants_tools:
        return text, blocks

    retries = 0
    max_retries = max(0, int(getattr(config, "MALFORMED_RETRIES", 0)))
    while has_no_usable_call(text, valid_names) and retries < max_retries:
        retries += 1
        print(f"[重发] 检测到 tool_call 坏结构，第 {retries}/{max_retries} 次重发…")
        backoff = float(getattr(config, "RETRY_BACKOFF_S", 0.0) or 0.0)
        if backoff > 0:
            await asyncio.sleep(backoff * retries)
        # 重试轮：纯提示词、不带 images、不换会话。
        text, blocks = await driver.send_chat(RETRY_PROMPT, key=key)

    if has_no_usable_call(text, valid_names):
        raise MalformedToolCallError(
            "网页端把 tool_call 生成坏了（空围栏 / 截断 / 语法错），"
            f"已重发 {retries} 次仍无可用调用。可稍后重试。"
        )

    return text, blocks
