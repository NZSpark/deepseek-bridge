# 当前状态（每次收尾覆盖，保持 < 60 行）

> 摘要：单围栏内并列多个裸 JSON tool_call 现已全部解析（此前只取第一个）；待提交推送。
> 更新于：2026-10-24 · HEAD `72e30b2`

## 正在进行

无。

## 最近完成（按时间倒序）

1. **单围栏多 tool_call 解析**（本次，未提交）：`_consume` 整体 `json.loads` 失败时退化为
   逐平衡对象解析；兜底段去掉 `break`，单个 marker 后的多个裸对象全部消费。
   覆盖围栏内并列裸对象 / 无围栏 marker + 多对象 / 重复对象 / JSON 数组回归，新增 2 条测试。
2. **移除代码块提取死代码**（本次，未提交）：`_extract_code_blocks`、`CODE_BLOCK_SELECTOR`、`CODE_TAG_SELECTOR`
   全部删除；`send_chat` / `_send_chat_locked` 返回 `str`，`complete_with_toolcall_retry` 返回 `str`，
   `responses.run_chat` 返回三元组。提取结果从未被任何客户端消费，属死代码。
   连带修 10 处测试 fake 的元组返回与 3 处解包；全量 pytest 绿、ruff 干净、harness 14 通过。
2. **按源码更新记忆**（commit `48d41e2`）：`project.md` 补全 19 个模块 / 7 条路由 / 真实环境变量名，
   `state.md` 覆盖重写。
3. **lint 修复**（commit `36c9695`）：远程 `abdcae9` 引入 ruff UP037（`from_payload` 返回注解带引号），
   `--fix` 修掉，行为不变。主干 lint 恢复绿色。
2. **Agent Harness 落地并推送**（commit `4645054`，rebase 到 `abdcae9` 后推送）：
   `AGENTS.md` 单一事实源 + `CLAUDE.md` / `.codex/instructions.md` 指针、
   `doc/harness.md` 路由表、四层记忆 `doc/memory/`、`doc/memory-cleanup.md`、`scripts/harness.py`。
   验证：`harness.py check` 14 项全过（断链注入能变红，退出码 1）；全量 pytest 绿；ruff 干净。
3. **Windows 11 安装脚本**（远程 `abdcae9`，已合并）：`scripts/setup_win11.ps1` + README 双平台说明。
4. **`start_all_bridges.sh`**（commit `7293613`）：从 `models.json` 动态解析 provider，
   start/stop/restart/status，CDP 两兄弟串行启动。
5. **DSML 判据 + 任务清单收口**（commit `09e1122`）：`DSML_CLAIM` 声称但救不出即判坏。
6. **skill 解除同族共享源**（v1.4.0，**本地改动，未进 git**）：删 `family.json` / `family_sync.py` /
   `test_family_sync.py` / 版本钉；门禁 R5 改为「必须真正自包含」；补 `scripts/__init__.py`；skill 测试 25 passed。

## 阻塞 / 待确认

- 无技术阻塞。
- 待确认：`skills/web-to-openai-api/` 仍未进版本库（见 lessons L05），skill 改动只算「本地已改」。

## 下一步（按优先级）

1. 提交并推送本次「单围栏多 tool_call 解析」。
2. **待决策：截断策略全盘优化**（已分析，未实施）。6 处截断点中 T1（`_seed_messages` 保最近）
   与 T2（`_clamp_prompt` 保头尾）方向相反，叠加会切碎最近上下文。建议先改 T2 为「按段保最近」，
   再调 `SEED_MAX_CHARS` 12000 → 32000。
3. 可选：给「闭合块内裸换行」补一条显式回归用例到 `tests/test_malformed.py`（L03 目前无测试钉住）。
3. 可选：`doc/tasks.md` 其余 `[ ]`（B2.3 错误码、H4 冒烟脚本、H6 验收记录）逐条按源码核实后处理。
4. 可选：`doc/update.md`（340 行）抽通用教训到 `lessons.md`。

## 基线（改动前后都应满足）

```bash
.venv/bin/python -m pytest -q          # 全绿
.venv/bin/ruff check .                 # 改动文件干净
python3 scripts/harness.py check       # 索引不断链
```
