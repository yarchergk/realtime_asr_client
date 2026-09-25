"""
GUI 渲染逻辑测试：分句上屏、双语排版、流式译文替换、清空、自动记录、跨会话的迟到结果、翻译设置。

需要 tkinter 和图形环境（Windows / macOS 直接运行；Linux 无显示器时用 xvfb-run），否则自动跳过。
不需要麦克风，也不会调用真实的识别 / 翻译接口（翻译请求发往本地假服务）。

运行：python -m unittest discover -s tests -v
"""

import gc
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_llm_server import FakeLLMServer, echo_reply, error_reply  # noqa: E402

try:
    import tkinter as tk
except ImportError:  # 部分 Linux 发行版需要单独安装 python3-tk
    tk = None

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
logging.getLogger("translator").setLevel(logging.ERROR)


def _import_app_module():
    import realtime_asr_client
    return realtime_asr_client


@unittest.skipIf(tk is None, "tkinter 不可用")
class RecordFormatTest(unittest.TestCase):
    def test_split_utterances(self):
        rac = _import_app_module()
        data = {"result": {"text": "A.B.c", "utterances": [
            {"text": "A.", "definite": True}, {"text": "B.", "definite": True},
            {"text": "c", "definite": False}]}}
        self.assertEqual(rac.split_utterances(data), (["A.", "B."], "c"))
        self.assertEqual(rac.split_utterances({"result": {"text": "hel"}}), ([], "hel"))
        self.assertEqual(rac.split_utterances({}), ([], ""))

    def test_format_record(self):
        rac = _import_app_module()
        S = rac.Segment
        segments = [
            S(1, "第一句。", False), S(2, "第二句。", False, break_after=True),
            S(3, "Hello.", True, status="done", translation="你好。"),
            S(4, "我们开始吧。", True, status="skipped"),
            S(5, "Bad.", True, status="error", error="HTTP 401"),
            S(6, "Pending.", True, status="pending", translation="待"),
            S(7, "尾巴", False),
        ]
        self.assertEqual(rac.format_record(segments), (
            "第一句。第二句。\n\n"
            "Hello.\n> 你好。\n\n"
            "我们开始吧。\n\n"
            "Bad.\n> [翻译失败] HTTP 401\n\n"
            "Pending.\n> 待…（翻译未完成）\n\n"
            "尾巴"))


