# 实时语音转文字

基于火山引擎「大模型流式语音识别（SAUC）」API 的 Windows 桌面客户端。实时采集**麦克风**和/或**系统音频**，通过 WebSocket 流式上传，边说边出字。

包含两个可独立运行的程序：

| 文件 | 用途 |
|---|---|
| `realtime_asr_client.py` | 实时语音转文字 GUI 客户端（主程序） |
| `sauc_websocket_demo.py` | 音频文件转写 demo（命令行） |

---

## 功能特性

- **三种音频源**：麦克风、系统音频（WASAPI 环回录制，可转写会议/视频里的声音）、或两者混音
- **流式显示**：灰色为临时识别结果，确定后转为黑色，句子级实时刷新
- **自动记录**：可按固定间隔把已确定的文字追加写入 `RecordMemory.md`，同时清空界面，避免长时间转录累积内存
- **开箱即用**：支持 PyInstaller 打包为单个 exe，无需 Python 环境即可分发
- **密钥外置**：API 密钥通过 `.env` 或环境变量注入，不写进代码

---

## 环境要求

- Windows（系统音频采集依赖 WASAPI 环回设备）
- Python 3.8+
- 火山引擎语音识别服务的 App ID 与 Access Token

## 安装

```bash
pip install -r requirements.txt
```

> 系统音频采集必须使用 `PyAudioWPatch`（标准 `PyAudio` 不提供 WASAPI loopback 接口）。程序会优先导入 `pyaudiowpatch`，找不到时回退到 `pyaudio`，此时仅麦克风可用。

## 配置密钥

复制 `.env.example` 为 `.env`，填入在火山引擎控制台获取的密钥：

```ini
VOLCENGINE_APP_KEY=你的_app_id
VOLCENGINE_ACCESS_KEY=你的_access_token
VOLCENGINE_RESOURCE_ID=volc.bigasr.sauc.duration   # 按量付费；并发包填 concurrent
```

`.env` 已被 `.gitignore` 排除，不会被提交。

---

## 运行

### GUI 客户端

```bash
python realtime_asr_client.py
```

界面操作：

1. 勾选**音频源**（麦克风 / 系统音频，可同时勾选，录音期间不可更改）
2. 点击**开始录音**，说话即出字
3. **停止录音**结束本次会话，**清空文字**清空显示区
4. 勾选**自动记录**并设置间隔（秒，最小 5 秒），已确定的文字会带时间戳追加到程序目录下的 `RecordMemory.md`；关闭窗口时若自动记录处于开启状态，会再保存一次

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

## 打包为 exe

```bash
pip install pyinstaller
python build_exe.py
```

产物为 `dist/语音转文字.exe`（单文件，GUI 模式）。分发时把 `.env`（或 `.env.example` 让用户自己填）放在 exe 同目录即可，程序会在 exe 所在目录读取配置、写入 `logs/` 和 `RecordMemory.md`。

更多打包参数与常见问题见 [打包说明.md](打包说明.md)。

---

## 项目结构

```
TTSV2/
├── realtime_asr_client.py           # 实时 GUI 客户端（主程序）
├── sauc_websocket_demo.py           # 文件转写 demo
├── build_exe.py                     # PyInstaller 打包脚本
├── requirements.txt                 # 依赖
├── .env.example                     # 密钥模板（.env 不入库）
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
查看 `logs/realtime_asr.log`（默认只记录 WARNING 及以上）。常见原因是密钥错误、`VOLCENGINE_RESOURCE_ID` 与实际计费方式不匹配，或账户额度用尽。
