"""Byte-preserving Markdown edits, fence validation, diffs, and backups.

This module deliberately has no dependency on FastAPI, Playwright, or an LLM.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


class MarkdownError(ValueError):
    """A Markdown document cannot be read or safely edited."""


@dataclass(frozen=True, slots=True)
class Fence:
    start: int
    end: int
    marker: str
    length: int
    language: str


@dataclass(frozen=True, slots=True)
class MdDoc:
    path: Path | None
    raw: bytes
    text: str
    lines: tuple[str, ...]
    newline: str
    trailing_newline: bool
    fences: tuple[Fence, ...]


@dataclass(frozen=True, slots=True)
class Backup:
    original_path: Path
    backup_path: Path


def _line_texts(text: str) -> tuple[str, ...]:
    """Return logical lines without CR/LF terminators; a final newline adds no row."""
    if not text:
        return ()
    lines = re.split(r"\r\n|\r|\n", text)
    if text.endswith(("\n", "\r")):
        lines.pop()
    return tuple(lines)


def _newline_style(raw: bytes) -> str:
    match = re.search(rb"\r\n|\n|\r", raw)
    if match is None:
        return "\n"
    return match.group().decode("ascii")


def scan_fences(lines: tuple[str, ...]) -> tuple[Fence, ...]:
    """Scan Markdown fenced blocks and reject unclosed or mismatched blocks."""
    opening: tuple[int, str, int, str] | None = None
    result: list[Fence] = []
    opener_re = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

    for number, line in enumerate(lines, start=1):
        match = opener_re.match(line)
        if opening is None:
            if not match:
                continue
            marker_run, info = match.groups()
            marker = marker_run[0]
            if marker == "`" and "`" in info:
                continue
            language = info.strip().split(maxsplit=1)[0] if info.strip() else ""
            opening = (number, marker, len(marker_run), language)
            continue

        start, marker, length, language = opening
        closing_re = re.compile(rf"^ {{0,3}}{re.escape(marker)}{{{length},}}[ \t]*$")
        if closing_re.match(line):
            result.append(Fence(start, number, marker, length, language))
            opening = None

    if opening is not None:
        start, marker, _length, _language = opening
        raise MarkdownError(
            f"未闭合的 Markdown 围栏：第 {start} 行开始，标记为 {marker!r}"
        )
    return tuple(result)


def parse_md(raw: bytes, path: Path | None = None) -> MdDoc:
    """Parse UTF-8 bytes without newline conversion."""
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MarkdownError("文件不是有效的 UTF-8 文本") from exc
    lines = _line_texts(text)
    fences = scan_fences(lines)
    return MdDoc(
        path=path,
        raw=raw,
        text=text,
        lines=lines,
        newline=_newline_style(raw),
        trailing_newline=text.endswith(("\n", "\r")),
        fences=fences,
    )


def read_md(path: Path) -> MdDoc:
    """Read and parse a Markdown file as bytes."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MarkdownError(f"无法读取文件：{exc.strerror or type(exc).__name__}") from exc
    return parse_md(raw, path)


