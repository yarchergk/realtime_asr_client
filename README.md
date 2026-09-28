# IEC 会议实时语音翻译

基于火山引擎「大模型流式语音识别（SAUC）」的 Windows 实时字幕工具。实时采集**麦克风**和/或**系统音频**，通过 WebSocket 流式上传，边说边出字；每句确定的识别结果立即交给大模型**翻译成中文**（默认 DeepSeek V4.1 Flash，可换成任何 OpenAI 兼容接口的模型），原文与译文对照上屏。

两套界面共用同一份音频采集、ASR 协议、翻译引擎和转录状态机，按需二选一：

| 程序 | 界面 | 说明 |
|---|---|---|
| `web_app.py` | 浏览器 | **推荐**。本地起 HTTP + WebSocket 服务，网页界面；有字幕条模式，适合投屏、录屏、OBS 采集 |
| `realtime_asr_client.py` | tkinter 桌面窗口 | 早期版本，功能相同，无字幕条模式 |
| `sauc_websocket_demo.py` | 命令行 | 音频文件转写 demo |

---

## 功能特性

- **三种音频源**：麦克风、系统音频（WASAPI 环回录制，可转写会议 / 视频里的声音）、或两者混音
- **流式显示**：灰色为临时识别结果，确定后转为正文；句子级实时刷新
- **实时翻译**：每个确定的句子立即翻译，译文流式显示在原文下方；原文本来就是中文的句子自动跳过，不产生调用
- **模型可切换**：默认 DeepSeek V4.1 Flash，在界面「翻译设置」或 `.env` 里可换成通义千问、豆包、Kimi、智谱 GLM、OpenAI、OpenRouter、Ollama 本地模型等
- **自动记录**：按固定间隔把已确定的文字（含译文）追加写入 `RecordMemory.md` 并清空界面，长时间转录不会越攒越多
- **字幕条模式**（浏览器版）：只留一块大字幕，宽高随窗口自适应，可用滚轮上下翻阅历史字幕
- **刷新不丢内容**（浏览器版）：服务端是唯一真相源，页面刷新或重开后由快照重建界面；连接断开会自动退避重连
- **离线可用**：网页字体随程序走，会议现场断网界面也不会变样
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

### 浏览器版（推荐）

```bash
python web_app.py                        # 启动并自动打开浏览器
python web_app.py --no-open --port 8760
```

服务只监听 `127.0.0.1`，不对局域网开放；界面地址默认 <http://127.0.0.1:8760/>。多开标签页看到的是同一份内容，互相同步。

界面操作：

1. 顶栏选**音频源**（麦克风 / 系统音频 / 同时采集，录音期间锁定）
2. **翻译**开关控制之后的句子是否翻译；齿轮按钮打开翻译设置（服务商、模型、目标语言、上文句数、自动记录间隔）
3. 点**开始录音**，说话即出字。底部一行灰字是尚未确定的临时结果，最多显示三行，长句往上滚
4. 鼠标移到某句上会出现**复制**、**重译**
5. **自动记录**开关打开后，每隔一段时间把已确定的文字连同译文追加写入 `RecordMemory.md` 并清空界面
6. **字幕条**按钮进入字幕条模式，`Esc` 或右下角按钮返回

快捷键：`Ctrl` + `+` / `-` 调整字号（0.8–1.6 倍，记在浏览器里），`Esc` 关闭设置抽屉 / 退出字幕条。右上角按钮切换明暗主题。

**字幕条模式**：隐藏正文列表，只留一块大字幕——宽度撑满窗口、高度吃满除底部两行之外的空间，把浏览器拉成扁条当字幕栏用也能正常显示（此时说明文字自动隐藏，让位给字幕）。鼠标滚轮可上下翻阅历史字幕（最近 200 条）：停在底部时自动跟随最新一句，往上翻之后不会被新字幕拽回去，滚回底部即恢复跟随。

### 桌面版（tkinter）

```bash
python realtime_asr_client.py
```

1. 勾选**音频源**（可同时勾选，录音期间不可更改）
2. 勾选**实时翻译为简体中文**（配置了翻译 API Key 时默认勾选），点**翻译设置…**可切换服务商 / 模型
3. **开始录音** → 说话即出字；**停止录音**结束会话；**清空文字**清空显示区（同时取消未完成的翻译）
4. 勾选**自动记录**并设置间隔（秒，最小 5 秒）；关闭窗口时若自动记录处于开启状态，会再保存一次

显示效果：

