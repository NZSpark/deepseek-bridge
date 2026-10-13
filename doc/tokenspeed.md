# Token 速度统计设计方案（`/bridge token`）

> 目标：为 DeepSeekBridge 增加 **prefill 速度** 与 **decode 速度** 统计，通过
> `/bridge token` 命令查看。
>
> 核心难点：本桥不是直接访问大模型，而是
> `客户端 → bridge → 浏览器 → DeepSeek 网页 → 浏览器 → bridge → 客户端`。
> 页面上**没有 token 级事件**，只有「整段回复快照」的轮询。因此必须先把「测什么」
> 定义清楚，再谈公式。
>
> 姊妹项目 ClaudeBridge 的同名文档：claude-bridge `doc/tokenspeed.md`（同一套口径，
> 落地到本项目的模块布局）。

---

## 1. 问题陈述

### 1.1 为什么不能照搬 API 的 token/s

客户端直接调用大模型 API（如 OpenAI / Anthropic）时，`usage` 字段给出精确的
`prompt_tokens` / `completion_tokens`，配合流式事件的到达时间，可以算出：

- **prefill 速度** = `prompt_tokens / 首 token 延迟(TTFT)`
- **decode 速度** = `completion_tokens / (末 token 时间 − 首 token 时间)`

但本桥面临三个差异：

1. **没有精确 token 数**：只有字符数，token 数靠 `estimate_tokens ≈ len//3` 估算
   （`deepseek_web/prompting.estimate_tokens`）。
2. **没有 token 级流**：网页只渲染整段文本；桥用轮询「回复快照」的增量来合成 SSE。
3. **存在四段额外时延**：客户端网络、bridge 进程内调度、Playwright/DOM 往返、
   网页版推理+渲染，全部叠在同一条时间线上。

### 1.2 我们真正要回答的问题

用户/开发者看 `/bridge token`，想知道的是：

- 这次（或平均）**网页版模型**的 prefill / decode 有多快？（→ 与「直接 API」可比的那部分）
- 我的 bridge 引入的额外时延占了多少？（→ 桥接效率）
- 是不是网络 / 页面渲染拖慢了体感？（→ 归因）

因此设计必须**区分「模型速度」与「端到端速度」**，不能只报一个大数。

---

## 2. 端到端时间线分解

一次非流式请求，从桥的视角看有这些时刻（都用 `time.monotonic()`，单调、不受系统时钟
调整影响；本项目的既有实现用 `time.monotonic()`）：

```
t0  bridge 收到请求
t3  submit（Enter）返回（本项目从此刻起算，见 chat_io.generate）
t5  页面首次读到**非空回复文本**（TTFT 的可观测代理）
t6  页面自述不再生成 + 内容静默落定（最后一轮 read）
t8  客户端收到完整响应（仅流式路径可观测）
```

对应的时延段：

| 段 | 定义 | 包含什么 |
| --- | --- | --- |
| **prefill（可观测）** | `t5 − t3` | 提交后到**首个非空回复文本**出现 = TTFT 代理 |
| **decode（可观测）** | `t6 − t5` | 首个文本到「落定」；期间文本持续增长 |
| **收尾** | `t7 − t6` | 最后一轮 read、tool 解析、响应构造 |
| **回传** | `t8 − t7` | 流式 SSE 写出到客户端（非流式只在客户端侧可见） |

> **关键区分**：`t5` 是「页面渲染出第一个字」，它**晚于**模型真正的 TTFT——中间还有
> 网页前端把流式 token 合成段落、DOM 更新、我们的轮询间隔。因此 `prefill(可观测)`
> 是**上界**（偏大）。

---

## 3. 公式设计

### 3.1 记号

- `P_chars` / `C_chars`：提示词 / 回复正文的**字符数**（`len(prompt)` / `len(reply)`）。
- `P_tok = estimate_tokens(prompt)`，`C_tok = estimate_tokens(reply)`（≈ 字符数 / 3）。
- `TTFT = t5 − t3`：首字延迟（time-to-first-token 的可观测代理）。
- `T_decode = t6 − t5`：从首字到落定的时长。
- `T_gen = t6 − t3`：提交到落定（prefill + decode 的可观测总和）。

### 3.2 观测速度（与「直接 API」口径尽量对齐）

**prefill 速度**（tokens/s）：

```
prefill_tps = P_tok / max(TTFT, ε)
```

**decode 速度**（tokens/s）：

```
decode_tps = C_tok / max(T_decode, ε)
```

两者都会**系统性偏保守**：prefill 偏大（TTFT 偏晚）、decode 偏小（落定判定把 decode
时长多算）。因此输出里都标注为「观测口径」。

### 3.3 修正项：扣掉可归因的桥接 / 落定开销

只做**保守修正**（只减能明确归因的部分）：

**decode 时长修正**：稳定判定要求内容静默若干轮（`STABLE_POLLS` / `LEN_STABLE_POLLS`），
这期间不属于解码时间。

```
T_decode_adj = max(T_decode − settle_credit, ε)
settle_credit = 稳定判定实际经历的轮询周期数 × POLL_INTERVAL_S（钳制在 decode 时长内）
```

**prefill 时长修正**：首字出现后，最多要等一个 `POLL_INTERVAL_S` 才被读到。

```
TTFT_adj = max(TTFT − poll_credit, ε)
poll_credit = POLL_INTERVAL_S / 2   # 期望半周期，且钳制在 TTFT 内
```

### 3.4 桥接开销占比

```
bridge_overhead_ratio = 1 − (T_gen_adj / total_s)
其中 T_gen_adj = TTFT_adj + T_decode_adj
```

它回答「端到端时间里，有多少不是模型生成」。**只作为诊断**，不追求精确；钳制在 `[0, 1]`。

---

