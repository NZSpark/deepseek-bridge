# 项目知识（稳定事实）

> 摘要：把 DeepSeek 网页版包装成 OpenAI 兼容本地 API。FastAPI + Playwright + 持久化 Chromium profile。
> 入口 `deepseek_api_server.py`（薄封装）→ 实现全在 `deepseek_web/`（19 个模块，约 6700 行）。监听 127.0.0.1:8000。
> 改代码前读本文件；细节读 `doc/design.md` 与 `doc/tasks.md`。

---

## 定位

把 `chat.deepseek.com` 网页版桥接成本地 OpenAI 兼容 API，供 Pi / Codex CLI / OpenAI SDK 等
客户端使用。**不使用官方 API Key**，靠 Playwright 驱动真实 Chromium + 复用登录态。

## 技术栈

- Python 3.14（`.venv/`）、FastAPI + uvicorn + Pydantic
- Playwright（`launch_persistent_context`，profile 在 `user_data/`）
- 测试：pytest（17 个测试文件 + `tests/e2e/` 契约测试）；ruff 做 lint

## 目录约定

| 路径 | 行数 | 职责 |
| --- | --- | --- |
| `deepseek_api_server.py` | — | 入口薄封装，重新导出配置 + `uvicorn.run` |
| `deepseek_web/config.py` | 306 | 全部可调参数（`.env` 加载），**运行期唯一取值来源** |
| `deepseek_web/server.py` | 579 | FastAPI 路由装配、lifespan、健康检查 |
| `deepseek_web/chat_io.py` | 1469 | 浏览器输入 / 提交 / 解析核心（最大模块） |
| `deepseek_web/bridge_commands.py` | 625 | 对话内 `/bridge ...` 命令：查状态、管理当前会话桶 |
| `deepseek_web/responses.py` | 579 | `/v1/responses`（Codex 用），keepalive + 工具缓冲 |
| `deepseek_web/page_pool.py` | 429 | 页面池与并发锁（mixin，从 driver 拆出） |
| `deepseek_web/session_store.py` | 425 | 会话状态与持久化（mixin，从 driver 拆出） |
| `deepseek_web/toolcalls.py` | 395 | 工具注入与解析（模拟 function calling） |
| `deepseek_web/prompting.py` | 318 | 消息数组 → 输入框文本；纯文本工具函数 |
| `deepseek_web/streaming.py` | 300 | SSE 流式编码 |
| `deepseek_web/token_speed.py` | 278 | token 速度估算，只存计数与时长，不存正文 |
| `deepseek_web/completion.py` | 212 | 生成结束判定（mixin，多信号与门） |
| `deepseek_web/tasks.py` | 177 | 任务快照：网页会话轮转后无损续接 |
| `deepseek_web/driver.py` | 176 | Playwright Driver 装配（混入上述 mixin） |
| `deepseek_web/malformed.py` | 136 | 坏结构判据 |
| `deepseek_web/models.py` | 129 | OpenAI 兼容 Pydantic 模型 |
| `deepseek_web/toolcall_retry.py` | 74 | 坏结构重发循环 |
| `deepseek_web/errors.py` | 48 | Driver 层异常与常量（防循环依赖） |
| `tests/` | — | 不联网、不启浏览器的回归用例；`tests/e2e/` 契约测试 |
| `doc/` | — | 设计 / 施工图 / 验收 / 记忆 |
| `skills/` | — | skill 本地副本（**不进 git**） |
| `scripts/` | — | harness、`start_all_bridges.sh`、`setup_win11.ps1`、`live_image_test.py` |

## 运行

```bash
.venv/bin/python deepseek_api_server.py      # 启动（默认 127.0.0.1:8000）
.venv/bin/python -m pytest -q                # 全量测试
.venv/bin/ruff check .                       # lint
scripts/start_all_bridges.sh status          # 所有 bridge 状态
```

Windows 11 用 `scripts/setup_win11.ps1` 建 venv 并装依赖（见 README）。

## 接口

- `POST /v1/chat/completions`、`POST /v1/responses`、`GET /v1/models`
- `GET /healthz`（健康检查）、`POST /session/reset`（重置指定桶会话，非公开 schema）
- `GET /`（信息页）、`GET /debug/dom`（需 `DEEPSEEK_DEBUG=1`）
- 会话隔离：`X-DeepSeek-Session` / `user` 字段 > 按提示词工作目录自动分桶 > `User-Agent`

## 环境变量（关键）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` / `HOST` | 8000 / 127.0.0.1 | 监听地址 |
| `HEADLESS` | — | 无头模式 |
| `DEEPSEEK_TIMEOUT` | 180 | 单轮回复超时（`RESPONSE_TIMEOUT_S`） |
| `DEEPSEEK_RETRIES` | 2 | 上游超时重试 |
| `MALFORMED_RETRIES` | 2 | 坏 tool_call 重发次数，0=关闭 |
| `PROMPT_MAX_CHARS` | 48000 | 超长 prompt 截断阈值 |
| `MAX_SESSION_BUCKETS` | 8 | 会话桶上限 |
| `BUCKET_IDLE_TTL_S` | 900 | 桶空闲回收 |
| `PARALLEL_BUCKETS` | false | 桶间并行（默认串行） |
| `ENABLE_RESPONSES_API` | true | 开关 `/v1/responses` |
| `IMAGE_PASTE_MAX` | 4 | 图片粘贴上限 |

完整列表（含选择器、续写按钮、限流检测等）见 `deepseek_web/config.py` 与 `.env.example`。
