# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

火山引擎大模型流式语音识别（ASR）客户端。包含两个实现：
- `sauc_websocket_demo.py`：文件转写 demo，从音频文件读取并识别
- `realtime_asr_client.py`：实时语音转文字客户端，采集麦克风并通过 GUI 显示结果

## 运行方式

```bash
# 安装依赖
pip install pyaudio aiohttp python-dotenv

# 实时客户端（GUI）
python realtime_asr_client.py

# 文件转写 demo
python sauc_websocket_demo.py --file audio.wav
python sauc_websocket_demo.py --file audio.wav --url wss://openspeech.bytedance.com/api/v3/sauc/bigmodel --seg-duration 200
```

## 密钥配置

密钥通过 `.env` 文件或环境变量注入，不写入代码：

```
VOLCENGINE_APP_KEY=your_app_key
VOLCENGINE_ACCESS_KEY=your_access_key
VOLCENGINE_RESOURCE_ID=volc.bigasr.sauc.duration   # 按量付费；并发包用 concurrent
```

`sauc_websocket_demo.py` 中的 `Config` 类仍使用硬编码占位符，修改时需同步更新该类。

## 架构说明

### 二进制协议（两个文件通用）

火山引擎 ASR 使用自定义 WebSocket 二进制协议，每条消息结构：

```
[4字节头] [4字节序列号，有符号大端] [4字节负载长度] [Gzip压缩的负载]
```

4字节头编码：
- Byte 0：`(版本 << 4) | 头尺寸`（头尺寸固定为 1，即 4 字节）
- Byte 1：`(消息类型 << 4) | 标志位`
- Byte 2：`(序列化方式 << 4) | 压缩类型`
- Byte 3：保留，固定 `0x00`

关键标志位（Byte 1 低4位）：
- `0x01`：负载前有序列号字段
- `0x02`：这是最后一个包
- `NEG_WITH_SEQ (0x03)`：最后一包 + 序列号，此时序列号取负值

### 会话流程

1. WebSocket 握手时携带认证头（`X-Api-App-Key` 等）
2. 发送 seq=1 的 **Full Client Request**（JSON 配置，Gzip 压缩）
3. 等待服务端确认响应（code=0 表示成功）
4. 循环发送 **Audio Only Request**（PCM 数据，每包 200ms，seq 递增）
5. 最后一包将 flags 设为 `NEG_WITH_SEQ`，seq 取负值
6. 接收流式识别结果直到 `is_last=True`

### `realtime_asr_client.py` 三层架构

```
MicCapture          →  pyaudio 非阻塞回调，每 200ms 产出一块 PCM
RealtimeAsrEngine   →  独立线程运行 asyncio 事件循环
                       ├── _send_audio：从 asyncio.Queue 取 PCM 发送
                       └── _recv_results：接收识别结果，回调给 GUI
App (tkinter)       →  主线程 GUI，每 50ms 轮询 queue.Queue 更新文字
```

跨线程通信：
- `MicCapture` → `RealtimeAsrEngine`：用 `loop.call_soon_threadsafe` 将 PCM 放入 `asyncio.Queue`
- `RealtimeAsrEngine` → `App`：将响应放入 `queue.Queue`，GUI 用 `after(50)` 轮询

### 识别结果处理

服务端返回的 `utterances` 数组中，`definite=True` 表示该句已确定，`definite=False` 表示临时结果。GUI 中临时结果以灰色显示，确定结果转为黑色，每收到新临时结果时先删除上一次的临时文字再重写。

## 可用的 WebSocket 端点

| URL 后缀 | 模式 |
|---|---|
| `/bigmodel` | 双向流式，实时输出，延迟最低 |
| `/bigmodel_nostream` | 流式输入，累积后输出，精度更高 |
| `/bigmodel_async` | 仅在结果变化时推送，性能优化版 |

## 音频格式要求

- 采样率：16000 Hz（唯一支持值）
- 位深：16-bit
- 声道：单声道（mono）
- 每包时长：100–200ms（推荐 200ms）
- `sauc_websocket_demo.py` 对非 WAV 格式自动调用 `ffmpeg` 转换
