"""
实时语音转文字 + 实时翻译客户端
调用火山引擎大模型流式语音识别 API，实时采集麦克风 / 系统音频并显示识别结果；
可选把每个已确定的句子交给大模型（默认 DeepSeek V4.1 Flash）实时翻译成中文。

依赖安装：
    pip install -r requirements.txt

配置：
    复制 .env.example 为 .env，填入 API 密钥
"""

import asyncio
import aiohttp
import json
import struct
import gzip
import uuid
import itertools
import logging
import os
import sys
import threading
import queue
import time
import struct as struct_mod
import tkinter as tk
from dataclasses import dataclass, replace
from tkinter import scrolledtext, messagebox, ttk
from typing import Dict, Any, List, Optional, Tuple

from translator import (
    PROVIDER_PRESETS, TEST_TEXT, TranslationEngine, TranslatorConfig,
    check_base_url, find_preset, format_extra_body, needs_translation,
    parse_extra_body, save_env_values,
)

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    try:
        import pyaudio
    except ImportError:
        pyaudio = None

# ─── 获取应用目录（兼容打包后的exe） ──────────────────────────────────────────
def get_application_path():
    """获取应用程序所在目录，兼容开发环境和打包后的exe"""
    if getattr(sys, 'frozen', False):
        # 打包后的exe环境：sys.executable 是 exe 文件路径
        return os.path.dirname(sys.executable)
    else:
        # 开发环境：__file__ 是脚本文件路径
        return os.path.dirname(os.path.abspath(__file__))

ENV_PATH = os.path.join(get_application_path(), ".env")

try:
    from dotenv import load_dotenv
    # 先读程序所在目录的 .env（exe 从快捷方式启动时工作目录可能不同），再按默认规则查找
    load_dotenv(ENV_PATH)
    load_dotenv()
except ImportError:
    pass  # python-dotenv 未安装时直接读环境变量

# ─── 日志 ────────────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(get_application_path(), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, 'realtime_asr.log'), encoding='utf-8')]
)
logger = logging.getLogger(__name__)

# ─── 音频参数 ─────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2          # 16-bit = 2 bytes
CHUNK_MS = 200            # 每包 200ms
CHUNK_SIZE = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * CHUNK_MS // 1000  # 6400 bytes

# ─── API 配置 ─────────────────────────────────────────────────────────────────
WS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"

def load_config() -> Dict[str, str]:
    app_key = os.environ.get("VOLCENGINE_APP_KEY", "")
    access_key = os.environ.get("VOLCENGINE_ACCESS_KEY", "")
    resource_id = os.environ.get("VOLCENGINE_RESOURCE_ID", "volc.bigasr.sauc.duration")
    return {"app_key": app_key, "access_key": access_key, "resource_id": resource_id}

# ─── 协议常量 ─────────────────────────────────────────────────────────────────
class _Proto:
    VERSION = 0b0001
    FULL_REQUEST = 0b0001
    AUDIO_ONLY = 0b0010
    FULL_RESPONSE = 0b1001
    ERROR_RESPONSE = 0b1111
    NO_SEQ = 0b0000
    POS_SEQ = 0b0001
    NEG_SEQ = 0b0010
    NEG_WITH_SEQ = 0b0011
    JSON = 0b0001
    GZIP = 0b0001

# ─── 协议工具函数 ──────────────────────────────────────────────────────────────
def _make_header(msg_type: int, flags: int, serial: int = _Proto.JSON, compress: int = _Proto.GZIP) -> bytes:
    return bytes([
        (_Proto.VERSION << 4) | 1,
        (msg_type << 4) | flags,
        (serial << 4) | compress,
        0x00,
    ])

def _gz(data: bytes) -> bytes:
    return gzip.compress(data)

def _ungz(data: bytes) -> bytes:
    return gzip.decompress(data)

def build_full_request(seq: int, cfg: Dict[str, str]) -> bytes:
    header = _make_header(_Proto.FULL_REQUEST, _Proto.POS_SEQ)
    payload = {
        "user": {"uid": "realtime_client"},
        "audio": {"format": "pcm", "codec": "raw", "rate": SAMPLE_RATE, "bits": 16, "channel": CHANNELS},
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": False,
            "show_utterances": True,
            "result_type": "full",
        },
    }
    compressed = _gz(json.dumps(payload).encode())
    return header + struct.pack('>i', seq) + struct.pack('>I', len(compressed)) + compressed

def build_audio_request(seq: int, pcm: bytes, is_last: bool) -> bytes:
    if is_last:
        header = _make_header(_Proto.AUDIO_ONLY, _Proto.NEG_WITH_SEQ)
        seq = -seq
    else:
        header = _make_header(_Proto.AUDIO_ONLY, _Proto.POS_SEQ)
    compressed = _gz(pcm)
    return header + struct.pack('>i', seq) + struct.pack('>I', len(compressed)) + compressed

def parse_response(msg: bytes) -> Dict[str, Any]:
    header_size = msg[0] & 0x0f
    msg_type = msg[1] >> 4
    flags = msg[1] & 0x0f
    serial = msg[2] >> 4
    compress = msg[2] & 0x0f

    payload = msg[header_size * 4:]
    result: Dict[str, Any] = {"code": 0, "is_last": False, "data": None}

    if flags & 0x01:
        result["seq"] = struct.unpack('>i', payload[:4])[0]
        payload = payload[4:]
    if flags & 0x02:
        result["is_last"] = True

    if msg_type == _Proto.FULL_RESPONSE:
        size = struct.unpack('>I', payload[:4])[0]
        payload = payload[4:]
    elif msg_type == _Proto.ERROR_RESPONSE:
        result["code"] = struct.unpack('>i', payload[:4])[0]
        size = struct.unpack('>I', payload[4:8])[0]
        payload = payload[8:]

    if not payload:
        return result

    if compress == _Proto.GZIP:
        try:
            payload = _ungz(payload)
        except Exception as e:
            logger.error(f"decompress error: {e}")
            return result

    if serial == _Proto.JSON:
        try:
            result["data"] = json.loads(payload.decode('utf-8'))
        except Exception as e:
            logger.error(f"json parse error: {e}")

    return result


