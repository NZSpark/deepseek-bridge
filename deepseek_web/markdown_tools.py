"""Schema, sandbox policy, and optional local execution for edit_markdown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config
from .markdown_io import MarkdownError, apply_edit, backup_md, read_md, unified_diff, write_md

EDIT_MARKDOWN_TOOL_NAME = "edit_markdown"
EDIT_MARKDOWN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": EDIT_MARKDOWN_TOOL_NAME,
        "description": (
            "Safely edit a local Markdown file by replacing an inclusive 1-based line range. "
            "Defaults to a dry-run diff. Actual writes require server permission."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": ("Relative Markdown path inside the configured workspace."),
                },
                "start": {
                    "type": "integer",
                    "minimum": 1,
                    "description": ("First line to replace, 1-based inclusive."),
                },
                "end": {
                    "type": "integer",
                    "minimum": 1,
                    "description": ("Last line to replace, 1-based inclusive."),
                },
                "new_text": {
                    "type": "string",
                    "description": (
                        "Replacement text; may contain newlines. "
                        "Empty text deletes the selected lines."
                    ),
                },
                "write": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Request an actual write; server-side permission is also required."
                    ),
                },
            },
            "required": ["path", "start", "end", "new_text"],
            "additionalProperties": False,
        },
    },
}


def effective_tools(
    tools: list[dict[str, Any]] | None, tool_choice: Any = None
) -> list[dict[str, Any]] | None:
    """Return request tools plus the canonical local tool when explicitly enabled."""
    result = list(tools or [])
    if not config.EDIT_MARKDOWN_LOCAL or tool_choice == "none":
        return result or None

    # Only join an existing tool set. A request that declared no tools is a plain
    # chat request: injecting the schema there would append the whole tool
    # instruction block to every prompt and invite unsolicited tool_calls.
    if not result:
        return None

    # The bridge owns this name in local mode; do not let a client redefine the
    # argument contract for a tool that can write files on the host.
    result = [
        tool
        for tool in result
        if not isinstance(tool, dict)
        or (tool.get("function") or tool).get("name") != EDIT_MARKDOWN_TOOL_NAME
    ]
    result.append(EDIT_MARKDOWN_TOOL)
    return result


def _function_name(tool: Any) -> str | None:
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function") or tool
    name = fn.get("name") if isinstance(fn, dict) else None
    return name if isinstance(name, str) and name else None


def _workspace_root() -> Path:
    raw = getattr(config, "EDIT_MARKDOWN_ROOT", str(config.PROJECT_ROOT))
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise MarkdownError("配置的 Markdown 工作区根目录不可用")
    return root


def resolve_edit_path(raw_path: Any) -> Path:
    """Resolve a strictly relative .md/.markdown file inside the workspace."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise MarkdownError("path 必须是非空相对路径")
    candidate_text = raw_path.strip()
    candidate = Path(candidate_text)
    if candidate.is_absolute() or candidate_text.startswith(("~", "\\")):
        raise MarkdownError("不允许绝对路径")
    if ".." in candidate.parts:
        raise MarkdownError("路径不能包含 '..' 路径段")
    if candidate.suffix.lower() not in {".md", ".markdown"}:
        raise MarkdownError("只允许编辑 .md 或 .markdown 文件")

    root = _workspace_root()
    try:
        resolved = (root / candidate).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise MarkdownError("目标路径不存在或超出允许的工作区") from exc
    if not resolved.is_file():
        raise MarkdownError("目标必须是已存在的普通文件")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MarkdownError("目标路径超出允许的工作区") from exc
    limit = int(getattr(config, "EDIT_MARKDOWN_MAX_FILE_BYTES", 4 * 1024 * 1024))
    if limit > 0 and resolved.stat().st_size > limit:
        raise MarkdownError("文件超过配置的大小上限")
    return resolved


