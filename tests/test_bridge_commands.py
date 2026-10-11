"""Bridge 聊天命令（``/bridge ...``）回归测试。

覆盖 doc/bridge_commands.md 的测试清单：精确匹配（多行 / 正文 / 代码块不执行）、
参数校验、只影响当前桶、浏览器未就绪时只读命令仍可用、两条协议路径一致、
改状态命令的回执与作用范围、命令不写任务快照、以及落盘偏好的优先级。

不依赖 httpx：直接 await 路由函数 / handle_command，用假 driver 替换单例。

运行：.venv/bin/python -m unittest discover -s tests -t . -v
"""

import asyncio
import json
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deepseek_api_server as srv  # noqa: E402
from deepseek_web import bridge_commands, config, models, responses, server, tasks  # noqa: E402
from deepseek_web.models import ChatMessage  # noqa: E402

SESSION_URL = "https://chat.deepseek.com/a/chat/s/483878e7-7179-4e63-a19b-365ad11a686e"
SESSION_ID = "483878e7-7179-4e63-a19b-365ad11a686e"


class FakeDriver:
    """记录命令都调用了什么，以及按桶返回的状态。"""

    def __init__(self, page=True, init_error=None, linked=None, needs_seed=False, stats=None):
        self.page = object() if page else None
        self.init_error = init_error
        self.linked = linked
        self.needs_seed_flag = needs_seed
        self.stats = dict(stats or {})
        self.save_files_pref = None
        self.calls = []
        self.chats = []
        self.saved = []

    # ---- 命令用到的那部分 ----
    def session_stats(self, key=None):
        self.calls.append(("session_stats", key))
        payload = {
            "url": SESSION_URL,
            "linked_url": self.linked,
            "has_history": not self.needs_seed_flag,
            "needs_seed": self.needs_seed_flag,
            "turns": 3,
            "est_tokens": 120,
            "cap_hit": False,
            "pending_rotation": False,
            "pending_reseed": False,
            "save_files": self.save_files_pref,
            "last_error": None,
        }
        payload.update(self.stats)
        return payload

    def linked_session_url(self, key=None):
        self.calls.append(("linked_session_url", key))
        return self.linked

    def reset_session(self, key=None):
        self.calls.append(("reset_session", key))
        self.linked = None

    def reseed_session(self, key=None):
        self.calls.append(("reseed_session", key))

    async def link_session(self, url, seed=False, key=None):
        self.calls.append(("link_session", url, seed, key))
        self.linked = url
        self.needs_seed_flag = bool(seed)

    def unlink_session(self, key=None):
        self.calls.append(("unlink_session", key))
        had = self.linked is not None
        self.linked = None
        return had

    def save_files_preference(self, key=None):
        return self.save_files_pref

    def set_save_files_preference(self, value, key=None):
        self.calls.append(("set_save_files_preference", value, key))
        self.save_files_pref = value

    # ---- 普通请求用到的那部分（路由测试）----
    def needs_seed(self, key=None):
        return self.needs_seed_flag

    def sent_prompt(self, key=None):
        return None

    def session_keys(self):
        return ["default"]

    def busy_keys(self):
        return []

    def cluster_stats(self):
        return {"parallel": False, "max_buckets": 8, "busy": [], "keys": ["default"]}

    async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None):
        self.chats.append({"prompt": prompt, "key": key})
        return "来自网页版的回复", []

    def save_extracted_files(self, raw_text, code_blocks, output_dir):
        self.saved.append((raw_text, code_blocks, output_dir))
        return ["/tmp/fake.md"]


def call_handle(text, driver=None, bucket=None):
    driver = driver or FakeDriver()
    reply = asyncio.run(bridge_commands.handle_command(
        [ChatMessage(role="user", content=text)], driver, bucket
    ))
    return reply, driver


# ==================== 解析 ====================


