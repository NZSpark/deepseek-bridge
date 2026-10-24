# DeepSeek Web-to-API Bridge · 设计文档

> 状态：与当前代码（HEAD `aa1ea71`，2026-10-17）一致。
> 本文回答「**是什么 / 为什么这么设计 / 边界在哪**」；施工分解见 [tasks.md](tasks.md)，
> 故障复盘见 [update.md](update.md)，验收证据见 [acceptance.md](acceptance.md)，
> `/bridge` 命令细节见 [bridge_commands.md](bridge_commands.md)。

---

## 1. 一句话定位

用 Playwright 驱动一个真实的 Chromium，复用本地登录态访问 `chat.deepseek.com`，把网页对话
**桥接**成本地 OpenAI 兼容 API：`/v1/models`、`/v1/chat/completions`（流式 + 非流式）、
`/v1/responses`（Codex CLI 用）、`/healthz`。**不需要官方 API Key**。

服务只监听 `127.0.0.1`，定位是「给本机的 Coding Agent 配一个不用官方 Key 的模型后端」，
不是多租户网关。

---

## 2. 为什么是「浏览器桥接」而不是逆向上游接口

三条路线，本项目选 **A：驱动真实页面**。

| 路线 | 做法 | 结论 |
| --- | --- | --- |
| **A 驱动真实页面** | Playwright 打开对话页，写入 composer、点发送、轮询 DOM 取回复 | **选它**：网页版的反爬与风控对「真人浏览器」最宽容；登录态、模型选择、账号权益全部自动继承 |
| B 复刻网页私有接口 | 抓 `/api/v0/chat/completion` 一类私有 HTTP 接口 | 不选：需要复刻签名 / PoW / 风控头，一次改版就全废；且更难解释失败 |
| C 只用官方 API | 走官方 Key | 不选：本项目的存在前提就是「没有 Key」 |

代价（必须接受，不能假装没有）：**慢**（一轮几秒到几十秒）、**脆**（网页改版会打穿选择器）、
**吃资源**（一个常驻 Chromium）。这些代价换来的是「零 Key + 零协议逆向 + 账号权益自动生效」，
对本地单用户场景是划算的。

---

## 3. 契约（对外承诺什么）

### 3.1 路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/models` | 与 `/bridge models` **同源**（`models.advertised_models()`） |
| POST | `/v1/chat/completions` | 流式 / 非流式；`stream=true` 出 SSE |
| POST | `/v1/responses` | Codex CLI；`ENABLE_RESPONSES_API=false` 时 404 |
| GET | `/healthz` | 就绪返回 200，浏览器未就绪返回 **503** + `status: degraded` |
| POST | `/session/reset` | 逃生口：指定桶下一轮开新会话（播种历史，不丢上下文） |
| GET | `/debug/dom` | 仅 `DEEPSEEK_DEBUG=1`，**不回显正文** |
| GET | `/` | 服务标识 |

### 3.2 参数三态（照抄，不临时发明）

| 类别 | 例子 | 行为 |
| --- | --- | --- |
| 真生效 | `messages`、`stream`、`tools`、`tool_choice`、图片分片 | 按语义执行 |
| 接受但忽略 | `temperature`、`max_tokens`、`top_p`、`reasoning_effort` | **收下不报错**，网页版没有这些旋钮 |
| 明确拒绝 | 空 `messages` / 空 `input` | 400 `invalid_request_error`，不进入浏览器 |

模型全部 `extra="allow"`：未知字段一律放行，**绝不 422**。严格模式会把真实客户端全部挡在门外
（请求里一定有 `store`、`service_tier` 这类字段）。

### 3.3 模型与用量

- 公布 `deepseek-chat` / `deepseek-reasoner`，`context_window` 65536（网页窗口口径，不是 API 口径）。
- 模型名**不强制**切换网页端实际模型——那由登录账号与网页设置决定，文档如实写明。
- `usage` 是**估算**（字符数 / 3），不是计费依据；`/bridge token` 与 `usage` 同一诚实口径。

---

## 4. 会话模型（本项目最核心的设计）

### 4.1 分桶：一次请求属于哪条网页会话

优先级（高 → 低），见 `server._session_key`：

