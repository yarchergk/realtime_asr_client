"""
浏览器端服务：本地起一个 HTTP + WebSocket 服务，把实时识别与翻译推给网页界面。

    python web_app.py            # 启动并自动打开浏览器
    python web_app.py --no-open --port 8760

和 tkinter 版（realtime_asr_client.py）共用同一套音频采集、ASR 协议、翻译模块，
区别只在于界面层：这里把 core.TranscriptState 吐出的事件广播给浏览器。

线程边界（沿用 CLAUDE.md 的约定，只是终点从 tkinter 换成事件循环）：

    pyaudio 回调线程 ─┐
    ASR asyncio 线程 ─┼─ loop.call_soon_threadsafe ─→ 服务端事件循环 ─→ WebSocket
    翻译 asyncio 线程 ┘

绝不要在引擎线程里直接碰 TranscriptState 或发送 WebSocket。
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import logging
import mimetypes
import os
import sys
import time
import webbrowser
from typing import Any, Dict, List, Optional, Set

from aiohttp import WSMsgType, web

from core import TranscriptState, format_record
# 复用 tkinter 版里的音频采集与 ASR 协议实现（它们本身不依赖 tkinter）
from realtime_asr_client import (
    AudioCapture, RealtimeAsrEngine, get_application_path, load_config,
)
from translator import (
    PROVIDER_PRESETS, TranslationEngine, TranslatorConfig,
    check_base_url, format_extra_body, parse_extra_body, save_env_values,
)

logger = logging.getLogger(__name__)

# Windows 注册表里通常没有 woff2，不补的话会以 application/octet-stream 发出去。
# aiohttp 的静态文件用它自己的 MimeTypes 实例，所以两边都要注册；
# 万一将来 aiohttp 换了内部结构，退回 octet-stream 浏览器也还能认（按魔数嗅探）。
mimetypes.add_type("font/woff2", ".woff2")
try:
    from aiohttp.web_fileresponse import CONTENT_TYPES
    CONTENT_TYPES.add_type("font/woff2", ".woff2")
except Exception:                                    # pragma: no cover
    logger.debug("aiohttp CONTENT_TYPES unavailable; woff2 served as octet-stream")

HOST = "127.0.0.1"          # 只监听本机，不对局域网开放
DEFAULT_PORT = 8760
ENV_PATH = os.path.join(get_application_path(), ".env")
RECORD_PATH = os.path.join(get_application_path(), "RecordMemory.md")

# 网页上的音频源取值 → AudioCapture 的 mode（两边命名不同，只在这里转换）
SOURCE_MODES = {"mic": "mic", "sys": "loopback", "both": "both"}
SOURCE_LABELS = {"mic": "麦克风", "sys": "系统音频", "both": "麦克风 + 系统音频"}


def resource_path(*parts: str) -> str:
    """只读资源（网页静态文件）的路径。

    注意和 get_application_path() 的区别：那个给**用户数据**用（.env / logs /
    RecordMemory.md，要在 exe 旁边）；静态资源打包进单文件 exe 后在 _MEIPASS 里。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


STATIC_DIR = resource_path("web", "static")


def rms_level(pcm: bytes) -> float:
    """16-bit 单声道 PCM 的音量，归一化到 0–1。给界面上的电平线用。"""
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return min(1.0, (total / len(samples)) ** 0.5 / 8000.0)


class Client:
    """一个浏览器连接。事件先进队列，由写出任务发送，慢客户端不拖住服务端。"""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def put(self, payload: str) -> None:
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("websocket client too slow, dropping event")

    async def run(self) -> None:
        while True:
            payload = await self.queue.get()
            await self.ws.send_str(payload)


