# realtime_asr_client.py 代码分析文档

## 概述

本文件是一个基于火山引擎大模型流式语音识别（SAUC BigModel）API 的实时语音转文字客户端。程序采用 tkinter 构建桌面 GUI，支持麦克风输入、系统音频（WASAPI Loopback）捕获，以及两者混音同时识别。

每个已确定的句子还可以交给大模型实时翻译成中文：翻译逻辑独立在 `translator.py`，走 OpenAI 兼容的 Chat Completions 接口，默认使用 DeepSeek V4.1 Flash（模型名 `deepseek-flash`），可在界面或 `.env` 中切换为其他模型服务。

---

## 目录

- [依赖与配置](#依赖与配置)
- [模块结构](#模块结构)
- [二进制协议层](#二进制协议层)
- [音频采集层 AudioCapture](#音频采集层-audiocapture)
- [ASR 引擎层 RealtimeAsrEngine](#asr-引擎层-realtimeasrengine)
- [GUI 应用层 App](#gui-应用层-app)
- [实时翻译模块 translator.py](#实时翻译模块-translatorpy)
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
| `aiohttp` | 异步 WebSocket 客户端（语音识别）、HTTP 客户端（翻译接口） | 必需 |
| `python-dotenv` | 从 `.env` 文件加载环境变量 | 可选 |

### API 密钥配置

通过环境变量或 `.env` 文件注入（优先读取程序所在目录的 `.env`）。语音识别使用三个变量：

```
VOLCENGINE_APP_KEY      # 应用密钥
VOLCENGINE_ACCESS_KEY   # 访问密钥
VOLCENGINE_RESOURCE_ID  # 资源 ID（默认：volc.bigasr.sauc.duration）
```

实时翻译使用 `TRANSLATE_*` 变量（均可选，完整列表见 README）：

```
TRANSLATE_API_KEY       # 翻译 API Key（接口为 DeepSeek 时也可用 DEEPSEEK_API_KEY）
TRANSLATE_BASE_URL      # OpenAI 兼容接口地址（默认：https://api.deepseek.com）
TRANSLATE_MODEL         # 模型名（默认：deepseek-flash，即 DeepSeek V4.1 Flash）
TRANSLATE_EXTRA_BODY    # 合并进请求体的 JSON（DeepSeek 默认用它关闭思考模式）
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
├── 识别结果 → 分句
│   ├── Segment                — 一条已确定分句及其译文状态
│   ├── AsrSessionState        — 一次录音会话的上屏进度
│   ├── queue_callback()       — 后台线程回调 → UI 队列
│   ├── split_utterances()     — 拆出已确定分句 / 临时文字
│   └── format_record()        — 生成写入 RecordMemory.md 的文本
│
├── class App (tk.Tk)           — GUI 应用层
│   ├── _build_ui()            — 构建界面
│   ├── _handle_asr()          — 处理识别结果，新增分句上屏
│   ├── _add_segment()         — 追加分句（开启翻译时双语排版并提交翻译）
│   ├── _handle_translation()  — 把流式译文替换到对应分句下方
│   └── _do_auto_save()        — 自动保存到 RecordMemory.md
│
├── class TranslationSettingsDialog — 翻译设置对话框
│
└── 入口
    └── if __name__ == "__main__"

translator.py
├── TranslatorConfig            — 翻译配置（from_env() 读取 TRANSLATE_*）
├── PROVIDER_PRESETS            — 常见服务商的 OpenAI 兼容接口地址
├── needs_translation()         — 判断是否需要翻译（中文、纯数字跳过）
├── build_messages()            — 构造提示词（系统提示 + 上文 + 待翻译）
├── clean_output()              — 过滤 <think> 内容和「译文：」前缀
├── class TranslationEngine     — 独立线程 asyncio 事件循环，并发 + 流式调用
└── save_env_values()           — 把设置写回 .env
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
┌──────────────────────────────────────────────────────┐
│  ScrolledText（识别结果显示区）                        │
│  ─ final 标签（黑色，已确定文字）                      │
│  ─ translation 标签（蓝色缩进，译文）                  │
│  ─ tr_pending / tr_error（灰色「翻译中…」/ 红色失败原因）│
│  ─ interim 标签（灰色，临时文字）                      │
│  ─ error 标签（红色，错误信息）                        │
├──────────────────────────────────────────────────────┤
│  音频源: [✓] 麦克风  [ ] 系统音频                     │
├──────────────────────────────────────────────────────┤
│  翻译: [✓] 实时翻译为简体中文  模型: xxx [翻译设置…]   │
├──────────────────────────────────────────────────────┤
│ [开始录音] [停止录音] [清空文字]                       │
│ [✓] 自动记录  间隔(秒): [30]                         │
├──────────────────────────────────────────────────────┤
│  状态栏：就绪 / 录音中… · 实时翻译：模型名 / 已停止    │
└──────────────────────────────────────────────────────┘
```

### 分句模型

界面上每个已确定的句子对应一个 `Segment`，按上屏顺序存放在 `App._segments`：

| 字段 | 含义 |
|------|------|
| `bilingual` | 上屏时是否开启了翻译：是则双语排版（原文一行、译文一行、空一行），否则沿用原来的连续排版 |
| `status` | `none` 不翻译 / `skipped` 原文已是中文 / `pending` 翻译中 / `done` 完成 / `error` 失败 |
| `translation` / `error` | 当前（流式累计的）译文 / 失败原因 |
| `break_after` | 连续排版下录音在这句之后结束，保存时另起一段 |

每次录音分配一个会话编号和 `AsrSessionState`。`result_type="full"` 时每个响应都带着会话内的全部分句，`rendered` 记录该会话已上屏的 definite 分句数，只处理 `definite[rendered:]`。这个计数**不随清空 / 自动记录归零**，所以清空界面后旧句子不会重新出现、也不会被重复翻译；停止后才到达的最终结果按所属会话处理，不会覆盖新一次录音的临时文字。

### 文字渲染逻辑

使用 tkinter `Text` 组件的 `mark` 机制定位：

```
[seg_1]Hello everyone.↵[trs_1]大家好。[tre_1]↵↵[seg_2]好的。↵↵|interim_start|[临时文字（灰）]
```

- `interim_start`（左黏附）：临时文字起点。新内容通过 `_insert_before_interim()` 插在它前面——插入时临时把 mark 改为右黏附，插入后 mark 仍位于新内容之后，临时文字不受影响
- `seg_<id>`：分句起点，自动记录时作为删除边界
- `trs_<id>`（左黏附）/ `tre_<id>`（右黏附）：译文区间。流式译文到达时 `delete(trs, tre)` 后 `insert(trs, 新译文)`，两个 mark 自动包住新内容；翻译结束后释放
- 更新临时文字只删除到 `end-1c`：Tk 删除「从行首一直到 `end`」的区间时会连带删掉前一个换行符，双语排版的空行会被吃掉

### UI 队列轮询

```python
def _poll_ui(self):
    # 每 50ms 批量取出 _ui_queue 中的事件：
    #   ("asr", 会话编号, 响应)                 → _handle_asr()
    #   ("tr", 分句编号, 译文, 是否结束, 错误)   → _handle_translation()
    self._poll_id = self.after(50, self._poll_ui)
```

这种方式确保了：
1. 所有 UI 更新都在主线程执行（tkinter 线程安全要求）
2. 不阻塞主线程事件循环
3. 单个事件处理出错只记日志，不会中断轮询；用户向上翻看历史时不强制滚动到底部

后台线程的回调由 `queue_callback()` 生成：闭包只引用队列、不持有 App，避免 Tk 对象的最后一个引用落在后台线程、在错误的线程被回收。

### 翻译设置对话框

`TranslationSettingsDialog` 提供服务商预设（自动填入接口地址）、接口地址、API Key、模型、附加参数；「测试翻译」用当前填写的配置翻译一句示例文本，「保存」后调用 `App._apply_translation_config()`：立即替换 `TranslationEngine` 的配置（对之后的句子生效），并用 `save_env_values()` 写回程序目录下的 `.env`（保留其他行和注释）。

---

## 实时翻译模块 translator.py

### 调用流程

```
App._add_segment(句子)
  ├── needs_translation() 为假 → status=skipped，add_context() 只记入上文
  └── 为真 → status=pending，显示「翻译中…」，TranslationEngine.submit(seg_id, 句子)
               ↓ loop.call_soon_threadsafe
        翻译线程的 asyncio 事件循环，Semaphore 限制并发（默认 3）
               ↓
        POST {base_url}/chat/completions   stream=true，合并 extra_body
               ↓ SSE：data: {"choices":[{"delta":{"content":"…"}}]} … data: [DONE]
        每收到一段 → clean_output(累计内容) → on_update(seg_id, 译文, done=False)（至少间隔 80ms）
        结束       → on_update(seg_id, 最终译文, True, None) 或 on_update(seg_id, "", True, 错误信息)
               ↓ queue.Queue
        App._handle_translation() 替换 [trs, tre) 区间
```

### 提示词

- 系统提示：同声传译角色；只输出译文；结合上下文修正断句不完整、同音错字；提问只翻译不回答；专有名词可保留原文；已是目标语言则原样输出
- 用户消息：`【上文】`（最近 `TRANSLATE_CONTEXT_SIZE` 句原文，只用于理解语境）+ `【待翻译】`（本句）
- 上文只用原文、不用译文：并发翻译时上一句的译文可能还没返回，只依赖原文就无需等待

### 语言判断 `needs_translation()`

- 没有任何字母 / 汉字（纯数字、标点）→ 不翻译
- 目标语言是中文，且 `is_mostly_chinese()`：汉字数不少于其他语言的单词数（中英混说算中文），并且假名 / 谚文占比很低（排除日文、韩文）→ 不翻译

### 兼容不同的模型服务

- 只发送 OpenAI 兼容的最小请求体：`model`、`messages`、`stream`；`temperature` 仅在配置时发送（部分推理模型只接受默认值）
- `extra_body` 原样合并进请求体：DeepSeek 默认 `{"thinking": {"type": "disabled"}}`（V4 系列默认开启思考模式），其他服务商默认 `{}`
- 按响应的 `Content-Type` 自动识别 SSE 或普通 JSON；忽略 `reasoning_content`，过滤 `<think>…</think>` 和「译文：」前缀
- HTTP 错误转换成可读原因（401 API Key 无效、402 余额不足、404 接口地址或模型名称错误、429 限流等）；429 / 5xx / 网络错误 / 超时自动重试 2 次
- `aiohttp.ClientSession(trust_env=True)`：遵循系统的 `HTTP(S)_PROXY` 代理设置

---

## 线程模型与跨线程通信

```
┌─────────────────────────────────────────────────────────────────────┐
│ 主线程（tkinter 事件循环）                                            │
│  App._poll_ui() — 每 50ms 从 _ui_queue 拉取识别结果 / 译文，更新 UI  │
│  App._schedule_auto_save() — tkinter after() 定时保存               │
└──────┬──────────────────────────────────────────────┬───────────────┘
       ↑ queue.Queue（线程安全）                        ↓ submit() / ↑ queue.Queue
┌──────┴──────────────────────────────┐   ┌───────────┴───────────────────────┐
│ ASR 线程（daemon thread）             │   │ 翻译线程（daemon thread）            │
│  asyncio 事件循环                     │   │  asyncio 事件循环                    │
│  ├── _send_audio coroutine           │   │  └── _translate coroutine（并发）    │
│  └── _recv_results → _ui_queue.put() │   │      → HTTP SSE → _ui_queue.put()   │
└──────┬──────────────────────────────┘   └───────────────────────────────────┘
       ↑ loop.call_soon_threadsafe → asyncio.Queue
┌──────┴──────────────────────────────────────────────────────────────┐
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
| tkinter → 翻译线程 | `TranslationEngine.submit()` 内部 `loop.call_soon_threadsafe` | 在翻译线程的事件循环里创建任务 |
| 翻译线程 → tkinter | 同一个 `queue.Queue` + `after(50)` 轮询 | tkinter 只能在主线程操作 |

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
[_handle_asr] → 新增的已确定分句上屏
     ↓ 开启翻译且原文不是中文
[TranslationEngine.submit] → POST /chat/completions（SSE 流式）→ 大模型服务
     ↓ 流式译文经 queue.Queue 回到主线程
[_handle_translation] → 替换原文下方的译文
```

---

## 错误处理机制

| 错误场景 | 处理方式 |
|----------|----------|
| API 密钥未配置 | 启动时弹出警告对话框，不阻止运行 |
| WebSocket 初始化失败（code!=0） | 通过 result_callback 报告错误，显示红色错误信息，并自动停止录音 |
| WebSocket 连接异常 | 捕获 Exception，通过 result_callback 报告，并自动停止录音 |
| WASAPI loopback 设备未找到 | 抛出 RuntimeError，在 _on_start 中捕获并显示错误，释放已打开的音频流、结束 ASR 会话 |
| Gzip 解压失败 | 记录日志，返回空数据（静默失败） |
| JSON 解析失败 | 记录日志，返回空数据（静默失败） |
| pyaudio 未安装 | start() 时抛出 RuntimeError |
| 自动保存写文件失败 | 记录日志，不中断运行 |
| 未配置翻译 API Key | 勾选翻译时提示原因，可直接打开翻译设置 |
| 翻译请求失败（HTTP 错误、超时、网络） | 429 / 5xx / 网络错误 / 超时重试 2 次；仍失败则在原文下方显示红色原因，不影响识别 |
| 翻译设置写入 .env 失败 | 弹出警告，新设置在本次运行中仍然生效 |

---

## 自动记录功能

开启后，每隔指定秒数（最小 5 秒，默认 30 秒）将已确定的文字追加到同目录下的 `RecordMemory.md`；开启翻译的句子，译文以引用块写在原文下方：

```markdown
## 2025-01-01 12:00:00

（连续排版下识别的文字内容）

Hello everyone.
> 大家好。
```

**实现细节**：
- 按 `App._segments` 的顺序保存，遇到第一条仍在翻译中（`pending`）的分句就停下，剩下的等译文完成后下次再保存
- 写入成功后从界面删除 `1.0` 到下一条未保存分句的 `seg_<id>`（全部保存时到 `interim_start`），灰色临时文字保留
- 窗口关闭时（`on_closing`）若自动保存开启，以 `force=True` 保存全部分句，未完成的译文标注「（翻译未完成）」
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

选用 `result_type="full"` 而非 `"single"`，服务端每次响应包含所有历史已确定 utterances。每个录音会话用 `AsrSessionState.rendered` 记录已上屏数量，只渲染新增部分；该计数与界面清空无关，简化了状态管理。

### 4. mark 机制管理临时文字

利用 tkinter Text 组件的 `mark`（书签）功能标记临时文字起始位置、每个分句的起点和译文区间，实现了精确删除旧临时文字、追加新内容、原地替换流式译文的逻辑，避免了复杂的字符串位置计算。

### 5. 逐句翻译，只翻译已确定的分句

临时结果变化频繁，翻译它们既浪费调用量又会让译文反复跳动；等分句 `definite` 后再翻译，每句只调用一次。附带最近几句原文作为上文，弥补逐句翻译丢失的语境。

### 6. OpenAI 兼容接口 + aiohttp 直连

主流模型服务都提供 OpenAI 兼容接口，换接口地址、Key、模型名即可切换；直接用已有依赖 `aiohttp` 调用，不引入 `openai` SDK，打包体积不变。

---

## 潜在问题与改进建议

### 1. 降采样质量

`_resample_to_mono16k` 使用简单的整数索引抽取（`mono[int(i * ratio)]`），属于最近邻插值，没有低通滤波，高频信号会产生混叠。

**建议**：使用 `scipy.signal.resample` 或 `resampy` 库进行高质量重采样。

### 2. both 模式长度不对齐

`_mix_pcm` 取两路音频的最短长度对齐，多余的样本直接丢弃。在实际使用中，两路音频包大小可能因采样率不同而略有差异，导致少量音频数据丢失。

**建议**：在混音时保留两路缓冲区的剩余数据，下次混合时补入。

### 3. ~~`_confirmed_count` 在停止后未重置~~（已解决）

原实现用全局的 `_confirmed_count` / `_confirmed_offset` 计数，连接异常中断时不会重置，且录音中点击「清空文字」会把两个计数清零，导致之前的句子在下一次响应时重新出现。现改为按录音会话记录上屏进度（`AsrSessionState.rendered`），新会话使用新计数，清空界面不影响计数。

### 4. 缺少重连机制

网络断开时，WebSocket 会话直接失败，用户需要手动重新点击"开始录音"。

**建议**：在 `_session` 中添加指数退避重连逻辑。

### 5. 日志仅写文件

日志级别设为 `WARNING` 且只写 `realtime_asr.log` 文件，调试信息不可见。

**建议**：开发时可通过环境变量动态调整日志级别。

### 7. 翻译按句请求

每句话一次请求，系统提示和上文会重复发送。DeepSeek 等服务对重复前缀有缓存计费优惠；如需进一步节省调用量，可以把间隔很短的几句合并后批量翻译。

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
| `load_config()` | `realtime_asr_client.py` 第 87 行 | 加载 API 密钥 |
| `_Proto` | `realtime_asr_client.py` 第 94 行 | 协议位常量 |
| `_make_header()` | `realtime_asr_client.py` 第 108 行 | 构造消息头 |
| `build_full_request()` | `realtime_asr_client.py` 第 122 行 | 构造初始化包 |
| `build_audio_request()` | `realtime_asr_client.py` 第 139 行 | 构造音频包 |
| `parse_response()` | `realtime_asr_client.py` 第 148 行 | 解析响应 |
| `RealtimeAsrEngine` | `realtime_asr_client.py` 第 192 行 | 异步 ASR 引擎类 |
| `_find_loopback_device()` | `realtime_asr_client.py` 第 312 行 | 查找 loopback 设备 |
| `_resample_to_mono16k()` | `realtime_asr_client.py` 第 325 行 | PCM 重采样 |
| `_mix_pcm()` | `realtime_asr_client.py` 第 340 行 | PCM 混音 |
| `AudioCapture` | `realtime_asr_client.py` 第 351 行 | 音频采集类 |
| `Segment` | `realtime_asr_client.py` 第 474 行 | 已确定分句及译文状态 |
| `AsrSessionState` | `realtime_asr_client.py` 第 490 行 | 录音会话上屏进度 |
| `split_utterances()` | `realtime_asr_client.py` 第 508 行 | 拆分已确定分句 / 临时文字 |
| `format_record()` | `realtime_asr_client.py` 第 527 行 | 生成自动记录文本 |
| `App` | `realtime_asr_client.py` 第 555 行 | tkinter GUI 应用类 |
| `App._do_auto_save()` | `realtime_asr_client.py` 第 827 行 | 自动保存逻辑 |
| `App._handle_asr()` | `realtime_asr_client.py` 第 896 行 | 识别结果处理 |
| `App._add_segment()` | `realtime_asr_client.py` 第 933 行 | 分句上屏、提交翻译 |
| `App._handle_translation()` | `realtime_asr_client.py` 第 967 行 | 流式译文渲染 |
| `TranslationSettingsDialog` | `realtime_asr_client.py` 第 1052 行 | 翻译设置对话框 |
| `PROVIDER_PRESETS` | `translator.py` 第 49 行 | 服务商预设 |
| `TranslatorConfig` | `translator.py` 第 162 行 | 翻译配置 |
| `needs_translation()` | `translator.py` 第 259 行 | 是否需要翻译 |
| `build_messages()` | `translator.py` 第 280 行 | 构造提示词 |
| `clean_output()` | `translator.py` 第 297 行 | 清理模型输出 |
| `TranslationEngine` | `translator.py` 第 451 行 | 翻译引擎类 |
| `save_env_values()` | `translator.py` 第 688 行 | 写回 .env |
