"""真机契约冒烟（E2E）：打的是**运行中的** bridge，所以默认整包 skip。

为什么单独放在 ``tests/e2e/``：这一层是「联网 + 复用你本人登录态 + 真的让网页版生成」的
测试，既慢又会消耗真实会话额度，因此**必须显式开关**，且不得混进 ``pytest`` 的默认入口
（同族 ChatGPTBridge / ClaudeBridge / GeminiBridge / GrokBridge 都是这个约定）。

开关与地址（详见 doc/e2e_test_design.md）：

* ``DEEPSEEK_E2E=1``        —— **必须**，否则整包 skip；
* ``DEEPSEEK_E2E_BASE_URL`` —— 运行中的 bridge 地址，默认 ``http://127.0.0.1:8000``；
* ``DEEPSEEK_E2E_TIMEOUT``  —— 单个请求超时秒数，默认 300（真机生成慢）。

只用标准库（``urllib``）：本项目当前环境没有 httpx，装不了 starlette 的 TestClient，
而且这里要验的正是「客户端真正会走的那条 HTTP 路径」，自己发请求比 TestClient 更贴近现场。

每个用例使用**独立会话桶**，避免复用上一次失败的上下文（见 references/acceptance.md §5）。
"""

from __future__ import annotations

import json
import os
import unittest
import urllib.error
import urllib.request

from deepseek_web.streaming import CHUNK_CHARS

E2E_ENABLED = os.environ.get("DEEPSEEK_E2E") == "1"
BASE_URL = os.environ.get("DEEPSEEK_E2E_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SESSION_HEADER = os.environ.get("DEEPSEEK_E2E_SESSION_HEADER", "X-DeepSeek-Session")
TIMEOUT_S = float(os.environ.get("DEEPSEEK_E2E_TIMEOUT", "300"))

ALLOWED_SCHEMES = ("http://", "https://")

REQUIRED_SKIP_REASON = (
    "真机 E2E 默认 skip：需要 DEEPSEEK_E2E=1，且 bridge 已在运行（见 doc/e2e_test_design.md）"
)


class _UnsafeTargetError(ValueError):
    """目标地址不是 http(s)：拒绝打开（这条检查就是 S310 存在的理由）。"""


def open_url(req: urllib.request.Request):
    """全文件**唯一**的 urlopen 出口：先显式收窄 scheme，再交给 urllib。

    s310 关注的是「可能被 file:// 或自定义 scheme 命中」，所以这里不是压制检查，
    而是把它要求的前置条件真的做出来（并在下面注明豁免范围就是这一行）。
    """
    if not req.full_url.startswith(ALLOWED_SCHEMES):
        raise _UnsafeTargetError(f"只允许 http(s) 目标，收到：{req.full_url}")
    return urllib.request.urlopen(req, timeout=TIMEOUT_S)


def build_request(path: str, payload: dict | None, headers: dict | None) -> urllib.request.Request:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, method="POST" if data is not None else "GET"
    )
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    return req


def request(path: str, payload: dict | None = None, headers: dict | None = None):
    """返回 (status, body_text)；HTTP 错误码也当结果返回，而不是抛异常。"""
    try:
        with open_url(build_request(path, payload, headers)) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def chat_payload(prompt: str, stream: bool) -> dict:
    return {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }


def stream_chat(headers: dict) -> str:
    """发一条流式请求并收集原始 SSE 文本。"""
    req = build_request("/v1/chat/completions", chat_payload("数到三", stream=True), headers)
    chunks: list[str] = []
    with open_url(req) as resp:
        chunks.extend(raw.decode("utf-8", "replace") for raw in resp)
    return "".join(chunks)


@unittest.skipUnless(E2E_ENABLED, REQUIRED_SKIP_REASON)
class BridgeContractTests(unittest.TestCase):
    """四层验收里 ② 契约冒烟 + ③ 真机对等的最小可复跑入口。"""

    def bucket(self, name: str) -> dict:
        """每个用例一个独立会话桶，互不干扰（带 pid，重跑也不会撞上一次的上下文）。"""
        return {SESSION_HEADER: f"e2e-{name}-{os.getpid()}"}

    # ---------------- A1 就绪 ----------------
    def test_a1_healthz_reports_browser_ready(self):
        status, body = request("/healthz")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload.get("status"), "ok", payload)
        self.assertTrue(payload.get("browser_ready"), f"浏览器未就绪：{payload}")

    # ---------------- A2 模型发现 ----------------
    def test_a2_models_lists_deepseek_chat(self):
        status, body = request("/v1/models")
        self.assertEqual(status, 200, body)
        ids = [m["id"] for m in json.loads(body)["data"]]
        self.assertIn("deepseek-chat", ids)

    # ---------------- A6/A7 错误体 ----------------
    def test_a6_empty_messages_is_4xx_with_openai_error_body(self):
        empty = {"model": "deepseek-chat", "messages": [], "stream": False}
        status, body = request("/v1/chat/completions", empty, self.bucket("a6"))
        self.assertEqual(status, 400, body)
        error = json.loads(body)["error"]
        # 必须是 OpenAI 兼容错误体，不是裸 500 字符串
        for key in ("message", "type", "code"):
            self.assertIn(key, error, error)
        self.assertTrue(str(error["message"]).strip())

    # ---------------- A3 非流式契约 ----------------
    def test_a3_non_stream_chat_returns_content_and_usage(self):
        status, body = request(
            "/v1/chat/completions",
            chat_payload("只回复两个字：你好", stream=False),
            self.bucket("a3"),
        )
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        choice = payload["choices"][0]
        self.assertTrue(choice["message"]["content"].strip(), f"content 为空：{payload}")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertGreater(payload["usage"]["total_tokens"], 0)

    # ---------------- A4 流式契约 ----------------
    def test_a4_stream_chat_emits_chunks_and_done(self):
        text = stream_chat(self.bucket("a4"))
        self.assertIn("data: [DONE]", text, text[-400:])
        deltas = [
            json.loads(line[len("data: ") :])["choices"][0]["delta"]
            for line in text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        pieces = [d["content"] for d in deltas if d.get("content")]
        content = "".join(pieces)
        self.assertTrue(content.strip(), f"流式没有任何内容增量：{text[-400:]}")
        # 分帧契约（streaming.py 的 CHUNK_CHARS）：每帧不超过一个切片，且帧数刚好是向上取整。
        # 这样写不依赖「模型这次回了多长」——短回复 1 帧、长回复多帧都成立。
        self.assertTrue(
            all(len(p) <= CHUNK_CHARS for p in pieces),
            f"有帧超过 {CHUNK_CHARS} 字符：{[len(p) for p in pieces]}",
        )
        expected_frames = max(1, -(-len(content) // CHUNK_CHARS))
        self.assertEqual(len(pieces), expected_frames, f"content={len(content)} 字符")


if __name__ == "__main__":
    unittest.main()