class ParseTests(unittest.TestCase):
    def parse(self, text):
        return bridge_commands.parse_command(text)

    def test_bare_namespace_is_help(self):
        command = self.parse("/bridge")
        self.assertEqual(command.action, "help")
        self.assertEqual(command.subaction, "")

    def test_top_level_commands(self):
        self.assertEqual(self.parse("/bridge status").action, "status")
        self.assertEqual(self.parse("/bridge models").action, "models")
        self.assertEqual(self.parse("/BRIDGE STATUS").action, "status")

    def test_help_topics(self):
        self.assertEqual(self.parse("/bridge help").subaction, "")
        self.assertEqual(self.parse("/bridge help session").subaction, "session")
        self.assertIn("没有这个帮助主题", self.parse("/bridge help nope").reason)
        self.assertIn("最多接受一个主题", self.parse("/bridge help session extra").reason)

    def test_session_subcommands(self):
        self.assertEqual(self.parse("/bridge session").subaction, "status")
        self.assertEqual(self.parse("/bridge session status").subaction, "status")
        for sub in ("reset", "reseed", "unlink"):
            self.assertEqual(self.parse(f"/bridge session {sub}").subaction, sub)
        self.assertIn("没有这个子命令", self.parse("/bridge session drop").reason)
        # 不接受参数的子命令给了参数必须报错，而不是忽略
        self.assertIn("不接受参数", self.parse("/bridge session reset now").reason)

    def test_session_link_accepts_url_and_seed_flag(self):
        command = self.parse(f"/bridge session link {SESSION_URL} --seed")
        self.assertEqual(command.subaction, "link")
        self.assertEqual(command.target, SESSION_URL)
        self.assertTrue(command.seed)
        self.assertFalse(self.parse(f"/bridge session link {SESSION_URL}").seed)

    def test_session_link_rejects_bad_arguments(self):
        self.assertIn("还需要一个会话地址", self.parse("/bridge session link").reason)
        self.assertIn("不认识选项", self.parse(f"/bridge session link {SESSION_URL} --x").reason)
        self.assertIn("不能有空格", self.parse("/bridge session link a b").reason)

    def test_settings(self):
        self.assertEqual(self.parse("/bridge settings save-files").subaction, "status")
        self.assertEqual(self.parse("/bridge settings save-files on").value, True)
        self.assertEqual(self.parse("/bridge settings save-files off").value, False)
        self.assertIsNone(self.parse("/bridge settings save-files default").value)
        self.assertIn("没有这个设置项", self.parse("/bridge settings think on").reason)
        self.assertIn("还需要一个设置名", self.parse("/bridge settings").reason)
        self.assertIn("最多接受一个取值", self.parse("/bridge settings save-files on off").reason)
        self.assertIn("不支持取值", self.parse("/bridge settings save-files maybe").reason)

    def test_invalid_commands_carry_usage(self):
        command = self.parse("/bridge nope")
        self.assertEqual(command.action, "invalid")
        self.assertIn(bridge_commands.BRIDGE_COMMAND, command.reason)

    def test_multiline_and_embedded_text_is_not_a_command(self):
        self.assertIsNone(self.parse("/bridge session reset\n请解释一下"))
        self.assertIsNone(self.parse("好的，/bridge session reset"))
        self.assertIsNone(self.parse("```\n/bridge session reset\n```"))
        self.assertIsNone(self.parse("> /bridge status"))
        self.assertIsNone(self.parse(""))

    def test_other_slash_text_is_not_a_command(self):
        self.assertIsNone(self.parse("/helpp"))
        self.assertIsNone(self.parse("/foo bar"))
        self.assertIsNone(self.parse("请解释 /bridge 命令"))

    def test_history_commands_are_not_replayed(self):
        # 只有「最后一条消息」是 user 时才可能是命令：工具循环里后续请求带着同一批
        # 历史（末尾是 tool 结果）时，那个命令不能每轮都被重新执行。
        messages = [
            ChatMessage(role="user", content="/bridge session reset"),
            ChatMessage(role="assistant", content="ok"),
            ChatMessage(role="tool", content="done", tool_call_id="c1"),
        ]
        self.assertIsNone(bridge_commands.latest_user_text(messages))
        self.assertFalse(bridge_commands.is_command(messages))

    def test_last_user_message_works_for_dicts_and_models(self):
        as_dicts = [{"role": "user", "content": "/bridge status"}]
        self.assertTrue(bridge_commands.is_command(as_dicts))
        # OpenAI 的分片数组形式
        parts = [{"role": "user", "content": [{"type": "text", "text": "/bridge status"}]}]
        self.assertTrue(bridge_commands.is_command(parts))
        self.assertFalse(bridge_commands.is_command([{"role": "user", "content": "你好"}]))
        self.assertFalse(bridge_commands.is_command([]))


