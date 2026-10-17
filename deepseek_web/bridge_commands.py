"""Bridge 聊天命令（``/bridge ...``）：在对话里查询桥接状态、管理当前会话桶。

设计约定参考姊妹项目 ChatGPTBridge 的 ``doc/bridge_command.md``，本项目落到
``doc/bridge_commands.md``。要点：

* **精确匹配**：只有「整条消息就是那一行命令」才执行——历史消息里的旧命令不重放，
  正文 / 多行文本 / 代码块里出现的命令字样一律当普通提问（见 :func:`parse_command`）。
  只检查**最后一条**消息，并且它必须是 ``user``：工具循环里后续请求带着同一批历史时
  不会再执行一遍（见 :func:`latest_user_text`）。
* **只作用于当前会话桶**：命令**没有**「指定别的桶」的参数；桶由 ``X-DeepSeek-Session`` /
  ``user`` / 工作目录 / User-Agent 决定（见 ``server._session_key``），所以多用户 /
  多 Agent 场景不会互相影响。
* **本地应答**：命令由桥直接回复、**不发给 DeepSeek 网页版**，因此页面失效、浏览器
  不可用时这些命令仍然可用（这正是排障最需要它们的时候；路由层的可用性放行见
  :func:`is_command`）。
* **不泄露**：回复只含状态摘要，不打印 ``.env`` 值、登录 Cookie、令牌或其它桶的信息。
* **改状态要讲清范围**：``session reset`` / ``reseed`` / ``link`` 都会在回执里写明
  「只影响当前桶」「不是删除客户端历史」「下一轮会发生什么」。

与 ChatGPTBridge 的差异（本项目没有的网页端能力不硬造）：

* 不提供 ``settings think``：DeepSeek 网页版没有可可靠读写的思考模式开关；
* 不提供 ``/link`` / ``/unlink`` 兼容别名：本项目原先没有这两个命令，统一走
  ``session link`` / ``session unlink``；

两条协议路径（Chat Completions 与 Responses）都调用 :func:`handle_command`，
避免行为分叉。
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import config, models
from .errors import DEFAULT_SESSION_KEY
from .prompting import _content_to_text, estimate_tokens

logger = logging.getLogger(__name__)

#: 命令命名空间。只识别这一个前缀；其它 ``/`` 开头的文本照旧交给网页版。
BRIDGE_COMMAND = "/bridge"
#: 「按播种方式绑定」的开关。
SEED_FLAG = "--seed"
#: ``/bridge help`` 支持的主题。
HELP_TOPICS = ("session",)
#: ``/bridge session`` 的子命令。
SESSION_SUBCOMMANDS = ("status", "reset", "reseed", "link", "unlink")
#: 回复 token 少于这个数时，测速误差大，``/bridge token`` 会加一句提示。
SHORT_REPLY_TOKENS = 50

#: 会话路径：``https://chat.deepseek.com/a/chat/s/<会话 ID>``。
_SESSION_PATH_RE = re.compile(r"^/a/chat/s/([0-9a-fA-F-]{8,})$")

_USAGE = (
    f"{BRIDGE_COMMAND} 用法：\n"
    f"  {BRIDGE_COMMAND} help [{'|'.join(HELP_TOPICS)}]        查看命令列表 / 分主题帮助\n"
    f"  {BRIDGE_COMMAND} status                       桥接与当前会话桶状态摘要\n"
    f"  {BRIDGE_COMMAND} token                        最近一轮与本桶平均的 token 速度\n"
    f"  {BRIDGE_COMMAND} models                       本桥对外公布的模型名（同 /v1/models）\n"
    f"  {BRIDGE_COMMAND} session [status]             当前桶的会话状态摘要\n"
    f"  {BRIDGE_COMMAND} session reset                下一轮开新网页会话并播种完整历史\n"
    f"  {BRIDGE_COMMAND} session reseed               下一轮把完整历史再播种一遍（一次性）\n"
    f"  {BRIDGE_COMMAND} session link <URL> [{SEED_FLAG}]   把当前桶绑定到指定网页会话\n"
    f"  {BRIDGE_COMMAND} session unlink               解除当前桶的会话绑定\n"
)


@dataclass(frozen=True)
class BridgeCommand:
    """一条已被识别的 ``/bridge`` 命令。"""

    action: str  # "help" | "status" | "models" | "session" | "invalid"
    subaction: str = ""  # help 主题 / session 子命令
    target: str = ""  # session link 的会话地址（原样，执行时再校验归一化）
    seed: bool = False  # session link 是否带 --seed
    reason: str = ""  # 命令未执行的原因（invalid 时展示给用户）


# ==================== 命令清单（帮助的唯一来源）====================


@dataclass(frozen=True)
class CommandSpec:
    """帮助里的一个条目。只列当前版本**真正注册**的命令，避免帮助与实现漂移。"""

    syntax: str
    summary: str
    group: str  # "" = 顶层；"session" / "settings" = 分主题
    readonly: bool  # True = 只读；False = 会改变会话桶状态


REGISTRY: tuple = (
    CommandSpec(f"{BRIDGE_COMMAND} help [{'|'.join(HELP_TOPICS)}]",
                "查看命令列表 / 分主题帮助", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} status",
                "桥接、浏览器与当前会话桶的状态摘要", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} models",
                "本桥对外公布的模型名（与 GET /v1/models 同源）", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} token",
                "最近一轮与本桶平均的 token 速度（prefill/decode）", "", True),
    CommandSpec(f"{BRIDGE_COMMAND} session [status]",
                "当前桶的会话状态摘要", "session", True),
    CommandSpec(f"{BRIDGE_COMMAND} session reset",
                "下一轮开新网页会话，并把客户端历史播种进去", "session", False),
    CommandSpec(f"{BRIDGE_COMMAND} session reseed",
                "下一轮把完整历史再播种一遍（一次性标记，落盘）", "session", False),
    CommandSpec(f"{BRIDGE_COMMAND} session link <URL> [{SEED_FLAG}]",
                "把当前桶绑定到指定网页会话（--seed 表示该会话里没有此前记录）", "session", False),
    CommandSpec(f"{BRIDGE_COMMAND} session unlink",
                "解除当前桶的会话绑定", "session", False),
)


# ==================== 解析 ====================


def parse_command(text: str) -> BridgeCommand | None:
    """整条消息是否是一条 ``/bridge`` 命令；不是则返回 ``None``（照常发给网页版）。

    只有「整条消息就是这一行」才算命令：混在正文里、出现在多行文本里、或出现在历史
    消息里都当作普通提问交给模型。保留命名空间内**语法已匹配但参数不合法**的写法
    返回 ``invalid``（带用法），因为 ``/bridge`` 是桥自己保留的前缀，回一条用法提示
    比把它发给网页版更可解释；其它任何 ``/`` 开头的文本一律不是命令。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    tokens = lines[0].split()
    if not tokens or tokens[0].lower() != BRIDGE_COMMAND:
        return None

    tail = tokens[1:]
    if not tail:
        # 裸 `/bridge` = 命令发现入口（帮助），而不是「未知命令」
        return BridgeCommand("help")
    head, rest = tail[0].lower(), tail[1:]

    if head == "help":
        if len(rest) > 1:
            return _invalid(f"{BRIDGE_COMMAND} help 最多接受一个主题。")
        topic = rest[0].lower() if rest else ""
        if topic and topic not in HELP_TOPICS:
            listed = "、".join(HELP_TOPICS)
            return _invalid(f"没有这个帮助主题：{rest[0]}（可用主题：{listed}）。")
        return BridgeCommand("help", subaction=topic)
    if head == "status":
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} status 不接受参数。")
        return BridgeCommand("status")
    if head == "models":
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} models 不接受参数。")
        return BridgeCommand("models")
    if head == "token":
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} token 不接受参数。")
        return BridgeCommand("token")
    if head == "session":
        return _parse_session(rest)
    return _invalid(f"未知子命令：{tail[0]}。")