## 4. 与「直接 API」的对应表

| 直接 API 指标 | 本桥对应 | 口径差异 |
| --- | --- | --- |
| `prompt_tokens` | `P_tok`（估算） | 字符数/3，非精确 |
| `completion_tokens` | `C_tok`（估算） | 同上 |
| TTFT | `TTFT_adj` | 含网页合成 + 半轮询周期，**偏大** |
| decode tokens/s | `decode_tps`（修正后） | 含渲染与轮询，**偏小** |
| — | `bridge_overhead_ratio` | 本桥特有诊断 |

**结论**：本桥给出的是「**网页版在真实桥接链路下的有效速度**」，不是「模型裸速度」。
文档与命令输出都必须**明确标注口径**，避免误导。

---

## 5. 数据采集点（落地设计）

### 5.1 埋点位置

| 时刻 | 采集点 | 位置 |
| --- | --- | --- |
| `t3` | 确认提交后 | `chat_io.generate`（`_submit_prompt` 返回后） |
| `t5` | 首次非空新回复文本 | 轮询循环里 `reply_seen and has_new_text` |
| `t6` | 循环 `break` | 生成结束判定后 |
| 落定修正 | `settle_credit_s` | 稳定判定命中时累加 `POLL_INTERVAL_S × threshold` |

### 5.2 承载对象

- `deepseek_web/token_speed.py`：`TokenSpeed` 数据类 + `SpeedTiming` + 纯函数
  `compute_speed(...)` + `SpeedAggregate`（滚动平均）。便于单测穷举。
- `deepseek_web/chat_io.py`：每轮成功后调用 `self.note_speed(speed_payload, bucket)`。
- `deepseek_web/session_store.py`：`SessionState` 增加数值和字段；`note_speed` 累加并落盘；
  `speed_stats` 读出本轮 + 桶平均。

### 5.3 会话级聚合

`SessionState` 新增（schema 向后兼容，缺失默认 0）：

```python
speed_samples: int = 0          # 已累计样本数
speed_prefill_tps_sum: float = 0.0
speed_decode_tps_sum: float = 0.0
speed_ttft_s_sum: float = 0.0
speed_decode_s_sum: float = 0.0
last_speed: dict | None = None  # 最近一轮的完整数值快照
```

- 每轮成功后累加（`chat_io.generate` → `note_speed`）。
- `/bridge token` 显示**本轮**（最近一次）与**该桶平均**两组数字。
- 只存**数值和**，不存正文、不存每次明细——符合「状态文件最小化 + 不落盘正文」。

---

## 6. `/bridge token` 命令

### 6.1 命令注册

`bridge_commands.REGISTRY` 新增只读条目；`parse_command` 的 `head == "token"` 分支
（不接受参数，与 `status` 同级）；`action = "token"` → `_token_reply`。

### 6.2 输出示例

```
[Bridge] token 速度（桶 default；本轮 = 最近一次成功生成）：
  本轮：
    prefill 1,234 tok/s（输入 3,700 tok / 首字 3.0s）
    decode      52 tok/s（输出   420 tok / 解码 8.1s）
    TTFT 3.0s（修正后 2.8s）；decode 8.1s（修正后 5.6s，已扣落定 2.5s）
    桥接开销占比 ≈ 18%
  本桶平均（近 12 轮）：
    prefill 1,180 tok/s；decode 55 tok/s；TTFT 3.1s
  口径：token 数为字符数/3 的估算；速度为「网页版真实链路下的有效速度」，
        非模型裸速度；仅统计成功的生成。
```

### 6.3 边界与降级

- **无样本**：显示「暂无样本；先发一轮普通请求」。
- **工具调用轮**：`C_tok` 用正文（`last_text`）估算，不把工具 JSON 算进 decode。
- **取消 / 失败轮**：**不计入**（`note_speed` 只在成功后调用）。
- **短回复**：`reply_tokens < SHORT_REPLY_TOKENS` 时加一句「样本过短，仅供参考」。
- **精度**：速度保留到整数 tok/s；时长保留 1 位小数；数字带千位分隔符。

### 6.4 隐私

- 只输出**计数与时长**，不回显提示词 / 回复正文。
- 不泄露其它桶的信息（与 `status` 同一约束）。

---

## 7. 为什么这样设计是合理的

1. **口径诚实**：明确区分「观测速度（修正前）」「修正速度」，写清各自偏向。
2. **与 API 可对应**：定义同构，可横向比较（在理解口径差异的前提下）。
3. **只做保守修正**：只扣可明确归因的落定等待与半轮询周期；修正量随结果一起输出，可审计。
4. **纯函数 + 可穷举测试**：`compute_speed` 用虚拟时钟测试，不依赖真实耗时。
5. **复用既有骨架**：埋点是给既有阶段加时间戳，不改判定逻辑；聚合复用 `SessionState`
   的落盘与缓存；命令复用 `/bridge` 的解析与本地应答机制。
6. **符合既有约束**：不落盘正文、状态文件只加数值和、只读命令不导航页面、不泄露。

---

## 8. 已知局限（已在文档与命令输出里承认）

- **token 数是估算**：`len//3` 对中英文混排、代码、JSON 都会偏；绝对值不可当精确计费依据。
- **TTFT 偏大**：包含网页前端合成 + 半轮询周期 + 渲染时间；修正只扣了轮询半周期。
- **decode 偏小**：`t6` 是「静默若干轮后才落定」，已扣落定耗时，但首字偏晚的误差无法完全消除。
- **轮询粒度限制分辨率**：短回复（< ~2s）的速度估计误差较大；命令对短回复加提示。
- **无真实模型对照**：无法知道「模型裸速度」真值，只能给有效速度。若将来能从网页 DOM
  读到精确 token 计数，应优先采用并废弃估算。
