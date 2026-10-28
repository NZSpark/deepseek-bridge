# 当前状态（每次收尾覆盖，保持 < 60 行）

> 摘要：`edit_markdown` 阶段一至四已落地；本机 `.env` 已启用（LOCAL/WRITE 均为 true），需重启生效。
> 更新于：2026-10-29

## 正在进行

无。

## 最近完成（按时间倒序，详细过程见 git log）

1. **修复未转义引号的 tool_call 导致死循环（P0）**：`_iter_balanced_objects` 引号失衡时
   把整段原文原样吐出，`_consume` 拿同一段递归调用自己 → 请求挂死直到 worker 被杀。
   修法：只递归**严格更短**的片段。用例 `tests/test_unescaped_quotes.py`（6 条），
   修改前已确认挂死 >120s。
2. **`effective_tools()` 只在已有工具集时并入 `edit_markdown`（方案 A）**：
   原先只要 `EDIT_MARKDOWN_LOCAL=true` 就给**所有**请求注入工具说明（约 1200 字符/轮），
   污染纯聊天并让 5 个既有测试变红。现在未声明 tools 的请求不被注入。
3. **本机 `.env` 启用 edit_markdown**：6 个 `EDIT_MARKDOWN_*`，`LOCAL=true`、`WRITE=true`、
   `ROOT=.`、备份落 `output/backups`。`.env` 不进 git；**需重启服务才生效**。
4. **`edit_markdown` 阶段四**：`responses.py` 与流式入口接入 `run_local_tool_round()`
   （单轮回喂，多轮闭环仍在非流式 chat 路径）；`verify()` 补上选区内围栏校验；
   `tests/test_markdown_tools.py`（27 条）+ Responses 路径 3 条用例。
5. **单围栏多 tool_call 解析**、**移除死代码**、**Agent Harness**、**Windows 安装脚本**、
   **`start_all_bridges.sh`**、**DSML 判据收口**。
6. **skill 解除同族共享源**（v1.4.0，**本地改动，未进 git**）。

## 阻塞 / 待确认

- 无技术阻塞。
- `skills/web-to-openai-api/` 仍未进版本库（lessons L05）。
- **真机验证未做**：本地工具闭环与写盘尚未在真实网页上跑过。这是唯一空白项。

## 下一步（按优先级）

1. **重启服务使 `.env` 生效**，再做真机验证：`LOCAL=true/WRITE=false` 先看 diff，
   再开 `WRITE=true` 验备份与原子替换。
2. 若真机显示模型常需连续两轮以上编辑，再评估把多轮循环搬进流式 runner。
3. **待决策：截断策略全盘优化**（已分析未实施）。T1（`_seed_messages` 保最近）与
   T2（`_clamp_prompt` 保头尾）方向相反，叠加会切碎最近上下文。
   建议先改 T2 为「按段保最近」，再调 `SEED_MAX_CHARS` 12000 → 32000。
4. 可选：`doc/tasks.md` 其余 `[ ]` 逐条按源码核实后处理。

## 基线（改动前后都应满足）

```bash
.venv/bin/python -m pytest -q          # 全绿
.venv/bin/ruff check .                 # 干净
python3 scripts/harness.py check       # 14 通过，0 警告
```