def execute_edit_markdown(arguments: Any) -> dict[str, Any]:
    """Validate, preview, or safely write a single Markdown line-range edit."""
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "arguments 必须是 JSON 对象"}
    allowed = {"path", "start", "end", "new_text", "write"}
    extras = set(arguments) - allowed
    if extras:
        return {"ok": False, "error": "存在未支持的参数"}

    path: Path | None = None
    try:
        raw_path = arguments.get("path")
        start = arguments.get("start")
        end = arguments.get("end")
        new_text = arguments.get("new_text")
        write_requested = arguments.get("write", False)
        if isinstance(start, bool) or not isinstance(start, int):
            raise MarkdownError("start 必须是整数行号")  # noqa: TRY301
        if isinstance(end, bool) or not isinstance(end, int):
            raise MarkdownError("end 必须是整数行号")  # noqa: TRY301
        if not isinstance(new_text, str):
            raise MarkdownError("new_text 必须是字符串")  # noqa: TRY301
        if not isinstance(write_requested, bool):
            raise MarkdownError("write 必须是布尔值")  # noqa: TRY301

        path = resolve_edit_path(raw_path)
        original = read_md(path)
        edited = apply_edit(original, start, end, new_text)
        display_path = str(path.relative_to(_workspace_root()))
        diff = unified_diff(original, edited, display_path)
        should_write = write_requested and bool(config.EDIT_MARKDOWN_WRITE)
        backup_path: str | None = None
        written = False
        note = None

        if original.raw != edited.raw and should_write:
            backup_dir_raw = getattr(config, "EDIT_MARKDOWN_BACKUP_DIR", "output/backups")
            backup_dir = Path(backup_dir_raw).expanduser()
            if not backup_dir.is_absolute():
                backup_dir = _workspace_root() / backup_dir
            backup_dir = backup_dir.resolve()
            # Revalidate the target at the write boundary to reduce path-swap risk.
            verified_again = resolve_edit_path(raw_path)
            if verified_again != path:
                raise MarkdownError("目标路径在写入前发生变化，已停止操作")  # noqa: TRY301
            backup = backup_md(path, backup_dir)
            backup_path = str(backup.backup_path)
            # write_md's backup is deliberately omitted because the backup has
            # already been created and its exact path must be returned to caller.
            # write_md uses an atomic replacement for the destination file.
            # 写盘失败时异常向上抛，备份**保留**——它是唯一的回滚点。
            write_md(edited, path)
            written = True
            # 写盘成功：备份用完即删，避免目录无限膨胀。工作副本由 git 兜底，
            # 改错可 `git checkout -- <file>`；备份只是写盘瞬间的安全垫。
            # 删除失败不影响本次写入结果，此时仍把路径回报给调用方。
            try:
                backup.backup_path.unlink(missing_ok=True)
                backup_path = None
            except OSError:
                pass
        elif original.raw != edited.raw and write_requested and not should_write:
            note = "服务端写盘权限已关闭，本次只生成 diff，未修改文件。"
        elif original.raw == edited.raw:
            note = "编辑结果与原文件相同，无需写盘。"

        result: dict[str, Any] = {
            "ok": True,
            "path": str(raw_path),
            "start": start,
            "end": end,
            "write_requested": write_requested,
            "written": written,
            "backup": backup_path,
            "diff": diff,
        }
        if note:
            result["note"] = note
        return result
    except (MarkdownError, OSError, ValueError, TypeError) as exc:
        # Never return an absolute host path or raw OS detail to model-controlled text.
        error = str(exc).replace("\n", " ")
        if path is not None:
            error = error.replace(str(path), "<目标文件>")
        return {"ok": False, "error": error[:500]}


def _is_local_only(calls: list[dict[str, Any]]) -> bool:
    return bool(calls) and all(call.get("name") == EDIT_MARKDOWN_TOOL_NAME for call in calls)


def local_round_eligible(calls: list[dict[str, Any]]) -> bool:
    """Whether a parsed batch may be executed by the bridge itself."""
    return config.EDIT_MARKDOWN_LOCAL and _is_local_only(calls)


def _max_steps() -> int:
    return max(1, int(getattr(config, "EDIT_MARKDOWN_MAX_STEPS", 3)))


