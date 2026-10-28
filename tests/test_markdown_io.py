"""Regression tests for byte-preserving Markdown editing."""

from __future__ import annotations

import pytest

from deepseek_web.markdown_io import (
    MarkdownError,
    apply_edit,
    backup_md,
    parse_md,
    read_md,
    rollback_md,
    scan_fences,
    unified_diff,
    write_md,
)


def test_replace_inclusive_range_preserves_outside_bytes() -> None:
    original = b"before  \r\nold text  \r\nafter\t\r\n"
    edited = apply_edit(parse_md(original), 2, 2, "new text\nsecond line")

    assert edited.raw == b"before  \r\nnew text\r\nsecond line\r\nafter\t\r\n"
    assert edited.raw.startswith(b"before  \r\n")
    assert edited.raw.endswith(b"after\t\r\n")


def test_delete_middle_and_first_or_last_line() -> None:
    doc = parse_md(b"one\ntwo\nthree\n")
    assert apply_edit(doc, 2, 2, "").raw == b"one\nthree\n"
    assert apply_edit(doc, 1, 1, "").raw == b"two\nthree\n"
    assert apply_edit(doc, 3, 3, "").raw == b"one\ntwo\n"


def test_empty_document_and_invalid_ranges() -> None:
    empty = parse_md(b"")
    assert empty.lines == ()
    assert apply_edit(empty, 1, 1, "x").raw == b"x"
    with pytest.raises(MarkdownError, match="行号区间无效"):
        apply_edit(empty, 1, 0, "x")

    doc = parse_md(b"one\ntwo")
    for start, end in ((0, 1), (2, 1), (1, 3), (3, 3)):
        with pytest.raises(MarkdownError, match="行号区间无效"):
            apply_edit(doc, start, end, "x")


def test_newline_style_trailing_newline_and_unicode() -> None:
    crlf = parse_md("标题\r\n旧行".encode())
    edited = apply_edit(crlf, 2, 2, "新行\n保留 emoji 🐈")
    assert edited.raw == "标题\r\n新行\r\n保留 emoji 🐈".encode()
    assert not edited.trailing_newline

    no_final_newline = apply_edit(parse_md(b"a\nb"), 2, 2, "c")
    assert no_final_newline.raw == b"a\nc"
    with_final_newline = apply_edit(parse_md(b"a\nb\n"), 2, 2, "c")
    assert with_final_newline.raw == b"a\nc\n"


def test_mixed_newlines_keep_outside_bytes_exact() -> None:
    original = b"first\r\nselected\nlast\r"
    edited = apply_edit(parse_md(original), 2, 2, "replacement\nline")
    assert edited.raw == b"first\r\nreplacement\r\nline\nlast\r"


def test_multiple_lines_blank_lines_indentation_and_trailing_spaces() -> None:
    original = b"head\n  old  \n\n tail\t\nend\n"
    edited = apply_edit(parse_md(original), 2, 4, "  new  \n\n\tindented")
    assert edited.raw == b"head\n  new  \n\n\tindented\nend\n"


def test_fence_scanner_handles_backticks_tildes_and_long_closers() -> None:
    lines = (
        "before",
        "```python",
        "inside ~~~",
        "````",
        "~~~text",
        "body",
        "~~~~~",
    )
    fences = scan_fences(lines)
    assert [(f.start, f.end, f.marker, f.length, f.language) for f in fences] == [
        (2, 4, "`", 3, "python"),
        (5, 7, "~", 3, "text"),
    ]


def test_unclosed_original_or_edited_fence_is_rejected() -> None:
    with pytest.raises(MarkdownError, match="未闭合"):
        parse_md(b"before\n```python\ncode\n")

    original = parse_md(b"before\nplain text\nafter\n")
    with pytest.raises(MarkdownError, match="未闭合"):
        apply_edit(original, 2, 2, "```python\ncode")


def test_diff_and_no_change_detection() -> None:
    original = parse_md(b"one\ntwo\n")
    unchanged = apply_edit(original, 2, 2, "two")
    assert unchanged.raw == original.raw
    assert unified_diff(original, unchanged) == ""

    edited = apply_edit(original, 2, 2, "new")
    diff = unified_diff(original, edited, "notes.md")
    assert "a/notes.md" in diff
    assert "b/notes.md" in diff
    assert "-two" in diff
    assert "+new" in diff


def test_backup_and_rollback_are_byte_exact(tmp_path) -> None:
    target = tmp_path / "notes.md"
    target.write_bytes(b"original\r\nbytes  \n")
    backup = backup_md(target, tmp_path / "backups")
    target.write_bytes(b"modified\n")

    rollback_md(backup)
    assert target.read_bytes() == b"original\r\nbytes  \n"
    assert backup.backup_path.read_bytes() == b"original\r\nbytes  \n"


def test_atomic_write_creates_backup_and_replaces_file(tmp_path) -> None:
    target = tmp_path / "notes.md"
    target.write_bytes(b"old\r\n")
    edited = apply_edit(read_md(target), 1, 1, "new")

    backup = write_md(edited, target, backup_dir=tmp_path / "backups")

    assert target.read_bytes() == b"new\r\n"
    assert backup is not None
    assert backup.backup_path.read_bytes() == b"old\r\n"


def test_atomic_replace_failure_leaves_original_unchanged(tmp_path, monkeypatch) -> None:
    from deepseek_web import markdown_io

    target = tmp_path / "notes.md"
    target.write_bytes(b"old\n")
    edited = apply_edit(read_md(target), 1, 1, "new")

    def fail_replace(_source, _target):
        raise OSError("simulated failure")

    monkeypatch.setattr(markdown_io.os, "replace", fail_replace)
    with pytest.raises(MarkdownError, match="写入文件失败"):
        write_md(edited, target)

    assert target.read_bytes() == b"old\n"
    assert list(tmp_path.glob("*.tmp")) == []


def test_backup_failure_does_not_change_target(tmp_path) -> None:
    target = tmp_path / "notes.md"
    target.write_bytes(b"old\n")
    edited = apply_edit(read_md(target), 1, 1, "new")
    not_a_directory = tmp_path / "backups"
    not_a_directory.write_text("file")

    with pytest.raises(MarkdownError, match="创建文件备份失败"):
        write_md(edited, target, backup_dir=not_a_directory)

    assert target.read_bytes() == b"old\n"


def test_rollback_rejects_missing_backup(tmp_path) -> None:
    target = tmp_path / "notes.md"
    target.write_text("original")
    from deepseek_web.markdown_io import Backup

    with pytest.raises(MarkdownError, match="备份文件不存在"):
        rollback_md(Backup(target, tmp_path / "missing.bak"))