1. `X-DeepSeek-Session` 请求头（`SESSION_KEY_HEADER` 可改名）；
2. OpenAI 的 `user` 字段；
3. **提示词里的工作目录**（`<cwd>` 段）——两个 Pi 在两个目录里跑，UA 相同但任务不同；
4. **User-Agent** 提取的客户端名——不同客户端自动隔离。

都没有 → `default` 桶（全局共用，保持历史行为）。取值消毒（只留 `[\w.\-:]`）并限长
`SESSION_KEY_MAX_LEN`（64），避免变成非法文件名或超长 JSON 键。

为什么第 3 条必须先于第 4 条：只按 UA 分桶会把「两个不同目录的 Pi 会话」并成一条，
上下文互相污染——这是真机踩过的坑。

### 4.2 轮转 + 播种（成对，缺一即静默失败）

默认策略：**复用一条网页会话，只发增量**。一旦触发轮转（到顶 / 超预算），下一轮
**必须同时播种**客户端传来的完整历史——否则就是一条孤立消息，模型答得莫名其妙。

| 触发条件 | 阈值 |
| --- | --- |
| 轮数 | `SESSION_MAX_TURNS`（60） |
| 估算 token | `SESSION_MAX_TOKENS`（60000） |
| 网页到顶提示 | `CAP_NOTICE_PATTERNS` 命中（每 `CAP_CHECK_EVERY` 轮扫一次，**扫描前剔除会话正文**） |

播种长度上限 `SEED_MAX_CHARS`（12000）。到顶**必须**映射成 400 `context_length_exceeded`，
不是 504——把「额度用尽」报成「超时」会让客户端无限重试。

### 4.3 页面池：自愈是硬要求

- 每桶一页；`MAX_SESSION_BUCKETS`（8）封顶，`BUCKET_IDLE_TTL_S`（900s）空闲回收 + LRU 淘汰。
- **判活**：`is_closed()` 为真 **或** `wait_for_selector` 立刻抛错 → `DeepSeekPageLostError`。
  这是本项目最贵的一个 bug：页面标签死了但还留在池里，于是该桶每个请求都瞬间 502
  并提示「请检查登录」，**永不恢复**。
- 重建：按状态里保存的会话 URL 重开，失败则新开会话 + 重放历史。
- **请求进行中的桶不得被回收**（`_mark_bucket_active` / `_other_request_in_flight`）。

### 4.4 并发与锁

默认**全局串行**（`PARALLEL_BUCKETS=false`）：真并发会同时驱动多条网页会话，有风控风险。
并行是显式开关。同桶并发等待超过 `BUCKET_LOCK_TIMEOUT_S`（默认 0 = 不等待）→ 503
`upstream_busy`，**不触发重试阶梯**（这是本地保护，不是上游故障）。

### 4.5 状态持久化

`user_data/.deepseek_session`：每桶的 `linked_url`、`pending_reseed`、`pending_rotation`、
轮数、token 估算、测速样本（`speed_*_sum` / `last_speed`，**只有数值，不含正文**）。
会话 URL 比较用「主机 + 路径精确匹配」，不做前缀比较（会话 ID 只差末位会被误判同一条）。

---

## 5. 输入与提交（写坏的两种方式都是静默的）

### 5.1 写入

「整段 `fill()` 优先 → 分块插入兜底」：

- 超 `FILL_TIMEOUT_MS`（10s）或首轮失败 → 分块，`FILL_CHUNK_CHARS`（4000）一段；
- 每块前**重读 composer 只补缺失段**（幂等、可断点续写），块间让出主线程；
- `FILL_RETRIES`（3）次；失败抛 `ChunkedInsertUnavailableError`。

两个真机根因写在代码注释里：**没有选区的 `insert_text` 是静默空操作**（不报错、0 字符）；
**有损读回不能否决成功写入**（富文本读回丢换行，据此清空重写会自己弄坏已写好的 prompt）。

### 5.2 提交

阶梯（`_submit_prompt`）：**真实键盘 Enter → 原生 `click()` 发送按钮 → 合成 Enter**，每级之后验
「输入框已清空 **或** 消息已出现在对话区」；读不到时返回「无法判断」，不据此报错也不空等。
`SUBMIT_VERIFY_MS`（3000）是每级验证窗口。

### 5.3 长 prompt

