#!/usr/bin/env python3
"""实际联机测试：把 doc/speed.png 作为 base64 data URI 发给本地 bridge，
让网页版 DeepSeek 描述图片内容，验证图片 paste 是否真的被处理。

用法：python scripts/live_image_test.py
需要本地服务已在 127.0.0.1:8000 运行（且运行的是含图片支持的新代码）。
"""

import base64
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE = ROOT / "doc" / "speed.png"
URL = "http://127.0.0.1:8000/v1/chat/completions"

PROMPT = (
    "请只根据你**看到的图片像素内容**回答，不要猜测。"
    "这张图里有哪些界面元素、文字或图表？主要颜色是什么？"
    "如果是网页截图或数据图表，请说出你能辨认出的具体标题/数字。"
    "若你无法看到图片（只收到 [图片] 文本占位），请直接回答『我看不到图片』。"
)


def data_uri(path: Path) -> str:
    raw = path.read_bytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def call(messages, timeout=180):
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": messages,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"Content-Type": "application/json"}
    )
    # URL 是下方写死的本地常量，不存在用户可控 scheme。
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def main():
    if not IMAGE.exists():
        print(f"缺少图片：{IMAGE}")
        return 1
    size = IMAGE.stat().st_size
    uri = data_uri(IMAGE)
    print(f"图片：{IMAGE.name}  {size} 字节  data-uri {len(uri)} 字符\n")

    # --- 测试 A：带图片 ---
    print("=" * 60)
    print("测试 A：带图片（image_url = base64 data URI）")
    print("=" * 60)
    msg_a = [{
        "role": "user",
        "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": uri}},
        ],
    }]
    try:
        out_a = call(msg_a)
        reply_a = out_a["choices"][0]["message"]["content"]
        print("网页版回复：\n" + reply_a)
        print(f"\nusage: {out_a.get('usage')}")
    except Exception as exc:  # noqa: BLE001 冒烟脚本：任何失败都打印并以非零退出
        print(f"[失败] {type(exc).__name__}: {exc}")
        return 1

    # --- 测试 B：对照（无图片）---
    print("\n" + "=" * 60)
    print("测试 B：对照——相同提示词，但不带图片")
    print("=" * 60)
    msg_b = [{"role": "user", "content": PROMPT}]
    try:
        out_b = call(msg_b)
        reply_b = out_b["choices"][0]["message"]["content"]
        print("网页版回复：\n" + reply_b)
    except Exception as exc:  # noqa: BLE001 同上
        print(f"[失败] {type(exc).__name__}: {exc}")
        return 1

    # --- 判定 ---
    print("\n" + "=" * 60)
    print("判定")
    print("=" * 60)
    blind_markers = [
        "我看不到图片", "无法看到图片", "看不到图片",
        "没有收到图片", "只收到", "无法查看图片",
    ]
    blind = any(k in reply_a for k in blind_markers)
    if blind:
        print("结果：❌ 模型表示看不到图片——图片 paste 很可能没有生效。")
    elif reply_a.strip() == reply_b.strip():
        print("结果：⚠️ 带图与不带图的回复完全相同——图片可能未影响回答。")
    else:
        print("结果：✅ 带图回复与对照不同，模型给出了基于图片内容的描述。")
        print("（请人工核对下方带图回复是否真的描述了 speed.png 的画面）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
