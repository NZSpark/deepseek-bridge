# 真机 E2E 测试设计（联网 + 复用登录态）

本文只讲 **`tests/e2e/`**：这一层会真的开浏览器、用你本地的登录态、让 DeepSeek 网页版生成内容。
它既慢（单轮分钟级）又会消耗真实会话额度，所以**必须显式开关，默认整包 skip**。

## 1. 与离线测试的边界

| 层 | 位置 | 联网 / 登录 | 默认入口 |
| --- | --- | --- | --- |
| 单元 / 路由回归 | `tests/*.py` | **不需要**（不联网、不起浏览器） | `pytest` |
| 真机契约冒烟 | `tests/e2e/test_bridge_contract.py` | **需要**：运行中的 bridge + 已登录的 `user_data/` | **只有** `DEEPSEEK_E2E=1` 才跑 |
| 人工排障 | `client_test.py` | 需要：同上 | 手动执行（示例客户端） |

`pyproject.toml` 里 `[tool.pytest.ini_options] testpaths = ["tests"]`，所以 `pytest` 默认会
**收集** `tests/e2e/`，但整包因开关缺失而 skip —— 这既保证「跑一次 `pytest` 能看到它们的存在」，
又保证它们不会在你不想联网时偷偷打真实账号。

## 2. 开关

| 变量 | 作用 |
| --- | --- |
| `DEEPSEEK_E2E=1` | **必须**。否则整包 skip。 |
| `DEEPSEEK_E2E_BASE_URL` | 运行中的 bridge 地址，默认 `http://127.0.0.1:8000`。 |
| `DEEPSEEK_E2E_TIMEOUT` | 单请求超时秒数，默认 300（真机生成慢，别用 HTTP 客户端的默认 5s）。 |
| `HEADLESS`（bridge 侧） | 首次登录必须 `HEADLESS=false`；登录过之后才能 `HEADLESS=1`。 |

## 3. 前置条件（缺一不可）

1. **bridge 已在运行**，且 `/healthz` 返回 `"browser_ready": true`。
   本层**不负责**启动服务：登录是要人工在有头浏览器里完成的，测试自己起服务只会拿到一个
   未登录的 503。
2. `user_data/` 里的登录态仍然有效（否则 `/healthz` 会 `degraded`，请求 503 `unavailable`）。
3. **没有第二个实例占用 `user_data/`**。同一个持久化 profile 被两个 Chromium 同时打开会
   互相锁死 —— 这也是 `tests/e2e/` 与手动 `client_test.py` 不要同时跑的原因。

> 也就是说：**真机层失败时，先看 `/healthz`，不要先怀疑选择器。**
> 「环境 / 登录态没就绪」与「选择器改版」表现相似，但归因与修法完全不同。

## 4. 判定矩阵（当前用例）

| 编号 | 用例 | 判据 |
| --- | --- | --- |
| A1 | `/healthz` | HTTP 200 + `status=ok` + `browser_ready=true` |
| A2 | `/v1/models` | 列表含 `deepseek-chat` |
| A6 | 空 `messages` | **4xx**（不是超时 / 500），且错误体是 `{"error":{message,type,code}}` |
| A3 | 非流式 chat | `content` 非空 + `finish_reason="stop"` + `usage.total_tokens > 0` |
| A4 | 流式 chat | 出现 `data: [DONE]`，且**有多个**非空增量帧（一整块 = 假流式） |

每个用例用**独立会话桶**（`X-DeepSeek-Session: e2e-<case>-<pid>`），不复用上一次失败的上下文。

## 5. 常见陷阱

- **端口**：宿主 shell 里的 `PORT` 会覆盖默认端口。真机层请显式给 `DEEPSEEK_E2E_BASE_URL`。
- **串线**：不用独立会话桶就可能读到上一轮的历史，把「串线」误判成「解析错误」。
- **无头**：无头会被站点的挑战页拦住，结果无效。真机必须**有头**（无显示服务器用 `xvfb-run`）。
- **未验证就勾选**：`skipped` 不等于通过。没跑过的项要写「未验证」并给出原因与下一步，
  不得含糊成「应该没问题」。

## 6. 怎么跑

```bash
# 1) 起服务（首次会有头浏览器，请在窗口里完成登录）
.venv/bin/python deepseek_api_server.py

# 2) 另开一个终端：确认就绪
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool | head -5

# 3) 跑真机契约冒烟
DEEPSEEK_E2E=1 DEEPSEEK_E2E_BASE_URL=http://127.0.0.1:8000 \
  .venv/bin/python -m pytest tests/e2e -v
```

跑完把**命令 + 时间 + 关键输出片段 + 未验证项**写进 `doc/acceptance.md`
（模板见 skill 的 `references/acceptance.md` §7）。
