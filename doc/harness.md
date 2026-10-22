# Agent Harness · 架构与索引

> **入口文件**。新会话 / 新工具进入项目，先读这一份，再按 §3 路由表按需加载。
> 本文只放「怎么找到知识」和「知识放哪」，具体知识在 `doc/memory/` 与既有 `doc/*.md` 里。

---

## 1. 设计原则（为什么是这样）

| 原则 | 落地方式 |
| --- | --- |
| 单一事实源 | 规则只写在 `AGENTS.md`。`CLAUDE.md` / `.codex/instructions.md` 都是 3 行指针，不重复内容 |
| 按变化频率分层 | 稳定事实 / 只追加决策 / 只追加经验 / 高频状态，四个文件互不污染 |
| 渐进式加载 | 启动只读 `AGENTS.md` + `memory/state.md`；命中路由表才展开对应文档 |
| 机械校验 | `scripts/harness.py check` 验索引不断链、摘要不超长；退出码即结论 |
| 不重复建设 | 复用现有 `doc/*.md`（design / tasks / acceptance / toolcall_retry…），不另写一份 |

**明确的非目标**：不做向量检索、不做自动摘要生成、不引入数据库。全部是 Markdown + 一个 100 行量级的校验脚本。

---

## 2. 文件职责

```
项目根
├── AGENTS.md                    # 唯一事实源：硬规则 + 入口 + 路由表（~120 行）
├── CLAUDE.md                    # 指针 → AGENTS.md（Claude Code）
├── .codex/instructions.md       # 指针 → AGENTS.md（Codex）
├── doc/
│   ├── harness.md               # 本文件：架构与索引
│   ├── memory/
│   │   ├── project.md           # 项目知识（技术栈、目录、入口、运行方式）— 少变
│   │   ├── decisions.md         # 决策记录（ADR 式，只追加，含被否方案）
│   │   ├── lessons.md           # 历史经验与踩坑（只追加，含可复现判据）
│   │   ├── state.md             # 当前状态（每次收尾覆盖，保持 < 60 行）
│   │   └── archive/             # 过期但仍有参考价值的内容
│   ├── design.md tasks.md …     # 既有设计 / 施工图 / 验收（不动）
│   └── harness_prompt.md        # 本次建设的需求原文（存档）
├── skills/web-to-openai-api/    # 自包含 skill（本地副本，不进 git）
└── scripts/
    ├── start_all_bridges.sh     # 一键启停所有 bridge
    └── harness.py               # 索引校验 + 状态查询（check / state / index）
```

---

## 3. 路由表（按需加载）

**先读 `AGENTS.md` + `memory/state.md`**。下表中「触发」命中时才读对应文件，不要全量预读。

| 触发（任务类型 / 关键词） | 读这些 | 不要读 |
| --- | --- | --- |
| 任何任务开局 | `AGENTS.md`、`memory/state.md` | 其余全部 |
| 改代码 / 加功能 | + `memory/project.md`、`doc/design.md`、`doc/tasks.md` | `archive/` |
| 决定方案 / 选型 / 取舍 | + `memory/decisions.md` | `acceptance.md` |
| 排错 / 复盘 / 回归变红 | + `memory/lessons.md`、`doc/update.md` | `prompt_design.md` |
| tool_call 坏结构 / 重发 | + `doc/toolcall_retry.md`、`doc/toolcall_retry_tasks.md` | 其它 |
| 流式 / SSE / Codex | + `doc/codex_support.md` | `image_upload.md` |
| 图片上传 / 多模态 | + `doc/image_upload.md` | `codex_support.md` |
| 提示词注入 / 工具描述 | + `doc/prompt_design.md` | `troubleshooting` 类 |
| 验收 / 交付 / 门禁 | + `doc/acceptance.md`、`doc/e2e_test_design.md` | `archive/` |
| 包网页 → OpenAI API（新站点） | + `skills/web-to-openai-api/SKILL.md` | 本项目 doc |
| 启停 / 运维所有 bridge | + `scripts/start_all_bridges.sh` 头部注释 | 其它 |
| 性能 / token 速度 | + `doc/tokenspeed.md` | — |
| 记忆清理 / 整合 | + `doc/memory-cleanup.md` | — |

**Token 预算**：开局 ≤ 200 行；单次任务命中路由后追加 ≤ 400 行。超了说明路由表太粗，改路由表而不是硬读。

---

## 4. 记忆写回机制

任务收尾时按此判断写到哪（**先改现有条目，不无限追加**）：

| 这次产生了什么 | 写到哪 | 规则 |
| --- | --- | --- |
| 当前做到哪 / 下一步 | `memory/state.md` | **覆盖重写**，保持 < 60 行 |
| 稳定事实变了（入口、目录、命令） | `memory/project.md` | 就地改，不新增章节 |
| 做了取舍 / 否掉了方案 | `memory/decisions.md` | 追加一条，含「为什么否掉」 |
| 踩了坑 / 找到根因 | `memory/lessons.md` | 追加一条，含**可复现判据** |
| 设计/验收细节 | 既有 `doc/*.md` | 改对应文档，不新建 |
| 过期但可能有用 | `memory/archive/` | 从原处移走并在索引里标注 |

**纪律**：
- 只写经过验证的内容；未验证写「未验证」。
- 临时过程信息（中间尝试、失败命令）不进核心记忆。
- 同一事实只存一处，其它地方引用路径。

---

## 5. 跨工具兼容

| 工具 | 读哪个入口 | 内容 |
| --- | --- | --- |
| Pi / 通用 Agent | `AGENTS.md` | 唯一事实源 |
| Claude Code | `CLAUDE.md` → `AGENTS.md` | 3 行指针 |
| Codex | `.codex/instructions.md` → `AGENTS.md` | 3 行指针 |
| 人类 | `README.md` + 本文件 | 项目说明与架构 |

**写入冲突规则**：`AGENTS.md` 与 `memory/*.md` 是共享可写；`doc/design.md` 等既有文档按各自标题里的职责写；`skills/` 是本地副本，不进 git，改动不报告为「已提交」。

---

## 6. 校验与维护

```bash
python3 scripts/harness.py check    # 索引断链 / 摘要超长 / 必需文件缺失 → 退出码即结论
python3 scripts/harness.py state    # 打印当前状态（等价于 cat memory/state.md）
python3 scripts/harness.py index    # 打印路由表
```

**Cleanup 机制**见 `doc/memory-cleanup.md`：日常轻量清理在任务收尾时做，深度清理按需手动触发。
