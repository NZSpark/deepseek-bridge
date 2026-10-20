# 施工图 · 任务清单（tasks.md）

> 依据：**[design.md](design.md)**（设计文档，HEAD `aa1ea71`）。
> 本文件是**编码阶段唯一要读的施工图**：每个任务都有可验证的「验收」与「回归」。
> 状态按当前代码实际能力标注，**不按历史计划标注**——过时的 `[x]` 比没有更坏。

---

## 图例与纪律

- `[x]` 已落地且有测试覆盖 · `[~]` 部分落地 / 有已知缺口 · `[ ]` 未开始
- **验收** = 完成标准（能跑出来的判定）；**回归** = 改动后必跑、必须仍绿的检查
- 纪律：① 有行为变化必须配回归测试；② 每条已修故障有**能变红**的用例；
  ③ 未验证就写「未验证」，不写「应该没问题」；④ 共享组件只调用不改签名。

**当前基线**（改动前后都应满足）：

```bash
.venv/bin/python -m pytest -q        # 337 passed, 5 skipped（skip 全是需真机的 E2E）
.venv/bin/ruff check .               # 本次改动文件必须干净
```

---

## A · 桥接内核（浏览器 → 回复）

### A1 页面与输入

- [x] **A1.1 惰性建页 + 持久化 profile**（`page_pool._ensure_page` / `launch_persistent_context`）
  - 验收：首轮请求才起浏览器；重启复用 `user_data/` 登录态
- [x] **A1.2 composer 候选全枚举、可见优先、逐个试填校验读回**（`chat_io._locate_input`）
  - 验收：同页多编辑器时选到可用的那个；读回为空即换下一个
  - 回归：`test_input_insert.py`（22 例）
- [x] **A1.3 写入：整段 fill 优先 → 分块兜底**（`_fill_prompt` / `_insert_prompt_in_chunks`）
  - 验收：超长 prompt 不卡死主线程；每块前重读只补缺失段（幂等）
  - 回归：`test_input_insert.py`
- [x] **A1.4 提交三级阶梯 + 验证**（`_submit_prompt`）
  - 验收：真实 Enter → 点发送 → 合成 Enter；每级后验「输入框已清空 或 消息已出现」
  - 验收：读不到内容时返回「无法判断」，不报错也不空等
- [x] **A1.5 长 prompt 截断告警**（`_clamp_prompt`，`PROMPT_MAX_CHARS=48000`）
- [x] **A1.6 图片粘贴**（`_download_image` / `_paste_images`，`IMAGE_PASTE_MAX=4`）
  - 验收：失败降级为文字引用，不让整轮失败
  - 回归：`test_image_upload.py`（18 例）

### A2 结束判定

- [x] **A2.1 多信号与门**：内容静默 / 长度静默 / 无节点快速失败 / 无进展快速失败 / 总超时
  - 阈值：`STABLE_POLLS=2`、`LEN_STABLE_POLLS=4`、`NO_NODE_FAIL_POLLS=10`、
    `NO_PROGRESS_FAIL_POLLS=40`、`RESPONSE_TIMEOUT_S=180`、`POLL_INTERVAL_S=1.5`
  - 回归：`test_end_detection.py`（11 例）
- [x] **A2.2 新回复两把钥匙**（节点数 > 基线 + 结构指纹），防「上一条当本轮」
- [x] **A2.3 「继续生成」续写**（`CONTINUE_BUTTON_ENABLED`，上限 5）
  - 回归：`test_continue_segments.py`（2 例）
- [x] **A2.4 提示词不写出原生工具标记形状**（P0-N）
  - 回归：能变红的用例（skill `check_templates.py` 的 p0n 模板）

### A3 代码块

- [x] **A3.1 代码块提取**（`_extract_code_blocks`，`CODE_BLOCK_SELECTOR` / `CODE_TAG_SELECTOR`）
- [ ] **A3.2 代码块落盘为文件**：设计里提到「回复含代码块 → 有落盘路径」
  - 现状：已提取但无独立落盘策略；是否需要先定策略（目录 / 命名 / 去重）
  - 验收：定策略后补用例；不定就标 WONTFIX 并写明原因

---

## B · OpenAI 契约层

### B1 Chat Completions

- [x] **B1.1 非流式 + 流式（SSE）**（`completion.py` / `streaming.py`）
  - 回归：`test_routes.py`（32 例）、`test_parsing.py`（45 例）
- [x] **B1.2 参数三态**：真生效 / 接受忽略 / 明确拒绝；`extra="allow"` 绝不 422
- [x] **B1.3 usage 估算且标注**（字符数 / 3），不作计费依据
- [x] **B1.4 `/v1/models` 与 `/bridge models` 同源**（`models.advertised_models()`）

### B2 错误语义

- [x] **B2.1 七类错误集中映射**（`server._error_response` 一处）
  - 回归：`test_routes.py`
