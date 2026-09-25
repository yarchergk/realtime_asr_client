# 实时语音转文字 + 实时翻译

基于火山引擎「大模型流式语音识别（SAUC）」API 的 Windows 桌面客户端。实时采集**麦克风**和/或**系统音频**，通过 WebSocket 流式上传，边说边出字；同时可把每句识别结果交给大模型**实时翻译成中文**（默认 DeepSeek V4.1 Flash，可换成任何 OpenAI 兼容接口的模型）。

包含两个可独立运行的程序：

| 文件 | 用途 |
|---|---|
| `realtime_asr_client.py` | 实时语音转文字 + 翻译 GUI 客户端（主程序，翻译逻辑在 `translator.py`） |
| `sauc_websocket_demo.py` | 音频文件转写 demo（命令行） |

---

## 功能特性

- **三种音频源**：麦克风、系统音频（WASAPI 环回录制，可转写会议/视频里的声音）、或两者混音
- **流式显示**：灰色为临时识别结果，确定后转为黑色，句子级实时刷新
- **实时翻译**：每个确定的句子立即翻译成中文，译文流式显示在原文下方；原文本来就是中文的句子自动跳过，不产生调用
- **模型可切换**：默认 DeepSeek V4.1 Flash（模型名 `deepseek-flash`），在界面「翻译设置」或 `.env` 里即可换成通义千问、豆包、Kimi、智谱 GLM、OpenAI、OpenRouter、Ollama 本地模型等
- **自动记录**：可按固定间隔把已确定的文字（含译文）追加写入 `RecordMemory.md`，同时清空界面，避免长时间转录累积内存
- **开箱即用**：支持 PyInstaller 打包为单个 exe，无需 Python 环境即可分发
- **密钥外置**：API 密钥通过 `.env` 或环境变量注入，不写进代码

---

## 环境要求

- Windows（系统音频采集依赖 WASAPI 环回设备）
- Python 3.9+（`aiohttp>=3.11` 要求）
- 火山引擎语音识别服务的 App ID 与 Access Token
- 实时翻译（可选）：DeepSeek 或其他 OpenAI 兼容服务的 API Key

## 安装

```bash
pip install -r requirements.txt
```

> 系统音频采集必须使用 `PyAudioWPatch`（标准 `PyAudio` 不提供 WASAPI loopback 接口）。程序会优先导入 `pyaudiowpatch`，找不到时回退到 `pyaudio`，此时仅麦克风可用。

## 配置密钥

复制 `.env.example` 为 `.env`，填入在火山引擎控制台获取的密钥；需要实时翻译时再填翻译 API Key：

```ini
VOLCENGINE_APP_KEY=你的_app_id
VOLCENGINE_ACCESS_KEY=你的_access_token
VOLCENGINE_RESOURCE_ID=volc.bigasr.sauc.duration   # 按量付费；并发包填 concurrent

# 实时翻译（默认 DeepSeek V4.1 Flash，API Key 在 https://platform.deepseek.com 获取）
TRANSLATE_API_KEY=你的_deepseek_api_key
TRANSLATE_BASE_URL=https://api.deepseek.com
TRANSLATE_MODEL=deepseek-flash
```

`.env` 已被 `.gitignore` 排除，不会被提交。程序优先读取程序（exe）所在目录的 `.env`。

---

## 运行

### GUI 客户端

```bash
python realtime_asr_client.py
```

界面操作：

1. 勾选**音频源**（麦克风 / 系统音频，可同时勾选，录音期间不可更改）
2. 勾选**实时翻译为简体中文**（配置了翻译 API Key 时默认勾选），右侧显示当前模型；点**翻译设置…**可切换服务商 / 模型
3. 点击**开始录音**，说话即出字；开启翻译时每句原文下方会出现蓝色译文
4. **停止录音**结束本次会话，**清空文字**清空显示区（同时取消未完成的翻译）
5. 勾选**自动记录**并设置间隔（秒，最小 5 秒），已确定的文字会带时间戳追加到程序目录下的 `RecordMemory.md`；关闭窗口时若自动记录处于开启状态，会再保存一次

翻译开关和模型在录音期间也能修改，对之后的句子生效。开启翻译时的显示效果：

```
Hello everyone, welcome to today's meeting.
  大家好，欢迎参加今天的会议。

好的，我们开始吧。                 ← 原文已是中文，不翻译

Let's look at the latency numbers.
  我们来看一下延迟数据。

So the next step is…              ← 灰色：尚未确定的临时识别结果
```

### 文件转写 demo

```bash
python sauc_websocket_demo.py --file audio.wav
python sauc_websocket_demo.py --file audio.mp3 --url wss://openspeech.bytedance.com/api/v3/sauc/bigmodel --seg-duration 200
```

- `--file`：音频文件路径（必填），非 WAV 格式会自动调用 `ffmpeg` 转换
- `--url`：WebSocket 端点，默认 `bigmodel_nostream`
- `--seg-duration`：每包音频时长（毫秒），默认 200

