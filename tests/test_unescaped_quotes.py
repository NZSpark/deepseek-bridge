"""内层双引号未转义的 tool_call：既不能死循环，也必须被判为损坏。"""

from __future__ import annotations

import pytest

from deepseek_web.malformed import has_malformed_tool_call, has_no_usable_call
from deepseek_web.toolcalls import parse_tool_calls

# 真实故障样本：shell 命令里的引号没有转义成 \"
BAD = (
    '```tool_call\n'
    '{"name": "bash", "arguments": {"command": "cd /tmp && grep -rn '
    '"deepProbe\\|AI-008D" app | head -50"}}\n'
    '```'
)

GOOD = (
    '```tool_call\n'
    '{"name": "bash", "arguments": {"command": '
    '"cd /tmp && grep -rn -e deepProbe app | head -50"}}\n'
    '```'
)


def test_bad_quotes_do_not_recurse_forever() -> None:
    """曾是 RecursionError：_iter_balanced_objects 产出原文导致自递归。"""
    assert parse_tool_calls(BAD, {"bash"}) == []


def test_bad_quotes_detected_as_malformed() -> None:
    assert has_malformed_tool_call(BAD) is True
    assert has_no_usable_call(BAD, {"bash"}) is True


def test_good_command_still_parses() -> None:
    calls = parse_tool_calls(GOOD, {"bash"})
    assert [c["name"] for c in calls] == ["bash"]
    assert "deepProbe" in calls[0]["arguments"]["command"]
    assert has_malformed_tool_call(GOOD) is False


@pytest.mark.parametrize(
    "text",
    [
        # 单引号/嵌套对象/数组里的同类问题
        '{"name": "bash", "arguments": {"command": "echo "hi""}}',
        '{"name": "a", "arguments": {"x": "b"c"}}',
        # 完全不闭合
        '{"name": "bash", "arguments": {"command": "abc}',
    ],
)
def test_other_broken_shapes_do_not_hang(text: str) -> None:
    assert parse_tool_calls(text, {"bash"}) == []