超过 `PROMPT_MAX_CHARS`（48000）**发送前截断并告警**，请求仍成功。

### 5.4 图片

`IMAGE_PASTE_ENABLED`（默认开）、`IMAGE_PASTE_MAX`（4）：把 `image_url` / data URI 下载后
以粘贴方式送进 composer，失败降级为文字引用（`[图片: url]`），不让整轮失败。

---

## 6. 结束判定（多信号与门，缺一个就读半截）

以**内容**为准，不看节点数量：

| 信号 | 配置 | 含义 |
| --- | --- | --- |
| 内容静默 | `STABLE_POLLS`（2）轮不变 | 短回复的落定 |
| 长度静默 | `LEN_STABLE_POLLS`（4）轮长度不变 | 长回复的落定（更严） |
| 无节点快速失败 | `NO_NODE_FAIL_POLLS`（10） | 选择器失效 → 尽早报错，不空等 |
| 无进展快速失败 | `NO_PROGRESS_FAIL_POLLS`（40） | 有节点但内容不涨 |
| 总超时 | `RESPONSE_TIMEOUT_S`（180） | 最终兜底 |
| 轮询间隔 | `POLL_INTERVAL_S`（1.5） | — |

「继续生成」按钮（`CONTINUE_BUTTON_ENABLED`，最多 `CONTINUE_BUTTON_MAX`=5 次）用于网页分段输出。
**反模式**：用装饰类名里的 `stopAtEnd-<hash>` 判断「生成中」→ 恒为 True，每轮都拖到 180s；
提示词里**不要逐字写出**原生工具标记形状（`DSML` / `<invoke>`），模型看到形状就会复现——
「禁止」等于「示范」。

---

## 7. 工具调用（网页版没有，模拟出来）

三步：**注入 → 解析 → 回填**。

1. **注入**（`toolcalls.format_tools_instruction`）：只在客户端声明了 `tools` 时注入；
   统一用 ` ```tool_call ` 围栏，提示词只描述链路不描述模型能力（「你是语言模型；客户端会真实
   执行你写出的调用」），禁止逐字贴出目标站点的原生标记。
2. **解析**（`toolcalls.parse_tool_calls`）：围栏优先，兼容 ` ```json ` 与旧标记行；
   工具名非法**静默丢弃前必须写日志**（否则客户端永远收不到 `tool_call` 而没人知道为什么）；
   别名能唯一对应时归一化（`bash` vs `shell`），有歧义仍拒绝。
3. **回填**：`role:"tool"` 结果拼回下一轮 prompt；**空输出必须解释**（已执行、无输出、继续），
   否则模型死循环重发同一条；同签名（工具名 + 规范化参数）重复 ≥2 次要点名要求换做法。

预算护栏：客户端**未声明**工具时不得注入任何脚手架（否则每请求白付约 970 tokens，
还会产生客户端没声明的 `tool_calls`）。

---

## 8. Responses API（Codex CLI）

`/v1/responses` 与 Chat Completions 共用 `send_chat` 与会话状态机，只是外层事件不同：
`response.created` → `output_item.added` → `content_part.added` → `output_text.delta`… →
`output_text.done` → `content_part.done` → `output_item.done` → `response.completed`
（工具路径额外发 `function_call_arguments.delta/done`；失败发 `response.failed`）。
`sequence_number` 严格递增，`RESPONSES_KEEPALIVE_S`（10s）发保活，`RESPONSES_TOOL_BUFFER`
可缓冲工具参数以免半截 JSON。默认开启，可 `ENABLE_RESPONSES_API=false` 关闭（此时 404，不影响 Pi）。

---

## 9. 任务快照

`TASK_SNAPSHOT_ENABLED`（默认开）：每桶把最近 `TASK_KEEP_MESSAGES`（8）条消息
落盘到 `TASK_FILE_DIR/<TASK_NAMESPACE>/`，用于**新会话播种**与重启续跑。
它**不是**对话历史的完整备份；`/bridge` 命令消息不写入快照。

轮转播种时 `resume_block()` 只输出「最近进展」列表，**不再注入「任务目标」**。
原因：目标取的是最早那条 user 消息，而换页时那个任务往往早已完成；再贴一遍
会把模型拉回旧任务。播种本身已按 `SEED_MAX_TURNS`（3）保留最近几轮历史，
足够传达当前在做什么。`goal` 字段仍会记录（观测用，上限 `TASK_GOAL_MAX_CHARS`），
但不参与注入。

