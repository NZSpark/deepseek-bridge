# 施工图 · 坏结构重发任务清单（toolcall_retry_tasks.md）

> 依据：**[toolcall_retry.md](toolcall_retry.md)**（设计文档）。
> 本文件把设计拆成**可按序执行、每条有可验证验收**的编码任务。
> 状态按当前代码实际能力标注，**不按计划标注**——过时的 `[x]` 比没有更坏。

---

## 图例与纪律

- `[x]` 已落地且有测试覆盖 · `[~]` 部分落地 / 有已知缺口 · `[ ]` 未开始
- **验收** = 完成标准（能跑出来的判定）；**回归** = 改动后必跑、必须仍绿的检查
- 纪律：① 有行为变化必须配回归测试；② 每条判据有**能变红**的用例；
  ③ 未验证就写「未验证」，不写「应该没问题」；④ 共享组件只调用不改签名。

**当前基线**（改动前后都应满足）：

```bash
.venv/bin/python -m pytest tests -q   # 全绿（369 用例；E2E 需真机的不在默认收集内）
.venv/bin/ruff check .                # 本次改动文件必须干净
```

**前置事实**（已核实，据此拆任务）：

- `toolcalls.py` 已有 `_normalize_json_text`（第 127 行），解析层与判据共用同一张全角映射。
- `parse_tool_calls` 三处调用点已全部改走 `complete_with_toolcall_retry`：
  `server.py:509`、`streaming.py:176`、`responses.py:294`。
- `config.MALFORMED_RETRIES = env_int("MALFORMED_RETRIES", 2)` 已存在（第 131 行）。
- `malformed.py` 已导出：`has_unclosed_tool_fence` / `has_no_usable_call` /
  `has_malformed_tool_call` / `DSML_CLAIM` / `RETRY_PROMPT`。

---

## T1 · 判据内核（纯函数，无 IO）

**目标**：把设计 §3 的判据做成纯函数，与解析层共用「合法调用」口径。

### T1.1 全角归一能力对齐 — `[x]`
- [x] `toolcalls.py:127` 提供 `_normalize_json_text`（ASCII↔全角映射）。
- [x] `malformed._loads_lenient` 先 `json.loads`，失败再归一重试——与解析层同口径。
- **验收**：一段全角渲染的合法调用，`parse_tool_calls` 能读出、判据也判合法。
- **回归**：`test_malformed.py` 内由 `_loads_lenient` 路径覆盖。
- **变红条件**：判据不做归一 → 全角好调用被判坏。

### T1.2 `_is_valid_call(body)` — `[x]`
- [x] `malformed.py:63`：`json.loads` 成 dict + 非空字符串 `name` + dict `arguments`。
- **验收**：空 / 截断 / 语法错 / 非对象 / 缺字段 全部 `False`；合法调用 `True`。
- **回归**：`test_malformed.py::HasNoUsableCallTests`（空正文、截断、尾逗号、裸标记、好调用）。
- **变红条件**：宽松放过尾逗号 / 单引号。

