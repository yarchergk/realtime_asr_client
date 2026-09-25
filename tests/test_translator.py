"""
translator.py 的单元测试（只依赖标准库 unittest + aiohttp）。

运行：python -m unittest discover -s tests -v
"""

import logging
import os
import queue
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import translator as tr  # noqa: E402
from fake_llm_server import (  # noqa: E402
    FakeLLMServer, error_reply, json_reply, sse_reply,
)

# 本机测试服务不走代理
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
# 预期内的失败会打 WARNING 日志，测试时不输出
logging.getLogger("translator").setLevel(logging.ERROR)


class LanguageDetectionTest(unittest.TestCase):
    def test_needs_translation(self):
        cases = {
            "Hello everyone, welcome to the meeting.": True,
            "今天我们讨论一下项目进度。": False,
            "我们用 Python 写一个 demo 吧。": False,          # 中英混说仍以中文为主
            "I met 张三 yesterday.": True,
            "今日はいい天気ですね。": True,                      # 日文
            "日本国憲法第九条について": True,
            "안녕하세요, 반갑습니다.": True,                     # 韩文
            "Bonjour tout le monde.": True,
            "12345。": False,                                   # 没有文字
            "   ": False,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tr.needs_translation(text), expected)

    def test_skip_chinese_can_be_disabled(self):
        self.assertTrue(tr.needs_translation("今天天气不错。", skip_chinese=False))

    def test_skip_only_applies_to_chinese_target(self):
        self.assertTrue(tr.needs_translation("今天天气不错。", target_lang="English"))
        self.assertFalse(tr.needs_translation("今天天气不错。", target_lang="zh-CN"))


