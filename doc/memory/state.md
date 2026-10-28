# 当前状态（每次收尾覆盖，保持 < 60 行）

> 摘要：`edit_markdown` 本地 Markdown 编辑能力阶段一至三已落地并提交；默认关闭，行为与之前一致。
> 更新于：2026-10-28 · HEAD `1d76966`

## 正在进行

无。

## 最近完成（按时间倒序）

1. **`edit_markdown` 本地 Markdown 编辑（阶段一至三，本次提交）**：
   - `deepseek_web/markdown_io.py`：纯文件引擎（字节读取 + 严格 UTF-8、围栏状态机、1-based 闭区间替换、
     unified diff、备份、临时文件原子替换）。
   - `deepseek_web/markdown_tools.py`：canonical schema、`effective_tools()` 合并、路径沙箱、
     双门槛权限、`execute_edit_markdown()`、`run_local_edit_markdown_loop()` 有限轮次执行。
   - `config.py` / `.env.example`：`EDIT_MARKDOWN_LOCAL|WRITE|ROOT|BACKUP_DIR|MAX_FILE_BYTES|MAX_STEPS`，
     默认 `LOCAL=false`、`WRITE=false`。
   - `server.py` / `prompting.py`：接入有效工具集合与本地执行循环；默认关闭时完全走旧路径。
   - `tests/test_markdown_io.py`：14 条用例（替换 / 删除 / 越界 / CRLF / 围栏 / diff / 备份 / 原子替换）。
   - `doc/edit_markdown.md`：状态改为「阶段一至三已实现」，明确未实现与未验证项。
   - 验证：全量 pytest 绿、`ruff check` 干净、`harness.py check` 14 通过。真机多轮闭环**未验证**。
2. **单围栏多 tool_call 解析**（已包含在本次提交前的基线中）：兜底段去掉 `break`，
   单个 marker 后的多个裸对象全部消费；新增 2 条测试。
3. **移除代码块提取死代码**：`_extract_code_blocks` 等删除，返回类型收敛为 `str`。
4. **按源码更新记忆**（`48d41e2`）、**lint 修复**（`36c9695`）、
   **Agent Harness 落地**（`4645054`）、**Windows 11 安装脚本**、**`start_all_bridges.sh`**（`7293613`）、
   **DSML 判据收口**（`09e1122`）。
5. **skill 解除同族共享源**（v1.4.0，**本地改动，未进 git**）：skill 测试 25 passed。

## 阻塞 / 待确认

- 无技术阻塞。
- `skills/web-to-openai-api/` 仍未进版本库（lessons L05），skill 改动只算「本地已改」。

## 下一步（按优先级）

1. `edit_markdown` 阶段四：把同一执行服务接入 `responses.py` 与流式入口，避免各入口各写一套权限/写盘逻辑。
2. `edit_markdown` 补齐 `tests/test_markdown_tools.py`（schema / 沙箱 / 权限双门槛 / 混合批次 / 步数上限）
   与 `verify()` 接入执行路径。
3. 真机验证：Playwright + 真实 DeepSeek 网页下多轮本地工具闭环与写盘（当前**未验证**）。
4. **待决策：截断策略全盘优化**（已分析，未实施）。T1（`_seed_messages` 保最近）与 T2（`_clamp_prompt` 保头尾）
   方向相反，叠加会切碎最近上下文。建议先改 T2 为「按段保最近」，再调 `SEED_MAX_CHARS` 12000 → 32000。
5. 可选：`doc/tasks.md` 其余 `[ ]`（B2.3 错误码、H4 冒烟脚本、H6 验收记录）逐条按源码核实后处理。

## 基线（改动前后都应满足）

```bash
.venv/bin/python -m pytest -q          # 全绿
.venv/bin/ruff check .                 # 干净
python3 scripts/harness.py check       # 14 通过
```
