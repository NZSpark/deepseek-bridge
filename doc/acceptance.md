# 验收记录（2026-10-13 / 工作区未提交，HEAD `1307d4e`）

本轮改动（32 项：29 个修改 + 3 个新增）：
1. **静态检查口径落地**：`pyproject.toml` 从 `uv init` 骨架补上 `[tool.ruff]`。
   此前**根本没有 ruff 配置**，`ruff check .` 走的是 ruff 0.17 的浮动默认集
   （不带配置时它默认就启用 400+ 条规则，跨版本会漂），于是 R1 报出 350 条。
   现在显式显死 `select = ["ALL"]` + `preview = false`，并按「豁免必须写明理由」逐条列出
   **28 条**豁免（中文标点 / 注解与 docstring 迁移 / unittest 风格 / 复杂度阈值…），
   另有 1 条 per-file 豁免（`tests/e2e/` 的 HTTP 客户端：scheme 已在 `open_url()` 里真校验）。
2. **把剩余 428 条逐条修掉**（325 条安全自动修复 + 103 条手写）：`Optional[X]→X | None`、
   `List/Dict→list/dict`、`try/except/pass→contextlib.suppress`、嵌套 `with` 合并、
   `hashlib.sha1(..., usedforsecurity=False)`、FastAPI 依赖改 `Annotated`、
   异常名补 `Error` 后缀、魔数提常量、`raise ... from ...`、异常收窄（`OSError` /
   `json.JSONDecodeError` / `UnicodeDecodeError`）。
3. **联网登录路径加显式开关**：新增 `tests/e2e/`（默认整包 skip，`DEEPSEEK_E2E=1` 才跑）
   与 `doc/e2e_test_design.md`。
4. **修掉 skill 自己的一个假 PASS**：跑门禁时 D4「路线决策有留档」居然 PASS，
   而它匹配到的只是本文件里「本轮没跑 recon」这句**否定陈述**。
   旧判据是「文档里出现 `路线` 或 `recon` 就算留档」，属于典型的假 PASS。
   已在共享源里改成必须写出**选了哪条**（形如 `路线：<A|B|C>`，占位符写法不算），
   并补了回归测试（只提 recon → SKIP；写了真实选择 → PASS；对着旧判据跑第一条必红）。

   > 说明：本文件里的路线写法一律用**占位符**（`<A|B|C>`）。
   > 真写了具体值（且门禁据此判 PASS）等于在验收记录里伪造一个没做过的决策 —— 不做。

   共享源 `1.2.3#6ac0a079` → **`1.2.4#b1a19e65`**，已同步到四个成员仓库。

## 环境

- 服务：`http://127.0.0.1:8000`（**已有实例**，`HEADLESS=false`，`browser_ready=true`）
- Python 3.14.5 / playwright 1.63.0 / Chromium `chromium-1243`
- 客户端：`urllib`（标准库）+ `curl`

## 结果

| 项 | 命令 | 结果 | 证据 |
| --- | --- | --- | --- |
| S1 静态检查 | `.venv/bin/ruff check .` | ✅ | `All checks passed!`（rc=0；配置 = `select=["ALL"]` + 写明理由的豁免） |
| S2 离线单测 | `.venv/bin/python -m pytest -q` | ✅ | **304 passed / 0 failed / 5 skipped**（skip = 真机 E2E，默认不跑） |
| S3 skill 自带回归 | `pytest skills/web-to-openai-api/tests -q` | ✅ | **21 passed / 0 failed**（含可移植性、门禁假 PASS 护栏、D4 判据回归）；五个仓库同版本均 21/0 |
| S4 模板区分力 | `check_templates.py` | ✅ | 「结论：通过」，抽出用例 25 条 |
| S5 四阶段门禁 | `gate.py --root .` | ✅ | **通过 14 / 失败 0 / 跳过 2**（D4、V1 如实 SKIP），`rc=0`；家族钉 `1.2.4#b1a19e65` 一致 5/0 |
| A1 就绪 | `curl /healthz` | ✅ | `{"status":"ok","browser_ready":true,"headless":false}` |
| A2 模型发现 | `tests/e2e` → `test_a2` | ✅ | 列表含 `deepseek-chat` |
| A6/A7 错误体 | `tests/e2e` → `test_a6` | ✅ | 空 `messages` → **400**，体为 `{"error":{message,type,code}}` |
| A3 非流式契约 | `tests/e2e` → `test_a3` | ✅ | `content` 非空 + `finish_reason="stop"` + `usage.total_tokens>0` |
| A4 流式契约 | `tests/e2e` → `test_a4` | ✅ | 出现 `data: [DONE]`；每帧 ≤ `CHUNK_CHARS`，帧数 = `ceil(len/CHUNK_CHARS)` |

