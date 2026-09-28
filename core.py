"""
UI 无关的转录状态机。

把「识别结果 → 分句 → 调度翻译 → 记录」这条链路从界面里剥出来，供浏览器端
（web/server.py）使用；本模块不依赖 tkinter / pyaudio / aiohttp / 网络，
可以单独单元测试。tkinter 版 realtime_asr_client.py 仍然自带一套渲染逻辑，
只从这里共用 split_utterances()。

对外只有两个方向：

    输入   begin_session / on_asr / on_translation / 控制方法
    输出   emit(event) 回调，event 是可以直接 json.dumps 的 dict

翻译不在这里发起网络请求：构造时传入一个翻译器对象，只要它有
submit / add_context / clear_context / cancel_all 四个方法即可
（translator.TranslationEngine 正好满足，测试里传假对象）。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

from translator import DEFAULT_TARGET_LANG, needs_translation

# 分句的翻译状态
NONE = "none"          # 未开启翻译
SKIPPED = "skipped"    # 原文已是中文 / 没有可翻译的文字
PENDING = "pending"    # 翻译中
DONE = "done"          # 翻译完成
ERROR = "error"        # 翻译失败

SETTLED = (NONE, SKIPPED, DONE, ERROR)


class Translator(Protocol):
    """translator.TranslationEngine 的最小接口。"""

    def submit(self, seg_id: int, text: str) -> None: ...
    def add_context(self, text: str) -> None: ...
    def clear_context(self) -> None: ...
    def cancel_all(self) -> None: ...


class NullTranslator:
    """不翻译时的占位实现。"""

    def submit(self, seg_id: int, text: str) -> None: pass
    def add_context(self, text: str) -> None: pass
    def clear_context(self) -> None: pass
    def cancel_all(self) -> None: pass


# ─── 识别结果 → 分句 ────────────────────────────────────────────────────────────
def split_utterances(data: Dict[str, Any]) -> Tuple[List[str], str]:
    """把识别结果拆成（已确定分句列表，当前临时文字）。"""
    result = data.get("result") or {}
    if isinstance(result, list):
        result = result[0] if result else {}
    utterances = result.get("utterances") or []
    definite: List[str] = []
    interim = ""
    for utt in utterances:
        if utt.get("definite"):
            definite.append(utt.get("text", ""))
        else:
            interim = utt.get("text", "")
    # 如果没有 utterances，用顶层 text 作为临时结果
    if not utterances:
        interim = result.get("text", "")
    return definite, interim


@dataclass
class Segment:
    """界面上的一条已确定分句，以及它的译文状态。"""
    id: int
    session_id: int
    source: str
    status: str = NONE
    translation: str = ""
    error: str = ""

    @property
    def settled(self) -> bool:
        return self.status in SETTLED

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "session_id": self.session_id,
            "source": self.source,
            "status": self.status,
        }
        if self.translation:
            d["translation"] = self.translation
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class SessionState:
    """一次录音会话的上屏进度。

    请求里 result_type="full"，服务端每个响应都带着会话内的全部分句，
    只需上屏 rendered 之后新增的 definite 分句。该计数**不随清空 / 自动记录
    归零**，否则旧句子会重新上屏并被重复翻译（重复计费）。
    """
    id: int
    at: float
    rendered: int = 0
    closed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "at": self.at}


def format_record(segments: Iterable[Segment]) -> str:
    """整理成写入 RecordMemory.md 的文本：译文用引用块写在原文下方。"""
    blocks: List[str] = []
    for seg in segments:
        if not seg.source.strip():
            continue
        lines = [seg.source]
        if seg.status == DONE:
            lines += ["> " + line for line in seg.translation.splitlines() if line.strip()]
        elif seg.status == ERROR:
            lines.append(f"> [翻译失败] {seg.error}")
        elif seg.status == PENDING:
            lines.append(f"> {seg.translation}…（翻译未完成）" if seg.translation else "> （翻译未完成）")
        blocks.append("\n".join(lines))
    return "\n\n".join(b.strip() for b in blocks if b.strip())


# ─── 状态机 ───────────────────────────────────────────────────────────────────
class TranscriptState:
    """持有界面上的分句与会话，吐出前端可直接消费的事件。

    所有方法都必须在同一个线程（服务端的事件循环线程）里调用；
    ASR / 翻译引擎的回调要先用 loop.call_soon_threadsafe 跳过来。
    """

    def __init__(
        self,
        emit: Callable[[Dict[str, Any]], None],
        translator: Optional[Translator] = None,
        *,
        target_lang: str = DEFAULT_TARGET_LANG,
        skip_chinese: bool = True,
        translate: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._emit = emit
        self._translator: Translator = translator or NullTranslator()
        self.target_lang = target_lang
        self.skip_chinese = skip_chinese
        self.translate = translate
        self._clock = clock

        self._session_ids = itertools.count(1)
        self._segment_ids = itertools.count(1)
        self._sessions: Dict[int, SessionState] = {}
        self._segments: Dict[int, Segment] = {}
        self._current_session: Optional[int] = None
        self._interim = ""

    # ── 只读视图 ──────────────────────────────────────────────────────────────
    @property
    def segments(self) -> List[Segment]:
        return list(self._segments.values())

    @property
    def interim(self) -> str:
        return self._interim

    def snapshot(self) -> Dict[str, Any]:
        """新连上来的浏览器用它重建整个界面（刷新页面不丢内容）。"""
        used = {seg.session_id for seg in self._segments.values()}
        return {
            "sessions": [s.to_dict() for s in self._sessions.values() if s.id in used],
            "segments": [seg.to_dict() for seg in self._segments.values()],
            "interim": self._interim,
            "translate": self.translate,
        }

    # ── 会话 ──────────────────────────────────────────────────────────────────
    def begin_session(self) -> int:
        """开始一次录音，返回会话编号；把它传给 ASR 引擎的回调。"""
        sid = next(self._session_ids)
        session = SessionState(id=sid, at=self._clock())
        self._sessions[sid] = session
        self._current_session = sid
        self._emit({"type": "session", **session.to_dict()})
        return sid

    def _close_session(self, session: SessionState) -> None:
        if session.closed:
            return
        session.closed = True
        if session.id == self._current_session:
            self._current_session = None
            self.set_interim("")
        self._emit({"type": "session_end", "id": session.id})

    def end_session(self, sid: int) -> None:
        """本地主动结束（停止录音时调用）；服务端的 is_last 也会走到这里。"""
        session = self._sessions.get(sid)
        if session is not None:
            self._close_session(session)

    # ── 识别结果 ──────────────────────────────────────────────────────────────
    def on_asr(self, sid: int, resp: Dict[str, Any]) -> None:
        """处理一条 ASR 响应。resp 形如 {"data": {...}, "is_last": bool} 或 {"error": str}。"""
        session = self._sessions.get(sid)
        if session is None or session.closed:
            return  # 会话已结束，迟到的响应丢弃

        if "error" in resp:
            self._emit({"type": "asr_error", "message": str(resp["error"])})
            self._close_session(session)
            return

        definite, interim = split_utterances(resp.get("data") or {})

        # result_type="full"：每次都是全量，只取 rendered 之后新增的部分
        for text in definite[session.rendered:]:
            if text:
                self._add_segment(session, text)
        session.rendered = max(session.rendered, len(definite))

        if resp.get("is_last"):
            self._close_session(session)
        elif sid == self._current_session:
            self.set_interim(interim)

    def set_interim(self, text: str) -> None:
        if text == self._interim:
            return
        self._interim = text
        self._emit({"type": "interim", "text": text})

    def _add_segment(self, session: SessionState, text: str) -> Segment:
        seg = Segment(id=next(self._segment_ids), session_id=session.id, source=text)

        if self.translate:
            if needs_translation(text, self.target_lang, self.skip_chinese):
                seg.status = PENDING
            else:
                seg.status = SKIPPED  # 原文已是中文，或没有可翻译的文字

        self._segments[seg.id] = seg
        self._emit({"type": "segment", **seg.to_dict()})

        if seg.status == PENDING:
            self._translator.submit(seg.id, text)
        elif seg.status == SKIPPED:
            self._translator.add_context(text)  # 不翻译，但留作后续句子的语境
        return seg

    # ── 翻译结果 ──────────────────────────────────────────────────────────────
    def on_translation(self, seg_id: int, text: str, done: bool, error: Optional[str]) -> None:
        seg = self._segments.get(seg_id)
        if seg is None or seg.status != PENDING:
            return  # 已被清空、已保存，或这条已经结束了

        if error:
            seg.status, seg.error = ERROR, error
            self._emit({"type": "error", "id": seg.id, "message": error})
            return

        seg.translation = text
        if done:
            seg.status = DONE
        self._emit({"type": "translation", "id": seg.id, "text": text, "done": bool(done)})

    def retranslate(self, seg_id: int) -> bool:
        """重译一条已完成或失败的分句。返回是否真的提交了。"""
        seg = self._segments.get(seg_id)
        if seg is None or seg.status == PENDING:
            return False
        if not needs_translation(seg.source, self.target_lang, self.skip_chinese):
            return False
        seg.status, seg.translation, seg.error = PENDING, "", ""
        self._emit({"type": "segment", **seg.to_dict()})
        self._translator.submit(seg.id, seg.source)
        return True

    # ── 控制 ──────────────────────────────────────────────────────────────────
    def set_translate(self, on: bool) -> None:
        """开关翻译，对之后的句子生效；已在翻译中的句子不受影响。"""
        if on == self.translate:
            return
        self.translate = on
        self._emit({"type": "translate", "on": on})

    def clear(self) -> None:
        """清空界面。各会话的 rendered 计数保持不变，旧句子不会重新上屏。"""
        self._translator.cancel_all()
        self._translator.clear_context()
        self._segments.clear()
        self._interim = ""
        self._emit({"type": "cleared"})

    def peek_settled(self, force: bool = False) -> List[Segment]:
        """查看开头一段已结束的分句（用于自动记录），**不**移除。

        只看到第一条仍在翻译中的分句之前，剩下的等译文完成后下次再看；
        force=True（退出前最后一次保存）则返回全部。
        调用方应当先写盘成功，再调 drop()——写失败时内容还留在界面上。
        """
        ready: List[Segment] = []
        for seg in self._segments.values():
            if not (seg.settled or force):
                break
            ready.append(seg)
        return ready

    def drop(self, segments: Iterable[Segment]) -> None:
        """把已经保存好的分句从界面移除。"""
        ids = [seg.id for seg in segments if seg.id in self._segments]
        if not ids:
            return
        for seg_id in ids:
            del self._segments[seg_id]
        self._emit({"type": "removed", "ids": ids})

    def take_settled(self, force: bool = False) -> List[Segment]:
        """peek_settled() + drop() 的合并写法。"""
        ready = self.peek_settled(force)
        self.drop(ready)
        return ready
