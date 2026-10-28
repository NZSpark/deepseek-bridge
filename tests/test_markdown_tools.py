"""Regression tests for the edit_markdown sandbox, permissions and local loop."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from deepseek_web import config, markdown_tools


def _call(name: str, arguments: dict) -> dict:
    return {"name": name, "arguments": arguments}


def _edit_call(path: str = "doc.md", start: int = 1, end: int = 1,
               new_text: str = "x", write: bool = False) -> dict:
    return _call(
        "edit_markdown",
        {"path": path, "start": start, "end": end, "new_text": new_text, "write": write},
    )


HEAD = "```tool_call\n"
TAIL = "\n```"


def _reply(*calls: dict) -> str:
    return "\n".join(HEAD + json.dumps(c) + TAIL for c in calls)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "doc.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "nested.md").write_text("a\n", encoding="utf-8")
    (root / "notes.txt").write_text("nope\n", encoding="utf-8")
    monkeypatch.setattr(config, "EDIT_MARKDOWN_ROOT", str(root))
    monkeypatch.setattr(config, "EDIT_MARKDOWN_BACKUP_DIR", "backups")
    monkeypatch.setattr(config, "EDIT_MARKDOWN_LOCAL", True)
    monkeypatch.setattr(config, "EDIT_MARKDOWN_WRITE", False)
    monkeypatch.setattr(config, "EDIT_MARKDOWN_MAX_FILE_BYTES", 1024)
    monkeypatch.setattr(config, "EDIT_MARKDOWN_MAX_STEPS", 3)
    return root


# ==================== sandbox ====================


@pytest.mark.parametrize(
    ("path", "fragment"),
    [
        ("/etc/passwd.md", "绝对路径"),
        ("~/x.md", "绝对路径"),
        ("../outside.md", ".."),
        ("sub/../../outside.md", ".."),
        ("notes.txt", ".md"),
        ("missing.md", "不存在"),
        ("", "相对路径"),
    ],
)
def test_resolve_rejects_paths_outside_sandbox(workspace: Path, path: str, fragment: str) -> None:
    result = markdown_tools.execute_edit_markdown(
        {"path": path, "start": 1, "end": 1, "new_text": "x"}
    )
    assert result["ok"] is False
    assert fragment in result["error"]


def test_resolve_accepts_nested_relative_markdown(workspace: Path) -> None:
    result = markdown_tools.execute_edit_markdown(
        {"path": "sub/nested.md", "start": 1, "end": 1, "new_text": "b"}
    )
    assert result["ok"] is True
    assert result["written"] is False
    assert "-a" in result["diff"]


def test_resolve_rejects_directory_and_oversized_file(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(markdown_tools.MarkdownError):
        markdown_tools.resolve_edit_path("sub")

    monkeypatch.setattr(config, "EDIT_MARKDOWN_MAX_FILE_BYTES", 2)
    with pytest.raises(markdown_tools.MarkdownError, match="大小上限"):
        markdown_tools.resolve_edit_path("doc.md")


def test_error_does_not_leak_absolute_host_path(workspace: Path) -> None:
    result = markdown_tools.execute_edit_markdown(
        {"path": "doc.md", "start": 1, "end": 99, "new_text": "x"}
    )
    assert result["ok"] is False
    assert str(workspace) not in result["error"]


# ==================== arguments and double gates ====================


def test_rejects_unknown_and_non_json_arguments(workspace: Path) -> None:
    assert markdown_tools.execute_edit_markdown("nope")["ok"] is False
    assert markdown_tools.execute_edit_markdown({"path": "doc.md", "extra": 1})["ok"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "doc.md", "start": True, "end": 1, "new_text": "x"},
        {"path": "doc.md", "start": 1, "end": 1.5, "new_text": "x"},
        {"path": "doc.md", "start": 1, "end": 1, "new_text": 3},
        {"path": "doc.md", "start": 1, "end": 1, "new_text": "x", "write": "yes"},
    ],
)
def test_rejects_wrong_argument_types(workspace: Path, arguments: dict) -> None:
    assert markdown_tools.execute_edit_markdown(arguments)["ok"] is False


def test_write_requires_both_gates(workspace: Path) -> None:
    result = markdown_tools.execute_edit_markdown(
        {"path": "doc.md", "start": 2, "end": 2, "new_text": "TWO", "write": False}
    )
    assert result["written"] is False
    assert (workspace / "doc.md").read_text(encoding="utf-8") == "one\ntwo\nthree\n"

    result = markdown_tools.execute_edit_markdown(
        {"path": "doc.md", "start": 2, "end": 2, "new_text": "TWO", "write": True}
    )
    assert result["written"] is False
    assert "权限" in result["note"]
    assert (workspace / "doc.md").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_write_happens_with_both_gates_and_creates_backup(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "EDIT_MARKDOWN_WRITE", True)
    result = markdown_tools.execute_edit_markdown(
        {"path": "doc.md", "start": 2, "end": 2, "new_text": "TWO", "write": True}
    )
    assert result["ok"] is True
    assert result["written"] is True
    assert (workspace / "doc.md").read_text(encoding="utf-8") == "one\nTWO\nthree\n"
    backup = Path(result["backup"])
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_noop_edit_does_not_write(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "EDIT_MARKDOWN_WRITE", True)
    result = markdown_tools.execute_edit_markdown(
        {"path": "doc.md", "start": 2, "end": 2, "new_text": "two", "write": True}
    )
    assert result["written"] is False
    assert result["backup"] is None
    assert "无需写盘" in result["note"]


def test_edit_that_breaks_outside_fence_is_rejected(workspace: Path) -> None:
    (workspace / "fence.md").write_text("```py\ncode\n```\nlast\n", encoding="utf-8")
    result = markdown_tools.execute_edit_markdown(
        {"path": "fence.md", "start": 1, "end": 1, "new_text": "```py\ncode"}
    )
    assert result["ok"] is False


# ==================== effective tools ====================


def test_effective_tools_adds_canonical_schema_only_in_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "EDIT_MARKDOWN_LOCAL", False)
    client = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    assert markdown_tools.effective_tools(client, None) == client

    monkeypatch.setattr(config, "EDIT_MARKDOWN_LOCAL", True)
    hijack = [{"type": "function", "function": {"name": "edit_markdown", "parameters": {}}}]
    result = markdown_tools.effective_tools(hijack, None)
    names = [(t.get("function") or t)["name"] for t in result]
    assert names.count("edit_markdown") == 1
    canonical = next(t for t in result if t["function"]["name"] == "edit_markdown")
    assert canonical["function"]["parameters"]["required"] == [
        "path", "start", "end", "new_text",
    ]


def test_effective_tools_respects_tool_choice_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "EDIT_MARKDOWN_LOCAL", True)
    assert markdown_tools.effective_tools(None, "none") is None


def test_effective_tools_does_not_inject_into_plain_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没声明任何工具的纯聊天请求不得被注入 edit_markdown。

    否则每轮 prompt 都会追加整段工具说明，既浪费 token 又诱导模型输出 tool_call，
    并让「无工具时 prompt 就是纯文本」这一契约失效。
    """
    monkeypatch.setattr(config, "EDIT_MARKDOWN_LOCAL", True)
    assert markdown_tools.effective_tools(None, None) is None
    assert markdown_tools.effective_tools([], None) is None