class SessionUrlTests(unittest.TestCase):
    def test_accepts_canonical_url_and_bare_id(self):
        self.assertEqual(bridge_commands.normalize_session_url(SESSION_URL), SESSION_URL)
        self.assertEqual(bridge_commands.normalize_session_url(SESSION_ID), SESSION_URL)
        self.assertEqual(
            bridge_commands.normalize_session_url(f"  <{SESSION_URL}>  "), SESSION_URL
        )

    def test_rejects_foreign_hosts_schemes_and_paths(self):
        self.assertIsNone(bridge_commands.normalize_session_url("http://chat.deepseek.com/a/chat/s/x1"))
        self.assertIsNone(bridge_commands.normalize_session_url("https://evil.example/a/chat/s/x1"))
        self.assertIsNone(bridge_commands.normalize_session_url("https://chat.deepseek.com/"))
        self.assertIsNone(bridge_commands.normalize_session_url("https://chat.deepseek.com/a/chat/s/short"))
        self.assertIsNone(bridge_commands.normalize_session_url(""))
        self.assertIsNone(bridge_commands.normalize_session_url("../../etc/passwd"))


# ==================== 执行 ====================


class HelpTests(unittest.TestCase):
    def test_help_lists_the_registry(self):
        reply, _ = call_handle("/bridge help")
        for spec in bridge_commands.REGISTRY:
            self.assertIn(spec.syntax, reply, spec.syntax)
        self.assertIn("只读", reply)
        self.assertIn("改状态", reply)

    def test_topic_help_filters_groups(self):
        reply, _ = call_handle("/bridge help session")
        self.assertIn("/bridge session reset", reply)
        self.assertNotIn("settings save-files", reply)
        settings_reply, _ = call_handle("/bridge help settings")
        self.assertIn("settings save-files", settings_reply)
        self.assertNotIn("session reset", settings_reply)

    def test_every_registry_entry_is_executable(self):
        # 帮助与实现不能漂移：注册表里的每条命令（去掉 [可选] 段后的最小形式）都要真的能执行
        for spec in bridge_commands.REGISTRY:
            invocation = re.sub(r"\s*\[[^\]]*\]", "", spec.syntax).replace("<URL>", SESSION_ID)
            command = bridge_commands.parse_command(invocation)
            self.assertIsNotNone(command, spec.syntax)
            self.assertNotEqual(command.action, "invalid", spec.syntax)


class StatusTests(unittest.TestCase):
    def test_status_mentions_bucket_browser_and_scoping(self):
        reply, driver = call_handle("/bridge status", bucket="pi-task-1")
        self.assertIn("[Bridge] 桥接状态", reply)
        self.assertIn("当前桶：pi-task-1", reply)
        self.assertIn("请求处理：可用", reply)
        self.assertIn("浏览器：已就绪", reply)
        self.assertIn(config.SESSION_KEY_HEADER, reply)
        self.assertIn("会话绑定：未绑定", reply)
        # 只查当前桶：不会去枚举其它桶
        self.assertIn(("session_stats", "pi-task-1"), driver.calls)
        self.assertNotIn("buckets", reply)

    def test_status_when_browser_missing_reports_reason(self):
        reply, _ = call_handle(
            "/bridge status", FakeDriver(page=False, init_error="profile 被占用\n堆栈细节")
        )
        self.assertIn("请求处理：不可用", reply)
        self.assertIn("profile 被占用", reply)
        self.assertNotIn("堆栈细节", reply)  # 只保留第一行
        self.assertIn("只读命令仍可用", reply)

    def test_session_status_reports_seed_and_rotation(self):
        seeded, _ = call_handle("/bridge session status", FakeDriver(needs_seed=True))
        self.assertIn("需要播种", seeded)
        rotating, _ = call_handle(
            "/bridge session", FakeDriver(stats={"pending_rotation": True})
        )
        self.assertIn("已请求重置", rotating)
        reseeding, _ = call_handle(
            "/bridge session", FakeDriver(stats={"pending_reseed": True})
        )
        self.assertIn("已排队重新播种", reseeding)

    def test_models_matches_the_route(self):
        reply, _ = call_handle("/bridge models")
        expected = [card["id"] for card in models.advertised_models()]
        for model_id in expected:
            self.assertIn(model_id, reply)
        # 与 GET /v1/models 同源（同一函数）
        route_ids = [card.id for card in asyncio.run(server.list_models()).data]
        self.assertEqual(route_ids, expected)


