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
.venv/bin/python -m pytest -q        # 337 passed, 5 skipped（skip 全是需真机的 E2E）
.venv/bin/ruff check .               # 本次改动文件必须干净
```

**前置事实**（编码前须确认，影响任务拆法）：

- `deepseek_web/toolcalls.py` **当前没有** `_normalize_json_text` / 全角归一（设计 §3.1 假定
  解析层已有此能力，但实际解析层是 `json.loads` + `_consume`）。**T1 需先补齐或确认口径**。
- `parse_tool_calls` 已有（`toolcalls.py:250`），三处调用点：`server.py:534`、
  `streaming.py:237`、`responses.py:299`；三处各自 `extract_images` 后 `send_chat`。
- `chat_io.send_chat` 返回 `(reply_text, code_blocks)`，已带上游重试阶梯（设计 §2）。
- `config.py` 目前**无** `MALFORMED_RETRIES`（T2 新增）。

---

## T1 · 判据内核（纯函数，无 IO）

**目标**：把设计 §3 的判据做成纯函数，与解析层共用「合法调用」口径。

### T1.1 全角归一能力对齐
- [ ] 在 `toolcalls.py` 引入/复用 `_normalize_json_text`（或等价的 ASCII↔全角映射），
  供判据与解析层**共用同一张映射**。
- **验收**：一段全角渲染的合法调用，`parse_tool_calls` 能读出、判据 `_is_valid_call` 也判合法。
- **回归**：新增 `tests/test_malformed.py`，用全角样例断言两处口径一致。
- **变红条件**：判据不做归一 → 全角好调用被判坏。

### T1.2 `_is_valid_call(body)`
- [ ] 实现设计 §3.1：`json.loads` 成 dict + 非空字符串 `name` + dict `arguments`。
- **验收**：空 / 截断 / 语法错 / 非对象 / 缺字段 全部 `False`；合法调用 `True`。
- **回归**：`test_malformed.py::IsValidCallTests`（≥6 例）。
- **变红条件**：宽松放过尾逗号 / 单引号。

### T1.3 `has_unclosed_tool_fence(text)`
- [ ] 设计 §3.4：开围栏匹配用 `(?![0-9A-Za-z_])`（不要求换行），逐对扫描「开→闭」；
  只判 `tool_call` 语言围栏。
- **验收**：
  - ` ```tool_call\n{...} ` 无闭合 → `True`；
  - 同行情形 ` ```tool_call{...} ` 无闭合 → `True`（旧换行口径会漏）；
  - 闭合的 `python` 围栏 → `False`。
- **回归**：`test_malformed.py::UnclosedFenceTests`（≥4 例，含 CRLF 与同行）。
- **变红条件**：要求必须换行 → 同行情形漏判。

### T1.4 `_iter_bodies(text)`
- [ ] 设计 §3.5：显式围栏与裸标记都产出正文；有显式围栏时不再重复扫裸标记。
- **验收**：同一段不被数两次。
- **回归**：断言 `list(_iter_bodies(...))` 段数与内容。

### T1.5 `has_no_usable_call(text)`（整轮版）
- [ ] 设计 §3.5：未闭合围栏优先 → `True`；所有段都不可用才 `True`；
  任一段可用 → `False`；无任何标记 → `False`。
- **验收**：「好的 + 坏的」混杂 → `False`（不误杀）；全坏 → `True`。
- **回归**：`test_malformed.py::NoUsableCallTests`（≥5 例）。
- **变红条件**：整轮判据用逐段逻辑 → 好坏混杂时误杀好调用。

### T1.6 `has_malformed_tool_call(text)`（逐段版）
- [ ] 设计 §3.5：任一段非法即 `True`。
- **验收**：好坏混杂 → `True`（逐段口径）；单独合法段 → `False`。
- **回归**：`test_malformed.py::MalformedPerSegmentTests`。

### T1.7 DSML 短路
- [ ] 设计 §6：`has_no_usable_call` 内先 `if parse_tool_calls(text, valid_names): return False`，
  「解析层救得活 → 不重发」为最高优先级短路。
- **验收**：一段可解析 DSML invoke → `has_no_usable_call=False`。
- **回归**：`test_malformed.py::DsmlShortCircuitTests`。
- **变红条件**：判据把 DSML 当损坏 → 与解析层打架。

---

## T2 · 配置与常量

### T2.1 `MALFORMED_RETRIES`
- [ ] `config.py` 新增 `MALFORMED_RETRIES = env_int("MALFORMED_RETRIES", 2)`，带注释说明 0=关闭。
- **验收**：`.env` 未设时为 2；设 0 时重发循环跳过。
- **回归**：`test_malformed.py::ConfigTests` 断言默认值。

### T2.2 `RETRY_PROMPT` 与 `MalformedToolCallError`
- [ ] 新增 `RETRY_PROMPT`（设计 §4 文本，**不写 DSML 字面形状**）与
  `MalformedToolCallError(RuntimeError)`（放 `errors.py`）。
- **验收**：`RETRY_PROMPT` 含 ` ```tool_call ` 正面示例 + 闭合要求，不含 `DSML` 字样。
- **回归**：断言 `"DSML" not in RETRY_PROMPT`。
- **变红条件**：提示词写出原生标记 → 变成诱导（P0-N）。