@unittest.skipIf(tk is None, "tkinter 不可用")
class AppRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeLLMServer().start()
        cls.rac = _import_app_module()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def setUp(self):
        self.server.reset()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for patcher in (
            mock.patch.dict(os.environ, {
                "VOLCENGINE_APP_KEY": "test", "VOLCENGINE_ACCESS_KEY": "test",
                "TRANSLATE_BASE_URL": self.server.base_url, "TRANSLATE_API_KEY": "sk-test",
                "TRANSLATE_MODEL": "fake-model", "TRANSLATE_EXTRA_BODY": "{}",
                "TRANSLATE_ENABLED": "true", "TRANSLATE_CONTEXT_SIZE": "3",
            }),
            mock.patch.object(self.rac, "get_application_path", return_value=self.tmp),
            mock.patch.object(self.rac, "ENV_PATH", os.path.join(self.tmp, ".env")),
            mock.patch.object(self.rac.messagebox, "showwarning"),
            mock.patch.object(self.rac.messagebox, "askyesno", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        try:
            self.app = self.rac.App()
        except tk.TclError as e:
            self.skipTest(f"没有图形环境：{e}")

    def tearDown(self):
        self.app._translator.stop()
        self.app.destroy()
        # Tk 对象之间有循环引用，在主线程里立刻回收，避免被后台线程触发的 GC 在错误的线程释放
        del self.app
        gc.collect()

    def gate(self) -> threading.Event:
        """让假服务暂停输出译文，测试结束时自动放行。"""
        gate = threading.Event()
        self.addCleanup(gate.set)
        return gate

    # ── 工具 ──────────────────────────────────────────────────────────────────
    def text(self) -> str:
        return self.app._text.get("1.0", "end-1c")

    def tagged(self, tag: str) -> str:
        ranges = self.app._text.tag_ranges(tag)
        return "".join(self.app._text.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2))

    def wait_for(self, condition, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"等待超时，当前文本：{self.text()!r}")
            self.app.update()
            time.sleep(0.01)

    def new_session(self) -> int:
        sid = next(self.app._session_ids)
        self.app._current_session = sid
        self.app._sessions[sid] = self.rac.AsrSessionState()
        return sid

    def asr(self, sid: int, definite=(), interim: str = "", is_last: bool = False):
        utterances = [{"text": t, "definite": True} for t in definite]
        if interim:
            utterances.append({"text": interim, "definite": False})
        resp = {"code": 0, "is_last": is_last, "data": {"result": {"utterances": utterances}}}
        self.app._render_events([("asr", sid, resp)])

    def read_record(self) -> str:
        with open(os.path.join(self.tmp, "RecordMemory.md"), encoding="utf-8") as f:
            return f.read()

    # ── 用例 ──────────────────────────────────────────────────────────────────
    def test_translation_enabled_from_env(self):
        self.assertTrue(self.app._translate_var.get())
        self.assertEqual(self.app._model_var.get(), "fake-model")

    def test_inline_layout_without_translation(self):
        self.app._translate_var.set(False)
        sid = self.new_session()
        self.asr(sid, [], "Hel")
        self.assertEqual(self.text(), "Hel")
        self.asr(sid, ["Hello."], "Wor")
        self.assertEqual(self.text(), "Hello.Wor")
        self.assertEqual(self.tagged("interim"), "Wor")
        self.asr(sid, ["Hello.", "World."], is_last=True)
        self.assertEqual(self.text(), "Hello.World.\n")
        self.assertEqual(self.tagged("interim"), "")
        self.assertEqual(self.server.requests, [])
        self.assertNotIn(sid, self.app._sessions)

    def test_bilingual_layout_with_streaming_translation(self):
        sid = self.new_session()
        self.asr(sid, ["Hello everyone."], "we")
        self.assertEqual(self.text(), "Hello everyone.\n翻译中…\n\nwe")
        self.wait_for(lambda: "译：Hello everyone." in self.text())
        self.assertEqual(self.text(), "Hello everyone.\n译：Hello everyone.\n\nwe")
        self.assertEqual(self.tagged("translation"), "译：Hello everyone.")
        self.assertEqual(self.tagged("tr_pending"), "")

        # 中文句子不调用翻译，但作为上文提供给后面的句子
        self.asr(sid, ["Hello everyone.", "我们开始吧。"], "Next")
        self.assertEqual(self.text(), "Hello everyone.\n译：Hello everyone.\n\n我们开始吧。\n\nNext")
        self.asr(sid, ["Hello everyone.", "我们开始吧。", "First item."])
        self.wait_for(lambda: "译：First item." in self.text())
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.server.requests[1]["body"]["messages"][1]["content"],
                         "【上文】\nHello everyone.\n我们开始吧。\n\n【待翻译】\nFirst item.")
        self.assertEqual([s.status for s in self.app._segments.values()], ["done", "skipped", "done"])

    def test_translation_error_is_shown_inline(self):
        self.server.handlers.append(error_reply(401, "Authentication Fails"))
        sid = self.new_session()
        self.asr(sid, ["Hello."])
        self.wait_for(lambda: "翻译失败" in self.text())
        self.assertEqual(self.text(),
                         "Hello.\n[翻译失败] API Key 无效或缺失（HTTP 401）：Authentication Fails\n\n")
        self.assertEqual(next(iter(self.app._segments.values())).status, "error")

    def test_clear_does_not_bring_back_old_sentences(self):
        sid = self.new_session()
        self.asr(sid, ["One."])
        self.wait_for(lambda: "译：One." in self.text())
        self.app._on_clear()
        self.assertEqual(self.text(), "")
        self.asr(sid, ["One.", "Two."], "thr")
        self.wait_for(lambda: "译：Two." in self.text())
        self.assertEqual(self.text(), "Two.\n译：Two.\n\nthr")
        self.assertEqual(len(self.server.requests), 2)   # One. 没有被重复翻译

    def test_clear_cancels_pending_translation(self):
        gate = self.gate()
        self.server.handlers.append(echo_reply(gate=gate))
        sid = self.new_session()
        self.asr(sid, ["Slow."])
        self.wait_for(lambda: len(self.server.requests) == 1)
        self.app._on_clear()
        gate.set()
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.01)
        self.assertEqual(self.text(), "")

    def test_auto_save_waits_for_pending_translation(self):
        gate = self.gate()
        self.server.handlers += [echo_reply(), echo_reply(gate=gate)]
        sid = self.new_session()
        self.asr(sid, ["First."])
        self.wait_for(lambda: "译：First." in self.text())
        self.asr(sid, ["First.", "Second."], "thi")
        self.wait_for(lambda: len(self.server.requests) == 2)

        self.app._do_auto_save()
        record = self.read_record()
        self.assertIn("First.\n> 译：First.\n", record)
        self.assertNotIn("Second.", record)
        self.assertEqual(self.text(), "Second.\n翻译中…\n\nthi")   # 仍在翻译的句子和临时文字保留

        gate.set()
        self.wait_for(lambda: "译：Second." in self.text())
        self.app._do_auto_save()
        self.assertIn("Second.\n> 译：Second.\n", self.read_record())
        self.assertEqual(self.text(), "thi")
        self.assertEqual(self.app._segments, {})

    def test_force_save_includes_pending(self):
        gate = self.gate()
        self.server.handlers.append(echo_reply(gate=gate))
        sid = self.new_session()
        self.asr(sid, ["Unfinished."])
        self.app._do_auto_save()
        with self.assertRaises(FileNotFoundError):
            self.read_record()
        self.app._do_auto_save(force=True)
        self.assertIn("Unfinished.\n> （翻译未完成）", self.read_record())
        self.assertEqual(self.text(), "")
        gate.set()

    def test_switch_from_inline_to_bilingual(self):
        self.app._translate_var.set(False)
        sid = self.new_session()
        self.asr(sid, ["你好。"])
        self.app._translate_var.set(True)
        self.asr(sid, ["你好。", "Hello."])
        self.assertEqual(self.text(), "你好。\nHello.\n翻译中…\n\n")
        self.wait_for(lambda: "译：Hello." in self.text())

    def test_late_final_result_of_previous_session(self):
        self.app._translate_var.set(False)
        s1 = self.new_session()
        self.asr(s1, ["A."], "b")
        s2 = self.new_session()                  # 用户停止后立刻重新开始
        self.asr(s2, [], "c")
        self.assertEqual(self.text(), "A.c")
        self.asr(s1, ["A.", "B."], is_last=True)  # 上一次录音的最终结果迟到
        self.assertEqual(self.text(), "A.B.\nc")
        self.asr(s2, ["C."])
        self.assertEqual(self.text(), "A.B.\nC.")

    def test_asr_error_stops_recording(self):
        sid = self.new_session()
        self.app._recording = True
        self.asr(sid, ["Hello."], "wor")
        self.app._render_events([("asr", sid, {"error": "connection lost"})])
        self.assertIn("[错误] connection lost\n", self.text())
        self.assertFalse(self.app._recording)
        self.assertEqual(self.app._status_var.get(), "连接已断开")
        self.assertNotIn(sid, self.app._sessions)

    def test_apply_config_persists_and_switches_model(self):
        cfg = replace(self.app._tr_cfg, model="another-model")
        self.app._apply_translation_config(cfg)
        self.assertEqual(self.app._model_var.get(), "another-model")
        with open(os.path.join(self.tmp, ".env"), encoding="utf-8") as f:
            env_text = f.read()
        self.assertIn("TRANSLATE_MODEL=another-model", env_text)
        self.assertIn(f"TRANSLATE_BASE_URL={self.server.base_url}", env_text)
        sid = self.new_session()
        self.asr(sid, ["Hello."])
        self.wait_for(lambda: "译：Hello." in self.text())
        self.assertEqual(self.server.requests[-1]["body"]["model"], "another-model")

    def test_settings_dialog(self):
        self.app._open_translation_settings()
        dlg = self.app._settings_dialog
        self.assertEqual(dlg._provider_box.get(), dlg.CUSTOM)
        self.assertEqual(dlg._model_entry.get(), "fake-model")
        self.assertEqual(dlg._key_entry.cget("show"), "•")
        dlg._on_toggle_key()
        self.assertEqual(dlg._key_entry.cget("show"), "")

        dlg._provider_box.set("DeepSeek（默认）")
        dlg._on_provider_selected()
        self.assertEqual(dlg._base_url_entry.get(), "https://api.deepseek.com")
        self.assertEqual(dlg._model_entry.get(), "deepseek-flash")
        self.assertEqual(dlg._extra_entry.get(), '{"thinking": {"type": "disabled"}}')

        dlg._provider_box.set("OpenAI")
        dlg._on_provider_selected()
        self.assertEqual(dlg._model_entry.get(), "")
        dlg._on_save_click()
        self.assertIn("模型名称", dlg.message)

        dlg._set_entry(dlg._base_url_entry, self.server.base_url)
        dlg._set_entry(dlg._model_entry, "m2")
        dlg._on_test()
        self.wait_for(lambda: dlg.message.startswith("✓"))
        self.assertIn("译：Hello! This is a real-time translation test.", dlg.message)
        self.assertEqual(self.server.requests[-1]["body"]["model"], "m2")

        dlg._set_entry(dlg._extra_entry, "{bad json")
        dlg._on_save_click()
        self.assertIn("JSON", dlg.message)
        self.assertTrue(dlg.winfo_exists())

        dlg._set_entry(dlg._extra_entry, "{}")
        dlg._on_save_click()
        self.assertFalse(dlg.winfo_exists())
        self.assertEqual(self.app._tr_cfg.model, "m2")
        self.assertEqual(self.app._translator.config.model, "m2")
        self.assertEqual(self.app._model_var.get(), "m2")

    def test_enable_translation_without_config_asks_for_settings(self):
        self.app._translate_var.set(False)
        self.app._apply_translation_config(
            replace(self.app._tr_cfg, base_url="https://api.deepseek.com", api_key=""))
        self.app._translate_var.set(True)
        self.app._on_translate_toggle()
        self.assertFalse(self.app._translate_var.get())
        self.rac.messagebox.askyesno.assert_called_once()


if __name__ == "__main__":
    unittest.main()