真机命令与耗时：

```bash
DEEPSEEK_E2E=1 DEEPSEEK_E2E_BASE_URL=http://127.0.0.1:8000 \
  .venv/bin/python -m pytest tests/e2e -v
# → 5 passed in 50.55s
```

> 第一次真机跑时 A4 失败，原因是**我自己的断言写错了**（要求「帧数 > 1」），
> 而不是 bridge 有问题：`streaming.py` 的 `_chunk_text` 按 64 字符切片，短回复只有 1 帧
> 属正常。已改成不依赖回复长度的确定性判据（每帧 ≤ 切片大小 + 帧数 = 向上取整），
> 并把那个 64 提成具名常量 `CHUNK_CHARS`。**这条按「修因不修表」处理，断言没有被削弱。**

## 未通过 / 未能验证

- **D4 路线决策留档**：本轮**没有**对真实站点跑 `recon.py --network-log`，
  因此没有新的 A/B/C 路线结论可写 —— 如实标为未验证，不当成通过。
- **真机 E2E 覆盖面**：只覆盖「单轮非流式 + 单轮流式 + 错误体 + 就绪 + 模型发现」。
  以下真机路径**本轮未验证**：会话轮转与播种（turns 到顶后重放历史）、
  页面丢失自愈（关标签后重建）、工具调用（`tool_calls`）、`/v1/responses`（Codex CLI 路径）、
  多桶并发、到顶提示识别。这些有离线回归，但离线不等于真机对等。
- **无头模式**：`HEADLESS=1` 在真机上是否会被挑战页拦截，本轮未测（本次实例是有头）。
- **`usedforsecurity=False`**：`/diagnostics/dom` 的 sha1 改动只由离线测试与 import 覆盖，
  未在真机上调用该诊断端点。

## 已知限制

- `usage` 是**估算值**（按字符估算），不是上游 token 计数；流式是 **SSE 合成流**
  （单轮生成结束后按 64 字符切片），不是上游 token 流。
- 真机层依赖运行中的 bridge 与有效登录态；测试自己不起服务（登录必须人工完成）。
- `BLE001`（盲捕 `Exception`）没有被消干净，如实记数：`deepseek_web/` 现有 **43 处**
  `except Exception`，其中 **37 处**带 `# noqa: BLE001`（其余 6 处是 re-raise 型，ruff 本身不报）。
  本次**删掉 11 处**（改成 `contextlib.suppress` 或收窄到 `OSError` / `UnicodeDecodeError` /
  `json.JSONDecodeError`），**新增 8 处**并逐条写明理由。保留的理由统一是：本项目**刻意**不 import
  Playwright 的异常类（见 `page_pool.py` 头注释），页面随时可能被关掉/导航，
  这些兜底必须收成安全默认值 —— 收窄类型会改变运行时行为，属于该规则不适用的场景。
- `pyproject.toml` 的 `[project].dependencies` 仍是 `uv init` 骨架（把 pytest/ruff 当运行时依赖、
  缺 fastapi/playwright/pydantic）。本轮**未改**：改它会与已提交的 `uv.lock` 失配，
  需要单独同步 lock，不在本次范围内。