```
Hello everyone, welcome to the meeting.
  大家好，欢迎参加今天的会议。

好的，我们开始吧。                 ← 原文已是中文，不翻译

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
- 限流（429）、服务端错误（5xx）、网络错误自动重试 2 次；失败时在原文位置显示原因（如「API Key 无效」「账户余额不足」「接口地址或模型名称错误」）
- DeepSeek V4 系列默认开启思考模式，程序默认发送 `{"thinking": {"type": "disabled"}}` 关闭它，首字延迟更低、也更省 token

### 切换模型

**方式一：界面**。打开翻译设置，选择服务商预设（自动填入接口地址）→ 填 API Key 和模型名 →「测试翻译」确认可用 →「保存」。新设置立即生效，并写入程序目录下的 `.env`，下次启动沿用。

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
| `TRANSLATE_ENABLED` | 自动 | 启动时是否开启翻译；默认配置了 API Key 就开启 |
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

Hello everyone, welcome to the meeting.
> 大家好，欢迎参加今天的会议。

好的，我们开始吧。
```

---

## 打包为 exe

```bash
pip install pyinstaller
python build_exe.py
```

产物为 `dist/语音转文字.exe`（单文件，GUI 模式）。分发时把 `.env`（或 `.env.example` 让用户自己填）放在 exe 同目录即可，程序会在 exe 所在目录读取配置、写入 `logs/` 和 `RecordMemory.md`；在翻译设置里保存的配置也写入该目录的 `.env`。

> `build_exe.py` 目前打的是桌面版（`realtime_asr_client.py`）。打包浏览器版需要把入口换成 `web_app.py`，并带上网页资源：`--add-data "web/static;web/static"`。这些是**只读资源**，打进单文件 exe 后位于 `sys._MEIPASS`，由 `web_app.resource_path()` 定位；而 `.env` / `logs/` / `RecordMemory.md` 属于**用户数据**，由 `get_application_path()` 定位到 exe 旁边——两者不要混用。

更多打包参数与常见问题见 [打包说明.md](打包说明.md)。

---

## 测试

```bash
python -m unittest discover -s tests -v      # 全部
python -m unittest tests.test_core -v        # 只跑状态机
```

| 文件 | 覆盖内容 |
|---|---|
| `tests/test_core.py` | 转录状态机：事件序列、全量语义、翻译调度、重译、清空、自动记录（35 个用例，不需要网络和图形界面） |
| `tests/test_translator.py` | 翻译模块：配置解析、语言判断、流式解析、错误与重试、`.env` 写入。请求发往本地假服务，不消耗真实额度 |
| `tests/test_gui.py` | tkinter 版界面渲染逻辑，需要图形界面，否则自动跳过（Linux 可用 `xvfb-run`） |

---

## 项目结构

```
TTSV2/
├── web_app.py                       # 浏览器版服务（HTTP + WebSocket）
├── web/static/                      # 网页界面
│   ├── index.html
│   ├── app.css
│   ├── app.js
│   ├── fonts.css                    # 由 tools/vendor_fonts.py 生成，勿手改
│   └── fonts/                       # 随程序走的字体子集 + OFL.txt（许可）
├── core.py                          # UI 无关的转录状态机（两套界面共用）
├── realtime_asr_client.py           # 桌面版（tkinter）+ 音频采集 + ASR 协议
├── translator.py                    # 实时翻译模块（OpenAI 兼容接口，默认 DeepSeek）
├── sauc_websocket_demo.py           # 文件转写 demo
├── tools/vendor_fonts.py            # 一次性脚本：把网页字体下载到本地
├── build_exe.py                     # PyInstaller 打包脚本
├── requirements.txt                 # 依赖
├── .env.example                     # 密钥模板（.env 不入库）
├── tests/                           # 单元测试
├── 打包说明.md                       # 打包指南
├── realtime_asr_client_analysis.md  # 桌面版逐段代码解析
└── Large Model Streaming Speech Recognition API.md   # 火山引擎官方 API 文档
```

不入库的运行时文件：`logs/`（日志）、`build/` `dist/`（打包产物）、`RecordMemory.md`（自动记录的转录内容）、`.env`（真实密钥）。

---

## 技术要点

**音频格式**：16000 Hz / 16-bit / 单声道，每包 200 ms（6400 字节）——采样率和声道数是 API 的唯一取值，不可调。系统音频按设备原生采样率采集后重采样到 16 kHz 单声道；混音模式下两路 PCM 相加并做硬裁剪。

**协议**：火山引擎使用自定义 WebSocket 二进制帧，`[4字节头][4字节序列号][4字节负载长度][Gzip 负载]`。会话流程为「配置包（seq=1）→ 确认 → 循环发送音频包（seq 递增）→ 末包 flags 置 `NEG_WITH_SEQ` 且 seq 取负」。

**全量语义**：请求里 `result_type="full"`，服务端每次响应都返回会话内**全部**已确定的分句，不是增量。`core.SessionState.rendered` 记录已上屏的条数，只取其后新增的部分；该计数不随清空 / 自动记录归零，否则旧句子会重新上屏并被重复翻译（重复计费）。

**线程模型**（浏览器版）：