> 注意：该 demo 的密钥仍写在文件内的 `Config` 类中（`app_key` / `access_key` 为占位符 `xxxxxxx`），使用前需手动替换。

---

## 实时翻译

### 工作方式

- 只翻译**已确定**（definite）的句子，灰色的临时结果不翻译，避免重复调用
- 每句话单独请求一次 `POST {接口地址}/chat/completions`（OpenAI 兼容格式），以 SSE 流式接收，边生成边上屏
- 请求里附带最近 3 句原文作为上文，帮助模型理解断句不完整、同音错字的语音识别结果
- 原文以中文为主（中英混说也算）时直接跳过；纯数字、标点也不翻译
- 限流（429）、服务端错误（5xx）、网络错误自动重试 2 次；失败时在原文下方显示红色原因（如「API Key 无效」「账户余额不足」「接口地址或模型名称错误」）
- DeepSeek V4 系列默认开启思考模式，程序默认发送 `{"thinking": {"type": "disabled"}}` 关闭它，首字延迟更低、也更省 token

### 切换模型

**方式一：界面**。点「翻译设置…」，选择服务商预设（自动填入接口地址）→ 填 API Key 和模型名 →「测试翻译」确认可用 →「保存」。新设置立即生效，并写入程序目录下的 `.env`，下次启动沿用。

**方式二：`.env`**。修改以下三项后重启程序：

```ini
TRANSLATE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
TRANSLATE_API_KEY=sk-xxxx
TRANSLATE_MODEL=qwen-plus
TRANSLATE_EXTRA_BODY={}          # 换用非 DeepSeek 服务时不需要 thinking 参数
```

常见服务商的接口地址（模型名以各服务商控制台为准）：

| 服务商 | `TRANSLATE_BASE_URL` | 说明 |
|---|---|---|
| DeepSeek（默认） | `https://api.deepseek.com` | 模型 `deepseek-flash`（V4.1 Flash） |
| 阿里云百炼 / 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 如 `qwen-plus` |
| 火山方舟 / 豆包 | `https://ark.cn-beijing.volces.com/api/v3` | 填模型 ID 或接入点 ID |
| 月之暗面 / Kimi | `https://api.moonshot.cn/v1` | |
| 智谱 / GLM | `https://open.bigmodel.cn/api/paas/v4` | |
| 硅基流动 SiliconFlow | `https://api.siliconflow.cn/v1` | |
| OpenRouter | `https://openrouter.ai/api/v1` | |
| OpenAI | `https://api.openai.com/v1` | |
| Ollama（本地） | `http://localhost:11434/v1` | 本地服务无需 API Key |

> 带思考模式的混合模型（如部分 Qwen3 / GLM / 豆包 Seed 模型）建议通过 `TRANSLATE_EXTRA_BODY` 关闭思考，参数名见各服务商文档，例如通义千问为 `{"enable_thinking": false}`。模型输出的 `<think>…</think>` 内容会被自动过滤。

### 可选配置

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TRANSLATE_API_KEY` | 空 | 翻译 API Key；为空且接口是 DeepSeek 时也会读取 `DEEPSEEK_API_KEY` |
| `TRANSLATE_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址（不含 `/chat/completions`） |
| `TRANSLATE_MODEL` | `deepseek-flash` | 模型名 |
| `TRANSLATE_EXTRA_BODY` | DeepSeek：`{"thinking": {"type": "disabled"}}`；其他：`{}` | 合并进请求体的 JSON |
| `TRANSLATE_TARGET_LANG` | `简体中文` | 目标语言 |
| `TRANSLATE_ENABLED` | 自动 | 启动时是否勾选翻译；默认配置了 API Key 就勾选 |
| `TRANSLATE_CONTEXT_SIZE` | `3` | 附带的上文句数（0–20） |
| `TRANSLATE_TEMPERATURE` | 不传 | 采样温度，留空由服务商决定 |
| `TRANSLATE_TIMEOUT` | `30` | 单次请求超时（秒） |
| `TRANSLATE_MAX_CONCURRENCY` | `3` | 同时进行的翻译请求数 |
| `TRANSLATE_SKIP_CHINESE` | `true` | 原文已是中文时跳过翻译 |
| `TRANSLATE_STREAM` | `true` | 流式接收译文 |

### 自动记录格式

开启翻译时，`RecordMemory.md` 中译文以引用块写在原文下方；还在翻译中的句子会等译文完成后再写入：

```markdown
## 2026-09-25 10:00:00

Hello everyone, welcome to today's meeting.
> 大家好，欢迎参加今天的会议。

好的，我们开始吧。
```

---

## 打包为 exe

```bash
pip install pyinstaller
python build_exe.py
```