def step_limit_reply(max_steps: int | None = None) -> str:
    """Text returned when the local round budget is exhausted."""
    limit = _max_steps() if max_steps is None else max_steps
    return (
        f"已达到本次请求的本地 Markdown 工具执行轮数上限（{limit} 轮）；"
        "未执行后续工具调用。请检查已返回的工具结果，必要时发起新的请求继续。"
    )


def _render_results(calls: list[dict[str, Any]]) -> str:
    sections: list[str] = []
    for index, call in enumerate(calls, start=1):
        result = execute_edit_markdown(call.get("arguments"))
        rendered = json.dumps(result, ensure_ascii=False)
        sections.append(f"[工具执行结果 edit_markdown #{index}]\n{rendered}")
    return "\n\n".join(sections)


def _continuation_prompt(result_block: str) -> str:
    return (
        f"{result_block}\n\n"
        "请根据以上真实工具执行结果继续完成用户任务。若还需调用工具，严格使用已声明的 "
        "tool_call JSON 格式；不要假称未执行的写入已经成功。"
    )


async def run_local_tool_round(  # noqa: PLR0913, PLR0917
    driver: Any,
    initial_reply: str,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    seeded_prompt: str,
    session_key: str | None,
) -> tuple[str, list[dict[str, Any]], bool]:
    """Run **one** local execution round, if eligible.

    Used by the Responses / streaming entrances, which cannot hold the whole
    multi-round loop inside a single buffered upstream call. Returns
    ``(reply, calls, handled)``; when not eligible the inputs pass through.
    """
    from .toolcall_retry import complete_with_toolcall_retry
    from .toolcalls import _tool_names, parse_tool_calls

    active_tools = effective_tools(tools, tool_choice)
    valid_names = _tool_names(active_tools)
    calls = parse_tool_calls(initial_reply, valid_names) if active_tools else []
    if not local_round_eligible(calls):
        return initial_reply, calls, False

    result_block = _render_results(calls)
    seed_context = (
        f"{seeded_prompt}\n\n[你之前的回复]\n{initial_reply}\n\n{result_block}"
    ).strip()
    current_reply = await complete_with_toolcall_retry(
        driver,
        _continuation_prompt(result_block),
        seeded_prompt=seed_context,
        key=session_key,
        images=None,
        wants_tools=bool(active_tools),
        valid_names=valid_names,
    )
    calls = parse_tool_calls(current_reply, valid_names) if active_tools else []
    return current_reply, calls, True


async def run_local_edit_markdown_loop(  # noqa: PLR0913, PLR0917
    driver: Any,
    initial_reply: str,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    prompt: str,
    seeded_prompt: str,
    session_key: str | None,
    images: list[Any] | None = None,
) -> tuple[str, list[dict[str, Any]], bool]:
    """Execute eligible local tool rounds, feeding results back to the web model.

    Returns ``(final_text, pending_external_calls, handled_locally)``. Mixed batches
    are never partially executed. Existing external tool calling remains unchanged.
    """
    from .toolcall_retry import complete_with_toolcall_retry
    from .toolcalls import _tool_names, parse_tool_calls

    active_tools = effective_tools(tools, tool_choice)
    valid_names = _tool_names(active_tools)
    calls = parse_tool_calls(initial_reply, valid_names) if active_tools else []
    if not local_round_eligible(calls):
        return initial_reply, calls, False

    seed_context = seeded_prompt
    current_reply = initial_reply
    steps = 0
    max_steps = _max_steps()

    while _is_local_only(calls):
        if steps >= max_steps:
            return step_limit_reply(max_steps), [], True
        steps += 1
        result_block = _render_results(calls)
        seed_context = (
            f"{seed_context}\n\n[你之前的回复]\n{current_reply}\n\n{result_block}"
        ).strip()
        current_reply = await complete_with_toolcall_retry(
            driver,
            _continuation_prompt(result_block),
            seeded_prompt=seed_context,
            key=session_key,
            images=None,
            wants_tools=bool(active_tools),
            valid_names=valid_names,
        )
        calls = parse_tool_calls(current_reply, valid_names) if active_tools else []

    # A later external call may follow a completed local round; expose it as normal API output.
    return current_reply, calls, True