def _invalid(reason: str) -> BridgeCommand:
    return BridgeCommand("invalid", reason=f"{reason}\n{_USAGE}")


def _parse_session(rest: list[str]) -> BridgeCommand:
    if not rest:
        return BridgeCommand("session", subaction="status")
    sub, args = rest[0].lower(), rest[1:]
    if sub not in SESSION_SUBCOMMANDS:
        listed = "、".join(SESSION_SUBCOMMANDS)
        return _invalid(f"session 没有这个子命令：{rest[0]}（可用：{listed}）。")
    if sub in ("status", "reset", "reseed", "unlink"):
        if args:
            return _invalid(f"{BRIDGE_COMMAND} session {sub} 不接受参数。")
        return BridgeCommand("session", subaction=sub)
    # session link <URL> [--seed]：选项个数与地址都单独校验，不做猜测式执行
    seed = False
    positional: list[str] = []
    for token in args:
        if token.lower() == SEED_FLAG:
            seed = True
        elif token.startswith("--"):
            return _invalid(f"session link 不认识选项 {token}（只支持 {SEED_FLAG}）。")
        else:
            positional.append(token)
    if not positional:
        return _invalid("session link 还需要一个会话地址（或会话 ID）。")
    if len(positional) > 1:
        return _invalid(f"会话地址里不能有空格：收到了 {len(positional)} 段参数。")
    return BridgeCommand("session", subaction="link", target=positional[0], seed=seed)