- [x] **B2.2 「上游明确拒绝」与「上游无响应」可区分**（到顶 400 ≠ 超时 504）
- [ ] **B2.3 错误码补全**：目前未覆盖 `composer_not_found` / `prompt_submission_failed` 的
  HTTP 映射验证（设计第 11 节列了这两类，实现有对应异常，缺一条「按错误码断言」的路由测试）
  - 验收：`test_routes.py` 新增两条断言（异常 → 502 + 对应 type）

### B3 Responses API（Codex CLI）

- [x] **B3.1 命名事件序 + `sequence_number` 递增 + keep-alive**
  - 回归：`test_responses.py`（17 例）
- [x] **B3.2 工具路径事件**（`function_call_arguments.delta/done`）
- [x] **B3.3 默认开启，`ENABLE_RESPONSES_API=false` 时 404，对 Pi 零影响**

---

## C · 会话与页面池

### C1 分桶

- [x] **C1.1 四级优先级**：header → `user` → cwd → UA（`server._session_key`）
  - 回归：`test_multi_session.py`（50 例）
- [x] **C1.2 取值消毒 + 限长**（`SESSION_KEY_MAX_LEN=64`）
- [x] **C1.3 默认桶 `default` 保持历史行为**

### C2 轮转与播种

- [x] **C2.1 轮转阈值**：`SESSION_MAX_TURNS=60` / `SESSION_MAX_TOKENS=60000` / 到顶提示
- [x] **C2.2 轮转必配播种**（`SEED_MAX_CHARS=12000`），成对出现
- [x] **C2.3 到顶映射 400 `context_length_exceeded`**（不伪装超时）
- [x] **C2.4 `/session/reset` 逃生口**
  - 回归：`test_session_lifecycle.py`（35 例）

### C3 页面池自愈

- [x] **C3.1 判活 + 专用异常**（`DeepSeekPageLostError`；`is_closed()` 或选择器立刻抛错）
  - 回归：`test_page_recovery.py`（14 例）
- [x] **C3.2 重建 + 回到会话 URL；失败则新开 + 重放历史**
- [x] **C3.3 空闲回收 + LRU**（`BUCKET_IDLE_TTL_S=900`，`MAX_SESSION_BUCKETS=8`）
- [x] **C3.4 请求进行中的桶不被回收**
- [x] **C3.5 会话 URL 精确比较**（主机 + 路径，不用前缀）

### C4 并发与状态

- [x] **C4.1 默认全局串行；`PARALLEL_BUCKETS=true` 才按桶加锁**
- [x] **C4.2 同桶排队超时 → 503 `upstream_busy`，不触发重试阶梯**
- [x] **C4.3 按桶状态落盘**（绑定 / 播种标记 / 轮数 / 测速样本，只有数值）

---

## D · 工具调用（模拟 function calling）

- [x] **D1 按需注入**：客户端未声明 `tools` 就不注入任何脚手架
  - 验收：无工具请求的 prompt 里不含工具指令（token 预算护栏）
  - 回归：`test_parsing.py`
- [x] **D2 统一载体**：```tool_call 围栏；兼容 ```json 与旧标记行
- [x] **D3 工具名非法丢弃必须记日志**（静默丢弃 = 客户端永远收不到且无人知情）
- [x] **D4 别名归一化**（`bash`↔`shell` 唯一对应时；有歧义仍拒绝）
- [x] **D5 回填**：`role:"tool"` 拼回 prompt；空输出必须解释；同签名重复 ≥2 次点名
- [x] **D6 Responses 侧工具事件**（见 B3.2）

---

## E · 聊天内命令（`/bridge ...`）

> 细节见 [bridge_commands.md](bridge_commands.md)。命令是页面失效时**唯一**还能用的入口。

### E1 只读命令

- [x] **E1.1 `help [session]`**：帮助只从 `REGISTRY` 生成（唯一来源）
  - 回归：`test_bridge_commands.py`（53 例）
- [x] **E1.2 `status`**：桥接 / 浏览器 / 当前桶摘要
- [x] **E1.3 `models`**：与 `/v1/models` 同源
- [x] **E1.4 `token`**：只输出计数与时长；标注估算与非裸速度；无样本说「暂无样本」
  - 回归：`test_token_speed.py`（10 例）
- [x] **E1.5 `session [status]`**

### E2 改状态命令

- [x] **E2.1 `session reset`**：解除绑定 + 下一轮新会话 + 播种
- [x] **E2.2 `session reseed`**：一次性标记**落盘**，重启仍有效，成功后自动清除
- [x] **E2.3 `session link <URL> [--seed]`**：地址校验（https + 白名单 + 路径）、裸 ID 补全、
  桶忙时延后到下一轮生效
- [x] **E2.4 `session unlink`**：只解除绑定，不动当前页面
- [x] **E2.5 回执四句**：影响范围 / 不是删除 / 下一轮发生什么 / 怎么撤销

### E3 识别与路由