class Hub:
    """服务端的全部可变状态。所有方法只在事件循环线程里调用。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.clients: Set[Client] = set()

        self.asr_cfg = load_config()
        self.tr_cfg = TranslatorConfig.from_env()
        self.translator = TranslationEngine(self.tr_cfg, self._translation_from_thread)
        self.translator.start()

        self.state = TranscriptState(
            self.broadcast,
            self.translator,
            target_lang=self.tr_cfg.target_lang,
            skip_chinese=self.tr_cfg.skip_chinese,
            translate=self.tr_cfg.enabled_by_default(),
        )

        self.engine: Optional[RealtimeAsrEngine] = None
        self.capture: Optional[AudioCapture] = None
        self.recording = False
        self.source = "mic"
        self.session_id: Optional[int] = None
        self.status_text = "就绪"

        self.auto_save = False
        self.save_interval = 30
        self._save_task: Optional[asyncio.Task] = None

    # ── 广播 ──────────────────────────────────────────────────────────────────
    def broadcast(self, event: Dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        for client in list(self.clients):
            client.put(payload)

    def emit_status(self) -> None:
        self.broadcast({
            "type": "status",
            "recording": self.recording,
            "source": self.source,
            "text": self.status_text,
        })

    def emit_config(self) -> None:
        self.broadcast({"type": "config", **self.config_dict()})

    def config_dict(self) -> Dict[str, Any]:
        cfg = self.tr_cfg
        return {
            "base_url": cfg.base_url,
            "api_key": "•" * 12 if cfg.api_key else "",   # 不把真实密钥发给页面
            "has_key": bool(cfg.api_key),
            "model": cfg.model,
            "target_lang": cfg.target_lang,
            "extra_body": format_extra_body(cfg.extra_body),
            "context_size": cfg.context_size,
            "ready": cfg.is_ready(),
            "reason": cfg.missing_reason(),
        }

    def snapshot(self) -> Dict[str, Any]:
        """浏览器刷新后靠它重建界面：服务端是唯一真相源。"""
        asr_ok = bool(self.asr_cfg["app_key"] and self.asr_cfg["access_key"])
        return {
            **self.state.snapshot(),
            "recording": self.recording,
            "source": self.source,
            "status": self.status_text,
            "auto_save": self.auto_save,
            "save_interval": self.save_interval,
            "config": self.config_dict(),
            "asr_ready": asr_ok,
            "asr_reason": "" if asr_ok else "未配置 VOLCENGINE_APP_KEY / VOLCENGINE_ACCESS_KEY，请填写 .env 后重启",
            "providers": [
                {"name": p.name, "base_url": p.base_url, "model": p.model,
                 "extra_body": format_extra_body(p.extra_body)}
                for p in PROVIDER_PRESETS
            ],
        }

    # ── 引擎线程 → 事件循环 ───────────────────────────────────────────────────
    def _asr_from_thread(self, sid: int):
        def callback(resp: Dict[str, Any]) -> None:
            self.loop.call_soon_threadsafe(self._on_asr, sid, resp)
        return callback

    def _translation_from_thread(self, seg_id: int, text: str, done: bool, error: Optional[str]) -> None:
        self.loop.call_soon_threadsafe(self.state.on_translation, seg_id, text, done, error)

    def _audio_from_thread(self, pcm: bytes) -> None:
        engine = self.engine
        if engine is not None:
            engine.push_audio(pcm)
        level = rms_level(pcm)
        self.loop.call_soon_threadsafe(self.broadcast, {"type": "level", "v": round(level, 3)})

    def _on_asr(self, sid: int, resp: Dict[str, Any]) -> None:
        had_error = "error" in resp
        self.state.on_asr(sid, resp)
        if had_error and sid == self.session_id and self.recording:
            self.stop_recording("连接已断开")

    # ── 录音 ──────────────────────────────────────────────────────────────────
    def start_recording(self, source: str) -> None:
        if self.recording:
            return
        if not (self.asr_cfg["app_key"] and self.asr_cfg["access_key"]):
            self.broadcast({"type": "asr_error", "message": "未配置语音识别密钥，请检查 .env"})
            return

        self.source = source if source in SOURCE_MODES else "mic"
        sid = self.state.begin_session()
        self.session_id = sid
        self.engine = RealtimeAsrEngine(self.asr_cfg, self._asr_from_thread(sid))
        self.engine.start()

        try:
            self.capture = AudioCapture(self._audio_from_thread, mode=SOURCE_MODES[self.source])
            self.capture.start()
        except Exception as e:                       # 设备打不开：回滚，别留下半开的状态
            logger.error(f"audio capture failed: {e}")
            if self.capture is not None:
                try:
                    self.capture.stop()
                except Exception as stop_err:
                    logger.error(f"audio cleanup failed: {stop_err}")
                self.capture = None
            self.engine.stop()
            self.engine = None
            self.state.end_session(sid)
            self.session_id = None
            self.status_text = "启动失败"
            self.broadcast({"type": "asr_error", "message": str(e)})
            self.emit_status()
            return

        self.recording = True
        self.status_text = f"录音中（{SOURCE_LABELS[self.source]}）"
        self.emit_status()

    def stop_recording(self, status: str = "已停止") -> None:
        if not self.recording:
            return
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        if self.engine is not None:
            # 软停止：先发结束包，再停引擎；反过来会丢掉最后一句话
            self.engine.send_eof()
            self.engine.stop()
            self.engine = None
        self.recording = False
        self.status_text = status
        self.state.set_interim("")
        # 会话不在这里关闭：末包的 is_last 响应还在路上，关早了最后一句就没了
        self.emit_status()

    # ── 自动记录 ──────────────────────────────────────────────────────────────
    def set_auto_save(self, on: bool, interval: Optional[int] = None) -> None:
        if interval is not None:
            self.save_interval = max(5, int(interval))
        self.auto_save = bool(on)
        if self._save_task is not None:
            self._save_task.cancel()
            self._save_task = None
        if self.auto_save:
            self._save_task = self.loop.create_task(self._save_loop())
        self.broadcast({"type": "auto_save", "on": self.auto_save, "interval": self.save_interval})

    async def _save_loop(self) -> None:
        try:
            while self.auto_save:
                await asyncio.sleep(self.save_interval)
                if self.auto_save:
                    self.save_now()
        except asyncio.CancelledError:
            pass

    def save_now(self, force: bool = False) -> int:
        """把已结束的分句追加写入 RecordMemory.md，写成功后再从界面移除。"""
        ready = self.state.peek_settled(force)
        if not ready:
            return 0
        content = format_record(ready)
        if content:
            try:
                with open(RECORD_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n## {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{content}\n")
            except Exception as e:
                logger.error(f"auto-save failed: {e}")
                self.broadcast({"type": "save_error", "message": f"写入 RecordMemory.md 失败：{e}"})
                return 0                                  # 写失败，内容留在界面上
        self.state.drop(ready)
        self.broadcast({"type": "saved", "count": len(ready)})
        return len(ready)

    # ── 翻译设置 ──────────────────────────────────────────────────────────────
    def _build_config(self, data: Dict[str, Any]) -> TranslatorConfig:
        base_url = check_base_url(str(data.get("base_url") or ""))
        api_key = str(data.get("api_key") or "")
        if not api_key or set(api_key) == {"•"}:
            api_key = self.tr_cfg.api_key          # 页面没改密钥，沿用原值
        model = str(data.get("model") or "").strip()
        if not model:
            raise ValueError("请填写模型名")
        return TranslatorConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            target_lang=str(data.get("target_lang") or "").strip() or self.tr_cfg.target_lang,
            extra_body=parse_extra_body(str(data.get("extra_body") or "")),
            context_size=max(0, min(20, int(data.get("context_size", self.tr_cfg.context_size)))),
            temperature=self.tr_cfg.temperature,
            timeout=self.tr_cfg.timeout,
            max_concurrency=self.tr_cfg.max_concurrency,
            skip_chinese=self.tr_cfg.skip_chinese,
            stream=self.tr_cfg.stream,
        )

    def apply_settings(self, data: Dict[str, Any]) -> None:
        cfg = self._build_config(data)
        self.tr_cfg = cfg
        self.translator.set_config(cfg)
        self.state.target_lang = cfg.target_lang
        self.state.skip_chinese = cfg.skip_chinese
        save_env_values(ENV_PATH, {
            "TRANSLATE_BASE_URL": cfg.base_url,
            "TRANSLATE_API_KEY": cfg.api_key,
            "TRANSLATE_MODEL": cfg.model,
            "TRANSLATE_TARGET_LANG": cfg.target_lang,
            "TRANSLATE_EXTRA_BODY": format_extra_body(cfg.extra_body),
            "TRANSLATE_CONTEXT_SIZE": str(cfg.context_size),
        })
        self.emit_config()

    async def test_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self._build_config(data)
        try:
            text, seconds = await asyncio.wrap_future(self.translator.test(cfg))
            return {"type": "test_result", "ok": True, "text": text, "seconds": round(seconds, 2)}
        except Exception as e:
            return {"type": "test_result", "ok": False, "message": str(e)}

    # ── 命令分发 ──────────────────────────────────────────────────────────────
    async def handle(self, msg: Dict[str, Any], client: Client) -> None:
        cmd = msg.get("cmd")

        if cmd == "start":
            self.start_recording(str(msg.get("source") or "mic"))
        elif cmd == "stop":
            self.stop_recording()
        elif cmd == "clear":
            self.state.clear()
        elif cmd == "translate":
            self.state.set_translate(bool(msg.get("on")))
        elif cmd == "retranslate":
            self.state.retranslate(int(msg.get("id", 0)))
        elif cmd == "auto_save":
            self.set_auto_save(bool(msg.get("on")), msg.get("interval"))
        elif cmd == "save_now":
            self.save_now(force=bool(msg.get("force")))
        elif cmd == "source":
            if not self.recording:
                self.source = str(msg.get("source") or "mic")
                self.emit_status()
        elif cmd == "save_settings":
            try:
                self.apply_settings(msg)
                client.put(json.dumps({"type": "settings_saved", "ok": True}, ensure_ascii=False))
            except Exception as e:
                client.put(json.dumps({"type": "settings_saved", "ok": False, "message": str(e)},
                                      ensure_ascii=False))
        elif cmd == "test_settings":
            try:
                result = await self.test_settings(msg)
            except Exception as e:
                result = {"type": "test_result", "ok": False, "message": str(e)}
            client.put(json.dumps(result, ensure_ascii=False))
        else:
            logger.warning(f"unknown command: {cmd!r}")

    async def shutdown(self) -> None:
        self.stop_recording("已退出")
        if self.auto_save:
            self.save_now(force=True)
        if self._save_task is not None:
            self._save_task.cancel()
        self.translator.stop()


# ─── HTTP / WebSocket ─────────────────────────────────────────────────────────
async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)

    hub: Hub = request.app["hub"]
    client = Client(ws)
    hub.clients.add(client)
    writer = asyncio.ensure_future(client.run())
    logger.info(f"client connected ({len(hub.clients)} total)")

    try:
        await ws.send_str(json.dumps({"type": "hello", "state": hub.snapshot()}, ensure_ascii=False))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(msg.data)
            except ValueError:
                logger.warning("malformed command from client")
                continue
            try:
                await hub.handle(payload, client)
            except Exception:
                logger.exception(f"command failed: {payload.get('cmd')!r}")
    finally:
        writer.cancel()
        hub.clients.discard(client)
        logger.info(f"client disconnected ({len(hub.clients)} left)")
    return ws


async def on_startup(app: web.Application) -> None:
    app["hub"] = Hub(asyncio.get_running_loop())


async def on_cleanup(app: web.Application) -> None:
    hub: Optional[Hub] = app.get("hub")
    if hub is not None:
        await hub.shutdown()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", websocket)
    app.router.add_static("/", STATIC_DIR, name="static")
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="IEC 会议实时语音翻译 · 浏览器版")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    args = parser.parse_args()

    url = f"http://{HOST}:{args.port}/"
    print(f"IEC 会议实时语音翻译 · 浏览器版\n界面地址：{url}\n按 Ctrl+C 退出")
    if not args.no_open:
        # 服务起来之前浏览器会连不上，延后一点再开
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    web.run_app(build_app(), host=HOST, port=args.port, print=None)


if __name__ == "__main__":
    main()