class SessionCommandTests(unittest.TestCase):
    def test_reset_targets_current_bucket_and_explains_scope(self):
        reply, driver = call_handle("/bridge session reset", bucket="pi-task-2")
        self.assertIn(("reset_session", "pi-task-2"), driver.calls)
        self.assertIn("播种", reply)
        self.assertIn("不是删除客户端的对话历史", reply)
        self.assertIn("只影响这个会话桶", reply)

    def test_reset_reports_the_binding_it_dropped(self):
        reply, _ = call_handle("/bridge session reset", FakeDriver(linked=SESSION_URL))
        self.assertIn("绑定已解除", reply)

    def test_reseed_queues_a_one_shot_marker(self):
        driver = FakeDriver(linked=SESSION_URL)
        reply, _ = call_handle("/bridge session reseed", driver, bucket="pi-task-3")
        self.assertIn(("reseed_session", "pi-task-3"), driver.calls)
        self.assertIn("重复内容", reply)
        self.assertIn(SESSION_URL, reply)

    def test_link_rejects_bad_address_without_touching_the_driver(self):
        for bad in ("http://chat.deepseek.com/a/chat/s/" + SESSION_ID,
                    "https://evil.example/a/chat/s/" + SESSION_ID,
                    "https://chat.deepseek.com/"):
            reply, driver = call_handle(f"/bridge session link {bad}")
            self.assertIn("命令未执行", reply)
            self.assertEqual([c for c in driver.calls if c[0] == "link_session"], [])

    def test_link_normalizes_and_forwards_seed(self):
        reply, driver = call_handle(f"/bridge session link {SESSION_ID} --seed", bucket="pi-task-4")
        self.assertIn(("link_session", SESSION_URL, True, "pi-task-4"), driver.calls)
        self.assertIn(SESSION_URL, reply)
        self.assertIn("播种", reply)

    def test_link_without_seed_assumes_existing_context(self):
        reply, driver = call_handle(f"/bridge session link {SESSION_URL}")
        self.assertIn(("link_session", SESSION_URL, False, "default"), driver.calls)
        self.assertIn("已有此前上下文", reply)

    def test_unlink(self):
        reply, driver = call_handle("/bridge session unlink", FakeDriver(linked=SESSION_URL))
        self.assertIn("已解除", reply)
        self.assertIn(("unlink_session", "default"), driver.calls)
        empty, _ = call_handle("/bridge session unlink", FakeDriver())
        self.assertIn("本来就没有绑定", empty)


class SettingsTests(unittest.TestCase):
    def test_status_reports_preference_and_effective_value(self):
        reply, _ = call_handle("/bridge settings save-files status")
        self.assertIn("跟随全局配置", reply)
        self.assertIn("SAVE_FILES", reply)
        self.assertIn(config.OUTPUT_DIR, reply)
        self.assertIn("不会覆盖已有文件", reply)

    def test_set_records_per_bucket_preference(self):
        driver = FakeDriver()
        reply, _ = call_handle("/bridge settings save-files off", driver, bucket="pi-task-5")
        self.assertIn(("set_save_files_preference", False, "pi-task-5"), driver.calls)
        self.assertIn("已记录设置", reply)

        default_reply, driver2 = call_handle("/bridge settings save-files default", FakeDriver())
        self.assertIn(("set_save_files_preference", None, "default"), driver2.calls)
        self.assertIn("跟随全局配置", default_reply)


class HandleCommandTests(unittest.TestCase):
    def test_plain_text_is_not_a_command(self):
        driver = FakeDriver()
        reply = asyncio.run(bridge_commands.handle_command(
            [ChatMessage(role="user", content="帮我改一下 README")], driver
        ))
        self.assertIsNone(reply)
        self.assertEqual(driver.calls, [])

    def test_invalid_command_returns_usage_without_calling_the_driver(self):
        reply, driver = call_handle("/bridge session drop")
        self.assertIn("命令未执行", reply)
        self.assertIn(bridge_commands.BRIDGE_COMMAND, reply)
        self.assertEqual(driver.calls, [])

    def test_driver_failure_is_reported_without_internals(self):
        driver = FakeDriver()
        driver.session_stats = unittest.mock.Mock(side_effect=RuntimeError("内部堆栈 /secret"))
        # 详细异常（含堆栈）只进日志，因此这里把日志替身掉，同时确认它确实被调用了
        with unittest.mock.patch.object(bridge_commands, "logger") as logger:
            reply = asyncio.run(bridge_commands.handle_command(
                [ChatMessage(role="user", content="/bridge status")], driver
            ))
        self.assertTrue(logger.warning.called)
        self.assertIn("命令未执行", reply)
        self.assertIn("RuntimeError", reply)
        self.assertNotIn("/secret", reply)