def test_effective_tools_joins_client_tool_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "EDIT_MARKDOWN_LOCAL", True)
    client = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    names = [(t.get("function") or t)["name"] for t in markdown_tools.effective_tools(client, None)]
    assert names == ["bash", "edit_markdown"]


# ==================== local execution loop ====================


class ScriptedDriver:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None, images=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)

    def sent_prompt(self, key=None):
        return None


def _run(coro):
    return asyncio.run(coro)


def test_loop_executes_local_calls_and_feeds_results_back(workspace: Path) -> None:
    initial = _reply(_edit_call(start=2, end=2, new_text="TWO"))
    driver = ScriptedDriver(["已改好"])
    reply, calls, handled = _run(markdown_tools.run_local_edit_markdown_loop(
        driver,
        initial,
        [markdown_tools.EDIT_MARKDOWN_TOOL],
        None,
        "prompt",
        "seeded",
        None,
    ))
    assert handled is True
    assert calls == []
    assert reply == "已改好"
    assert "工具执行结果 edit_markdown" in driver.prompts[0]
    assert '"ok": true' in driver.prompts[0]


def test_loop_does_not_execute_mixed_batches(workspace: Path) -> None:
    mixed = [_edit_call(start=2, end=2, new_text="TWO"), _call("bash", {"command": "ls"})]
    initial = _reply(*mixed)
    driver = ScriptedDriver([])
    reply, calls, handled = _run(markdown_tools.run_local_edit_markdown_loop(
        driver,
        initial,
        [markdown_tools.EDIT_MARKDOWN_TOOL, {"type": "function",
                                             "function": {"name": "bash", "parameters": {}}}],
        None,
        "prompt",
        "seeded",
        None,
    ))
    assert handled is False
    assert reply == initial
    assert [c["name"] for c in calls] == ["edit_markdown", "bash"]
    assert driver.prompts == []
    assert (workspace / "doc.md").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_loop_is_inert_when_local_mode_disabled(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "EDIT_MARKDOWN_LOCAL", False)
    initial = _reply(_edit_call(start=2, end=2, new_text="TWO"))
    driver = ScriptedDriver([])
    # 关闭本地模式时，客户端声明的同名工具仍走原路径返回给客户端执行。
    client_tools = [{"type": "function", "function": {"name": "edit_markdown",
                                                     "parameters": {}}}]
    reply, calls, handled = _run(markdown_tools.run_local_edit_markdown_loop(
        driver, initial, client_tools, None, "p", "s", None,
    ))
    assert handled is False
    assert reply == initial
    assert [c["name"] for c in calls] == ["edit_markdown"]
    assert driver.prompts == []
    assert (workspace / "doc.md").read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_loop_stops_at_max_steps(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "EDIT_MARKDOWN_MAX_STEPS", 2)
    call_reply = _reply(_edit_call(start=1, end=1, new_text="x"))
    driver = ScriptedDriver([call_reply, call_reply, call_reply])
    reply, calls, handled = _run(markdown_tools.run_local_edit_markdown_loop(
        driver, call_reply, [markdown_tools.EDIT_MARKDOWN_TOOL], None, "p", "s", None,
    ))
    assert handled is True
    assert calls == []
    assert "轮数上限" in reply
    # 上限 2 轮 = 两次回喂，第 3 次工具回复前必须停手。
    assert len(driver.prompts) == 2  # noqa: PLR2004


def test_single_round_helper_passes_through_when_not_eligible(workspace: Path) -> None:
    driver = ScriptedDriver([])
    reply, calls, handled = _run(markdown_tools.run_local_tool_round(
        driver, "纯文本回复", None, None, "s", None,
    ))
    assert handled is False
    assert reply == "纯文本回复"
    assert calls == []
    assert driver.prompts == []


def test_single_round_helper_executes_one_round(workspace: Path) -> None:
    driver = ScriptedDriver(["继续"])
    reply, calls, handled = _run(markdown_tools.run_local_tool_round(
        driver,
        _reply(_edit_call(start=3, end=3, new_text="THREE")),
        [markdown_tools.EDIT_MARKDOWN_TOOL],
        None,
        "seeded",
        None,
    ))
    assert handled is True
    assert reply == "继续"
    assert calls == []
    assert len(driver.prompts) == 1