# ─── 异步 ASR 引擎 ─────────────────────────────────────────────────────────────
class RealtimeAsrEngine:
    """
    在独立线程中运行 asyncio 事件循环。
    通过 audio_queue 接收 PCM 数据，通过 result_callback 回调识别结果。
    """

    def __init__(self, cfg: Dict[str, str], result_callback):
        self.cfg = cfg
        self.result_callback = result_callback
        self._audio_queue: Optional[asyncio.Queue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    def start(self):
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._stop_event.set()
        # 不强杀事件循环；_send_audio 检测到 _running=False 后会发结束包，
        # _recv_results 收到 is_last 后自然退出，事件循环随 _session 完成而结束。

    def push_audio(self, pcm: bytes):
        """从主线程（pyaudio 回调）安全地推送音频数据。"""
        self._enqueue(pcm)

    def send_eof(self):
        """通知引擎音频结束。"""
        self._enqueue(None)

    def _enqueue(self, item: Optional[bytes]):
        loop = self._loop
        if loop is None or self._audio_queue is None or not self._running or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._audio_queue.put_nowait, item)
        except RuntimeError:
            pass  # 会话已异常结束、事件循环已关闭

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        except Exception as e:
            logger.error(f"ASR engine error: {e}")
        finally:
            self._loop.close()

    async def _session(self):
        self._audio_queue = asyncio.Queue()
        headers = {
            "X-Api-Resource-Id": self.cfg["resource_id"],
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Access-Key": self.cfg["access_key"],
            "X-Api-App-Key": self.cfg["app_key"],
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(WS_URL, headers=headers) as ws:
                    # 发送初始配置包
                    await ws.send_bytes(build_full_request(1, self.cfg))
                    init_msg = await ws.receive()
                    if init_msg.type != aiohttp.WSMsgType.BINARY:
                        logger.error(f"Unexpected init response type: {init_msg.type}")
                        return
                    init_resp = parse_response(init_msg.data)
                    if init_resp["code"] != 0:
                        logger.error(f"Init failed, code={init_resp['code']}")
                        self.result_callback({"error": f"连接失败，错误码 {init_resp['code']}"})
                        return

                    # 并发发送音频 + 接收结果
                    send_task = asyncio.create_task(self._send_audio(ws))
                    recv_task = asyncio.create_task(self._recv_results(ws))
                    await asyncio.gather(send_task, recv_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"WebSocket session error: {e}")
            self.result_callback({"error": str(e)})

    async def _send_audio(self, ws):
        seq = 2  # seq=1 已被 full_request 使用
        while True:
            try:
                pcm = await asyncio.wait_for(self._audio_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if not self._running:
                    # 发送结束包
                    await ws.send_bytes(build_audio_request(seq, b'', is_last=True))
                    break
                continue

            if pcm is None:
                # EOF 信号
                await ws.send_bytes(build_audio_request(seq, b'', is_last=True))
                break

            await ws.send_bytes(build_audio_request(seq, pcm, is_last=False))
            seq += 1

    async def _recv_results(self, ws):
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                resp = parse_response(msg.data)
                if resp["data"]:
                    self.result_callback(resp)
                if resp["is_last"] or resp["code"] != 0:
                    break
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                break


# ─── 音频采集 ──────────────────────────────────────────────────────────────────
def _find_loopback_device(pa):
    """查找 WASAPI loopback 设备。"""
    try:
        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError:
        return None
    for i in range(pa.get_device_count()):
        dev = pa.get_device_info_by_index(i)
        if dev.get("hostApi") == wasapi_info["index"] and dev.get("isLoopbackDevice", False):
            return dev
    return None


def _resample_to_mono16k(data: bytes, native_channels: int, native_rate: int) -> bytes:
    """将多声道/高采样率 PCM 转换为 16kHz 单声道。"""
    sample_count = len(data) // (2 * native_channels)
    samples = struct_mod.unpack(f'<{sample_count * native_channels}h', data)
    # 取第一声道
    mono = samples[::native_channels] if native_channels > 1 else samples
    # 降采样
    if native_rate != SAMPLE_RATE:
        ratio = native_rate / SAMPLE_RATE
        resampled = [mono[int(i * ratio)] for i in range(int(len(mono) / ratio))]
    else:
        resampled = list(mono)
    return struct_mod.pack(f'<{len(resampled)}h', *resampled)


def _mix_pcm(pcm1: bytes, pcm2: bytes) -> bytes:
    """混合两路 16-bit PCM，硬裁剪。"""
    n1 = len(pcm1) // 2
    n2 = len(pcm2) // 2
    n = min(n1, n2)
    s1 = struct_mod.unpack(f'<{n}h', pcm1[:n * 2])
    s2 = struct_mod.unpack(f'<{n}h', pcm2[:n * 2])
    mixed = [max(-32768, min(32767, a + b)) for a, b in zip(s1, s2)]
    return struct_mod.pack(f'<{len(mixed)}h', *mixed)


class AudioCapture:
    """支持麦克风、系统音频（loopback）、或两者同时采集。"""

    def __init__(self, on_chunk, mode: str = "mic"):
        self._on_chunk = on_chunk
        self._mode = mode  # "mic", "loopback", "both"
        self._pa = None
        self._mic_stream = None
        self._loopback_stream = None
        self._loopback_dev = None
        # both 模式下的缓冲区
        self._mic_buf = b''
        self._loopback_buf = b''
        self._buf_lock = threading.Lock()
        self._mix_timer: Optional[threading.Timer] = None
        self._running = False

    def start(self):
        if pyaudio is None:
            raise RuntimeError("pyaudiowpatch 未安装，请运行: pip install pyaudiowpatch")
        self._pa = pyaudio.PyAudio()
        self._running = True

        if self._mode in ("mic", "both"):
            self._mic_stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE // SAMPLE_WIDTH,
                stream_callback=self._mic_callback if self._mode == "both" else self._direct_callback,
            )
            self._mic_stream.start_stream()

        if self._mode in ("loopback", "both"):
            self._loopback_dev = _find_loopback_device(self._pa)
            if not self._loopback_dev:
                raise RuntimeError("未找到 WASAPI loopback 设备，请检查音频驱动")
            dev_rate = int(self._loopback_dev["defaultSampleRate"])
            dev_channels = max(1, int(self._loopback_dev["maxInputChannels"]))
            self._lb_native_rate = dev_rate
            self._lb_native_channels = dev_channels
            # 计算 loopback 每包帧数，使其对应 CHUNK_MS 时长
            lb_frames = dev_rate * CHUNK_MS // 1000
            self._loopback_stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=dev_channels,
                rate=dev_rate,
                input=True,
                input_device_index=int(self._loopback_dev["index"]),
                frames_per_buffer=lb_frames,
                stream_callback=self._loopback_callback if self._mode == "both" else self._direct_loopback_callback,
            )
            self._loopback_stream.start_stream()

        if self._mode == "both":
            self._start_mix_timer()

    def stop(self):
        self._running = False
        if self._mix_timer:
            self._mix_timer.cancel()
            self._mix_timer = None
        for stream in (self._mic_stream, self._loopback_stream):
            if stream:
                stream.stop_stream()
                stream.close()
        self._mic_stream = None
        self._loopback_stream = None
        if self._pa:
            self._pa.terminate()
            self._pa = None

    # ── 单源模式回调 ─────────────────────────────────────────────────────────
    def _direct_callback(self, in_data, frame_count, time_info, status):
        """麦克风单源模式：直接推送。"""
        self._on_chunk(in_data)
        return (None, pyaudio.paContinue)

    def _direct_loopback_callback(self, in_data, frame_count, time_info, status):
        """系统音频单源模式：重采样后推送。"""
        pcm = _resample_to_mono16k(in_data, self._lb_native_channels, self._lb_native_rate)
        if pcm:
            self._on_chunk(pcm)
        return (None, pyaudio.paContinue)

    # ── 混合模式回调 + 定时混合 ──────────────────────────────────────────────
    def _mic_callback(self, in_data, frame_count, time_info, status):
        with self._buf_lock:
            self._mic_buf += in_data
        return (None, pyaudio.paContinue)

    def _loopback_callback(self, in_data, frame_count, time_info, status):
        pcm = _resample_to_mono16k(in_data, self._lb_native_channels, self._lb_native_rate)
        with self._buf_lock:
            self._loopback_buf += pcm
        return (None, pyaudio.paContinue)

    def _start_mix_timer(self):
        if not self._running:
            return
        self._flush_mix()
        self._mix_timer = threading.Timer(CHUNK_MS / 1000.0, self._start_mix_timer)
        self._mix_timer.daemon = True
        self._mix_timer.start()

    def _flush_mix(self):
        with self._buf_lock:
            mic_data = self._mic_buf[:CHUNK_SIZE]
            lb_data = self._loopback_buf[:CHUNK_SIZE]
            self._mic_buf = self._mic_buf[CHUNK_SIZE:]
            self._loopback_buf = self._loopback_buf[CHUNK_SIZE:]

        if mic_data and lb_data:
            self._on_chunk(_mix_pcm(mic_data, lb_data))
        elif mic_data:
            self._on_chunk(mic_data)
        elif lb_data:
            self._on_chunk(lb_data)


