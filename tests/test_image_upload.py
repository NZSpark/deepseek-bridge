"""图片上传（合成 paste 事件）的回归测试，用假 page 驱动，不开浏览器。

参照 DoubaoBridge TASK-208 的 image_upload.md：
  * base64 data URI 解码为字节；远程 URL 惰性下载；
  * 文本引用行只记类型与估算大小，**不含完整 base64 字节**（不外泄 + 防 prompt 爆炸）；
  * 提交前在输入框上合成 paste（File + DataTransfer + ClipboardEvent）；
  * 任何一步失败都优雅降级，绝不阻断文本提交；
  * 开关关闭时不粘贴，全部退回文本引用。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import base64
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv
from deepseek_web import config
from deepseek_web.prompting import ImageAttachment, extract_images

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03"
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()


def user_msg(*parts):
    return {"role": "user", "content": list(parts)}


def image_part(uri):
    return {"type": "image_url", "image_url": {"url": uri}}


class ExtractImagesTests(unittest.TestCase):
    def test_base64_is_decoded_to_bytes(self):
        images = extract_images([user_msg({"type": "text", "text": "看这个"},
                                          image_part(PNG_URI))])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].data, PNG_BYTES)
        self.assertEqual(images[0].mime, "image/png")
        self.assertEqual(images[0].name, "image.png")
        self.assertIsNone(images[0].url)

    def test_remote_url_is_kept_for_lazy_download(self):
        images = extract_images([user_msg(image_part("https://x/y.jpg?a=1"))])
        self.assertEqual(len(images), 1)
        self.assertIsNone(images[0].data)
        self.assertEqual(images[0].url, "https://x/y.jpg?a=1")
        self.assertEqual(images[0].name, "y.jpg")

    def test_image_url_may_be_a_bare_string(self):
        images = extract_images([user_msg({"type": "image_url", "image_url": PNG_URI})])
        self.assertEqual(images[0].data, PNG_BYTES)

    def test_non_image_parts_are_skipped(self):
        images = extract_images([user_msg({"type": "text", "text": "hi"})])
        self.assertEqual(images, [])

    def test_invalid_base64_is_skipped(self):
        bad = "data:image/png;base64,%%%%"
        self.assertEqual(extract_images([user_msg(image_part(bad))]), [])

    def test_order_is_preserved(self):
        second = b"\x89PNG-second"
        uri2 = "data:image/jpeg;base64," + base64.b64encode(second).decode()
        images = extract_images([
            user_msg(image_part(PNG_URI)),
            user_msg(image_part(uri2)),
        ])
        self.assertEqual([i.data for i in images], [PNG_BYTES, second])


class ExtractImagesDeltaTests(unittest.TestCase):
    """only_delta=True：只粘贴本轮新增消息里的图片，历史图片不重复粘贴。"""

    def test_history_images_are_not_re_pasted(self):
        # 第一轮带图，assistant 回复后，第二轮只发文本：不应再抽到任何图。
        messages = [
            user_msg(image_part(PNG_URI)),
            {"role": "assistant", "content": "我看到了"},
            {"role": "user", "content": "再描述一次"},
        ]
        self.assertEqual(extract_images(messages, only_delta=True), [])
        # 全量抽取仍能拿到历史图片（兼容默认行为）
        self.assertEqual(len(extract_images(messages)), 1)

    def test_new_turn_image_is_pasted(self):
        second = b"\x89PNG-second"
        uri2 = "data:image/png;base64," + base64.b64encode(second).decode()
        messages = [
            user_msg(image_part(PNG_URI)),
            {"role": "assistant", "content": "我看到了"},
            user_msg(image_part(uri2)),
        ]
        images = extract_images(messages, only_delta=True)
        self.assertEqual([i.data for i in images], [second])

    def test_no_assistant_means_all_images(self):
        # 首轮没有 assistant：只有一条带图 user，仍应抽到。
        images = extract_images([user_msg(image_part(PNG_URI))], only_delta=True)
        self.assertEqual(len(images), 1)


class ImageReferenceTests(unittest.TestCase):
    def test_data_uri_reference_hides_full_bytes(self):
        text = srv._content_to_text([
            {"type": "text", "text": "看这个"},
            image_part(PNG_URI),
        ])
        self.assertIn("[图片]", text)
        self.assertIn("image/png", text)
        self.assertIn("base64", text)
        # 绝不回显完整 base64 正文
        payload = PNG_URI.split(",", 1)[1]
        self.assertNotIn(payload, text)

    def test_remote_url_reference_keeps_url(self):
        text = srv._content_to_text([image_part("https://x/y.jpg")])
        self.assertEqual(text, "[图片] https://x/y.jpg")

    def test_plain_string_unchanged(self):
        self.assertEqual(srv._content_to_text("hi"), "hi")


class PasteImagesTests(unittest.TestCase):
    """直接驱动 ChatIOMixin._paste_images（用假 page / 假 composer）。"""

    def _driver(self, page, context=None):
        driver = srv.DeepSeekWebDriver()
        driver.page = page
        driver.context = context
        return driver

    def test_dispatches_clipboard_event_with_raw_bytes(self):
        calls = []

        class FakePage:
            async def evaluate(self, script, arg):
                calls.append((script, arg))
                return True

        class FakeComposer:
            async def evaluate(self, *a):
                return True

            async def click(self, **k):
                return None

        driver = self._driver(FakePage())
        images = [ImageAttachment(name="a.png", mime="image/png", data=PNG_BYTES)]
        pasted = asyncio.run(driver._paste_images(FakePage(), FakeComposer(), images))
        self.assertEqual(pasted, 1)
        name, mime, byte_list = calls[0][1]
        self.assertEqual(name, "a.png")
        self.assertEqual(mime, "image/png")
        self.assertEqual(bytes(byte_list), PNG_BYTES)
        self.assertIn("ClipboardEvent", calls[0][0])
        self.assertIn("DataTransfer", calls[0][0])

    def test_degrades_on_dispatch_error(self):
        class BoomPage:
            async def evaluate(self, script, arg):
                raise RuntimeError("nope")

        class FakeComposer:
            async def evaluate(self, *a):
                return True

            async def click(self, **k):
                return None

        driver = self._driver(BoomPage())
        images = [ImageAttachment(name="a.png", mime="image/png", data=b"x")]
        self.assertEqual(asyncio.run(driver._paste_images(BoomPage(), FakeComposer(), images)), 0)

    def test_disabled_switch_pastes_nothing(self):
        with unittest.mock.patch.object(config, "IMAGE_PASTE_ENABLED", False):
            driver = self._driver(object())
            images = [ImageAttachment(name="a.png", mime="image/png", data=b"x")]
            self.assertEqual(asyncio.run(driver._paste_images(object(), None, images)), 0)

    def test_remote_url_is_downloaded(self):
        calls = []

        class FakePage:
            async def evaluate(self, script, arg):
                calls.append(arg)
                return True

        driver = self._driver(FakePage())

        async def fake_download(url):
            return PNG_BYTES

        driver._download_image = fake_download
        images = [ImageAttachment(name="y.png", mime="image/png", url="https://x/y.png")]
        pasted = asyncio.run(driver._paste_images(FakePage(), None, images))
        self.assertEqual(pasted, 1)
        self.assertEqual(bytes(calls[0][2]), PNG_BYTES)

    def test_download_failure_is_skipped(self):
        class FakePage:
            async def evaluate(self, script, arg):
                raise AssertionError("should not dispatch when download fails")

        driver = self._driver(FakePage())

        async def fake_download(url):
            return None

        driver._download_image = fake_download
        images = [ImageAttachment(name="y.png", mime="image/png", url="https://x/y.png")]
        self.assertEqual(asyncio.run(driver._paste_images(FakePage(), None, images)), 0)


class SendChatWiresImagesTests(unittest.TestCase):
    """send_chat 必须把 images 透传到 _send_chat_locked。"""

    def test_images_are_forwarded(self):
        driver = srv.DeepSeekWebDriver()
        captured = {}

        async def fake_locked(prompt, on_delta=None, key=None, images=None):
            captured["images"] = images
            return "ok"

        async def fake_ensure_page(bucket):
            return None

        driver._send_chat_locked = fake_locked
        driver._ensure_page = fake_ensure_page
        imgs = [ImageAttachment(name="a.png", mime="image/png", data=b"x")]
        asyncio.run(driver.send_chat("hi", images=imgs))
        self.assertEqual(captured["images"], imgs)


if __name__ == "__main__":
    unittest.main()
