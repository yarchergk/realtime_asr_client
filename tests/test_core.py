"""core.TranscriptState 的单元测试：事件序列、全量语义、翻译调度、自动记录。

不发网络请求、不需要图形界面。运行：
    python -m unittest tests.test_core -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import (
    DONE, ERROR, NONE, PENDING, SKIPPED,
    Segment, TranscriptState, format_record, split_utterances,
)


class FakeTranslator:
    """记录调用而不真的翻译。"""

    def __init__(self):
        self.submitted = []      # [(seg_id, text)]
        self.context = []        # [text]
        self.cleared = 0
        self.cancelled = 0

    def submit(self, seg_id, text):
        self.submitted.append((seg_id, text))

    def add_context(self, text):
        self.context.append(text)

    def clear_context(self):
        self.cleared += 1

    def cancel_all(self):
        self.cancelled += 1


def asr(*utterances, is_last=False):
    """构造一条 ASR 响应。每项是 (text, definite)。"""
    return {
        "data": {"result": {"utterances": [
            {"text": t, "definite": d} for t, d in utterances
        ]}},
        "is_last": is_last,
    }


class Harness:
    """搭好一个状态机，并收集它吐出的事件。"""

    def __init__(self, **kwargs):
        self.events = []
        self.translator = FakeTranslator()
        clock = kwargs.pop("clock", lambda: 1_700_000_000.0)
        self.state = TranscriptState(
            self.events.append, self.translator, clock=clock, **kwargs
        )

    def types(self):
        return [e["type"] for e in self.events]

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]

    def drain(self):
        out, self.events[:] = list(self.events), []
        return out


# ─── split_utterances ─────────────────────────────────────────────────────────
class TestSplitUtterances(unittest.TestCase):

    def test_separates_definite_from_interim(self):
        definite, interim = split_utterances(
            asr(("第一句", True), ("第二句", True), ("还没说完", False))["data"]
        )
        self.assertEqual(definite, ["第一句", "第二句"])
        self.assertEqual(interim, "还没说完")

    def test_result_as_list(self):
        data = {"result": [{"utterances": [{"text": "hi", "definite": True}]}]}
        self.assertEqual(split_utterances(data), (["hi"], ""))

    def test_falls_back_to_top_level_text(self):
        self.assertEqual(split_utterances({"result": {"text": "partial"}}), ([], "partial"))

    def test_empty(self):
        self.assertEqual(split_utterances({}), ([], ""))


# ─── 全量语义 ─────────────────────────────────────────────────────────────────
class TestFullResultSemantics(unittest.TestCase):
    """result_type="full"：每个响应都带全部已确定分句，不能重复上屏。"""

    def test_repeated_utterances_are_not_re_emitted(self):
        h = Harness(translate=False)
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("one", True)))
        h.state.on_asr(sid, asr(("one", True), ("two", True)))
        h.state.on_asr(sid, asr(("one", True), ("two", True), ("three", True)))

        sources = [e["source"] for e in h.of("segment")]
        self.assertEqual(sources, ["one", "two", "three"])

    def test_clear_does_not_reset_rendered_counter(self):
        """清空后同样的全量响应再来一次，旧句子不能重新上屏、不能重复翻译。"""
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello there.", True), ("Second one.", True)))
        self.assertEqual(len(h.of("segment")), 2)
        self.assertEqual(len(h.translator.submitted), 2)

        h.state.clear()
        h.drain()

        # 服务端仍然把这两句放在全量结果里
        h.state.on_asr(sid, asr(("Hello there.", True), ("Second one.", True), ("Third.", True)))
        self.assertEqual([e["source"] for e in h.of("segment")], ["Third."])
        self.assertEqual(len(h.translator.submitted), 3)

    def test_auto_save_does_not_reset_rendered_counter(self):
        h = Harness(translate=False)
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("one", True), ("two", True)))
        self.assertEqual(len(h.state.take_settled()), 2)
        h.drain()

        h.state.on_asr(sid, asr(("one", True), ("two", True), ("three", True)))
        self.assertEqual([e["source"] for e in h.of("segment")], ["three"])

    def test_empty_definite_text_is_skipped(self):
        h = Harness(translate=False)
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("", True), ("real", True)))
        self.assertEqual([e["source"] for e in h.of("segment")], ["real"])


# ─── 会话 ─────────────────────────────────────────────────────────────────────
class TestSessions(unittest.TestCase):

    def test_session_event_carries_id_and_time(self):
        h = Harness(clock=lambda: 1234.5)
        sid = h.state.begin_session()
        self.assertEqual(h.of("session"), [{"type": "session", "id": sid, "at": 1234.5}])

    def test_is_last_closes_session_and_clears_interim(self):
        h = Harness(translate=False)
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("done", True), ("tail", False)))
        self.assertEqual(h.state.interim, "tail")

        h.state.on_asr(sid, asr(("done", True), is_last=True))
        self.assertEqual(h.state.interim, "")
        self.assertIn("session_end", h.types())

    def test_late_response_after_close_is_dropped(self):
        h = Harness(translate=False)
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("a", True), is_last=True))
        h.drain()
        h.state.on_asr(sid, asr(("a", True), ("b", True)))
        self.assertEqual(h.events, [])

    def test_interim_from_previous_session_is_ignored(self):
        h = Harness(translate=False)
        first = h.state.begin_session()
        second = h.state.begin_session()
        h.drain()
        h.state.on_asr(first, asr(("stale", False)))
        self.assertEqual(h.state.interim, "")
        self.assertEqual(h.of("interim"), [])
        self.assertEqual(second, first + 1)

    def test_asr_error_closes_session(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, {"error": "连接已断开"})
        self.assertEqual(h.of("asr_error"), [{"type": "asr_error", "message": "连接已断开"}])
        self.assertIn("session_end", h.types())

    def test_interim_is_not_re_emitted_when_unchanged(self):
        h = Harness(translate=False)
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("same", False)))
        h.state.on_asr(sid, asr(("same", False)))
        self.assertEqual(len(h.of("interim")), 1)


# ─── 翻译调度 ─────────────────────────────────────────────────────────────────
class TestTranslationDispatch(unittest.TestCase):

    def test_english_is_submitted_chinese_is_context_only(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello everyone.", True), ("好的，我们开始吧。", True)))

        statuses = [(e["source"], e["status"]) for e in h.of("segment")]
        self.assertEqual(statuses, [("Hello everyone.", PENDING), ("好的，我们开始吧。", SKIPPED)])
        self.assertEqual(h.translator.submitted, [(1, "Hello everyone.")])
        self.assertEqual(h.translator.context, ["好的，我们开始吧。"])

    def test_translate_off_means_no_translator_calls(self):
        h = Harness(translate=False)
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello everyone.", True)))
        self.assertEqual(h.of("segment")[0]["status"], NONE)
        self.assertEqual(h.translator.submitted, [])
        self.assertEqual(h.translator.context, [])

    def test_toggle_applies_only_to_later_segments(self):
        h = Harness(translate=False)
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("first", True)))
        h.state.set_translate(True)
        h.state.on_asr(sid, asr(("first", True), ("second", True)))

        self.assertEqual([e["status"] for e in h.of("segment")], [NONE, PENDING])
        self.assertEqual(h.of("translate"), [{"type": "translate", "on": True}])

    def test_toggle_to_same_value_emits_nothing(self):
        h = Harness(translate=True)
        h.state.set_translate(True)
        self.assertEqual(h.of("translate"), [])

    def test_streaming_then_done(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello everyone.", True)))
        h.drain()

        h.state.on_translation(1, "大家", False, None)
        h.state.on_translation(1, "大家好。", True, None)

        self.assertEqual(
            h.of("translation"),
            [{"type": "translation", "id": 1, "text": "大家", "done": False},
             {"type": "translation", "id": 1, "text": "大家好。", "done": True}],
        )
        self.assertEqual(h.state.segments[0].status, DONE)

    def test_failure_emits_error_and_settles(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello.", True)))
        h.drain()
        h.state.on_translation(1, "", True, "API Key 无效或缺失（HTTP 401）")

        self.assertEqual(h.of("error")[0]["message"], "API Key 无效或缺失（HTTP 401）")
        seg = h.state.segments[0]
        self.assertEqual(seg.status, ERROR)
        self.assertTrue(seg.settled)

    def test_translation_for_cleared_segment_is_dropped(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello.", True)))
        h.state.clear()
        h.drain()
        h.state.on_translation(1, "你好", True, None)
        self.assertEqual(h.events, [])

    def test_translation_after_done_is_dropped(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello.", True)))
        h.state.on_translation(1, "你好", True, None)
        h.drain()
        h.state.on_translation(1, "迟到的片段", False, None)
        self.assertEqual(h.events, [])


# ─── 重译 ─────────────────────────────────────────────────────────────────────
class TestRetranslate(unittest.TestCase):

    def test_retranslate_failed_segment(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello.", True)))
        h.state.on_translation(1, "", True, "请求过于频繁")
        h.drain()

        self.assertTrue(h.state.retranslate(1))
        self.assertEqual(h.of("segment")[0]["status"], PENDING)
        self.assertEqual(h.translator.submitted, [(1, "Hello."), (1, "Hello.")])

    def test_cannot_retranslate_while_pending(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello.", True)))
        h.drain()
        self.assertFalse(h.state.retranslate(1))
        self.assertEqual(h.events, [])

    def test_cannot_retranslate_chinese_or_missing(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("好的，开始吧。", True)))
        self.assertFalse(h.state.retranslate(1))
        self.assertFalse(h.state.retranslate(999))


# ─── 自动记录 ─────────────────────────────────────────────────────────────────
class TestTakeSettled(unittest.TestCase):

    def test_stops_at_first_pending(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("One.", True), ("Two.", True), ("Three.", True)))
        h.state.on_translation(1, "一。", True, None)
        # 第 2 句仍在翻译中，第 3 句已完成，但不能越过第 2 句取走
        h.state.on_translation(3, "三。", True, None)
        h.drain()

        ready = h.state.take_settled()
        self.assertEqual([s.source for s in ready], ["One."])
        self.assertEqual(h.of("removed"), [{"type": "removed", "ids": [1]}])
        self.assertEqual([s.id for s in h.state.segments], [2, 3])

    def test_force_takes_everything(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("One.", True), ("Two.", True)))
        ready = h.state.take_settled(force=True)
        self.assertEqual(len(ready), 2)
        self.assertEqual(h.state.segments, [])

    def test_nothing_ready_emits_nothing(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("One.", True)))
        h.drain()
        self.assertEqual(h.state.take_settled(), [])
        self.assertEqual(h.events, [])


# ─── 记录格式 ─────────────────────────────────────────────────────────────────
class TestFormatRecord(unittest.TestCase):

    def test_translation_as_quote_block(self):
        segs = [
            Segment(1, 1, "Hello everyone.", DONE, "大家好。"),
            Segment(2, 1, "好的，我们开始吧。", SKIPPED),
        ]
        self.assertEqual(
            format_record(segs),
            "Hello everyone.\n> 大家好。\n\n好的，我们开始吧。",
        )

    def test_error_and_pending_are_marked(self):
        segs = [
            Segment(1, 1, "A.", ERROR, "", "账户余额不足"),
            Segment(2, 1, "B.", PENDING, "半句"),
            Segment(3, 1, "C.", PENDING),
        ]
        self.assertEqual(
            format_record(segs),
            "A.\n> [翻译失败] 账户余额不足\n\n"
            "B.\n> 半句…（翻译未完成）\n\n"
            "C.\n> （翻译未完成）",
        )

    def test_multiline_translation(self):
        segs = [Segment(1, 1, "A.", DONE, "第一行\n\n第二行")]
        self.assertEqual(format_record(segs), "A.\n> 第一行\n> 第二行")

    def test_blank_sources_dropped(self):
        self.assertEqual(format_record([Segment(1, 1, "   ", NONE)]), "")
        self.assertEqual(format_record([]), "")


# ─── 快照 ─────────────────────────────────────────────────────────────────────
class TestSnapshot(unittest.TestCase):

    def test_snapshot_rebuilds_the_view(self):
        h = Harness(clock=lambda: 99.0)
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello.", True), ("好的。", True), ("tail", False)))
        h.state.on_translation(1, "你好。", True, None)

        snap = h.state.snapshot()
        self.assertEqual(snap["sessions"], [{"id": sid, "at": 99.0}])
        self.assertEqual(snap["interim"], "tail")
        self.assertTrue(snap["translate"])
        self.assertEqual(
            snap["segments"],
            [{"id": 1, "session_id": sid, "source": "Hello.", "status": DONE, "translation": "你好。"},
             {"id": 2, "session_id": sid, "source": "好的。", "status": SKIPPED}],
        )

    def test_sessions_without_visible_segments_are_omitted(self):
        """自动记录清走了某个会话的全部内容后，不该再留一条空的分隔线。"""
        h = Harness(translate=False)
        first = h.state.begin_session()
        h.state.on_asr(first, asr(("gone", True), is_last=True))
        h.state.take_settled()
        second = h.state.begin_session()
        h.state.on_asr(second, asr(("kept", True)))

        self.assertEqual([s["id"] for s in h.state.snapshot()["sessions"]], [second])

    def test_clear_empties_snapshot_and_resets_translator_context(self):
        h = Harness()
        sid = h.state.begin_session()
        h.state.on_asr(sid, asr(("Hello.", True), ("tail", False)))
        h.state.clear()

        snap = h.state.snapshot()
        self.assertEqual(snap["segments"], [])
        self.assertEqual(snap["interim"], "")
        self.assertEqual(h.translator.cancelled, 1)
        self.assertEqual(h.translator.cleared, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