class PromptAndOutputTest(unittest.TestCase):
    def test_build_messages_without_context(self):
        messages = tr.build_messages("Hello.", [])
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn("简体中文", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "【待翻译】\nHello.")

    def test_build_messages_with_context(self):
        messages = tr.build_messages("Third.", ["First.", "第二句。"], "繁體中文")
        self.assertIn("繁體中文", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "【上文】\nFirst.\n第二句。\n\n【待翻译】\nThird.")

    def test_clean_output(self):
        cases = {
            "  你好。 ": "你好。",
            "<think>用户想翻译……</think>\n你好。": "你好。",
            "推理过程……</think>你好。": "你好。",                # 只有结束标签
            "<think>还在思考": "",                                 # 思考尚未结束
            "译文：你好。": "你好。",
            "【译文】你好。": "你好。",
            "翻译是一门艺术。": "翻译是一门艺术。",               # 正文以「翻译」开头不能被误删
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(tr.clean_output(raw), expected)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = tr.TranslatorConfig.from_env({})
        self.assertEqual(cfg.base_url, "https://api.deepseek.com")
        self.assertEqual(cfg.model, "deepseek-flash")
        self.assertEqual(cfg.endpoint, "https://api.deepseek.com/chat/completions")
        self.assertEqual(cfg.extra_body, {"thinking": {"type": "disabled"}})
        self.assertIsNone(cfg.temperature)
        self.assertFalse(cfg.is_ready())
        self.assertIn("API Key", cfg.missing_reason())
        self.assertFalse(cfg.enabled_by_default())

    def test_deepseek_key_fallback(self):
        cfg = tr.TranslatorConfig.from_env({"DEEPSEEK_API_KEY": "sk-ds"})
        self.assertEqual(cfg.api_key, "sk-ds")
        self.assertTrue(cfg.is_ready())
        self.assertTrue(cfg.enabled_by_default())
        # 其他服务商不借用 DeepSeek 的 Key
        cfg = tr.TranslatorConfig.from_env({"DEEPSEEK_API_KEY": "sk-ds",
                                            "TRANSLATE_BASE_URL": "https://api.openai.com/v1"})
        self.assertEqual(cfg.api_key, "")

    def test_other_provider(self):
        cfg = tr.TranslatorConfig.from_env({
            "TRANSLATE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1/",
            "TRANSLATE_API_KEY": "sk-qwen",
        })
        self.assertEqual(cfg.model, "qwen-plus")            # 预设里的默认模型
        self.assertEqual(cfg.extra_body, {})                # 非 DeepSeek 不带 thinking 参数
        self.assertEqual(cfg.endpoint, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")

        cfg = tr.TranslatorConfig.from_env({"TRANSLATE_BASE_URL": "https://api.openai.com/v1",
                                            "TRANSLATE_API_KEY": "sk"})
        self.assertEqual(cfg.model, "")
        self.assertIn("模型", cfg.missing_reason())

    def test_local_service_needs_no_key(self):
        cfg = tr.TranslatorConfig.from_env({"TRANSLATE_BASE_URL": "http://localhost:11434/v1",
                                            "TRANSLATE_MODEL": "qwen3:8b"})
        self.assertTrue(cfg.is_ready())

    def test_overrides(self):
        cfg = tr.TranslatorConfig.from_env({
            "TRANSLATE_API_KEY": "sk",
            "TRANSLATE_MODEL": "deepseek-v4-pro",
            "TRANSLATE_EXTRA_BODY": '{"thinking": {"type": "enabled"}}',
            "TRANSLATE_TEMPERATURE": "1.3",
            "TRANSLATE_CONTEXT_SIZE": "99",
            "TRANSLATE_TIMEOUT": "abc",
            "TRANSLATE_SKIP_CHINESE": "false",
            "TRANSLATE_ENABLED": "off",
        })
        self.assertEqual(cfg.model, "deepseek-v4-pro")
        self.assertEqual(cfg.extra_body, {"thinking": {"type": "enabled"}})
        self.assertEqual(cfg.temperature, 1.3)
        self.assertEqual(cfg.context_size, 20)     # 上限
        self.assertEqual(cfg.timeout, 30.0)        # 非法值回退默认
        self.assertFalse(cfg.skip_chinese)
        self.assertFalse(cfg.enabled_by_default())

    def test_invalid_extra_body_falls_back(self):
        cfg = tr.TranslatorConfig.from_env({"TRANSLATE_EXTRA_BODY": "{not json"})
        self.assertEqual(cfg.extra_body, {"thinking": {"type": "disabled"}})
        cfg = tr.TranslatorConfig.from_env({"TRANSLATE_EXTRA_BODY": "{}"})
        self.assertEqual(cfg.extra_body, {})

    def test_parse_extra_body_and_url(self):
        self.assertEqual(tr.parse_extra_body(""), {})
        with self.assertRaises(ValueError):
            tr.parse_extra_body("[1, 2]")
        with self.assertRaises(ValueError):
            tr.parse_extra_body("{'a': 1}")
        self.assertEqual(tr.check_base_url(" https://api.deepseek.com "), "https://api.deepseek.com")
        with self.assertRaises(ValueError):
            tr.check_base_url("api.deepseek.com")
        self.assertEqual(tr.build_endpoint("http://x/v1/chat/completions/"), "http://x/v1/chat/completions")

    def test_find_preset(self):
        self.assertEqual(tr.find_preset("https://API.deepseek.com/").model, "deepseek-flash")
        self.assertIsNone(tr.find_preset("https://example.com/v1"))


class SaveEnvTest(unittest.TestCase):
    def test_save_env_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8-sig") as f:   # 带 BOM，模拟记事本保存的文件
                f.write("# 注释\nVOLCENGINE_APP_KEY=abc\nTRANSLATE_MODEL=old\n"
                        "# TRANSLATE_API_KEY=commented\nTRANSLATE_MODEL=old2\n")
            tr.save_env_values(path, {
                "TRANSLATE_MODEL": "deepseek-flash",
                "TRANSLATE_API_KEY": "sk-123",
                "TRANSLATE_EXTRA_BODY": '{"thinking": {"type": "disabled"}}',
            })
            with open(path, "rb") as f:
                raw = f.read()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            lines = raw.decode("utf-8").splitlines()
            self.assertEqual(lines[:5], ["# 注释", "VOLCENGINE_APP_KEY=abc", "TRANSLATE_MODEL=deepseek-flash",
                                         "# TRANSLATE_API_KEY=commented", "TRANSLATE_MODEL=deepseek-flash"])
            self.assertIn("TRANSLATE_API_KEY=sk-123", lines)
            self.assertIn("""TRANSLATE_EXTRA_BODY='{"thinking": {"type": "disabled"}}'""", lines)

            try:
                from dotenv import dotenv_values
            except ImportError:
                return
            values = dotenv_values(path)
            self.assertEqual(values["TRANSLATE_EXTRA_BODY"], '{"thinking": {"type": "disabled"}}')
            self.assertEqual(values["VOLCENGINE_APP_KEY"], "abc")

    def test_quote_round_trip(self):
        try:
            from dotenv import dotenv_values
        except ImportError:
            self.skipTest("python-dotenv 未安装")
        samples = ["plain", "with space", "has#hash", 'it\'s "quoted" \\ path', "",
                   "C:\\new\\dir", "\\\\server\\share", '{"a": "b\\\\c"}']
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            tr.save_env_values(path, {f"K{i}": v for i, v in enumerate(samples)})
            values = dotenv_values(path)
            for i, v in enumerate(samples):
                self.assertEqual(values[f"K{i}"], v)


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeLLMServer().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset()
        self.events: "queue.Queue" = queue.Queue()
        self.cfg = tr.TranslatorConfig(base_url=self.server.base_url, api_key="sk-test",
                                       model="fake-model", extra_body={"thinking": {"type": "disabled"}},
                                       timeout=5)
        self.engine = tr.TranslationEngine(self.cfg, lambda *event: self.events.put(event))
        self.engine.start()

    def tearDown(self):
        self.engine.stop()

    def wait_done(self, seg_id: int, timeout: float = 5.0):
        """返回 (中间结果列表, 最终事件)。"""
        partials = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"seg {seg_id} 未在 {timeout}s 内完成")
            try:
                event = self.events.get(timeout=remaining)
            except queue.Empty:
                continue
            if event[0] != seg_id:
                continue
            if event[2]:
                return partials, event
            partials.append(event[1])

    def test_streaming_translation(self):
        self.server.handlers.append(sse_reply(["大家好，", "欢迎参加", "今天的会议。"],
                                              reasoning=["(ignored)"], delay=0.1))
        self.engine.submit(1, "Hello everyone, welcome to today's meeting.")
        partials, (_, text, done, error) = self.wait_done(1)
        self.assertIsNone(error)
        self.assertEqual(text, "大家好，欢迎参加今天的会议。")
        self.assertTrue(partials, "应该收到流式的中间结果")
        self.assertTrue(all(text.startswith(p) for p in partials))

        request = self.server.requests[0]
        body = request["body"]
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(body["model"], "fake-model")
        self.assertTrue(body["stream"])
        self.assertEqual(body["thinking"], {"type": "disabled"})   # 附加参数合并进请求体
        self.assertNotIn("temperature", body)                       # 未配置时不传
        self.assertTrue(body["messages"][1]["content"].endswith("【待翻译】\nHello everyone, welcome to today's meeting."))

    def test_context_is_sent(self):
        cfg = tr.TranslatorConfig(base_url=self.server.base_url, api_key="sk", model="m",
                                  extra_body={}, context_size=2, timeout=5)
        self.engine.set_config(cfg)
        self.engine.submit(1, "First.")
        self.wait_done(1)
        self.engine.add_context("第二句是中文。")
        self.engine.submit(2, "Third.")
        self.wait_done(2)
        self.engine.submit(3, "Fourth.")
        self.wait_done(3)
        users = [r["body"]["messages"][1]["content"] for r in self.server.requests]
        self.assertEqual(users[0], "【待翻译】\nFirst.")
        self.assertEqual(users[1], "【上文】\nFirst.\n第二句是中文。\n\n【待翻译】\nThird.")
        self.assertEqual(users[2], "【上文】\n第二句是中文。\nThird.\n\n【待翻译】\nFourth.")
        self.assertNotIn("thinking", self.server.requests[0]["body"])
        self.engine.clear_context()
        self.engine.submit(4, "Fifth.")
        self.wait_done(4)
        self.assertEqual(self.server.requests[3]["body"]["messages"][1]["content"], "【待翻译】\nFifth.")

    def test_think_tags_are_removed(self):
        self.server.handlers.append(sse_reply(["<think>", "先分析一下", "</think>", "\n你好。"]))
        self.engine.submit(1, "Hello.")
        partials, (_, text, _, error) = self.wait_done(1)
        self.assertIsNone(error)
        self.assertEqual(text, "你好。")
        self.assertFalse(any("分析" in p for p in partials))

    def test_non_stream_json_response(self):
        self.server.handlers.append(json_reply("译文：你好。"))
        self.engine.submit(7, "Hello.")
        _, (_, text, _, error) = self.wait_done(7)
        self.assertIsNone(error)
        self.assertEqual(text, "你好。")

    def test_http_errors_are_readable_and_not_retried(self):
        for status, keyword in ((401, "API Key"), (402, "余额不足"), (404, "模型名称")):
            with self.subTest(status=status):
                self.server.reset()
                self.server.handlers.append(error_reply(status, "Authentication Fails"))
                self.engine.submit(status, "Hello.")
                _, (_, text, done, error) = self.wait_done(status)
                self.assertTrue(done)
                self.assertIn(keyword, error)
                self.assertIn(f"HTTP {status}", error)
                self.assertIn("Authentication Fails", error)
                self.assertEqual(len(self.server.requests), 1)

    def test_retry_on_server_error(self):
        self.server.handlers += [error_reply(500, "busy"), error_reply(429, "rate limited"), sse_reply(["好的。"])]
        self.engine.submit(1, "OK.")
        _, (_, text, _, error) = self.wait_done(1, timeout=10)
        self.assertIsNone(error)
        self.assertEqual(text, "好的。")
        self.assertEqual(len(self.server.requests), 3)

    def test_connection_error(self):
        self.engine.MAX_RETRIES = 0
        with socket_closed_port() as port:
            cfg = tr.TranslatorConfig(base_url=f"http://127.0.0.1:{port}/v1", api_key="sk", model="m", timeout=5)
            self.engine.set_config(cfg)
            self.engine.submit(1, "Hello.")
            _, (_, _, done, error) = self.wait_done(1)
        self.assertTrue(done)
        self.assertIn("无法连接", error)

    def test_cancel_all(self):
        gate = threading.Event()
        self.server.handlers.append(sse_reply(["不会", "出现"], gate=gate))
        self.engine.submit(1, "Hello.")
        deadline = time.monotonic() + 5
        while not self.server.requests and time.monotonic() < deadline:
            time.sleep(0.02)
        self.engine.cancel_all()
        time.sleep(0.2)
        gate.set()
        time.sleep(0.5)
        self.assertTrue(self.events.empty(), "取消后不应再有回调")

    def test_engine_test_method(self):
        self.server.handlers.append(sse_reply(["你好！这是一个实时翻译测试。"]))
        text, elapsed = self.engine.test(self.cfg).result(5)
        self.assertEqual(text, "你好！这是一个实时翻译测试。")
        self.assertGreaterEqual(elapsed, 0)
        self.server.handlers.append(error_reply(401, "bad key"))
        with self.assertRaises(tr.TranslationError):
            self.engine.test(self.cfg).result(5)

    def test_submit_after_stop_reports_error(self):
        self.engine.stop()
        self.engine.submit(9, "Hello.")
        _, (_, _, done, error) = self.wait_done(9)
        self.assertTrue(done)
        self.assertIn("未启动", error)


class socket_closed_port:
    """得到一个当前没有服务监听的本地端口。"""

    def __enter__(self) -> int:
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def __exit__(self, *exc) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