```
AudioCapture          pyaudio 非阻塞回调，每 200ms 产出一块 PCM
      ↓ call_soon_threadsafe
RealtimeAsrEngine     独立线程跑 asyncio 事件循环（发送 + 接收并发）
      ↓ call_soon_threadsafe
Hub / TranscriptState 服务端事件循环线程：唯一能碰状态的地方
      ↓ WebSocket 广播                    ↑ call_soon_threadsafe
浏览器（app.js）                      TranslationEngine（独立线程 + 事件循环）
```

桌面版把最后一段换成 tkinter 主线程每 50 ms 轮询 `queue.Queue`。两版的共同铁律：**绝不在 pyaudio 回调或引擎线程里直接碰界面状态**。

**事件协议**（服务端 → 浏览器，全部是可直接 `json.dumps` 的 dict）：

| 事件 | 含义 |
|---|---|
| `hello` | 连上时的完整快照，浏览器据此重建界面 |
| `session` / `session_end` | 一次录音的开始 / 结束 |
| `segment` | 新的一条已确定分句（含翻译状态） |
| `translation` | 译文流式片段 / 完成 |
| `error` | 某条分句翻译失败 |
| `interim` | 当前临时识别结果 |
| `level` | 音量电平（画波形用） |
| `status` / `config` / `translate` / `auto_save` | 录音状态、翻译配置、开关变化 |
| `cleared` / `removed` / `saved` / `save_error` | 清空、自动记录移除、写盘结果 |
| `asr_error` / `settings_saved` / `test_result` | 识别中断、设置保存、测试翻译结果 |

浏览器 → 服务端的命令：`start` `stop` `clear` `translate` `retranslate` `auto_save` `save_now` `source` `save_settings` `test_settings`。

**可用端点**：

| URL 后缀 | 模式 |
|---|---|
| `/bigmodel` | 双向流式，实时输出，延迟最低（两套界面都用它） |
| `/bigmodel_nostream` | 流式输入、累积后输出，精度更高（demo 默认） |
| `/bigmodel_async` | 仅在结果变化时推送 |

详细的协议实现与代码解析见 [realtime_asr_client_analysis.md](realtime_asr_client_analysis.md)。

---

## 常见问题

**浏览器没有自动打开 / 打不开页面**
手动访问 <http://127.0.0.1:8760/>。端口被占用时换一个：`python web_app.py --port 8761`。服务只监听本机，别的机器访问不到是预期行为。

**网页显示「与程序的连接已断开，正在重连…」**
`web_app.py` 已退出或崩溃。重新启动即可，页面会自动重连并恢复内容；原因查 `logs/realtime_asr.log`。

**启动时提示缺少 API 密钥**
`.env` 不存在或未填写。开发环境下 `.env` 需与脚本同目录；exe 环境下需与 exe 同目录。

**提示「未找到 WASAPI loopback 设备」**
未安装 `PyAudioWPatch`，或声卡驱动不支持环回录制。只用麦克风时把音频源切到「麦克风」即可。

**报错「pyaudiowpatch 未安装」**
`pip install PyAudioWPatch`。

**识别没有反应 / 中途断开**
查看 `logs/realtime_asr.log`（默认只记录 WARNING 及以上）。常见原因是密钥错误、`VOLCENGINE_RESOURCE_ID` 与实际计费方式不匹配，或账户额度用尽。连接断开时程序会自动停止录音并提示。

**开启翻译时提示未配置**
没有填翻译 API Key（或模型名）。在翻译设置里填写并保存即可，也可以直接编辑 `.env`。

**译文处显示「翻译失败」**
点开可以看到原因：`API Key 无效` 检查密钥是否属于当前服务商；`账户余额不足` 需充值；`接口地址或模型名称错误` 检查 `TRANSLATE_BASE_URL` 和 `TRANSLATE_MODEL`；`请求参数错误` 多为 `TRANSLATE_EXTRA_BODY` 里带了该服务商不支持的参数（例如给非 DeepSeek 服务传了 `thinking`），改成 `{}` 即可。可先在翻译设置里点「测试翻译」排查。

**中文句子没有译文**
这是预期行为：目标语言是中文时，原文已是中文的句子会跳过翻译。如需强制翻译，设置 `TRANSLATE_SKIP_CHINESE=false`。

**网页字体看起来和别人的不一样**
字体文件缺失时浏览器**不会报错**，只会静默回落到系统字体。确认 `web/static/fonts/` 存在；需要重新生成时联网跑一次 `python tools/vendor_fonts.py`。

---

## 许可

网页界面内置 Source Sans 3、Noto Sans SC、IBM Plex Mono 三套字体（`web/static/fonts/`），
均以 SIL Open Font License 1.1 授权分发，版权声明与许可全文见 [web/static/fonts/OFL.txt](web/static/fonts/OFL.txt)。
