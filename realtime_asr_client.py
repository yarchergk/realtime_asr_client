"""
实时语音转文字客户端
调用火山引擎大模型流式语音识别 API，实时采集麦克风音频并显示识别结果。

依赖安装：
    pip install pyaudio aiohttp python-dotenv

配置：
    复制 .env.example 为 .env，填入 API 密钥
"""

import asyncio
import aiohttp
import json
import struct
import gzip
import uuid
import logging
import os
import sys
import threading
import queue
import time
import struct as struct_mod
import tkinter as tk
from tkinter import scrolledtext, messagebox
from typing import Dict, Any, Optional

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    try:
        import pyaudio
    except ImportError:
        pyaudio = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv 未安装时直接读环境变量

# ─── 获取应用目录（兼容打包后的exe） ──────────────────────────────────────────
def get_application_path():
    """获取应用程序所在目录，兼容开发环境和打包后的exe"""
    if getattr(sys, 'frozen', False):
        # 打包后的exe环境：sys.executable 是 exe 文件路径
        return os.path.dirname(sys.executable)
    else:
        # 开发环境：__file__ 是脚本文件路径
        return os.path.dirname(os.path.abspath(__file__))

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
        if self._loop and self._audio_queue and self._running:
            self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, pcm)

    def send_eof(self):
        """通知引擎音频结束。"""
        if self._loop and self._audio_queue and self._running:
            self._loop.call_soon_threadsafe(self._audio_queue.put_nowait, None)

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


# ─── GUI ───────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("实时语音转文字")
        self.geometry("700x500")
        self.resizable(True, True)

        self._cfg = load_config()
        self._engine: Optional[RealtimeAsrEngine] = None
        self._capture: Optional[AudioCapture] = None
        self._recording = False
        self._confirmed_count = 0  # 已写入的 definite utterance 数量
        self._confirmed_offset = 0  # 清空 GUI 时跳过的历史 utterance 数量
        self._ui_queue: queue.Queue = queue.Queue()
        self._use_mic = tk.BooleanVar(value=True)
        self._use_loopback = tk.BooleanVar(value=False)
        self._auto_save = tk.BooleanVar(value=False)
        self._save_interval_var = tk.StringVar(value="30")
        self._save_timer_id: Optional[str] = None
        self._last_saved_pos = "1.0"  # 上次保存到的文本位置

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
        self._confirmed_count = 0
        self._confirmed_offset = 0
        self._btn_start.config(state=tk.DISABLED)
        self._btn_stop.config(state=tk.NORMAL)
        self._chk_mic.config(state=tk.DISABLED)
        self._chk_loopback.config(state=tk.DISABLED)
        self._set_status("连接中…")

        self._engine = RealtimeAsrEngine(self._cfg, self._on_result)
        self._engine.start()

        try:
            self._capture = AudioCapture(self._engine.push_audio, mode=mode)
            self._capture.start()
            labels = {"mic": "麦克风", "loopback": "系统音频", "both": "麦克风+系统音频"}
            self._set_status(f"录音中… [{labels[mode]}]")
        except Exception as e:
            self._append_text(f"[错误] {e}\n", "error")
            self._reset_state()

    def _on_stop(self):
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
        self._set_status("已停止")

    def _on_clear(self):
        self._text.config(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.mark_set("interim_start", "end-1c")
        self._text.config(state=tk.DISABLED)
        self._confirmed_count = 0
        self._confirmed_offset = 0
        self._last_saved_pos = "1.0"

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

    def _do_auto_save(self):
        # 只保存 interim_start 之前的已确定文字（不含灰色临时文字）
        content = self._text.get(self._last_saved_pos, "interim_start")
        if not content.strip():
            return
        record_path = os.path.join(get_application_path(), "RecordMemory.md")
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(record_path, "a", encoding="utf-8") as f:
                f.write(f"\n## {timestamp}\n\n{content.strip()}\n")
            logger.info(f"Auto-saved to {record_path}")
        except Exception as e:
            logger.error(f"Auto-save failed: {e}")
            return

        # 写入成功后清空 GUI 文字及后台缓存，防止长时间转录内存积累
        # 保留当前临时文字（灰色），清除所有已确定文字
        interim_text = self._text.get("interim_start", tk.END)
        self._text.config(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.mark_set("interim_start", "end-1c")
        self._text.mark_gravity("interim_start", "left")
        if interim_text.strip():
            self._text.insert(tk.END, interim_text.rstrip("\n"), "interim")
        self._text.config(state=tk.DISABLED)

        # 记录清空时已处理的 utterance 总数，让 _handle_result 跳过这些历史条目
        self._confirmed_offset += self._confirmed_count
        self._confirmed_count = 0
        self._last_saved_pos = "1.0"

        # 清空 UI 队列中积压的旧响应，避免重复渲染已保存内容
        while not self._ui_queue.empty():
            try:
                self._ui_queue.get_nowait()
            except queue.Empty:
                break

    # ── 结果回调（来自后台线程）────────────────────────────────────────────────
    def _on_result(self, resp: Dict[str, Any]):
        self._ui_queue.put(resp)

    # ── UI 轮询（主线程）──────────────────────────────────────────────────────
    def _poll_ui(self):
        try:
            while True:
                resp = self._ui_queue.get_nowait()
                self._handle_result(resp)
        except queue.Empty:
            pass
        self.after(50, self._poll_ui)

    def _handle_result(self, resp: Dict[str, Any]):
        if "error" in resp:
            self._append_text(f"[错误] {resp['error']}\n", "error")
            return

        data = resp.get("data", {})
        result = data.get("result", {})
        utterances = result.get("utterances", [])

        # 分离已确定和临时的 utterances
        definite_parts = []
        interim_part = ""
        for utt in utterances:
            if utt.get("definite"):
                definite_parts.append(utt.get("text", ""))
            else:
                interim_part = utt.get("text", "")

        # 如果没有 utterances，用顶层 text 作为临时结果
        if not utterances:
            interim_part = result.get("text", "")

        self._text.config(state=tk.NORMAL)

        # 删除上一次的临时文字（mark 到末尾之间的内容）
        self._text.delete("interim_start", tk.END)

        # result_type="full" 时，每次响应包含所有已确定的 utterances，
        # 只追加本次新增的 definite 部分；_confirmed_offset 是清空 GUI 时跳过的历史数量
        new_parts = definite_parts[self._confirmed_offset + self._confirmed_count:]
        for part in new_parts:
            if part:
                self._text.insert(tk.END, part, "final")
        self._confirmed_count = len(definite_parts) - self._confirmed_offset

        # 在已确定文字之后设置 mark，再写入临时文字
        self._text.mark_set("interim_start", "end-1c")
        if interim_part:
            self._text.insert(tk.END, interim_part, "interim")

        # 最后一包，换行并重置计数
        if resp.get("is_last"):
            self._text.delete("interim_start", tk.END)
            self._text.insert(tk.END, "\n", "final")
            self._confirmed_count = 0
            self._confirmed_offset = 0
            self._text.mark_set("interim_start", "end-1c")

        self._text.see(tk.END)
        self._text.config(state=tk.DISABLED)

    # ── 工具方法 ──────────────────────────────────────────────────────────────
    def _append_text(self, text: str, tag: str = "final"):
        self._text.config(state=tk.NORMAL)
        self._text.insert(tk.END, text, tag)
        self._text.see(tk.END)
        self._text.config(state=tk.DISABLED)

    def _set_status(self, msg: str):
        self._status_var.set(msg)

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
            self._do_auto_save()
        if self._recording:
            self._on_stop()
        self.destroy()


# ─── 入口 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