### T1.3 `has_unclosed_tool_fence(text)` — `[x]`
- [x] `malformed.py:30` + `TOOL_CALL_OPEN` 用 `(?![0-9A-Za-z_])`（不要求换行）。
- **验收**：无闭合 → `True`；同行 ` ```tool_call{...} ` 无闭合 → `True`；闭合的 `python` 围栏 → `False`。
- **回归**：`test_malformed.py::UnclosedFenceTests`（未闭合 / 同行开 / 已闭合 / python 围栏不计）。
- **变红条件**：要求必须换行 → 同行情形漏判。

### T1.4 `_iter_bodies(text)` — `[x]`
- [x] `malformed.py:76`：显式围栏优先；有显式围栏时不再重复扫裸标记。
- **验收**：同一段不被数两次。
- **回归**：由 `HasNoUsableCallTests::test_bare_marker_empty_is_bad` / `test_mixed_good_and_bad_is_ok` 覆盖。

### T1.5 `has_no_usable_call(text, valid_names)`（整轮版）— `[x]`
- [x] `malformed.py:87`：未闭合围栏优先 → `True`；所有段都不可用才 `True`；任一段可用 → `False`；无任何标记 → `False`。
- **验收**：「好的 + 坏的」混杂 → `False`（不误杀）；全坏 → `True`。
- **回归**：`HasNoUsableCallTests`（≥8 例）。
- **变红条件**：整轮判据用逐段逻辑 → 好坏混杂时误杀好调用。

### T1.6 `has_malformed_tool_call(text)`（逐段版）— `[x]`
- [x] `malformed.py:120`：任一段非法即 `True`。
- **验收**：好坏混杂 → `True`（逐段口径）；单独合法段 → `False`。
- **回归**：`MalformedPerSegmentTests`。

### T1.7 DSML 短路（能救不判坏）— `[x]`
- [x] `has_no_usable_call` 内先 `parse_tool_calls(text, valid_names)`，能救出任何调用就 `return False`。
- **验收**：可解析 DSML invoke → `False`；截断但 `invoke`/`parameter` 完整 → `False`。
- **回归**：`DsmlMalformedTests::test_parsable_dsml_is_ok` / `test_truncated_but_parsable_dsml_is_ok`。
- **变红条件**：判据把 DSML 一律当损坏 → 与解析层打架。

### T1.8 DSML 声称但救不出 → 判坏 — `[x]`
- [x] `malformed.py:19` `DSML_CLAIM` 匹配 `<…DSML… calls>` / `<…DSML… invoke>`（竖线 `[｜|]{1,4}`）。
- [x] 短路之后仍见到 `DSML_CLAIM` → `has_no_usable_call` / `has_malformed_tool_call` 均判坏。
- **验收**：`calls` 空壳、`invoke` 缺 `name`、参数被换行截断成非法 JSON → 全部 `True`。
- **回归**：`DsmlMalformedTests`（7 例，含 3 正例不误判）。
- **变红条件**：判据完全不看 DSML → 真实坏形态静默交给客户端。

---

## T2 · 配置与常量

### T2.1 `MALFORMED_RETRIES` — `[x]`
- [x] `config.py:131`：`env_int("MALFORMED_RETRIES", 2)`，注释说明 0=关闭。
- **验收**：未设时为 2；设 0 时重发循环跳过。
- **回归**：由 `test_toolcall_retry_flow.py` 的重发次数断言间接覆盖。

### T2.2 `RETRY_PROMPT` 与 `MalformedToolCallError` — `[x]`
- [x] `malformed.py:28` 定义 `RETRY_PROMPT`（正面格式 + 闭合要求，**不写 DSML 字面形状**）。
- [x] `errors.py:29` 定义 `MalformedToolCallError(RuntimeError)`。
- **验收**：`RETRY_PROMPT` 含 ` ```tool_call ` 正面示例，不含 `DSML` 字样。
- **回归**：`RetryPromptTests`（含 `test_does_not_show_dsml_shape`）。
- **变红条件**：提示词写出原生标记 → 变成诱导（P0-N）。

---

## T3 · 重发循环

### T3.1 `complete_with_toolcall_retry(...)` — `[x]`
- [x] `toolcall_retry.py:21`：先 `send_chat`；`wants_tools` 为假直接返回；
  `while has_no_usable_call(...) and retries < MALFORMED_RETRIES` 重发；耗尽抛 `MalformedToolCallError`。
- **验收**：坏回复触发重发后拿到好回复 → 返回好回复；重发耗尽 → 抛出。
- **回归**：`test_toolcall_retry_flow.py::test_bad_then_good_retries_once` / `test_exhausted_raises`。
- **变红条件**：重发耗尽仍静默返回坏文本。

### T3.2 重试轮不带 images — `[x]`
- [x] 重发调用 `send_chat(RETRY_PROMPT, key=...)` 不传 `images`。
- **验收**：假 driver 记录两次调用：首轮有图、重试轮为空。
- **回归**：`test_toolcall_retry_flow.py::test_retry_has_no_images`。

### T3.3 只在 `wants_tools` 时启用 — `[x]`
- [x] `wants_tools=False` 时 `send_chat` 只调用一次，不重发。
- **验收**：纯文本请求出现 `tool_call` 字样不触发重发。
- **回归**：`test_toolcall_retry_flow.py::test_not_wants_tools_no_retry`。

---

## T4 · 三处调用点接入

### T4.1 `server.py` 非流式 — `[x]`
- [x] `server.py:509` 改走 `complete_with_toolcall_retry`；`MalformedToolCallError` → 502（第 530 行）。
- **验收**：坏回复在非流式路径被重发；异常映射为 502/upstream_error。
- **回归**：`test_routes.py` / `test_parsing.py` 全绿。

### T4.2 `streaming.py` 流式 — `[x]`
- [x] `streaming.py:176` 接入。**取舍已定**：工具模式下 `on_delta=None`（先缓冲），
  因此重发时**尚未吐出任何增量**，客户端不会看到两段内容。
- [x] `MalformedToolCallError` → `done` 帧带 `upstream_error`（第 201 行）。
- **验收**：流式坏回复重发后，`[DONE]` 前只有好回复的增量。
- **回归**：`test_routes.py` 流式用例。