# ─── 识别结果 → 分句 ────────────────────────────────────────────────────────────
@dataclass
class Segment:
    """界面上的一条已确定分句，以及它的译文状态。"""
    id: int
    source: str
    bilingual: bool                  # True：双语排版（原文、译文各一行，句间空一行）；False：原来的连续排版
    status: str = "none"             # none 不翻译 | skipped 无需翻译 | pending 翻译中 | done 完成 | error 失败
    translation: str = ""
    error: str = ""
    break_after: bool = False        # 连续排版下本次录音在这句之后结束，保存时另起一段

    @property
    def settled(self) -> bool:
        return self.status != "pending"


@dataclass
class AsrSessionState:
    """一次录音会话的上屏进度。

    result_type="full" 时每个响应都带着会话内的全部分句，只需上屏 rendered 之后新增的 definite 分句。
    该计数不随清空 / 自动记录归零，避免旧句子重新出现、被重复翻译。
    """
    rendered: int = 0
    last_seg_id: Optional[int] = None


def queue_callback(ui_queue: queue.Queue, kind: str, *prefix):
    """后台线程的回调：把结果放进 UI 队列。

    闭包只引用队列、不持有 App，避免 Tk 对象的最后一个引用落在后台线程里、在错误的线程被回收。
    """
    return lambda *args: ui_queue.put((kind,) + prefix + args)


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