def latest_user_text(messages: Sequence[Any] | None) -> str | None:
    """最后一条消息的文本，仅当它是 ``user`` 消息时返回（否则 ``None``）。

    只看最后一条：历史里的旧命令是**已经执行过**的会话内容，不能被重放执行——
    工具循环的后续请求会把同一条 user 命令再带一遍，若按「最后一条 user 消息」找，
    那个命令会每一轮都被重新执行。
    """
    if not messages:
        return None
    last = messages[-1]
    role = last.get("role") if isinstance(last, dict) else getattr(last, "role", None)
    if role != "user":
        return None
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
    return _content_to_text(content)


def is_command(messages: Sequence[Any] | None) -> bool:
    """这批消息的最后一条是否会由桥自己应答（``/bridge ...``）。

    路由层用它把「桥内命令」从「需要浏览器才能处理的普通提问」里分出来：浏览器尚未
    初始化时命令仍应答得出来（这正是用户排障时最需要它们的时候）。
    """
    text = latest_user_text(messages)
    return parse_command(text) is not None


# ==================== 会话地址校验 ====================


def normalize_session_url(raw: str) -> str | None:
    """把用户给出的会话地址（或裸会话 ID）归一化成合法会话 URL；不合法返回 ``None``。

    只接受：``https`` + 主机在 ``config.LINK_ALLOWED_HOSTS`` 里 + 路径是
    ``/a/chat/s/<会话 ID>``。裸 ID 会被补成标准地址（用户从地址栏复制的往往只是那一段）。
    校验失败时绝不「猜一个」目标导航过去。
    """
    text = (raw or "").strip().strip("`\"'<>")
    if not text:
        return None
    if "://" not in text:
        text = f"https://chat.deepseek.com/a/chat/s/{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme.lower() != "https":
        return None
    host = (parts.hostname or "").lower()
    if host not in (config.LINK_ALLOWED_HOSTS or []):
        return None
    if not _SESSION_PATH_RE.fullmatch(parts.path or ""):
        return None
    return f"https://{host}{parts.path}"


# ==================== 执行 ====================


async def handle_command(
    messages: Sequence[Any] | None, driver, session_key: str | None = None
) -> str | None:
    """识别并执行聊天命令，返回桥自己给出的应答；不是命令时返回 ``None``。

    调用方拿到应答后直接回给客户端，**不会**把这条消息发给网页版（因此命令在页面
    失效 / 浏览器未初始化时依然可用）。
    """
    text = latest_user_text(messages)
    if text is None:
        return None
    command = parse_command(text)
    if command is None:
        return None

    bucket = session_key or DEFAULT_SESSION_KEY
    try:
        return await _execute(command, driver, bucket)
    except Exception as exc:
        # 失败要可解释，但不把内部堆栈 / 敏感信息发给客户端（细节留在日志里）
        logger.warning("[命令] 执行 %s 失败：%r", BRIDGE_COMMAND, exc, exc_info=True)
        return f"[Bridge] 命令未执行：内部错误（{type(exc).__name__}）。请查看服务日志。"
    finally:
        logger.info("[命令] bucket=%s 已处理 %s", bucket, text.split("\n")[0][:120])


