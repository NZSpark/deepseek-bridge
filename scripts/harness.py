#!/usr/bin/env python3
"""Agent Harness 校验与查询工具。退出码即结论：0 全过 / 1 有问题 / 2 跑不起来。

子命令：
  check   校验索引不断链、摘要不超长、必需文件存在（默认）
  state   打印当前状态
  index   打印路由表

设计原则：只做机械检查，不做语义判断。语义留给人和 AI。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "doc" / "harness.md"

# 必需文件：(路径, 说明, 最大行数上限或 None)
REQUIRED = (
    ("AGENTS.md", "唯一事实源", 200),
    ("CLAUDE.md", "Claude Code 指针", 20),
    (".codex/instructions.md", "Codex 指针", 20),
    ("doc/harness.md", "架构与索引", 200),
    ("doc/memory/project.md", "项目知识", 200),
    ("doc/memory/decisions.md", "决策记录", 400),
    ("doc/memory/lessons.md", "历史经验", 400),
    ("doc/memory/state.md", "当前状态", 60),
)

# 摘要行：memory/*.md 头部 `> 摘要：…`，用于渐进式加载
SUMMARY_RE = re.compile(r"^>\s*摘要[：:]\s*(.+)$", re.MULTILINE)
SUMMARY_MAX_CHARS = 200

# 索引里出现的反引号路径（`doc/xxx.md` / `scripts/xxx.py` 之类）
PATH_RE = re.compile(
    r"`((?:doc|scripts|deepseek_web|tests|skills)/[A-Za-z0-9_./-]+\.(?:md|py|sh))`"
)

results: list[tuple[str, str, str]] = []  # (状态, 标题, 证据)


def record(status: str, title: str, evidence: str = "") -> None:
    results.append((status, title, evidence))


def check_required() -> None:
    for rel, desc, max_lines in REQUIRED:
        path = ROOT / rel
        if not path.exists():
            record("FAIL", f"必需文件 {rel}", f"不存在（{desc}）")
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if max_lines and lines > max_lines:
            record("WARN", f"{rel} 行数", f"{lines} > {max_lines}，考虑压缩或归档")
        else:
            record("PASS", f"必需文件 {rel}", f"{lines} 行")


def check_summaries() -> None:
    mem = ROOT / "doc" / "memory"
    if not mem.is_dir():
        record("FAIL", "doc/memory/ 目录", "不存在")
        return
    for path in sorted(mem.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        m = SUMMARY_RE.search(text)
        if not m:
            record("WARN", f"{path.name} 摘要", "缺 `> 摘要：…` 行，影响按需加载")
            continue
        n = len(m.group(1))
        status = "WARN" if n > SUMMARY_MAX_CHARS else "PASS"
        record(status, f"{path.name} 摘要", f"{n} 字符（上限 {SUMMARY_MAX_CHARS}）")


def check_links() -> None:
    """校验 harness.md 与 AGENTS.md 里反引号引用的文件都存在。"""
    seen: set[str] = set()
    for src in (HARNESS, ROOT / "AGENTS.md"):
        if not src.exists():
            continue
        for rel in PATH_RE.findall(src.read_text(encoding="utf-8")):
            if rel in seen:
                continue
            seen.add(rel)
            # skills/ 是本地副本，允许缺失
            if rel.startswith("skills/") and not (ROOT / rel).exists():
                record("WARN", f"引用 {rel}", "不存在（skills/ 是本地副本，可忽略）")
                continue
            if not (ROOT / rel).exists():
                record("FAIL", f"引用 {rel}", f"在 {src.name} 里被引用但不存在（断链）")
    if seen:
        record("PASS", "索引引用", f"检查 {len(seen)} 个路径")


def check_routing_table() -> None:
    """harness.md 必须有 §3 路由表且含若干关键触发词。"""
    if not HARNESS.exists():
        record("FAIL", "路由表", "doc/harness.md 不存在")
        return
    text = HARNESS.read_text(encoding="utf-8")
    if "## 3. 路由表" not in text and "## 3. 路由表（按需加载）" not in text:
        record("FAIL", "路由表", "doc/harness.md 缺 §3 路由表")
        return
    must = ("AGENTS.md", "memory/state.md", "memory/decisions.md", "memory/lessons.md")
    missing = [k for k in must if k not in text]
    if missing:
        record("WARN", "路由表覆盖", f"未提及：{', '.join(missing)}")
    else:
        record("PASS", "路由表", "含核心入口")


def cmd_check() -> int:
    check_required()
    check_summaries()
    check_links()
    check_routing_table()

    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    for status, title, evidence in sorted(results, key=lambda r: order[r[0]]):
        mark = {"PASS": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[status]
        print(f"{mark} {title}" + (f" — {evidence}" if evidence else ""))

    fails = sum(1 for s, _, _ in results if s == "FAIL")
    warns = sum(1 for s, _, _ in results if s == "WARN")
    print(f"\n结论：{fails} 个失败，{warns} 个警告，{len(results) - fails - warns} 个通过")
    return 1 if fails else 0


def cmd_state() -> int:
    path = ROOT / "doc" / "memory" / "state.md"
    if not path.exists():
        print("doc/memory/state.md 不存在", file=sys.stderr)
        return 2
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_index() -> int:
    """打印 harness.md 的 §3 路由表。"""
    if not HARNESS.exists():
        print("doc/harness.md 不存在", file=sys.stderr)
        return 2
    lines = HARNESS.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    on = False
    for ln in lines:
        if ln.startswith("## 3."):
            on = True
        elif on and ln.startswith("## "):
            break
        if on:
            out.append(ln)
    print("\n".join(out) if out else "未找到 §3 路由表")
    return 0 if out else 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    try:
        if cmd == "check":
            return cmd_check()
        if cmd == "state":
            return cmd_state()
        if cmd == "index":
            return cmd_index()
    except Exception as exc:  # noqa: BLE001
        print(f"harness.py 运行失败：{exc}", file=sys.stderr)
        return 2
    print(f"未知子命令：{cmd}（check | state | index）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
