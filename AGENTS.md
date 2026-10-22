# AGENTS.md · 唯一事实源

> **所有 AI 工具的唯一入口**。`CLAUDE.md`、`.codex/instructions.md` 都是指向这里的 3 行指针。
> 规则只在这一份文件里改；改别处等于制造分叉。

## 0. 开局做什么

1. 读本文件（约 120 行，一次读完）
2. 读 `doc/memory/state.md`（当前状态，< 60 行）
3. 按 `doc/harness.md` §3 路由表，**只**加载当前任务命中的文档

不要预读全部 `doc/`。不要猜——不确定就读路由表指的那一份。

## 1. 硬规则（违反必重演）

1. **不能确认成功，就不报告成功**。提交、生成结束、工具解析、页面落点都要有可观察判据。
2. **没跑过的验证项写「未验证」**，不得勾选、不得含糊成「应该没问题」。
3. **有行为变化必须配回归测试**；每条已修故障要有**能变红**的用例。
4. **文档与代码不一致时以代码为准，并立刻改文档**。
5. **报「已提交」前必须 `git ls-files` 确认该文件真的被跟踪**（`skills/` 是本地副本，不进 git）。
6. **同一事实只存一处**，其它地方引用路径，不复制正文。

## 2. 项目速览

DeepSeek 网页版 → OpenAI 兼容本地 API。FastAPI + Playwright + 持久化 Chromium。
监听 `127.0.0.1:8000`。**不用官方 API Key**。

```bash
.venv/bin/python deepseek_api_server.py    # 启动
.venv/bin/python -m pytest -q              # 全量测试（改动前后都应绿）
.venv/bin/ruff check .                     # lint（改动文件必须干净）
python3 scripts/harness.py check           # 索引校验
```

细节见 `doc/memory/project.md`；设计见 `doc/design.md`；施工图见 `doc/tasks.md`。

## 3. 知识与记忆放哪

| 内容 | 位置 |
| --- | --- |
| 架构与路由表 | `doc/harness.md` |
| 稳定项目事实 | `doc/memory/project.md` |
| 决策与被否方案 | `doc/memory/decisions.md` |
| 踩坑与判据 | `doc/memory/lessons.md` |
| 当前状态 / 下一步 | `doc/memory/state.md` |
| 过期但有用 | `doc/memory/archive/` |
| 设计 / 施工 / 验收 | `doc/design.md` `doc/tasks.md` `doc/acceptance.md` |
| 包网页为 API 的方法论 | `skills/web-to-openai-api/SKILL.md` |
| 运维脚本 | `scripts/start_all_bridges.sh` |
| 记忆清理机制 | `doc/memory-cleanup.md` |

## 4. 收尾必做

1. 跑基线：`pytest -q` 全绿、`ruff check` 干净、`harness.py check` 无断链
2. 写回记忆：状态 → `state.md`（覆盖）；取舍 → `decisions.md`（追加）；
   踩坑 → `lessons.md`（追加）。只写**验证过**的内容。
3. 提交时 commit message 写清「改了什么 / 为什么 / 怎么验证」

## 5. 不要做

- 不要在 `skills/` 里新增共享文件后声称「已提交」——它不进 git。
- 不要为同一规则在多个文件各写一份。
- 不要用固定 `sleep` 做结束判定；不要用轮询次数当超时阈值。
- 不要把临时尝试过程写进 `memory/`。
- 不要跳过路由表直接全量读 `doc/`。

## 6. 需要更细的信息时

按 `doc/harness.md` §3 路由表命中关键词 → 读那一份 → 不够再读它引用的文件。