async def _execute(command: BridgeCommand, driver, bucket: str) -> str:
    if command.action == "help":
        return _help_reply(command.subaction)
    if command.action == "status":
        return _status_reply(driver, bucket)
    if command.action == "models":
        return _models_reply()
    if command.action == "token":
        return _token_reply(driver, bucket)
    if command.action == "invalid":
        return f"[Bridge] 命令未执行：{command.reason}"
    if command.action == "session":
        return await _session_reply(command, driver, bucket)
    return f"[Bridge] 命令未执行：未知命令。\n{_USAGE}"  # 防御：注册表与执行分支必须同步


def command_completion_response(model: str, reply: str) -> models.ChatCompletionResponse:
    """把命令应答包装成一条普通的 assistant 回复（非流式）。

    usage 里 ``prompt_tokens=0``：这条回复**没有**上游 prompt，不能算成网页版用量。
    """
    return models.ChatCompletionResponse(
        model=model,
        choices=[models.Choice(
            index=0,
            message=models.ChoiceMessage(role="assistant", content=reply),
            finish_reason="stop",
        )],
        usage=models.Usage(
            prompt_tokens=0,
            completion_tokens=estimate_tokens(reply),
            total_tokens=estimate_tokens(reply),
        ),
    )


def _help_reply(topic: str) -> str:
    """分主题帮助：只列当前版本真正注册的命令，并标明是否只读。"""
    lines = [f"[Bridge] {BRIDGE_COMMAND} 命令列表（当前版本）："]
    for spec in REGISTRY:
        if topic and spec.group != topic:
            continue
        mark = "只读" if spec.readonly else "改状态"
        lines.append(f"  {spec.syntax}\n      {spec.summary}（{mark}）")
    if not topic:
        lines.append(
            "  分主题帮助：" + "、".join(f"{BRIDGE_COMMAND} help {item}" for item in HELP_TOPICS)
        )
    lines.append("只读命令不导航网页、不触发生成；改状态的命令只影响当前会话桶。")
    lines.append(
        f"会话地址形如 https://chat.deepseek.com/a/chat/s/<会话 ID>；"
        f"也可以只发会话 ID（{BRIDGE_COMMAND} session link <ID>）。"
    )
    return "\n".join(lines)


def _models_reply() -> str:
    """本桥对外公布的模型名：与 ``GET /v1/models`` 用同一份数据（models.advertised_models）。"""
    ids = [card["id"] for card in models.advertised_models()]
    lines = ["[Bridge] 本桥对外公布的模型（与 GET /v1/models 同源）："]
    lines.extend(f"  {model_id}" for model_id in ids)
    lines.append(
        "说明：这些是 API 层的模型名，供客户端配置使用；它们**不强制**切换 DeepSeek "
        "网页端实际选用的模型（那由登录账号与网页端设置决定）。"
    )
    return "\n".join(lines)


