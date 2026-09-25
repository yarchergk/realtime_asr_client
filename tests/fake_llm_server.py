"""
测试用的 OpenAI 兼容假服务：在后台线程运行 aiohttp 服务，记录收到的请求，按预设方式应答。
"""

import asyncio
import json
import socket
import threading
from typing import Any, Awaitable, Callable, Dict, List

from aiohttp import web

Handler = Callable[[web.Request, Dict[str, Any]], Awaitable[web.StreamResponse]]


def sse_event(obj: Any) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def delta(content: str = None, reasoning: str = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    if content is not None:
        d["content"] = content
    if reasoning is not None:
        d["reasoning_content"] = reasoning
    return {"choices": [{"index": 0, "delta": d, "finish_reason": None}]}


def source_of(body: Dict[str, Any]) -> str:
    """取出请求里【待翻译】部分的原文。"""
    user = body["messages"][-1]["content"]
    return user.split("【待翻译】\n", 1)[-1]


def sse_reply(pieces: List[str], reasoning: List[str] = (), delay: float = 0.0,
              gate: threading.Event = None) -> Handler:
    """按片段流式返回；gate 不为空时，等 gate 被 set 后才开始输出正文。"""
    async def handler(request: web.Request, body: Dict[str, Any]) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream; charset=utf-8"})
        await resp.prepare(request)
        try:
            await resp.write(b": keep-alive\n\n")
            for r in reasoning:
                await resp.write(sse_event(delta(reasoning=r)))
            if gate is not None:
                while not gate.is_set():
                    await asyncio.sleep(0.02)
            for p in pieces:
                await resp.write(sse_event(delta(content=p)))
                if delay:
                    await asyncio.sleep(delay)
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
        except ConnectionResetError:
            pass  # 客户端已取消请求
        return resp
    return handler


def echo_reply(prefix: str = "译：", gate: threading.Event = None) -> Handler:
    """把原文加上前缀作为「译文」流式返回（分两段输出）。"""
    async def handler(request: web.Request, body: Dict[str, Any]) -> web.StreamResponse:
        text = prefix + source_of(body)
        half = max(1, len(text) // 2)
        return await sse_reply([text[:half], text[half:]], gate=gate)(request, body)
    return handler


def json_reply(content: str, status: int = 200) -> Handler:
    async def handler(request: web.Request, body: Dict[str, Any]) -> web.StreamResponse:
        return web.json_response(
            {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]},
            status=status)
    return handler


def error_reply(status: int, message: str) -> Handler:
    async def handler(request: web.Request, body: Dict[str, Any]) -> web.StreamResponse:
        return web.json_response({"error": {"message": message, "type": "invalid_request_error"}},
                                 status=status)
    return handler


class FakeLLMServer:
    """handlers 按请求顺序依次使用；用完后使用 default_handler。"""

    def __init__(self):
        self.requests: List[Dict[str, Any]] = []
        self.handlers: List[Handler] = []
        self.default_handler: Handler = echo_reply()
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._runner = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> "FakeLLMServer":
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(5)
        return self

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()

    def reset(self) -> None:
        self.requests.clear()
        self.handlers.clear()
        self.default_handler = echo_reply()

    async def _start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._handle)
        self._runner = web.AppRunner(app, shutdown_timeout=1.0)
        await self._runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        await web.SockSite(self._runner, sock).start()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append({"headers": dict(request.headers), "body": body})
        handler = self.handlers.pop(0) if self.handlers else self.default_handler
        return await handler(request, body)