def format_record(segments: List[Segment]) -> str:
    """整理成写入 RecordMemory.md 的文本：连续排版的句子拼成段落，译文用引用块标出。"""
    blocks: List[str] = []
    paragraph = ""
    for seg in segments:
        if not seg.bilingual:
            paragraph += seg.source
            if seg.break_after:
                blocks.append(paragraph)
                paragraph = ""
            continue
        if paragraph:
            blocks.append(paragraph)
            paragraph = ""
        lines = [seg.source]
        if seg.status == "done":
            lines += ["> " + line for line in seg.translation.splitlines() if line.strip()]
        elif seg.status == "error":
            lines.append(f"> [翻译失败] {seg.error}")
        elif seg.status == "pending":
            lines.append(f"> {seg.translation}…（翻译未完成）" if seg.translation else "> （翻译未完成）")
        blocks.append("\n".join(lines))
    if paragraph:
        blocks.append(paragraph)
    return "\n\n".join(b.strip() for b in blocks if b.strip())


# ─── GUI ───────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("实时语音转文字 + 翻译")
        self.geometry("760x560")
        self.resizable(True, True)

        self._cfg = load_config()
        self._engine: Optional[RealtimeAsrEngine] = None
        self._capture: Optional[AudioCapture] = None
        self._recording = False
        self._mode = "mic"
        self._ui_queue: queue.Queue = queue.Queue()
        self._use_mic = tk.BooleanVar(value=True)
        self._use_loopback = tk.BooleanVar(value=False)
        self._auto_save = tk.BooleanVar(value=False)
        self._save_interval_var = tk.StringVar(value="30")
        self._save_timer_id: Optional[str] = None
        self._poll_id: Optional[str] = None

        # 识别会话与界面上的分句（按上屏顺序）
        self._session_ids = itertools.count(1)
        self._current_session = 0
        self._sessions: Dict[int, AsrSessionState] = {}
        self._segment_ids = itertools.count(1)
        self._segments: Dict[int, Segment] = {}

        # 实时翻译
        self._tr_cfg = TranslatorConfig.from_env()
        self._translator = TranslationEngine(self._tr_cfg, queue_callback(self._ui_queue, "tr"))
        self._translator.start()
        self._translate_var = tk.BooleanVar(value=self._tr_cfg.enabled_by_default())
        self._model_var = tk.StringVar(value=self._tr_cfg.model)
        self._settings_dialog: Optional["TranslationSettingsDialog"] = None

        self._build_ui()
        self._check_config()
        self._poll_ui()

    def _build_ui(self):
        # 状态栏
        self._status_var = tk.StringVar(value="就绪")
        status_bar = tk.Label(self, textvariable=self._status_var,
                              anchor="w", relief=tk.SUNKEN, padx=6)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # 按钮区
        btn_frame = tk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=6)

        self._btn_start = tk.Button(btn_frame, text="开始录音", width=12,
                                    bg="#4CAF50", fg="white",
                                    command=self._on_start)
        self._btn_start.pack(side=tk.LEFT, padx=4)

        self._btn_stop = tk.Button(btn_frame, text="停止录音", width=12,
                                   bg="#f44336", fg="white",
                                   state=tk.DISABLED,
                                   command=self._on_stop)
        self._btn_stop.pack(side=tk.LEFT, padx=4)

        tk.Button(btn_frame, text="清空文字", width=10,
                  command=self._on_clear).pack(side=tk.LEFT, padx=4)

        tk.Checkbutton(btn_frame, text="自动记录",
                       variable=self._auto_save,
                       command=self._on_auto_save_toggle).pack(side=tk.LEFT, padx=4)

        tk.Label(btn_frame, text="间隔(秒):").pack(side=tk.LEFT, padx=(0, 2))
        interval_entry = tk.Entry(btn_frame, textvariable=self._save_interval_var, width=5)
        interval_entry.pack(side=tk.LEFT, padx=(0, 4))

        # 翻译区（录音期间也可开关、切换模型，对之后的句子生效）
        tr_frame = tk.Frame(self)
        tr_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 2))
        tk.Label(tr_frame, text="翻译:").pack(side=tk.LEFT, padx=(0, 4))
        tk.Checkbutton(tr_frame, text=f"实时翻译为{self._tr_cfg.target_lang}",
                       variable=self._translate_var,
                       command=self._on_translate_toggle).pack(side=tk.LEFT, padx=4)
        tk.Label(tr_frame, text="模型:").pack(side=tk.LEFT, padx=(12, 2))
        tk.Label(tr_frame, textvariable=self._model_var, fg="#1565C0").pack(side=tk.LEFT)
        tk.Button(tr_frame, text="翻译设置…",
                  command=self._open_translation_settings).pack(side=tk.LEFT, padx=(12, 4))

        # 音频源选择区
        src_frame = tk.Frame(self)
        src_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 2))
        tk.Label(src_frame, text="音频源:").pack(side=tk.LEFT, padx=(0, 4))
        self._chk_mic = tk.Checkbutton(src_frame, text="麦克风",
                                       variable=self._use_mic)
        self._chk_mic.pack(side=tk.LEFT, padx=4)
        self._chk_loopback = tk.Checkbutton(src_frame, text="系统音频",
                                            variable=self._use_loopback)
        self._chk_loopback.pack(side=tk.LEFT, padx=4)

        # 文字显示区
        self._text = scrolledtext.ScrolledText(self, wrap=tk.WORD,
                                               font=("Microsoft YaHei", 14),
                                               state=tk.DISABLED)
        self._text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))
        self._text.tag_config("interim", foreground="#999999")
        self._text.tag_config("final", foreground="#111111")
        self._text.tag_config("error", foreground="#cc0000")
        self._text.tag_config("translation", foreground="#1565C0", lmargin1=16, lmargin2=16)
        self._text.tag_config("tr_pending", foreground="#9E9E9E", lmargin1=16, lmargin2=16,
                              font=("Microsoft YaHei", 11))
        self._text.tag_config("tr_error", foreground="#cc0000", lmargin1=16, lmargin2=16,
                              font=("Microsoft YaHei", 11))
        # 用 mark 标记临时文字的起始位置
        self._text.mark_set("interim_start", "end-1c")
        self._text.mark_gravity("interim_start", "left")

    def _check_config(self):
        if not self._cfg["app_key"] or not self._cfg["access_key"]:
            messagebox.showwarning(
                "缺少 API 密钥",
                "未找到 VOLCENGINE_APP_KEY / VOLCENGINE_ACCESS_KEY。\n"
                "请创建 .env 文件（参考 .env.example）并重启程序。"
            )

    # ── 按钮回调 ──────────────────────────────────────────────────────────────
    def _on_start(self):
        if self._recording:
            return
        # 确定音频源模式
        use_mic = self._use_mic.get()
        use_lb = self._use_loopback.get()
        if not use_mic and not use_lb:
            messagebox.showwarning("提示", "请至少选择一个音频源")
            return
        if use_mic and use_lb:
            mode = "both"
        elif use_lb:
            mode = "loopback"
        else:
            mode = "mic"

        self._recording = True
        self._mode = mode
        self._btn_start.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        self._chk_mic.config(state=tk.DISABLED)
        self._chk_loopback.config(state=tk.DISABLED)
        self._set_status("连接中…")

        # 每次录音分配一个会话编号：停止后迟到的最终结果也能按所属会话正确上屏
        sid = next(self._session_ids)
        self._current_session = sid
        self._sessions[sid] = AsrSessionState()
        self._engine = RealtimeAsrEngine(self._cfg, queue_callback(self._ui_queue, "asr", sid))
        self._engine.start()

        try:
            self._capture = AudioCapture(self._engine.push_audio, mode=mode)
            self._capture.start()
            self._set_status(self._recording_status())
        except Exception as e:
            if self._capture:
                try:
                    self._capture.stop()   # 释放已经打开的音频流
                except Exception as stop_err:
                    logger.error(f"Audio capture cleanup failed: {stop_err}")
                self._capture = None
            self._engine.stop()
            self._engine = None
            self._append_text(f"[错误] {e}\n", "error")
            self._reset_state()
            self._set_status("启动失败")

    def _on_stop(self, status: str = "已停止"):
        if not self._recording:
            return
        self._set_status("停止中…")
        if self._capture:
            self._capture.stop()
            self._capture = None
        if self._engine:
            self._engine.send_eof()
            self._engine.stop()
            self._engine = None
        self._reset_state()
        self._set_status(status)

    def _on_clear(self):
        # 取消未完成的翻译并清空上文；各会话的上屏计数保持不变，清空后旧句子不会重新出现
        self._translator.cancel_all()
        self._translator.clear_context()
        self._text.config(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        for seg in list(self._segments.values()):
            self._forget_segment(seg)
        self._text.mark_set("interim_start", "end-1c")
        self._text.config(state=tk.DISABLED)

    # ── 实时翻译 ─────────────────────────────────────────────────────────────
    def _on_translate_toggle(self):
        if self._translate_var.get():
            reason = self._tr_cfg.missing_reason()
            if reason:
                self._translate_var.set(False)
                if messagebox.askyesno("翻译未配置", f"{reason}。\n\n是否现在打开翻译设置？", parent=self):
                    self._open_translation_settings(enable_after=True)
                return
        if self._recording:
            self._set_status(self._recording_status())
        elif self._translate_var.get():
            self._set_status(f"实时翻译已开启（{self._tr_cfg.model} → {self._tr_cfg.target_lang}）")
        else:
            self._set_status("实时翻译已关闭")

    def _open_translation_settings(self, enable_after: bool = False):
        dialog = self._settings_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus_set()
            return

        def on_save(cfg: TranslatorConfig):
            self._apply_translation_config(cfg)
            if enable_after and cfg.is_ready():
                self._translate_var.set(True)
                self._on_translate_toggle()

        self._settings_dialog = TranslationSettingsDialog(self, self._tr_cfg, self._translator, on_save)

    def _apply_translation_config(self, cfg: TranslatorConfig):
        """应用新的翻译配置（对之后的句子立即生效），并写入 .env。"""
        self._tr_cfg = cfg
        self._translator.set_config(cfg)
        self._model_var.set(cfg.model)
        if self._translate_var.get() and not cfg.is_ready():
            self._translate_var.set(False)
        values = {
            "TRANSLATE_BASE_URL": cfg.base_url,
            "TRANSLATE_API_KEY": cfg.api_key,
            "TRANSLATE_MODEL": cfg.model,
            "TRANSLATE_EXTRA_BODY": format_extra_body(cfg.extra_body),
        }
        try:
            save_env_values(ENV_PATH, values)
        except OSError as e:
            logger.error(f"Save translation settings failed: {e}")
            messagebox.showwarning("保存失败", f"新设置已在本次运行中生效，但写入 .env 失败：\n{e}", parent=self)
            return
        self._set_status(self._recording_status() if self._recording
                         else f"翻译设置已保存（模型：{cfg.model}）")

    # ── 自动记录 ─────────────────────────────────────────────────────────────
    def _get_save_interval(self) -> int:
        try:
            val = int(self._save_interval_var.get())
            return max(val, 5)  # 最小 5 秒
        except ValueError:
            return 30

    def _on_auto_save_toggle(self):
        if self._auto_save.get():
            interval = self._get_save_interval()
            self._schedule_auto_save()
            self._set_status(f"自动记录已开启（每 {interval}s）")
        else:
            if self._save_timer_id:
                self.after_cancel(self._save_timer_id)
                self._save_timer_id = None
            self._set_status("自动记录已关闭")

    def _schedule_auto_save(self):
        self._do_auto_save()
        if self._auto_save.get():
            interval = self._get_save_interval()
            self._save_timer_id = self.after(interval * 1000, self._schedule_auto_save)

    def _do_auto_save(self, force: bool = False):
        """把已确定的分句（开启翻译时连同译文）追加写入 RecordMemory.md，并从界面移除。

        只保存到第一条仍在翻译中的分句之前，剩下的等译文完成后下次再存；
        force=True（关闭窗口时）则全部保存。灰色临时文字不保存。
        """
        ready: List[Segment] = []
        for seg in self._segments.values():
            if not (seg.settled or force):
                break
            ready.append(seg)
        if not ready:
            return

        content = format_record(ready)
        if content:
            record_path = os.path.join(get_application_path(), "RecordMemory.md")
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                with open(record_path, "a", encoding="utf-8") as f:
                    f.write(f"\n## {timestamp}\n\n{content}\n")
                logger.info(f"Auto-saved to {record_path}")
            except Exception as e:
                logger.error(f"Auto-save failed: {e}")
                return

        # 写入成功后从界面移除这些分句，防止长时间转录内存积累；
        # 仍在翻译中的分句及之后的内容、临时文字（灰色）保留
        if len(ready) == len(self._segments):
            boundary = "interim_start"
        else:
            boundary = f"seg_{list(self._segments)[len(ready)]}"
        self._text.config(state=tk.NORMAL)
        self._text.delete("1.0", boundary)
        for seg in ready:
            self._forget_segment(seg)
        self._text.config(state=tk.DISABLED)

    # ── UI 轮询（主线程）──────────────────────────────────────────────────────
    def _poll_ui(self):
        try:
            events = []
            while True:
                try:
                    events.append(self._ui_queue.get_nowait())
                except queue.Empty:
                    break
            if events:
                self._render_events(events)
        finally:
            self._poll_id = self.after(50, self._poll_ui)

    def _render_events(self, events: List[tuple]):
        follow = self._text.yview()[1] >= 0.999  # 用户往上翻看时不强制滚动到底部
        self._text.config(state=tk.NORMAL)
        try:
            for event in events:
                try:
                    if event[0] == "asr":
                        self._handle_asr(event[1], event[2])
                    else:
                        self._handle_translation(*event[1:])
                except Exception:
                    logger.exception(f"Render {event[0]} event failed")
        finally:
            self._text.config(state=tk.DISABLED)
        if follow:
            self._text.see(tk.END)

    def _handle_asr(self, sid: int, resp: Dict[str, Any]):
        session = self._sessions.get(sid)
        if session is None:
            return
        is_current = sid == self._current_session

        if "error" in resp:
            self._insert_before_interim(f"[错误] {resp['error']}\n", "error")
            del self._sessions[sid]
            if is_current and self._recording:
                self._on_stop(status="连接已断开")
            return

        definite, interim = split_utterances(resp.get("data") or {})
        for text in definite[session.rendered:]:
            if text:
                session.last_seg_id = self._add_segment(text).id
        session.rendered = max(session.rendered, len(definite))

        if resp.get("is_last"):
            if is_current:
                self._set_interim("")
            self._end_paragraph(session)
            del self._sessions[sid]
        elif is_current:
            self._set_interim(interim)

    def _end_paragraph(self, session: AsrSessionState):
        """连续排版下录音结束时换行（与原来的行为一致）；双语排版每句本来就独占一段。"""
        seg = self._segments.get(session.last_seg_id) if session.last_seg_id is not None else None
        if seg is None or seg.bilingual or seg.break_after:
            return
        if next(reversed(self._segments)) != seg.id:
            return  # 之后已经有新会话的内容
        self._insert_before_interim("\n", "final")
        seg.break_after = True

    def _add_segment(self, text: str) -> Segment:
        """在临时文字之前追加一条已确定分句；开启翻译时按双语排版并提交翻译。"""
        seg = Segment(id=next(self._segment_ids), source=text, bilingual=self._translate_var.get())
        if seg.bilingual and self._segments:
            prev = self._segments[next(reversed(self._segments))]
            if not prev.bilingual and not prev.break_after:
                self._insert_before_interim("\n", "final")  # 从连续排版切换到双语排版，先换行

        start = self._text.index("interim_start")
        if not seg.bilingual:
            self._insert_before_interim(text, "final")
        else:
            self._insert_before_interim(text + "\n", "final")
            if needs_translation(text, self._tr_cfg.target_lang, self._tr_cfg.skip_chinese):
                seg.status = "pending"
                tr_start = self._text.index("interim_start")
                self._insert_before_interim("翻译中…", "tr_pending")
                tr_end = self._text.index("interim_start")
                self._insert_before_interim("\n\n", "final")
                # 译文区间 [trs, tre)：流式译文到达时整体替换
                self._set_mark(f"trs_{seg.id}", tr_start, "left")
                self._set_mark(f"tre_{seg.id}", tr_end, "right")
            else:
                seg.status = "skipped"  # 原文已是中文或没有可翻译的文字
                self._insert_before_interim("\n", "final")
        self._set_mark(f"seg_{seg.id}", start, "left")
        self._segments[seg.id] = seg

        if seg.status == "pending":
            self._translator.submit(seg.id, text)
        elif seg.bilingual:
            self._translator.add_context(text)
        return seg

    def _handle_translation(self, seg_id: int, text: str, done: bool, error: Optional[str]):
        seg = self._segments.get(seg_id)
        if seg is None or seg.status != "pending":
            return  # 已被清空或已保存
        if error:
            seg.status, seg.error = "error", error
            self._replace_translation(seg, f"[翻译失败] {error}", "tr_error")
        else:
            seg.translation = text
            self._replace_translation(seg, text, "translation")
            if done:
                seg.status = "done"
        if seg.settled:
            self._text.mark_unset(f"trs_{seg.id}", f"tre_{seg.id}")

    # ── 工具方法 ──────────────────────────────────────────────────────────────
    def _insert_before_interim(self, text: str, tag: str):
        """在灰色临时文字之前插入内容（临时 mark 改为右黏附，插入后 mark 仍位于新内容之后）。"""
        self._text.mark_gravity("interim_start", "right")
        try:
            self._text.insert("interim_start", text, tag)
        finally:
            self._text.mark_gravity("interim_start", "left")

    def _set_interim(self, text: str):
        # 只删到 end-1c：删除区间若从行首一直到 end，Tk 会连带删掉前面的换行符
        self._text.delete("interim_start", "end-1c")
        if text:
            self._text.insert("end-1c", text, "interim")

    def _replace_translation(self, seg: Segment, text: str, tag: str):
        start, end = f"trs_{seg.id}", f"tre_{seg.id}"
        self._text.delete(start, end)
        self._text.insert(start, text, tag)

    def _set_mark(self, name: str, index: str, gravity: str):
        self._text.mark_set(name, index)
        self._text.mark_gravity(name, gravity)

    def _forget_segment(self, seg: Segment):
        self._text.mark_unset(f"seg_{seg.id}", f"trs_{seg.id}", f"tre_{seg.id}")
        self._segments.pop(seg.id, None)

    def _append_text(self, text: str, tag: str = "final"):
        self._text.config(state=tk.NORMAL)
        self._insert_before_interim(text, tag)
        self._text.see(tk.END)
        self._text.config(state=tk.DISABLED)

    def _set_status(self, msg: str):
        self._status_var.set(msg)

    def _recording_status(self) -> str:
        labels = {"mic": "麦克风", "loopback": "系统音频", "both": "麦克风+系统音频"}
        status = f"录音中… [{labels[self._mode]}]"
        if self._translate_var.get():
            status += f"  ·  实时翻译：{self._tr_cfg.model}"
        return status

    def _reset_state(self):
        self._recording = False
        self._btn_start.config(state=tk.NORMAL)
        self._btn_stop.config(state=tk.DISABLED)
        self._chk_mic.config(state=tk.NORMAL)
        self._chk_loopback.config(state=tk.NORMAL)

    def on_closing(self):
        if self._save_timer_id:
            self.after_cancel(self._save_timer_id)
            self._save_timer_id = None
        if self._auto_save.get():
            self._do_auto_save(force=True)
        if self._recording:
            self._on_stop()
        self._translator.stop()
        self.destroy()

    def destroy(self):
        for timer_id in (self._poll_id, self._save_timer_id):
            if timer_id:
                self.after_cancel(timer_id)
        self._poll_id = self._save_timer_id = None
        super().destroy()


class TranslationSettingsDialog(tk.Toplevel):
    """翻译设置：服务商预设 / 接口地址 / API Key / 模型 / 附加参数，可先测试再保存到 .env。

    不使用 tk.StringVar 等变量对象：对话框关闭后可能在后台线程被垃圾回收，
    Variable.__del__ 会在错误的线程里调用 Tcl。
    """

    CUSTOM = "自定义"

    def __init__(self, master: tk.Tk, cfg: TranslatorConfig, engine: TranslationEngine, on_save):
        super().__init__(master)
        self._cfg = cfg
        self._engine = engine
        self._on_save = on_save
        self._test_future = None
        self._poll_id: Optional[str] = None
        self._grab_id: Optional[str] = None
        self._grab_retries = 10
        self._key_hidden = True

        self.title("翻译设置")
        self.transient(master)
        self.resizable(False, False)
        self.geometry(f"+{master.winfo_rootx() + 60}+{master.winfo_rooty() + 60}")

        body = tk.Frame(self, padx=14, pady=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)

        def add_row(row: int, label: str, widget: tk.Widget, span: int = 2):
            tk.Label(body, text=label).grid(row=row, column=0, sticky="e", padx=(0, 8), pady=4)
            widget.grid(row=row, column=1, columnspan=span, sticky="we", pady=4)
            return widget

        preset = find_preset(cfg.base_url)
        self._provider_box = add_row(0, "服务商:", ttk.Combobox(
            body, state="readonly", width=46, values=[p.name for p in PROVIDER_PRESETS] + [self.CUSTOM]))
        self._provider_box.set(preset.name if preset else self.CUSTOM)
        self._provider_box.bind("<<ComboboxSelected>>", self._on_provider_selected)

        self._base_url_entry = add_row(1, "接口地址:", tk.Entry(body, width=50))
        self._key_entry = add_row(2, "API Key:", tk.Entry(body, show="•", width=42), span=1)
        self._btn_show_key = tk.Button(body, text="显示", width=5, command=self._on_toggle_key)
        self._btn_show_key.grid(row=2, column=2, sticky="w", padx=(6, 0))
        self._model_entry = add_row(3, "模型:", tk.Entry(body, width=50))
        self._extra_entry = add_row(4, "附加参数:", tk.Entry(body, width=50))
        for entry, value in ((self._base_url_entry, cfg.base_url), (self._key_entry, cfg.api_key),
                             (self._model_entry, cfg.model), (self._extra_entry, format_extra_body(cfg.extra_body))):
            entry.insert(0, value)

        tk.Label(body, fg="#666666", justify=tk.LEFT, wraplength=440, text=(
            "附加参数为 JSON，会合并进请求体。DeepSeek 默认用 "
            '{"thinking": {"type": "disabled"}} 关闭思考模式以降低延迟；其他服务商一般填 {}。\n'
            f"保存后立即生效，并写入 {ENV_PATH}")).grid(row=5, column=1, columnspan=2, sticky="w")

        test_row = tk.Frame(body)
        test_row.grid(row=6, column=0, columnspan=3, sticky="we", pady=(12, 0))
        self._btn_test = tk.Button(test_row, text="测试翻译", width=10, command=self._on_test)
        self._btn_test.pack(side=tk.LEFT, anchor="n")
        self._msg_label = tk.Label(test_row, anchor="w", justify=tk.LEFT, wraplength=420)
        self._msg_label.pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)

        btn_row = tk.Frame(body)
        btn_row.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))
        tk.Button(btn_row, text="保存", width=10, command=self._on_save_click).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_row, text="取消", width=10, command=self.destroy).pack(side=tk.LEFT, padx=4)

        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._model_entry.focus_set()
        self._grab_id = self.after_idle(self._grab_modal)

    def _grab_modal(self):
        """模态显示；窗口还没映射到屏幕时 grab 会失败，稍后重试。"""
        self._grab_id = None
        try:
            self.grab_set()
        except tk.TclError:
            self._grab_retries -= 1
            if self._grab_retries > 0:
                self._grab_id = self.after(100, self._grab_modal)

    @property
    def message(self) -> str:
        return self._msg_label.cget("text")

    def _show_message(self, text: str, color: str = "#666666"):
        self._msg_label.config(text=text, fg=color)

    @staticmethod
    def _set_entry(entry: tk.Entry, value: str):
        entry.delete(0, tk.END)
        entry.insert(0, value)

    def _on_toggle_key(self):
        self._key_hidden = not self._key_hidden
        self._key_entry.config(show="•" if self._key_hidden else "")
        self._btn_show_key.config(text="显示" if self._key_hidden else "隐藏")

    def _on_provider_selected(self, _event=None):
        preset = next((p for p in PROVIDER_PRESETS if p.name == self._provider_box.get()), None)
        if preset is None:
            return  # 自定义：保持当前填写的内容
        self._set_entry(self._base_url_entry, preset.base_url)
        self._set_entry(self._model_entry, preset.model)
        self._set_entry(self._extra_entry, format_extra_body(preset.extra_body))
        if preset.model:
            self._show_message(f"已填入 {preset.name} 的接口地址和默认模型，请确认 API Key 属于该服务商。")
        else:
            self._show_message(f"已填入 {preset.name} 的接口地址，请填写模型名称，并确认 API Key 属于该服务商。")
            self._model_entry.focus_set()

    def _collect(self) -> TranslatorConfig:
        model = self._model_entry.get().strip()
        if not model:
            raise ValueError("请填写模型名称")
        return replace(
            self._cfg,
            base_url=check_base_url(self._base_url_entry.get()),
            api_key=self._key_entry.get().strip(),
            model=model,
            extra_body=parse_extra_body(self._extra_entry.get()),
        )

    def _on_test(self):
        try:
            cfg = self._collect()
            reason = cfg.missing_reason()
            if reason:
                raise ValueError(reason)
            self._test_future = self._engine.test(cfg)
        except (ValueError, RuntimeError) as e:
            self._show_message(str(e), "#cc0000")
            return
        self._btn_test.config(state=tk.DISABLED)
        self._show_message(f"正在请求 {cfg.model} …")
        self._poll_id = self.after(100, self._poll_test)

    def _poll_test(self):
        future = self._test_future
        if future is None:
            return
        if not future.done():
            self._poll_id = self.after(100, self._poll_test)
            return
        self._test_future = None
        self._poll_id = None
        self._btn_test.config(state=tk.NORMAL)
        try:
            translation, elapsed = future.result()
        except Exception as e:
            self._show_message(f"✗ 失败：{e}", "#cc0000")
        else:
            self._show_message(f"✓ 成功，用时 {elapsed:.1f} 秒\n{TEST_TEXT}\n→ {translation}", "#2E7D32")

    def _on_save_click(self):
        try:
            cfg = self._collect()
        except ValueError as e:
            self._show_message(str(e), "#cc0000")
            return
        self._on_save(cfg)
        self.destroy()

    def destroy(self):
        for timer_id in (self._poll_id, self._grab_id):
            if timer_id:
                self.after_cancel(timer_id)
        self._poll_id = self._grab_id = None
        if self._test_future is not None:
            self._test_future.cancel()
            self._test_future = None
        super().destroy()


# ─── 入口 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