---

## T3 · 重发循环

### T3.1 `complete_with_toolcall_retry(...)` 公共函数
- [ ] 抽出设计 §5 的循环：先 `send_chat`，`wants_tools` 为假直接返回；否则
  `while has_no_usable_call(text) and retries < MALFORMED_RETRIES` 重发；
  超限则按设计 §5 处理（抛 `MalformedToolCallError` 或退回原文 + warning，**二选一在实现时定**）。
- **验收**：坏回复触发一次重发后拿到好回复 → 返回好回复；重发耗尽 → 抛/降级如约定。
- **回归**：`tests/test_toolcall_retry_flow.py` 用假 driver（脚本化返回序列）。
- **变红条件**：重发耗尽仍静默返回坏文本。

### T3.2 重试轮不带 images
- [ ] 设计 §5.1：重发调用 `send_chat(RETRY_PROMPT, key=...)` 不传 `images`。
- **验收**：假 driver 记录两次调用的 `images` 参数：首轮有、重试轮为 `None`/空。
- **回归**：同上文件断言。

### T3.3 只在 `wants_tools` 时启用
- [ ] 纯文本请求（`tool_choice=none` 或未声明 tools）出现 `tool_call` 字样**不重发**。
- **验收**：`wants_tools=False` 时 `send_chat` 只调用一次。
- **回归**：同上文件。

---

## T4 · 三处调用点接入

### T4.1 `server.py` 非流式
- [ ] `server.py:505` 的 `send_chat` 改走 `complete_with_toolcall_retry`，保留现有异常映射
  （`DeepSeekContextLimitError`→400 等），新增 `MalformedToolCallError` 的映射（如 502）。
- **验收**：坏回复在非流式路径被重发；异常映射有测试。
- **回归**：`test_routes.py` 补一例；`test_parsing.py` 全绿。

### T4.2 `streaming.py` 流式
- [ ] `streaming.py:168` 接入；注意流式下 `on_chunk` 已可能吐出增量——**坏结构重发只在
  拿到完整 `reply` 后判定**，重发轮是否重放增量需按设计取舍（默认：重发轮静默，最终只吐好回复）。
- **验收**：流式路径坏回复重发后，`[DONE]` 前只有好回复的增量。
- **回归**：`test_routes.py` 补一例流式（或标注需真机 E2E）。
- **风险**：流式已吐增量再重发会导致客户端看到两段；实现时**先缓冲判断**或声明「流式不重发」。

### T4.3 `responses.py`
- [ ] `responses.py:292` 接入。
- **验收**：Responses API 路径坏回复重发。
- **回归**：`test_responses.py` 补一例。

---

## T5 · 测试矩阵（对齐设计 §7）

- [ ] 新建 `tests/test_malformed.py`，逐条覆盖设计 §7 表格：

| 用例 | 期望 | 归属 |
| --- | --- | --- |
| 围栏正文为空 | `has_no_usable_call=True` | T1.5 |
| JSON 截断 | `True` | T1.2/T1.5 |
| 尾逗号 / 单引号 | `True` | T1.2 |
| 裸 `tool_call` + 空 | `True` | T1.4/T1.5 |
| 围栏未闭合但 JSON 合法 | `True` | T1.3/T1.5 |
| 好坏混杂 | `False` | T1.5 |
| 全角好调用 | `False` | T1.1 |
| 普通 `python` 代码块 | `False` | T1.5 |
| DSML 可解析 | `False` | T1.7 |
| 重发后仍坏 | 抛出/降级 | T3.1 |

- **验收**：`pytest -q` 计数只增不减；每条能变红（换回旧写法/删判据即红）。

---

## 建议顺序

```
T1（判据内核） → T2（配置） → T3（重发循环） → T4（接入） → T5（矩阵收口）
```

理由：T1 是纯函数、无 IO、最容易变红验证；T3 依赖 T1/T2；T4 逐路径接入，
每条单独可回滚；T5 最后把设计 §7 表格全部落成用例。

**T4.2 流式有风险**：若实现「已吐增量再重发」，客户端会看到重复内容。建议先做成
「流式路径只在**尚未吐出任何增量**时才重发」，或干脆流式不启用重发、只在非流式/Responses
启用（在实现时定并在 tasks 里回填结论）。

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
| 判据看工具名是否声明 | 那是解析层 `declared` 过滤的事 |