# ==================== 路由集成（chat + responses）====================


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDriver()
        patch = unittest.mock.patch.object(server, "driver", self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def chat_request(text, **extra):
        payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": text}]}
        payload.update(extra)
        return srv.ChatCompletionRequest(**payload)

    def call(self, request, header=None):
        return asyncio.run(server.chat_completions(request, header))

    def body(self, response):
        if hasattr(response, "body"):
            return json.loads(response.body)
        return response.model_dump()


class ChatCommandRouteTests(RouteTestCase):
    def test_command_is_answered_locally(self):
        response = self.call(self.chat_request("/bridge status"))
        payload = self.body(response)
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertIn("[Bridge] 桥接状态", payload["choices"][0]["message"]["content"])
        # 命令不发给网页版，也不算网页版用量
        self.assertEqual(self.fake.chats, [])
        self.assertEqual(payload["usage"]["prompt_tokens"], 0)
        self.assertGreater(payload["usage"]["completion_tokens"], 0)

    def test_command_does_not_write_a_task_snapshot(self):
        with unittest.mock.patch.object(tasks, "record") as record:
            self.call(self.chat_request("/bridge status"))
        record.assert_not_called()

    def test_plain_prompt_still_reaches_the_driver(self):
        payload = self.body(self.call(self.chat_request("你好")))
        self.assertEqual(payload["choices"][0]["message"]["content"], "来自网页版的回复")
        self.assertEqual(len(self.fake.chats), 1)

    def test_command_is_answered_when_browser_is_down(self):
        self.fake.page = None
        self.fake.init_error = "登录页未就绪"
        payload = self.body(self.call(self.chat_request("/bridge status")))
        self.assertIn("请求处理：不可用", payload["choices"][0]["message"]["content"])

    def test_plain_prompt_still_503_when_browser_is_down(self):
        self.fake.page = None
        self.fake.init_error = "登录页未就绪"
        response = self.call(self.chat_request("你好"))
        self.assertEqual(response.status_code, 503)
        self.assertIn("登录页未就绪", self.body(response)["error"]["message"])

    def test_command_uses_the_session_header_bucket(self):
        self.call(self.chat_request("/bridge session reset"), "pi-task-9")
        self.assertIn(("reset_session", "pi-task-9"), self.fake.calls)

    def test_command_is_answered_on_the_streaming_path(self):
        response = self.call(self.chat_request("/bridge models", stream=True), "pi-task-10")
        self.assertEqual(response.media_type, "text/event-stream")

        async def drain():
            return [chunk async for chunk in response.body_iterator]

        text = "".join(asyncio.run(drain()))
        self.assertIn("data: [DONE]", text)
        self.assertIn('"finish_reason": "stop"', text)
        self.assertIn("deepseek-chat", text)
        self.assertEqual(self.fake.chats, [])

    def test_streaming_command_reports_zero_prompt_tokens(self):
        response = self.call(self.chat_request(
            "/bridge help", stream=True, stream_options={"include_usage": True}
        ))

        async def drain():
            return [chunk async for chunk in response.body_iterator]

        text = "".join(asyncio.run(drain()))
        usage_lines = [line for line in text.splitlines() if '"usage"' in line]
        self.assertEqual(len(usage_lines), 1)
        usage = json.loads(usage_lines[0][len("data: "):])["usage"]
        self.assertEqual(usage["prompt_tokens"], 0)
        self.assertGreater(usage["completion_tokens"], 0)


class SaveFilesPreferenceTests(RouteTestCase):
    def test_bucket_preference_off_disables_saving(self):
        self.fake.save_files_pref = False
        self.call(self.chat_request("你好"))
        self.assertEqual(self.fake.saved, [])

    def test_bucket_preference_on_still_saves(self):
        self.fake.save_files_pref = True
        self.call(self.chat_request("你好"))
        self.assertEqual(len(self.fake.saved), 1)

    def test_request_field_beats_the_bucket_preference(self):
        self.fake.save_files_pref = True
        self.call(self.chat_request("你好", save_files=False))
        self.assertEqual(self.fake.saved, [])
        self.fake.save_files_pref = False
        self.call(self.chat_request("你好", save_files=True))
        self.assertEqual(len(self.fake.saved), 1)

    def test_config_default_applies_when_nothing_is_set(self):
        with unittest.mock.patch.object(config, "SAVE_FILES", False):
            self.call(self.chat_request("你好"))
        self.assertEqual(self.fake.saved, [])


class ResponsesCommandTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDriver()
        self.request = None

    def make(self, text, **extra):
        payload = {"model": "deepseek-chat", "input": text}
        payload.update(extra)
        return responses.ResponsesRequest(**payload)

    def test_command_is_answered_locally(self):
        result = asyncio.run(responses.handle_responses(
            self.make("/bridge status"), "pi-task-11", self.fake
        ))
        data = result if isinstance(result, dict) else json.loads(result.body)
        self.assertEqual(data["output"][0]["type"], "message")
        self.assertIn("[Bridge] 桥接状态", data["output"][0]["content"][0]["text"])
        self.assertEqual(data["usage"]["input_tokens"], 0)
        self.assertEqual(self.fake.chats, [])

    def test_command_is_answered_when_browser_is_down(self):
        self.fake.page = None
        result = asyncio.run(responses.handle_responses(
            self.make("/bridge help"), None, self.fake
        ))
        data = result if isinstance(result, dict) else json.loads(result.body)
        self.assertIn(bridge_commands.BRIDGE_COMMAND, data["output"][0]["content"][0]["text"])

    def test_plain_input_still_503_when_browser_is_down(self):
        self.fake.page = None
        result = asyncio.run(responses.handle_responses(self.make("你好"), None, self.fake))
        self.assertEqual(result.status_code, 503)

    def test_command_streams_as_response_events(self):
        request = responses.to_chat_request(self.make("/bridge models", stream=True))
        events = []

        async def collect():
            async for chunk in responses.stream_responses(request, self.fake, None):
                events.append(chunk)

        asyncio.run(collect())
        text = "".join(events)
        self.assertIn("response.created", text)
        self.assertIn("response.completed", text)
        self.assertIn("deepseek-chat", text)
        self.assertEqual(self.fake.chats, [])


# ==================== Driver 状态（绑定 / 重新播种的一次性语义）====================

from tests.test_session_lifecycle import FakePage, SessionTestCase  # noqa: E402


class FakeContext:
    """只提供 new_page（_rebuild_page / _ensure_default_page 需要它）。"""

    def __init__(self):
        self.pages = []

    async def new_page(self):
        self.pages.append(FakePage(url="https://chat.deepseek.com/"))
        return self.pages[-1]


class DriverBindingTests(SessionTestCase):
    def test_reseed_marker_persists_and_is_consumed_once(self):
        page = FakePage(script=[["答案"], ["答案"]])
        driver = self.driver_for(page)
        driver.session_has_history = True
        driver.reseed_session("default")
        self.assertTrue(driver.needs_seed("default"))
        # 落盘后换一个实例读同一份状态：重启 / 并发都不会丢这个一次性标记
        self.assertTrue(self.driver_for(FakePage()).needs_seed("default"))

        text, _ = asyncio.run(driver.send_chat("DELTA", seeded_prompt="SEEDED"))
        self.assertEqual(text, "答案")
        # 这一轮发的必须是播种版（用户在要求重播历史，而不是只发增量）
        self.assertEqual(driver.sent_prompt(), "SEEDED")
        # 兑现一次后标记清除：不会每轮都重复发一遍历史
        self.assertFalse(driver.needs_seed("default"))
        self.assertFalse(
            json.loads(self._tmp.read_text(encoding="utf-8"))["pending_reseed"]
        )

    def test_link_retargets_an_open_page_and_persists(self):
        old = "https://chat.deepseek.com/a/chat/s/11111111-1111-1111-1111-111111111111"
        driver = srv.DeepSeekWebDriver()
        driver.context = FakeContext()
        driver.page = FakePage(url=old)
        asyncio.run(driver.link_session(SESSION_URL))
        self.assertEqual(driver._bucket_target("default"), SESSION_URL)
        # 绑定优先于「上次的会话」：启动恢复 / 页面重建都回到绑定的那条会话
        self.assertEqual(driver._saved_session_url(), SESSION_URL)
        self.assertEqual(driver.page.gotos[-1], SESSION_URL)

    def test_link_defers_the_page_swap_while_the_bucket_is_busy(self):
        # 该桶正在生成时，绑定不能当场换页（会打断在飞的回复），
        # 但必须在下一次发送前对齐到绑定会话。
        old = "https://chat.deepseek.com/a/chat/s/11111111-1111-1111-1111-111111111111"
        driver = srv.DeepSeekWebDriver()
        driver.context = FakeContext()
        driver.page = FakePage(url=old)
        driver._mark_bucket_active("default")
        try:
            asyncio.run(driver.link_session(SESSION_URL))
        finally:
            driver._unmark_bucket_active("default")
        self.assertEqual(driver.page.url, old)
        self.assertEqual(driver._bucket_target("default"), SESSION_URL)
        asyncio.run(driver._ensure_page("default"))
        self.assertEqual(driver._current_session_url("default"), SESSION_URL)

    def test_alignment_defers_while_another_request_is_in_flight(self):
        # 另一个请求正在用这条页面时，本轮不能换页（会把它断掉）；
        # 但它结束后，下一次 _ensure_page 必须把页面换到绑定会话。
        old = "https://chat.deepseek.com/a/chat/s/11111111-1111-1111-1111-111111111111"
        driver = srv.DeepSeekWebDriver()
        driver.context = FakeContext()
        driver.page = FakePage(url=old)
        asyncio.run(driver.link_session(SESSION_URL))
        self.assertEqual(driver._current_session_url("default"), SESSION_URL)

        driver.page = FakePage(url=old)  # 页面漂回了别的会话
        driver._mark_bucket_active("default")
        driver._mark_bucket_active("default")  # 模拟另一个请求的两处标记
        try:
            asyncio.run(driver._ensure_page("default"))
            self.assertEqual(driver.page.url, old)
        finally:
            driver._unmark_bucket_active("default")
            driver._unmark_bucket_active("default")
        asyncio.run(driver._ensure_page("default"))
        self.assertEqual(driver._current_session_url("default"), SESSION_URL)

    def test_link_on_a_bucket_persists_binding_and_seed_intent(self):
        driver = self.driver_for(FakePage())
        asyncio.run(driver.link_session(SESSION_URL, seed=True, key="pi-task-1"))
        asyncio.run(driver.link_session(SESSION_URL, key="pi-task-2"))
        fresh = srv.DeepSeekWebDriver()
        self.assertEqual(fresh.linked_session_url("pi-task-1"), SESSION_URL)
        self.assertTrue(fresh.needs_seed("pi-task-1"))  # --seed：下一轮播种
        self.assertFalse(fresh.needs_seed("pi-task-2"))  # 默认：认为已有上下文

    def test_reset_drops_the_binding(self):
        driver = self.driver_for(FakePage())
        asyncio.run(driver.link_session(SESSION_URL, key="pi-task-3"))
        driver.reset_session("pi-task-3")
        self.assertIsNone(driver.linked_session_url("pi-task-3"))
        self.assertEqual(driver._bucket_target("pi-task-3"), "https://chat.deepseek.com/")

    def test_unlink_keeps_the_session_but_drops_the_pin(self):
        driver = self.driver_for(FakePage(url=SESSION_URL))
        asyncio.run(driver.link_session(SESSION_URL))
        self.assertTrue(driver.unlink_session())
        self.assertIsNone(driver.linked_session_url())
        # 上下文不丢：页面仍停在原会话上
        self.assertEqual(driver._saved_session_url(), SESSION_URL)
        self.assertFalse(driver.unlink_session())

    def test_save_files_preference_round_trip(self):
        driver = self.driver_for(FakePage())
        driver.set_save_files_preference(False, "pi-task-4")
        fresh = srv.DeepSeekWebDriver()
        self.assertFalse(fresh.save_files_preference("pi-task-4"))
        self.assertIsNone(fresh.save_files_preference("pi-task-5"))
        fresh.set_save_files_preference(None, "pi-task-4")
        self.assertIsNone(fresh.save_files_preference("pi-task-4"))


if __name__ == "__main__":
    unittest.main()
