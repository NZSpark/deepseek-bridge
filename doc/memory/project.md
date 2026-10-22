# 项目知识（稳定事实）

> 摘要：把 DeepSeek 网页版包装成 OpenAI 兼容本地 API。FastAPI + Playwright + 持久化 Chromium profile。
> 入口 `deepseek_api_server.py`（薄封装）→ 实现全在 `deepseek_web/`。监听 127.0.0.1:8000。
> 改代码前读本文件；细节读 `doc/design.md` 与 `doc/tasks.md`。

---

## 定位

把 `chat.deepseek.com` 网页版桥接成本地 OpenAI 兼容 API，供 Pi / Codex CLI / OpenAI SDK 等
客户端使用。**不使用官方 API Key**，靠 Playwright 驱动真实 Chromium + 复用登录态。

## 技术栈

- Python 3.14（`.venv/`）、FastAPI + uvicorn + Pydantic
- Playwright（`launch_persistent_context`，profile 在 `user_data/`）
- 测试：stdlib `unittest` + pytest；ruff 做 lint

## 目录约定

| 路径 | 职责 |
| --- | --- |
| `deepseek_api_server.py` | 入口薄封装，重新导出配置 + `uvicorn.run` |
| `deepseek_web/config.py` | 全部可调参数（`.env` 加载），**运行期唯一取值来源** |
| `deepseek_web/server.py` | FastAPI 路由（`/v1/models`、`/v1/chat/completions`、`/healthz`） |
| `deepseek_web/chat_io.py` | 浏览器输入 / 提交 / 解析核心（最大模块） |
| `deepseek_web/driver.py` | Playwright Driver + 会话持久化 |
| `deepseek_web/streaming.py` | SSE 流式编码 |
| `deepseek_web/responses.py` | `/v1/responses`（Codex 用） |
| `deepseek_web/toolcalls.py` | 工具注入与解析（模拟 function calling） |
| `deepseek_web/malformed.py` | 坏结构判据 |
| `deepseek_web/toolcall_retry.py` | 坏结构重发循环 |
| `tests/` | 不联网、不启浏览器的回归用例 |
| `doc/` | 设计 / 施工图 / 验收 / 记忆 |
| `skills/` | skill 本地副本（**不进 git**） |
| `scripts/` | 运维与 harness 工具 |

## 运行

```bash
.venv/bin/python deepseek_api_server.py      # 启动（默认 127.0.0.1:8000）
.venv/bin/python -m pytest -q                # 全量测试
.venv/bin/ruff check .                       # lint
scripts/start_all_bridges.sh status          # 所有 bridge 状态
```

## 接口

- `GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/responses`
- `GET /healthz`（健康检查）、`GET /debug/dom`（需 `DEEPSEEK_DEBUG=1`）
- 会话隔离：`X-DeepSeek-Session` / `user` 字段 > 按提示词工作目录自动分桶 > `User-Agent`

## 环境变量（关键）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` / `HOST` | 8000 / 127.0.0.1 | 监听地址 |
| `HEADLESS` | — | 无头模式 |
| `MALFORMED_RETRIES` | 2 | 坏 tool_call 重发次数，0=关闭 |
| `MAX_UPSTREAM_RETRIES` | 2 | 上游超时重试 |
| `RESPONSE_TIMEOUT_S` | 180 | 单轮回复超时 |
| `PROMPT_MAX_CHARS` | 48000 | 超长 prompt 截断阈值 |

完整列表见 `deepseek_web/config.py` 与 `.env.example`。