---

## 10. 聊天内命令（`/bridge ...`）

由桥**本地应答**，不发给网页版——页面失效、浏览器未初始化时仍可用，这是排障时唯一
还能用的入口。9 条命令 + 裸 `/bridge`：

| 命令 | 风险 |
| --- | --- |
| `help [session]` / `status` / `models` / `token` / `session [status]` | 只读 |
| `session reset` / `session reseed` / `session link <URL> [--seed]` / `session unlink` | 改状态 |

识别四条规则：**精确匹配**（整条消息就是那一行）、**只看最后一条且必须是 `user`**
（工具循环第二轮不会重放旧命令）、命名空间保留（参数不合法回用法，不执行也不丢给模型）、
两条协议共用 `handle_command`。帮助文本**只从 `REGISTRY` 生成**。

**命令的桶不走 cwd 推断**（`_session_key(..., for_command=True)`）：命令请求的首条 user
消息就是命令文本本身，里面没有 `<cwd>`，若照常推断会落到 UA 桶上，使 `session reset` /
`token` 等作用在**错误的会话**上——表现为「一条只读命令打断了正在进行的对话」。命令只认
显式 `X-DeepSeek-Session` / `user`，缺省时用 UA 兜底（同一客户端内始终是同一个桶）。

改状态回执必须写清四句：影响范围（只当前桶）、不是删除历史、下一轮会发生什么、怎么撤销。
`link` 地址校验：`https` + `LINK_ALLOWED_HOSTS` 白名单 + `/a/chat/s/<ID>` 路径，裸 ID 自动补全，
**校验失败绝不猜一个目标导航过去**。细节见 [bridge_commands.md](bridge_commands.md)。

---

## 11. 错误语义（一次定死，直接决定客户端行为）

| 现象 | HTTP | `type` | 判据 |
| --- | --- | --- | --- |
| 浏览器未就绪 / 未登录 | 503 | `unavailable` | `/healthz` degraded |
| 请求不合法 | 400 | `invalid_request_error` | 校验阶段，不进浏览器 |
| 上下文到顶 | 400 | `context_length_exceeded` | `CAP_NOTICE_PATTERNS` 命中 |
| 同桶排队超时 | 503 | `upstream_busy` | 等锁超 `BUCKET_LOCK_TIMEOUT_S`，**不重试** |
| 真超时 | 504 | `timeout` | 超 `RESPONSE_TIMEOUT_S` 且内容不再变化 |
| 页面句柄失效 | 502 | `upstream_error` | `DeepSeekPageLostError` |
| 输入区不吃输入 / 提交无法确认 | 502 | `composer_not_found` / `prompt_submission_failed` | 候选全枚举后读回仍空 / 输入框仍留着提示词 |

铁律：**「上游明确拒绝」与「上游无响应」必须可区分**。错误体统一
`{"error":{"message","type","code"}}`，由 `server._error_response` 一处集中映射。

---

## 12. 模块职责（17 模块 / 6414 行）

| 模块 | 行数 | 职责 |
| --- | --- | --- |
| `chat_io.py` | 1469 | 浏览器输入 / 提交 / 结束判定 / 解析核心（最大单模块） |
| `bridge_commands.py` | 625 | `/bridge` 命令解析 + 注册表 + 执行 + 回执 |
| `responses.py` | 574 | Responses API 事件序 |
| `server.py` | 566 | 路由 / 分桶 / 错误映射 |
| `page_pool.py` | 429 | 页面池 / 判活 / 重建 / LRU / 绑定 |
| `session_store.py` | 426 | 按桶状态与持久化 |
| `toolcalls.py` | 362 | 工具指令注入 + 围栏解析 + 模型转换 |
| `prompting.py` | 318 | 增量 / 播种 / 图片提取 / token 估算 |
| `config.py` | 301 | 配置即文档（唯一真源） |
| `streaming.py` | 285 | SSE 编码（普通回复与命令应答同形） |
| `token_speed.py` | 279 | 测速纯函数（只数值） |
| `completion.py` | 212 | Chat Completions 主流程 |
| `tasks.py` | 177 | 任务快照 |
| `driver.py` | 175 | 驱动聚合（Mixin 组装 + re-export） |
| `models.py` | 129 | 宽松数据模型 + 模型目录 |
| `__init__.py` | 47 | facade（保留旧 import 路径） |
| `errors.py` | 40 | 异常与常量（避免循环依赖） |

