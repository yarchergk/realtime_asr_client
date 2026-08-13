# realtime_asr_client.py 代码分析文档

## 概述

本文件是一个基于火山引擎大模型流式语音识别（SAUC BigModel）API 的实时语音转文字客户端。程序采用 tkinter 构建桌面 GUI，支持麦克风输入、系统音频（WASAPI Loopback）捕获，以及两者混音同时识别。

---

## 目录

- [依赖与配置](#依赖与配置)
- [模块结构](#模块结构)
- [二进制协议层](#二进制协议层)
- [音频采集层 AudioCapture](#音频采集层-audiocapture)
- [ASR 引擎层 RealtimeAsrEngine](#asr-引擎层-realtimeasrengine)
- [GUI 应用层 App](#gui-应用层-app)
- [线程模型与跨线程通信](#线程模型与跨线程通信)
- [数据流全链路](#数据流全链路)
- [错误处理机制](#错误处理机制)
- [自动记录功能](#自动记录功能)
- [关键设计决策](#关键设计决策)
- [潜在问题与改进建议](#潜在问题与改进建议)

---

## 依赖与配置

### 外部依赖

| 包名 | 用途 | 可选性 |
|------|------|--------|
| `pyaudiowpatch` | 音频采集（优先，支持 WASAPI Loopback） | 可选，回退到 `pyaudio` |
| `pyaudio` | 音频采集（备用） | 可选 |
| `aiohttp` | 异步 WebSocket 客户端 | 必需 |
| `python-dotenv` | 从 `.env` 文件加载环境变量 | 可选 |

### API 密钥配置

通过环境变量或 `.env` 文件注入，支持三个变量：

```
VOLCENGINE_APP_KEY      # 应用密钥
VOLCENGINE_ACCESS_KEY   # 访问密钥
VOLCENGINE_RESOURCE_ID  # 资源 ID（默认：volc.bigasr.sauc.duration）
```

### 全局音频参数

```python
SAMPLE_RATE = 16000   # 采样率（API 唯一支持值）
CHANNELS    = 1       # 单声道
SAMPLE_WIDTH = 2      # 16-bit，每样本 2 字节
CHUNK_MS    = 200     # 每包 200ms
CHUNK_SIZE  = 6400    # 每包字节数 = 16000 × 1 × 2 × 200 / 1000
```

---

## 模块结构

```
realtime_asr_client.py
│
├── 全局常量与配置
│   ├── load_config()          — 从环境变量加载 API 密钥
│   └── _Proto                 — 协议位常量
│
├── 二进制协议工具函数
│   ├── _make_header()         — 构造 4 字节消息头
│   ├── _gz() / _ungz()        — Gzip 压缩/解压
│   ├── build_full_request()   — 构造会话初始化包
│   ├── build_audio_request()  — 构造音频数据包
│   └── parse_response()       — 解析服务端响应
│
├── class AudioCapture          — 音频采集层
│   ├── 支持 mic / loopback / both 三种模式
│   └── 提供 on_chunk 回调接口
│
├── class RealtimeAsrEngine     — 异步 ASR 引擎层
│   ├── 独立线程运行 asyncio 事件循环
│   ├── _send_audio()          — 音频发送协程
│   └── _recv_results()        — 结果接收协程
│
├── class App (tk.Tk)           — GUI 应用层
│   ├── _build_ui()            — 构建界面
│   ├── _handle_result()       — 处理识别结果并渲染文本
│   └── _do_auto_save()        — 自动保存到 RecordMemory.md
│
└── 入口
    └── if __name__ == "__main__"
```

---

## 二进制协议层

### 消息格式

每条 WebSocket 消息采用自定义二进制格式：

```
┌─────────────┬──────────────────┬───────────────────┬──────────────────────┐
│ 4B 消息头   │ 4B 序列号(有符号)│ 4B 负载长度       │ N B Gzip 压缩负载   │
│             │  大端序，可选    │  大端无符号        │                      │
└─────────────┴──────────────────┴───────────────────┴──────────────────────┘
```

### 消息头字段

| 字节 | 高4位 | 低4位 |
|------|-------|-------|
| Byte 0 | 版本号（固定 0x1） | 头尺寸（固定 1，即 4 字节） |
| Byte 1 | 消息类型 | 标志位 |
| Byte 2 | 序列化方式（0x1=JSON） | 压缩类型（0x1=Gzip） |
| Byte 3 | 保留，固定 0x00 | — |

### 消息类型（_Proto 常量）

| 常量 | 值 | 含义 |
|------|----|------|
| `FULL_REQUEST` | 0x1 | 会话初始化包（客户端→服务端） |
| `AUDIO_ONLY` | 0x2 | 音频数据包（客户端→服务端） |
| `FULL_RESPONSE` | 0x9 | 正常响应（服务端→客户端） |
| `ERROR_RESPONSE` | 0xF | 错误响应（服务端→客户端） |

### 标志位

| 常量 | 值 | 含义 |
|------|----|------|
| `NO_SEQ` | 0x0 | 无序列号字段 |
| `POS_SEQ` | 0x1 | 含正序列号 |
| `NEG_SEQ` | 0x2 | 是最后一包（无序列号） |
| `NEG_WITH_SEQ` | 0x3 | 最后一包且含序列号（序列号取负值） |

### 会话流程

```
Client                                    Server
  │                                          │
  │── WebSocket 握手 (携带认证 Headers) ────→│
  │── Full Request (seq=1, JSON 配置) ──────→│
  │←─ Full Response (code=0 表示成功) ───────│
  │                                          │
  │── Audio Only (seq=2, PCM+Gzip) ────────→│←── 循环发送
  │── Audio Only (seq=3, PCM+Gzip) ────────→│
  │←─ Full Response (中间识别结果) ──────────│←── 流式接收
  │   ...                                    │
  │── Audio Only (seq=-N, 最后一包) ────────→│
  │←─ Full Response (is_last=True) ──────────│
  │                                          │
```

---

## 音频采集层 AudioCapture

### 三种采集模式

```
mode="mic"       → 仅麦克风（直接回调推送）
mode="loopback"  → 仅系统音频（重采样后推送）
mode="both"      → 麦克风+系统音频混音后推送
```

### loopback 模式原理

使用 `pyaudiowpatch` 的 WASAPI Loopback API 捕获系统播放音频。由于系统音频的采样率和声道数可能与 API 要求不符，需要进行重采样：

**`_resample_to_mono16k(data, native_channels, native_rate)`**
1. 按帧解包原始 PCM（16-bit 小端）
2. 取第一声道实现单声道化
3. 按比例（native_rate / 16000）降采样（简单索引抽取，非高质量滤波）

### both 模式混音机制

```
麦克风回调 ──→ _mic_buf (缓冲区)
                                   ──→ 定时器每 200ms 执行 _flush_mix()
系统音频回调 → _loopback_buf        ──→ _mix_pcm() 硬裁剪混音 → on_chunk
```

**`_mix_pcm(pcm1, pcm2)`**：对两路 16-bit PCM 逐样本相加，结果截断至 [-32768, 32767]，长度取两者最短值对齐。

### 关键实现细节

- 所有 pyaudio 回调运行在 pyaudio 内部线程，通过 `_buf_lock` 保护缓冲区
- `_mix_timer` 使用 `threading.Timer` 递归调度，实现每 200ms 定时混合
- `stop()` 中取消 timer 再关流，确保不会有野指针访问

---

## ASR 引擎层 RealtimeAsrEngine

### 架构

```
主线程                     ASR 线程（daemon）
   │                              │
   │  engine.start()              │  _run_loop()
   │ ─────────────────────────→  │  asyncio 事件循环
   │                              │
   │  engine.push_audio(pcm)      │  asyncio.Queue (self._audio_queue)
   │  call_soon_threadsafe ──────→│  ↓
   │                              │  _send_audio(ws)  ──→ WebSocket
   │                              │
   │                              │  _recv_results(ws) ←── WebSocket
   │                              │  result_callback(resp)
   │  _ui_queue.put(resp) ←──────│
   │                              │
```

### 关键方法

**`push_audio(pcm)`**：线程安全地将 PCM 数据注入异步队列。使用 `loop.call_soon_threadsafe` 确保从 pyaudio 回调线程到 asyncio 事件循环的安全传递。

**`send_eof()`**：发送 `None` 作为哨兵值通知音频结束。

**`_send_audio(ws)`**：
- 使用 `asyncio.wait_for(..., timeout=1.0)` 避免永久阻塞
- 超时后检查 `_running` 标志决定是否发送结束包
- 收到 `None`（EOF）时发送最后一包（flags=NEG_WITH_SEQ，seq 取负）

**`_recv_results(ws)`**：
- 异步迭代 WebSocket 消息
- 遇到 `is_last=True` 或 `code != 0` 时退出循环
- WebSocket 错误/关闭时也退出

---

## GUI 应用层 App

### 界面布局

```
┌──────────────────────────────────────────┐
│  ScrolledText（识别结果显示区）            │
│  ─ final 标签（黑色，已确定文字）          │
│  ─ interim 标签（灰色，临时文字）          │
│  ─ error 标签（红色，错误信息）            │
├──────────────────────────────────────────┤
│  音频源: [✓] 麦克风  [ ] 系统音频         │
├──────────────────────────────────────────┤
│ [开始录音] [停止录音] [清空文字]           │
│ [✓] 自动记录  间隔(秒): [30]             │
├──────────────────────────────────────────┤
│  状态栏：就绪 / 录音中… / 停止中…         │
└──────────────────────────────────────────┘
```

### 文字渲染逻辑

使用 tkinter `Text` 组件的 `mark` 机制管理临时文字：

```
  [已确定文字...][已确定文字...]  |interim_start|  [临时文字（灰）]
                                       ↑
                               每次新结果到来时：
                               1. delete("interim_start", END) 删除旧临时文字
                               2. 追加新的 definite 部分（只追加新增的）
                               3. 更新 mark 位置
                               4. 插入新的临时文字
```

**`_confirmed_count`**：记录已写入 UI 的 `definite` utterance 数量。由于 `result_type="full"` 模式下每次响应包含所有已确定的 utterances，通过 `definite_parts[self._confirmed_count:]` 只取新增部分避免重复渲染。

### UI 队列轮询

```python
def _poll_ui(self):
    # 每 50ms 轮询一次 queue.Queue
    # 批量处理所有待处理消息（while True + get_nowait）
    self.after(50, self._poll_ui)
```

这种方式确保了：
1. 所有 UI 更新都在主线程执行（tkinter 线程安全要求）
2. 不阻塞主线程事件循环

---

## 线程模型与跨线程通信

```
┌─────────────────────────────────────────────────────────────────────┐
│ 主线程（tkinter 事件循环）                                            │
│  App._poll_ui() — 每 50ms 从 _ui_queue 拉取结果更新 UI              │
│  App._schedule_auto_save() — tkinter after() 定时保存               │
└───────────────────────┬─────────────────────────────────────────────┘
                        │ queue.Queue（线程安全）
                        ↑
┌───────────────────────┴─────────────────────────────────────────────┐
│ ASR 线程（daemon thread）                                             │
│  asyncio 事件循环                                                     │
│  ├── _send_audio coroutine                                           │
│  └── _recv_results coroutine → result_callback → _ui_queue.put()    │
└───────────────────────┬─────────────────────────────────────────────┘
                        ↑ loop.call_soon_threadsafe → asyncio.Queue
┌───────────────────────┴─────────────────────────────────────────────┐
│ pyaudio 回调线程（内部线程）                                           │
│  AudioCapture._direct_callback / _mic_callback / _loopback_callback │
│  → engine.push_audio(pcm)                                            │
└─────────────────────────────────────────────────────────────────────┘
```

### 跨线程通信方式

| 通信路径 | 机制 | 原因 |
|----------|------|------|
| pyaudio → asyncio | `loop.call_soon_threadsafe` | asyncio 不是线程安全的 |
| asyncio → tkinter | `queue.Queue` + `after(50)` 轮询 | tkinter 只能在主线程操作 |
| both 模式缓冲区 | `threading.Lock` | 两路音频回调并发写 |

---

## 数据流全链路

```
麦克风/系统音频
     ↓
[pyaudio 回调] ← 每 200ms 一包 PCM（6400 bytes）
     ↓ call_soon_threadsafe
[asyncio.Queue]
     ↓ await get()
[_send_audio] → build_audio_request() → Gzip 压缩 → WebSocket 发送
                                                            ↓
                                              火山引擎 BigModel ASR 服务
                                                            ↓
[_recv_results] ← WebSocket 接收 ← parse_response() ← Gzip 解压
     ↓ result_callback
[queue.Queue]
     ↓ after(50) 轮询
[_handle_result] → 更新 tkinter Text 组件
```

---

## 错误处理机制

| 错误场景 | 处理方式 |
|----------|----------|
| API 密钥未配置 | 启动时弹出警告对话框，不阻止运行 |
| WebSocket 初始化失败（code!=0） | 通过 result_callback 报告错误，回调显示红色错误信息 |
| WebSocket 连接异常 | 捕获 Exception，通过 result_callback 报告 |
| WASAPI loopback 设备未找到 | 抛出 RuntimeError，在 _on_start 中捕获并显示错误 |
| Gzip 解压失败 | 记录日志，返回空数据（静默失败） |
| JSON 解析失败 | 记录日志，返回空数据（静默失败） |
| pyaudio 未安装 | start() 时抛出 RuntimeError |
| 自动保存写文件失败 | 记录日志，不中断运行 |

---

## 自动记录功能

开启后，每隔指定秒数（最小 5 秒，默认 30 秒）将已确定的文字追加到同目录下的 `RecordMemory.md`：

```markdown
## 2025-01-01 12:00:00

（本段时间内识别的文字内容）
```

**实现细节**：
- 使用 `_last_saved_pos` 文本位置索引记录上次保存的位置
- 仅保存 `interim_start` mark 之前的内容（不含灰色临时文字）
- 窗口关闭时（`on_closing`）若自动保存开启，执行最后一次保存
- 使用 `tkinter.after()` 调度，运行在主线程，无竞争问题

---

## 关键设计决策

### 1. asyncio + 独立线程

使用独立线程运行 asyncio 事件循环，而非在主线程中混合 asyncio 与 tkinter。原因：
- tkinter 主循环（`mainloop()`）本身不是 asyncio 兼容的
- 独立 asyncio 线程可以充分利用异步 I/O 处理 WebSocket 收发

### 2. 双队列隔离

- `asyncio.Queue`：pyaudio 回调 → asyncio 协程（异步场景）
- `queue.Queue`：ASR 线程 → tkinter 主线程（跨线程场景）

两者分工明确，避免将非线程安全的 asyncio 原语暴露给其他线程。

### 3. result_type="full"

选用 `result_type="full"` 而非 `"single"`，服务端每次响应包含所有历史已确定 utterances。通过 `_confirmed_count` 计数器只渲染新增部分，简化了状态管理。

### 4. mark 机制管理临时文字

利用 tkinter Text 组件的 `mark`（书签）功能标记临时文字起始位置，实现了精确删除旧临时文字、追加新内容的逻辑，避免了复杂的字符串位置计算。

---

## 潜在问题与改进建议

### 1. 降采样质量

`_resample_to_mono16k` 使用简单的整数索引抽取（`mono[int(i * ratio)]`），属于最近邻插值，没有低通滤波，高频信号会产生混叠。

**建议**：使用 `scipy.signal.resample` 或 `resampy` 库进行高质量重采样。

### 2. both 模式长度不对齐

`_mix_pcm` 取两路音频的最短长度对齐，多余的样本直接丢弃。在实际使用中，两路音频包大小可能因采样率不同而略有差异，导致少量音频数据丢失。

**建议**：在混音时保留两路缓冲区的剩余数据，下次混合时补入。

### 3. `_confirmed_count` 在停止后未重置

`_confirmed_count` 在 `_handle_result` 的 `is_last` 分支中重置为 0，但如果连接异常中断（未收到 `is_last`），该计数不会重置，再次开始录音时会导致部分文字遗漏。

**建议**：在 `_on_start` 或 `_reset_state` 中也重置 `_confirmed_count`（代码第 545 行已在 `_on_start` 中重置，但 `_reset_state` 未重置）。

### 4. 缺少重连机制

网络断开时，WebSocket 会话直接失败，用户需要手动重新点击"开始录音"。

**建议**：在 `_session` 中添加指数退避重连逻辑。

### 5. 日志仅写文件

日志级别设为 `WARNING` 且只写 `realtime_asr.log` 文件，调试信息不可见。

**建议**：开发时可通过环境变量动态调整日志级别。

### 6. 简单降采样的替代方案

当 `native_rate` 与 `SAMPLE_RATE` 相差较大时（如 48000→16000），简单索引抽取会引入明显的音频质量问题。

**建议**：
```python
# 使用 audioop（标准库，Python < 3.13）
import audioop
data, _ = audioop.ratecv(data, 2, native_channels, native_rate, SAMPLE_RATE, None)
```

---

## 文件索引

| 符号 | 位置 | 说明 |
|------|------|------|
| `load_config()` | 第 60 行 | 加载 API 密钥 |
| `_Proto` | 第 67 行 | 协议位常量 |
| `_make_header()` | 第 81 行 | 构造消息头 |
| `build_full_request()` | 第 95 行 | 构造初始化包 |
| `build_audio_request()` | 第 112 行 | 构造音频包 |
| `parse_response()` | 第 121 行 | 解析响应 |
| `_find_loopback_device()` | 第 278 行 | 查找 loopback 设备 |
| `_resample_to_mono16k()` | 第 291 行 | PCM 重采样 |
| `_mix_pcm()` | 第 306 行 | PCM 混音 |
| `AudioCapture` | 第 317 行 | 音频采集类 |
| `RealtimeAsrEngine` | 第 165 行 | 异步 ASR 引擎类 |
| `App` | 第 439 行 | tkinter GUI 应用类 |
| `App._handle_result()` | 第 640 行 | 核心结果渲染逻辑 |
| `App._do_auto_save()` | 第 611 行 | 自动保存逻辑 |
