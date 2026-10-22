# 当前状态（每次收尾覆盖，保持 < 60 行）

> 摘要：工作区干净，本地与 `origin/main` 同步于 `36c9695`。无进行中任务、无阻塞。
> 更新于：2026-10-22 · HEAD `36c9695`

## 正在进行

无。

## 最近完成（按时间倒序）

1. **lint 修复**（commit `36c9695`）：远程 `abdcae9` 引入 ruff UP037（`from_payload` 返回注解带引号），
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

1. 可选：给「闭合块内裸换行」补一条显式回归用例到 `tests/test_malformed.py`。
2. 可选：`doc/update.md` 已有 340 行复盘，可把通用教训抽到 `lessons.md` 去重。
3. 可选：`doc/memory/project.md` 的模块行数会随开发漂移，收尾时顺手核对 `wc -l deepseek_web/*.py`。

## 基线（改动前后都应满足）

```bash
.venv/bin/python -m pytest -q          # 全绿
.venv/bin/ruff check .                 # 改动文件干净
python3 scripts/harness.py check       # 索引不断链
```
