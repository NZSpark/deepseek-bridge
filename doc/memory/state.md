# 当前状态（每次收尾覆盖，保持 < 60 行）

> 摘要：`edit_markdown` 本地 Markdown 编辑能力阶段一至四已落地；默认关闭，行为与之前一致。
> 更新于：2026-10-28

## 正在进行

无。

## 最近完成（按时间倒序）

1. **`edit_markdown` 阶段四 + 测试补齐（本次改动）**：
   - `responses.py`：`to_chat_request` 走 `effective_tools()`；`run_chat` 在解析后调用
     `run_local_tool_round()` 做**单轮**执行+回喂。非流式与流式两个入口自动共享。
   - `streaming.py`：`_stream_chat_completion` 的 runner 内，命中本地批次后单轮回喂。
   - `markdown_tools.py`：抽出 `local_round_eligible()` / `_render_results()` /
     `_continuation_prompt()` / `step_limit_reply()` / `run_local_tool_round()`；
     原多轮循环改为复用这些片段，行为不变。
   - `markdown_io.py`：`verify()` 现在**同时**校验选区外与选区内的围栏字节；
     修复「只改开围栏行会把围栏代码变成正文却不报错」的真实缺口。
   - `tests/test_markdown_tools.py`（新增 27 条）：沙箱（绝对路径/`..`/非 md/不存在/目录/超限）、
     参数类型、双门槛权限（含备份与 no-op 不写盘）、围栏破坏、`effective_tools` 劫持防护、
     混合批次不执行、步数上限、单轮 helper。
   - `tests/test_responses.py`：新增 `SequencedDriver` 与 `LocalToolRoundTests`（2 条），
     覆盖 Responses 路径本地执行与关闭模式保持原路径。
   - 验证：全量 pytest 绿、`ruff check` 干净、`harness.py check` 14 通过。
     真机多轮闭环与写盘**仍未验证**。
2. **`edit_markdown` 阶段一至三**：`markdown_io.py` 纯文件引擎、`markdown_tools.py` 沙箱与多轮循环、
   `EDIT_MARKDOWN_*` 配置、`tests/test_markdown_io.py` 14 条。
3. **单围栏多 tool_call 解析**：兜底段去掉 `break`，单个 marker 后多个裸对象全部消费。
4. **移除代码块提取死代码**：`_extract_code_blocks` 等删除，返回类型收敛为 `str`。
5. **按源码更新记忆**、**lint 修复**、**Agent Harness 落地**、**Windows 11 安装脚本**、
   **`start_all_bridges.sh`**、**DSML 判据收口**。
6. **skill 解除同族共享源**（v1.4.0，**本地改动，未进 git**）：skill 测试 25 passed。

## 阻塞 / 待确认

- 无技术阻塞。
- `skills/web-to-openai-api/` 仍未进版本库（lessons L05），skill 改动只算「本地已改」。

## 下一步（按优先级）

1. 真机验证：Playwright + 真实 DeepSeek 网页下多轮本地工具闭环与写盘（当前**未验证**）。
   建议顺序：`LOCAL=true`/`WRITE=false` 看 diff → 再开 `WRITE=true` 验备份与原子替换。
2. 流式入口目前只做**单轮**回喂（避免把多轮循环塞进一次缓冲的上游调用）。
   若真机显示模型常需连续两轮以上编辑，再评估把多轮循环搬到流式 runner 内。
3. **待决策：截断策略全盘优化**（已分析，未实施）。T1（`_seed_messages` 保最近）与
   T2（`_clamp_prompt` 保头尾）方向相反，叠加会切碎最近上下文。
   建议先改 T2 为「按段保最近」，再调 `SEED_MAX_CHARS` 12000 → 32000。
4. 可选：`doc/tasks.md` 其余 `[ ]`（B2.3 错误码、H4 冒烟脚本、H6 验收记录）逐条按源码核实后处理。

## 基线（改动前后都应满足）

```bash
.venv/bin/python -m pytest -q          # 全绿
.venv/bin/ruff check .                 # 干净
python3 scripts/harness.py check       # 14 通过
```