def apply_edit(doc: MdDoc, start: int, end: int, new_text: str) -> MdDoc:
    """Replace the 1-based inclusive line interval while preserving outside bytes."""
    if isinstance(start, bool) or not isinstance(start, int):
        raise MarkdownError("start 必须是整数行号")
    if isinstance(end, bool) or not isinstance(end, int):
        raise MarkdownError("end 必须是整数行号")
    if not isinstance(new_text, str):
        raise MarkdownError("new_text 必须是字符串")
    empty_document_insert = not doc.lines and start == 1 and end == 1
    if start < 1 or end < start or (end > len(doc.lines) and not empty_document_insert):
        raise MarkdownError(f"行号区间无效：文件共 {len(doc.lines)} 行，收到 [{start}, {end}]")

    # Byte spans avoid normalizing existing CRLF, mixed newlines, or trailing spaces.
    raw_lines = doc.raw.splitlines(keepends=True)
    if len(raw_lines) != len(doc.lines):
        raise MarkdownError("无法稳定映射 Markdown 行与原始字节")
    prefix = b"".join(raw_lines[: start - 1])
    suffix = b"".join(raw_lines[end:])

    normalized = new_text.replace("\r\n", "\n").replace("\r", "\n")
    replacement = normalized.replace("\n", doc.newline).encode("utf-8")

    # Retain the selected final line's exact terminator at the splice boundary.
    # This matters for mixed-newline files: bytes belonging to the suffix must not
    # be silently changed merely because the replacement is multiline.
    selected_last = raw_lines[end - 1] if raw_lines else b""
    ending = re.search(rb"(\r\n|\r|\n)$", selected_last)
    separator = ending.group(1) if ending else doc.newline.encode("ascii")
    needs_separator = (
        replacement
        and not replacement.endswith((b"\n", b"\r"))
        and (bool(suffix) or doc.trailing_newline)
    )
    if needs_separator:
        replacement += separator

    # A prefix is made of complete lines, so it already ends in its original terminator.
    new_raw = prefix + replacement + suffix
    edited = parse_md(new_raw, doc.path)
    issues = verify(doc, edited, start, end)
    if issues:
        first = issues[0]
        raise MarkdownError(str(first["message"]))
    return edited


def verify(original: MdDoc, edited: MdDoc, start: int, end: int) -> list[dict[str, object]]:
    """Validate the edited document and return structural issues, if any.

    Parsing already rejects unclosed fences. This additional check keeps the API
    convenient for callers and detects accidental loss of fence ranges outside
    the targeted line interval.
    """
    issues: list[dict[str, object]] = []
    old_outside = [f for f in original.fences if f.end < start or f.start > end]
    for old in old_outside:
        # Line numbers after the edited range may shift; compare the fence's literal
        # bytes instead of requiring the old numeric line positions to remain fixed.
        before = original.lines[old.start - 1 : old.end]
        found = any(
            tuple(edited.lines[i : i + len(before)]) == before
            for i in range(max(0, len(edited.lines) - len(before) + 1))
        )
        if not found:
            issues.append({
                "code": "outside_fence_changed",
                "message": "编辑可能影响了选区外的代码围栏",
                "line": old.start,
            })
    return issues


def unified_diff(original: MdDoc, edited: MdDoc, display_path: str = "document.md") -> str:
    """Generate a unified diff without changing either document."""
    before = original.text.splitlines(keepends=True)
    after = edited.text.splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        before,
        after,
        fromfile=f"a/{display_path}",
        tofile=f"b/{display_path}",
    ))


def backup_md(path: Path, backup_dir: Path) -> Backup:
    """Create an exclusive, uniquely named byte-for-byte backup."""
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        fd, name = tempfile.mkstemp(
            prefix=f"{path.name}.", suffix=".bak", dir=backup_dir
        )
        backup_path = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise MarkdownError(f"创建文件备份失败：{exc.strerror or type(exc).__name__}") from exc
    return Backup(path, backup_path)


def write_md(doc: MdDoc, path: Path, *, backup_dir: Path | None = None) -> Backup | None:
    """Atomically replace ``path`` after optionally creating a mandatory backup."""
    backup = backup_md(path, backup_dir) if backup_dir is not None else None
    mode = path.stat().st_mode
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(doc.raw)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.chmod(mode)
        temp_path.replace(path)
        # Best-effort directory metadata durability; unsupported platforms may reject it.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise MarkdownError(f"写入文件失败：{exc.strerror or type(exc).__name__}") from exc
    return backup


def rollback_md(backup: Backup) -> None:
    """Restore a known backup using a temporary file and atomic replacement."""
    if not backup.backup_path.is_file():
        raise MarkdownError("备份文件不存在")
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{backup.original_path.name}.",
        suffix=".restore.tmp",
        dir=backup.original_path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(backup.backup_path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(backup.original_path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise MarkdownError(f"恢复备份失败：{exc.strerror or type(exc).__name__}") from exc