- [x] **E3.1 精确匹配**：整条消息就是那一行；正文 / 多行 / 代码块当普通提问
- [x] **E3.2 只看最后一条且必须是 `user`**（工具循环第二轮不重放旧命令）
- [x] **E3.3 命名空间保留**：参数不合法回用法，不执行也不丢给模型
- [x] **E3.4 浏览器未就绪时命令仍可用**（`is_command` 在就绪检查前分流）
- [x] **E3.5 两条协议共用 `handle_command`**
- [x] **E3.6 命令不写任务快照**

---

## F · 任务快照

- [x] **F1 目标 + 最近 8 条落盘**（`TASK_SNAPSHOT_ENABLED`，`TASK_KEEP_MESSAGES=8`）
  - 回归：`test_tasks.py`（15 例）
- [x] **F2 按 `TASK_NAMESPACE` 分子目录**，多项目共用目录不互相覆盖
- [x] **F3 用于新会话播种与重启续跑**（不是完整历史备份）

---

## G · 可观测性与安全

- [x] **G1 `/healthz`**：就绪 200 / 未就绪 503 `degraded`；含 `session_stats` / `cluster`
- [x] **G2 `/debug/dom`**：仅 `DEEPSEEK_DEBUG=1`，**不回显正文**
- [x] **G3 只监听 `127.0.0.1`**（`HOST` 默认值）
- [x] **G4 `user_data/` 与 `.env` 不进 git**
- [~] **G5 指标部分缺位**
  - 已有：`rewritten` 作为**测速质量标记**存在（`token_speed.py`，被标记的样本排除出
    聚合；`test_token_speed.py::test_rewritten_is_flagged_and_excluded_from_aggregate`），
    且 `session_store` 把 `rewritten` 视为无效测速样本
  - 缺失：它只是「测速时的标记」，**没有进程级计数器**，无法回答「今天发生了多少次流重写」；
    也没有 `session_link_miss_total`（link 落点不符的次数）
  - 验收：两个计数器 + 用例；`/healthz` 或 `/bridge status` 能读出来
  - 说明：不影响正确性，只影响排障时「正在发生」的证据强度

---

## H · 测试与验收

- [x] **H1 单测 337 例全绿、5 例 E2E 默认 skip**（`DEEPSEEK_E2E=1` 才跑）
- [x] **H2 单测不联网、不启浏览器**（假 page / 假 driver）
- [x] **H3 `ruff` 配置显式写死**（`select=["ALL"]` + 逐条豁免并写理由）
- [ ] **H4 契约冒烟脚本未入库**：skill 有 `scripts/smoke_test.py`，本项目 `scripts/` 只有
  `live_image_test.py`
  - 验收：把冒烟脚本落到 `scripts/smoke_test.py`（打真实端点：models / 非流式 / SSE 形状 /
    未知字段不 422 / 空输入 4xx / 错误体 / token 预算护栏）
- [x] **H5 真机对等 E2E 骨架**（`tests/e2e/test_bridge_contract.py` + [e2e_test_design.md](e2e_test_design.md)）
- [ ] **H6 验收记录待补本轮**：design.md 重写后，[acceptance.md](acceptance.md) 尚未记录

---

## 优先级与顺序

```
已全部落地（A/B/C/D/E/F 主干）
        │
        ├─▶ P1  B2.3 错误码补全测试        （小，补契约缺口）
        ├─▶ P2  H4   冒烟脚本入库          （中，验收自动化）
        ├─▶ P3  G5   两个指标 + 用例       （中，排障证据）
        ├─▶ P4  H6   补本轮验收记录        （小，文档闭环）
        └─▶ P5  A3.2 代码块落盘策略       （待定，可能 WONTFIX）
```

**建议顺序**：P1 → P4 → P2 → P3 → P5。
理由：P1 是纯补测试、风险最低且直接堵住契约缺口；P4 让文档闭环；P2 把验收从「手工」
变成「一条命令」；P3 依赖 P2 的冒烟框架；P5 需先做产品决策。

---

## 完成定义（DoD）

一个任务「完成」须同时满足：

1. 验收项能跑出来（不是「看起来对」）；
2. 有行为变化 → 配回归测试，且**把修复换回旧写法时用例必须变红**；
3. `pytest -q` 全绿且计数只增不削；`ruff check` 对改动文件干净；
4. 触及共享组件（driver / `_session_key` / prompting / toolcalls / config / streaming）
   时，`git diff` 中无签名或行为改动；
5. 文档同步：design.md 与实际不符立刻改；故障写进 update.md；验收写进 acceptance.md。

---

## 明确不做（WONTFIX）

| 项 | 原因 |
| --- | --- |
| 复刻网页私有 HTTP 接口 | 一次改版全废，且更难解释失败 |
| 多租户 / 公网部署 / 内置鉴权 | 定位是本地单用户；只监听回环 |
| 绕过风控 / 额度 | 不绕过，只如实报告失败 |
| 思考模式开关（`settings think`） | 网页版无可可靠读写的开关，不做无法验证的「成功」 |
| `/link`、`/unlink` 兼容别名 | 统一走 `session link\|unlink`，不凭空加 |
| 跨桶列举 / `/reset` / `/cancel` / 任意 shell | 语义模糊或涉及取消 / 权限 / 会话隔离 |