产物为 `dist/语音转文字.exe`（单文件，GUI 模式）。分发时把 `.env`（或 `.env.example` 让用户自己填）放在 exe 同目录即可，程序会在 exe 所在目录读取配置、写入 `logs/` 和 `RecordMemory.md`；在「翻译设置」里保存的配置也写入该目录的 `.env`。

更多打包参数与常见问题见 [打包说明.md](打包说明.md)。

---

## 测试

```bash
python -m unittest discover -s tests -v
```

- `tests/test_translator.py`：翻译模块（配置解析、语言判断、流式解析、错误与重试、`.env` 写入），请求发往本地假服务，不消耗真实额度
- `tests/test_gui.py`：界面渲染逻辑（双语排版、流式译文、清空、自动记录、设置对话框），需要 tkinter 和图形界面，否则自动跳过（Linux 服务器可用 `xvfb-run` 运行）

---

## 项目结构

```
TTSV2/
├── realtime_asr_client.py           # 实时 GUI 客户端（主程序）
├── translator.py                    # 实时翻译模块（OpenAI 兼容接口，默认 DeepSeek）
├── sauc_websocket_demo.py           # 文件转写 demo
├── build_exe.py                     # PyInstaller 打包脚本
├── requirements.txt                 # 依赖
├── .env.example                     # 密钥模板（.env 不入库）
├── tests/                           # 单元测试
├── 打包说明.md                       # 打包指南
├── realtime_asr_client_analysis.md  # 主程序逐段代码解析
└── Large Model Streaming Speech Recognition API.md   # 火山引擎官方 API 文档
```

不入库的运行时目录：`logs/`（日志）、`build/` `dist/`（打包产物）、`RecordMemory.md`（自动记录的转录内容）。

---

## 技术要点

**音频格式**：16000 Hz / 16-bit / 单声道，每包 200 ms（6400 字节）。系统音频按设备原生采样率采集后重采样到 16 kHz 单声道；混音模式下两路 PCM 相加并做硬裁剪。

**协议**：火山引擎使用自定义 WebSocket 二进制帧，`[4字节头][4字节序列号][4字节负载长度][Gzip 负载]`。会话流程为「配置包（seq=1）→ 确认 → 循环发送音频包（seq 递增）→ 末包 flags 置 `NEG_WITH_SEQ` 且 seq 取负」。

**线程模型**：

```
AudioCapture        pyaudio 非阻塞回调，每 200ms 产出一块 PCM
      ↓ call_soon_threadsafe
RealtimeAsrEngine   独立线程跑 asyncio 事件循环（发送 + 接收并发）
      ↓ queue.Queue
App (tkinter)       主线程 GUI，每 50ms 轮询队列刷新文字
      ↓ submit(已确定的句子)          ↑ queue.Queue（流式译文）
TranslationEngine   独立线程跑 asyncio 事件循环，并发调用翻译接口
```

**可用端点**：

| URL 后缀 | 模式 |
|---|---|
| `/bigmodel` | 双向流式，实时输出，延迟最低（GUI 客户端使用） |
| `/bigmodel_nostream` | 流式输入、累积后输出，精度更高（demo 默认） |
| `/bigmodel_async` | 仅在结果变化时推送 |

详细的协议实现与代码解析见 [realtime_asr_client_analysis.md](realtime_asr_client_analysis.md)。

---

## 常见问题

**启动时弹出「缺少 API 密钥」**
`.env` 不存在或未填写。开发环境下 `.env` 需与脚本同目录；exe 环境下需与 exe 同目录。

**提示「未找到 WASAPI loopback 设备」**
未安装 `PyAudioWPatch`，或声卡驱动不支持环回录制。只用麦克风时取消勾选「系统音频」即可。

**报错「pyaudiowpatch 未安装」**
`pip install PyAudioWPatch`。

**识别没有反应 / 中途断开**
查看 `logs/realtime_asr.log`（默认只记录 WARNING 及以上）。常见原因是密钥错误、`VOLCENGINE_RESOURCE_ID` 与实际计费方式不匹配，或账户额度用尽。连接断开时程序会自动停止录音并在状态栏提示。

**勾选翻译时提示「翻译未配置」**
没有填翻译 API Key（或模型名）。在弹出的「翻译设置」里填写并保存即可，也可以直接编辑 `.env`。

**译文处显示「[翻译失败] …」**
红字说明了原因：`API Key 无效` 检查密钥是否属于当前服务商；`账户余额不足` 需充值；`接口地址或模型名称错误` 检查 `TRANSLATE_BASE_URL` 和 `TRANSLATE_MODEL`；`请求参数错误` 多为 `TRANSLATE_EXTRA_BODY` 里带了该服务商不支持的参数（例如给非 DeepSeek 服务传了 `thinking`），改成 `{}` 即可。可先在「翻译设置」里点「测试翻译」排查。

**中文句子没有译文**
这是预期行为：目标语言是中文时，原文已是中文的句子会跳过翻译。如需强制翻译，设置 `TRANSLATE_SKIP_CHINESE=false`。