### T4.3 `responses.py` — `[x]`
- [x] `responses.py:294` 接入；502 映射在第 367 行。
- **验收**：Responses API 路径坏回复重发。
- **回归**：`test_responses.py` 全绿。

---

## T5 · 测试矩阵（对齐设计 §7）

`tests/test_malformed.py` 25 例 + `tests/test_toolcall_retry_flow.py` 4 例：

| 用例 | 期望 | 归属 | 状态 |
| --- | --- | --- | --- |
| 围栏正文为空 | `True` | T1.5 | `[x]` |
| JSON 截断 | `True` | T1.2/T1.5 | `[x]` |
| 尾逗号 / 单引号 | `True` | T1.2 | `[x]` |
| 裸 `tool_call` + 空 | `True` | T1.4/T1.5 | `[x]` |
| 围栏未闭合但 JSON 合法 | `True` | T1.3/T1.5 | `[x]` |
| 同行开围栏未闭合 | `True` | T1.3 | `[x]` |
| 好坏混杂（整轮） | `False` | T1.5 | `[x]` |
| 好坏混杂（逐段） | `True` | T1.6 | `[x]` |
| 全角好调用 | `False` | T1.1 | `[x]` |
| 普通 `python` 代码块 | `False` | T1.5 | `[x]` |
| 无任何标记 | `False` | T1.5 | `[x]` |
| DSML 可解析 | `False` | T1.7 | `[x]` |
| DSML 截断但结构完整 | `False` | T1.7 | `[x]` |
| DSML `calls` 空壳 | `True` | T1.8 | `[x]` |
| DSML `invoke` 缺 `name` | `True` | T1.8 | `[x]` |
| DSML 参数换行截断成非法 JSON | `True` | T1.8 | `[x]` |
| 正文只提到「DSML」字样 | `False` | T1.8 | `[x]` |
| 重发后仍坏 | 抛出 | T3.1 | `[x]` |
| 重试轮不带 images | 首轮有 / 重试无 | T3.2 | `[x]` |
| `wants_tools=False` 不重发 | 只调一次 | T3.3 | `[x]` |

- **验收**：`pytest -q` 计数只增不减；每条能变红（换回旧写法/删判据即红）。

---

## 剩余缺口（`[~]` / `[ ]`）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 全角归一独立用例 | `[~]` | 能力已落地，但 `test_malformed.py` 里没有**单独点名**的全角样例，靠 `_loads_lenient` 路径间接覆盖；建议补一条显式断言防回归。 |
| `has_empty_fence_triple` 别名 | `[~]` | `malformed.py:135` 仍作为旧名保留（指向 `has_malformed_tool_call`）。确认无调用方后可删。 |
| 流式 E2E | `[~]` | 流式重发的最终形态只在单测层验证；需真机的 E2E 未纳入默认收集。 |

---

## 建议顺序（后续新增反模式时）

```
T1（判据内核） → T2（配置） → T3（重发循环） → T4（接入） → T5（矩阵收口）
```

理由：T1 是纯函数、无 IO、最容易变红验证；T3 依赖 T1/T2；T4 逐路径接入，
每条单独可回滚；T5 最后把设计 §7 表格全部落成用例。

**新增 DSML 类反模式时**：先确认「解析层能否救活」，再决定加在 T1.7（短路正例）
还是 T1.8（声称但救不出）。两条路径必须各有一个正例，否则判据容易一边倒。

---

## 完成定义（DoD）

一个任务「完成」须同时满足：

1. 验收项能跑出来（不是「看起来对」）；
2. 有行为变化 → 配回归测试，且**把修复换回旧写法时用例必须变红**；
3. `pytest -q` 全绿且计数只增不削；`ruff check` 对改动文件干净；
4. 触及共享组件（`toolcalls` / `config` / `streaming` / `server` / `responses`）时，
   `git diff` 中无签名或行为改动（接入用包装，不改 `parse_tool_calls` / `send_chat` 签名）；
5. 文档同步：实现与 `toolcall_retry.md` 不符时立刻改设计或改实现，不两边漂移。

---

## 明确不做（WONTFIX，对齐设计 §9）

| 项 | 原因 |
| --- | --- |
| 无限重发 | 烧额度且掩盖真故障 |
| 重发时换会话 / 重放历史 | 上下文没丢，换会话是 `send_chat` 另一套阶梯 |
| 把正文里 `tool_call` 字样当损坏 | 正常讨论会被误伤 |
| 把正文里「DSML」字样当损坏 | 同理，只认 `<…DSML… calls/invoke>` 标签（`DSML_CLAIM`） |
| 判据看工具名是否声明 | 那是解析层 `declared` 过滤的事 |
| 重发提示词里写 DSML 标记 | 等于把坏形态示范一遍（P0-N） |