入口 `deepseek_api_server.py` 是薄封装。拆分原则：**旧 import 路径保留 + facade**，API 行为不变。

---

## 13. 安全

- 只监听 `127.0.0.1`；不提供 `0.0.0.0` 的「方便开关」。
- `user_data/`（登录态 = 凭据）与 `.env` 不进 git，也**不要同步到 iCloud / Dropbox**。
- 调试端点默认 404 且**不回显正文**（只给选择器命中数 / 长度）。
- 客户端 API Key 不构成身份验证：有副作用的 `/bridge` 命令靠「清晰回执 + 只影响当前桶」约束，
  不假装它有鉴权。
- `link` 的用户可控 URL 有白名单，不允许变成任意导航目标。
- 默认串行；并行是显式开关并接受风控风险。

---

## 14. 验收与测试

- **单测**：`337 passed, 5 skipped`（skip 全部是需要真浏览器 / 登录态的 E2E，
  `DEEPSEEK_E2E=1` 才跑）。**不联网、不启浏览器**。
- **契约冒烟**：`scripts/smoke_test.py` 打真实端点（models / 非流式 / SSE 首块 `role` /
  增量 / `finish_reason` / `[DONE]` / 未知字段不 422 / 空输入 4xx / 错误体形状 / token 预算护栏）。
- **真机对等 E2E**：直连网页 vs bridge 同 prompt 对等；上游失败转 SKIP，bridge 不达记 FAIL。
- **门禁**：`gate.py` 把各阶段退出条件串成一条命令，**退出码即结论**，跳过的项如实标注。
- 每条已修故障都有能变红的回归用例；未验证的项在 [acceptance.md](acceptance.md) 里如实写「未验证」。

---

## 15. 显式取舍（没有唯一答案，但我们选了）

| 议题 | 选择 | 理由 |
| --- | --- | --- |
| 页面重建后的会话 | 回到状态里的会话 URL | 落点校验实验通过；失败则新开 + 重放 |
| 结束判定阈值 | 秒 + 内容静默窗口 | 次数阈值会被调快轮询等比削弱 |
| 工具调用载体 | ```tool_call 围栏 | 真机 A/B 字节级回声对照；解析层兼容三种形态 |
| 无头 vs 有头 | 默认有头（`HEADLESS` 可开） | 挑战页会拦无头；无显示服务器用 `xvfb-run` |
| 页面外壳剔除 | 克隆体剪子树 | 字符串替换会静默失败 |
| 并发 | 默认全局串行 | 稳；并行是显式开关 |
| 思考模式开关 | **不做** | 网页版没有可可靠读写的开关，不做无法验证的「成功」 |
| `/link` 兼容别名 | **不做** | 原本没有这两个命令，统一走 `session link\|unlink` |
| 跨桶列举 / `/reset` / `/cancel` / 任意 shell | **不做** | 语义模糊或涉及取消 / 权限 / 会话隔离，不能只靠字符串分支保证正确 |

---

## 16. 已知限制（如实写在前面，不藏在角落）

1. **速度**：受网页渲染与轮询限制，比官方 API 慢一个量级。
2. **改版脆弱**：选择器集中在 `config.py`，改版先跑 `recon.py` 复位，不凭猜测改。
3. **模型选择不精确**：`/v1/models` 的模型名不强制切换网页端实际模型。
4. **`usage` 是估算**：不可当计费依据。
5. **单用户**：没有真正的鉴权与多租户隔离，只监听回环。
6. **网页端权益变化**：额度、风控、灰度都会影响可用性，桥只如实报告失败，不绕过。

---

## 17. 不做的事（WONTFIX）

- 不复刻网页私有 HTTP 接口（路线 B）。
- 不做多租户 / 公网部署 / 内置鉴权体系。
- 不做「绕过风控 / 额度」。
- 不做语义模糊的命令（跨桶操作、取消、任意 shell）。
- 不为了「兼容姊妹项目」凭空加本站没有的能力。