def _token_reply(driver, bucket: str) -> str:
    """本桶的 token 速度摘要：最近一轮 + 桶平均。只读，不导航、不触发生成。

    口径诚实优先：只输出**计数与时长**，不回显提示词 / 回复正文；并显式标注
    「token 数为估算」「速度为网页版有效速度，非模型裸速度」。
    """
    stats = driver.speed_stats(bucket)
    samples = int(stats.get("samples") or 0)
    if not samples:
        return "\n".join([
            f"[Bridge] token 速度（桶 {bucket}）：暂无样本。",
            "  先发一轮普通请求（成功生成一次）后再查看。",
            "  只统计成功的生成；取消 / 失败轮不计入。",
        ])

    lines = [f"[Bridge] token 速度（桶 {bucket}；本轮 = 最近一次成功生成）："]
    last = stats.get("last") or {}
    if last:
        lines.extend(_token_block("本轮", last, indent="  "))
    lines.append(f"  本桶统计（近 {samples} 轮有效样本）：")
    lines.append(
        f"    平均单次输出速度 {_num(stats.get('mean_decode_tps_est'))} tok/s；"
        f"整体聚合吞吐率 {_num(stats.get('aggregate_decode_tps_est'))} tok/s"
    )
    lines.append(
        f"    平均单次输入速率 {_num(stats.get('mean_prompt_rate_est_tps'))} tok/s；"
        f"整体聚合输入速率 {_num(stats.get('aggregate_prompt_rate_est_tps'))} tok/s"
    )
    lines.append(
        f"    平均页面首字延迟 {_sec(stats.get('ttft_s_avg'))}s；"
        f"平均增长窗口 {_sec(stats.get('decode_s_avg'))}s"
    )
    lines.append(
        "  口径：token 数为字符估算（非精确）；时间与速度均为网页快照观测值，"
        "非模型裸速度；仅统计成功且有效的生成。"
    )
    reply_tokens = int(last.get("reply_tokens_est") or 0)
    if reply_tokens and reply_tokens < SHORT_REPLY_TOKENS:
        lines.append("  注意：最近一轮回复过短，速度估计误差较大，仅供参考。")
    flags = last.get("quality_flags") or ()
    if flags:
        lines.append(f"  质量标记：{', '.join(flags)}")
    return "\n".join(lines)


def _token_block(label: str, speed: dict, indent: str = "  ") -> list[str]:
    """把一轮的数值指标格式化成若干行；只读字段，缺项按 0 处理。"""
    inner = indent + "  "
    lines = [f"{indent}{label}："]
    lines.append(
        f"{inner}输入速率约 {_num(speed.get('prompt_rate_est_tps'))} tok/s（估算）"
        f"（输入 {_count(speed.get('prompt_tokens_est'))} tok / 页面首字 "
        f"{_sec(speed.get('page_ttft_observed_s'))}s）"
    )
    lines.append(
        f"{inner}输出速度约 {_num(speed.get('page_decode_tps_est'))} tok/s（估算）"
        f"（输出 {_count(speed.get('reply_tokens_est'))} tok / 增长窗口 "
        f"{_sec(speed.get('page_growth_window_s'))}s）"
    )
    lines.append(
        f"{inner}完成判定等待 {_sec(speed.get('settle_observed_s'))}s；"
        f"首字至结束 {_sec(speed.get('page_generation_to_finish_s'))}s"
    )
    e2e = speed.get("request_e2e_s")
    if e2e is not None:
        lines.append(f"{inner}请求端到端 {_sec(e2e)}s")
    return lines


def _num(value) -> str:
    """速度：四舍五入到整数，带千位分隔符；缺项 / 非法值回落到 0。"""
    try:
        return f"{round(float(value)):,}"
    except (TypeError, ValueError):
        return "0"


def _count(value) -> str:
    """token 计数：整数，带千位分隔符；缺项 / 非法值回落到 0。"""
    try:
        return f"{round(float(value)):,}"
    except (TypeError, ValueError):
        return "0"


def _sec(value) -> str:
    """时长：保留 1 位小数；缺项 / 非法值回落到 0.0。"""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "0.0"


def _browser_line(driver) -> str:
    init_error = getattr(driver, "init_error", None)
    if getattr(driver, "page", None) is None:
        detail = f"（初始化失败：{_one_line(init_error)}）" if init_error else ""
        return f"未初始化{detail}"
    return "已就绪"


def _status_reply(driver, bucket: str) -> str:
    """桥接与当前桶的状态摘要：只读，不导航、不触发生成。"""
    stats = driver.session_stats(bucket)
    page_ready = getattr(driver, "page", None) is not None
    init_error = getattr(driver, "init_error", None)
    lines = ["[Bridge] 桥接状态："]
    if init_error:
        lines.append(f"  请求处理：不可用（浏览器初始化失败：{_one_line(init_error)}）")
    elif not page_ready:
        lines.append("  请求处理：不可用（浏览器尚未初始化）")
    else:
        lines.append("  请求处理：可用")
    lines.append(f"  浏览器：{_browser_line(driver)}")
    lines.append(f"  当前桶：{bucket}")
    lines.append(
        f"  会话分桶：{'开启' if config.SESSION_SCOPING else '关闭'}"
        f"（请求头 {config.SESSION_KEY_HEADER}）"
    )
    lines.append(f"  当前桶页面：{_page_line(stats, page_ready)}")
    lines.append(f"  会话绑定：{stats.get('linked_url') or '未绑定'}")
    lines.append(f"  网页会话：{_history_line(stats)}")
    lines.append(
        f"  轮数：{int(stats.get('turns') or 0)}；"
        f"估算 tokens：{int(stats.get('est_tokens') or 0)}"
    )
    if not page_ready:
        lines.append(
            "  下一步：检查登录状态或查看 GET /healthz；浏览器不可用时只读命令仍可用。"
        )
    return "\n".join(lines)


def _page_line(stats: dict, page_ready: bool) -> str:
    url = stats.get("url")
    if url:
        return url
    if page_ready:
        return "已打开（当前不在会话页上）"
    return "未打开（按需惰性创建）"


def _history_line(stats: dict) -> str:
    if stats.get("pending_rotation"):
        return "已请求重置（下一轮开新网页会话并把完整历史播种进去）"
    if stats.get("pending_reseed"):
        return "已排队重新播种（下一轮重发完整历史，见 `session reseed`）"
    if stats.get("needs_seed"):
        return "需要播种（下一轮会把客户端历史重放进去）"
    return "已有上下文（之后只发增量）"


async def _session_reply(command: BridgeCommand, driver, bucket: str) -> str:
    sub = command.subaction
    if sub == "status":
        return _session_status_reply(driver, bucket)
    if sub == "reset":
        return _session_reset_reply(driver, bucket)
    if sub == "reseed":
        return _session_reseed_reply(driver, bucket)
    if sub == "link":
        return await _session_link_reply(command, driver, bucket)
    if sub == "unlink":
        return _session_unlink_reply(driver, bucket)
    return f"[Bridge] 命令未执行：未知会话子命令。\n{_USAGE}"


def _session_status_reply(driver, bucket: str) -> str:
    """当前桶的会话状态摘要（只查当前桶：默认不提供「列出所有桶」的命令，避免泄露他桶）。"""
    stats = driver.session_stats(bucket)
    lines = [f"[Bridge] 会话桶 {bucket} 的会话状态："]
    lines.append(f"  会话绑定：{stats.get('linked_url') or '未绑定'}")
    lines.append(f"  网页会话：{_history_line(stats)}")
    lines.append(
        f"  轮数：{int(stats.get('turns') or 0)}；"
        f"估算 tokens：{int(stats.get('est_tokens') or 0)}"
    )
    if stats.get("cap_hit"):
        lines.append("  上次生成：因上下文长度上限中断（下一轮会轮转到新会话）")
    last_error = stats.get("last_error")
    if last_error:
        lines.append(f"  最近错误：{_one_line(last_error)}")
    lines.append(f"  任务快照：{'开启' if config.TASK_SNAPSHOT_ENABLED else '关闭'}")
    lines.append(
        f"  管理：`{BRIDGE_COMMAND} session reset`（换新网页会话）、"
        f"`{BRIDGE_COMMAND} session reseed`（重播历史）、"
        f"`{BRIDGE_COMMAND} session link <URL>`"
    )
    return "\n".join(lines)


def _session_reset_reply(driver, bucket: str) -> str:
    """重置当前桶的**网页会话**：复用 driver.reset_session，只改状态、不碰页面。"""
    had_link = bool(driver.linked_session_url(bucket))
    driver.reset_session(bucket)
    logger.info("[命令] key=%s 已按 %s session reset 请求重置网页会话。", bucket, BRIDGE_COMMAND)
    lines = [
        f"[Bridge] 已把会话桶 {bucket} 标记为「下一轮开新网页会话」。",
        "  影响范围：只影响这个会话桶，其它桶不动。",
        "  语义：下一轮会新开一条网页会话，并把客户端传来的完整历史**播种**进去（上下文不丢）。",
        "  注意：这不是删除客户端的对话历史，也不清除 DeepSeek 账户里的数据或远程会话记录。",
    ]
    if had_link:
        lines.append("  绑定：原有的会话绑定已解除（重置要求一条全新的网页会话）。")
    lines.append(
        f"  HTTP 等价入口：POST /session/reset?session={bucket}"
    )
    return "\n".join(lines)


def _session_reseed_reply(driver, bucket: str) -> str:
    """排队「下一轮重新播种」：标记落盘，重启 / 并发都不会丢，成功发送后自动清除。"""
    driver.reseed_session(bucket)
    logger.info("[命令] key=%s 已按 %s session reseed 排队重新播种。", bucket, BRIDGE_COMMAND)
    linked = driver.linked_session_url(bucket)
    target = f"绑定的网页会话 {linked}" if linked else "当前网页会话"
    return "\n".join([
        f"[Bridge] 会话桶 {bucket} 已排队「下一轮重新播种」。",
        f"  下一轮会把客户端传来的完整历史再发进{target}（一次性标记，已落盘，重启后仍有效）。",
        "  注意：这不是清除旧消息，而是**再次发送**上下文——那条会话里会出现重复内容。",
        f"  会话绑定未变；要换新网页会话请用 `{BRIDGE_COMMAND} session reset`。",
    ])


async def _session_link_reply(command: BridgeCommand, driver, bucket: str) -> str:
    url = normalize_session_url(command.target)
    if url is None:
        allowed = "、".join(config.LINK_ALLOWED_HOSTS or []) or "（未配置）"
        return "\n".join([
            f"[Bridge] 命令未执行：这不是一条可绑定的会话地址：{command.target}",
            f"  只接受 https + 允许的站点（{allowed}）+ 形如 /a/chat/s/<会话 ID> 的路径；",
            "  也可以只发会话 ID（会自动补全成完整地址）。",
        ])
    await driver.link_session(url, seed=command.seed, key=bucket)
    logger.info("[命令] key=%s 已按 %s session link 绑定到 %s。", bucket, BRIDGE_COMMAND, url)
    lines = [f"[Bridge] 已把会话桶 {bucket} 绑定到：{url}"]
    if command.seed:
        lines.append(
            "  下一轮会把客户端传来的完整历史**播种**进这条会话（历史在网页端会重复一遍）。"
        )
    else:
        lines.append("  按「该会话里已有此前上下文」处理，之后只发增量。")
        lines.append(
            f"  若那条会话其实是空的，请改用 "
            f"`{BRIDGE_COMMAND} session link <URL> {SEED_FLAG}`。"
        )
    lines.append("  影响范围：只影响这个会话桶；页面丢失重建 / 服务重启后仍会回到这条会话。")
    lines.append(f"  解除绑定：`{BRIDGE_COMMAND} session unlink`。")
    return "\n".join(lines)


def _session_unlink_reply(driver, bucket: str) -> str:
    had = driver.unlink_session(bucket)
    logger.info("[命令] key=%s 已按 %s session unlink。", bucket, BRIDGE_COMMAND)
    if not had:
        return f"[Bridge] 会话桶 {bucket} 本来就没有绑定网页会话。"
    return "\n".join([
        f"[Bridge] 已解除会话桶 {bucket} 的会话绑定。",
        "  当前页面与上下文保持不变（不会被清空）；只是不再被钉在那条会话上。",
        f"  需要一条全新的网页会话：`{BRIDGE_COMMAND} session reset`。",
    ])


def _one_line(text: str, limit: int = 200) -> str:
    """只保留错误的第一行有效内容并限长。

    回复里不带换行，也不会把整段堆栈 / 多行内部报告倒进对话：只需要足以定位问题的
    一句话，其余细节留在日志里。同时天然避免把敏感内容整段回显。
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    flat = " ".join(lines[0].split()) if lines else ""
    return flat[:limit] + ("…" if len(flat) > limit else "")
