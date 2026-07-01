# 小米语音转录 API 中转代理

将小米小爱语音转录 API（fasttranscribe / fetchtranscriberesult）封装为简单的 HTTP 服务。调用者只需上传音频文件即可获得转录结果，无需关心认证参数和轮询逻辑。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 编辑 config.json，填入有效的 Authorization 头

# 3. 启动服务
python server.py
```

服务默认监听 `0.0.0.0:8080`，可在 `config.json` 中修改 `port`。

## 配置文件

`config.json` 示例：

```json
{
  "authorization": "DAA-TOKEN-V1 client_id:...,api_key:...,access_token:...,signature:...",
  "device_id": "f8a5c2c6be47162c",
  "app_id": "476338648271290368",
  "port": 8080,
  "poll_interval": 3,
  "poll_timeout": 300
}
```

| 字段 | 说明 |
|------|------|
| `authorization` | 完整的 Authorization 头，每次请求小米 API 时使用 |
| `device_id` | 设备 ID |
| `app_id` | 应用 ID |
| `port` | 服务监听端口 |
| `poll_interval` | 轮询结果的间隔秒数 |
| `poll_timeout` | 轮询超时秒数，超时标记任务失败 |

> **注意：** `authorization` 中的 `api_key` 和 `signature` 每次请求都不同，无法自动生成。需要从抓包中获取，并通过 `PUT /auth` 接口动态更新。

## API 接口

### 上传音频转录

```
POST /transcribe
```

上传音频文件，服务自动提交转录任务。默认为异步模式，立即返回 `job_id`，客户端通过 `GET /jobs/{job_id}` 轮询结果。

**请求：** `multipart/form-data`，`file` 字段为音频文件（必填）。

**可选查询参数：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `language` | `zh-CN` | 识别语言 |
| `speaker_recognition` | `true` | 是否启用说话人识别 |
| `polish` | `false` | 是否启用文字润色 |
| `format` | `text` | 返回格式：`text`（带说话人标签的纯文本）/ `full`（完整 JSON） |
| `audio_duration` | 自动估算 | 音频时长（秒），留空则按 opus 32kbps 码率估算 |
| `wait` | `false` | `true` 时阻塞等待结果直接返回（短音频 / 兼容旧调用） |

**异步模式（默认，适合长音频）：**

```bash
# 1. 提交任务，立即返回
curl -X POST http://localhost:8080/transcribe -F "file=@long.opus"
# → {"job_id": "e6a26da6d28c470d...", "status": "pending"}

# 2. 轮询直到完成
curl http://localhost:8080/jobs/e6a26da6d28c470d...
# → {"job_id": "...", "status": "processing"}
# → {"job_id": "...", "status": "done", "result": {"task_id": "...", "duration_ms": 125396, "text": "..."}}
```

**同步模式（wait=true，适合短音频）：**

```bash
curl -X POST "http://localhost:8080/transcribe?wait=true" -F "file=@short.opus"
# → 阻塞直到完成，直接返回结果
```

**text 格式结果：**

```json
{
  "task_id": "e6a26da6d28c470dbd73280e2687168d",
  "duration_ms": 125396,
  "text": "[说话人1] 领导你好，你好...\n[说话人2] 不是甘肃集团，是甘肃下级的股份是吗？"
}
```

**full 格式结果：** 返回小米 API 的完整 JSON，包含 phrases、sentences、words 等详细信息。

---

### 查询任务状态

```
GET /jobs/{job_id}
```

查询异步转录任务的状态和结果。`job_id` 为提交时返回的 `job_id`。

**响应示例：**

```bash
# 进行中
{"job_id": "...", "status": "processing"}

# 完成
{"job_id": "...", "status": "done", "result": { ... }}

# 失败（HTTP 500）
{"job_id": "...", "status": "failed", "error": "transcription timed out"}

# 不存在（HTTP 404）
{"error": "job not found"}
```

> **注意：** 任务仅保存在内存中，服务重启后历史 job 记录清空。

---

### 更新 Authorization

```
PUT /auth
```

更新内存中的 Authorization 头，同时写回 `config.json` 持久化。

```bash
curl -X PUT http://localhost:8080/auth \
  -H "Content-Type: application/json" \
  -d '{"authorization": "DAA-TOKEN-V1 client_id:...,api_key:...,access_token:...,signature:..."}'
```

---

### 查看当前 Authorization

```
GET /auth
```

返回脱敏后的 Authorization 头，用于确认当前使用的凭证。

```bash
curl http://localhost:8080/auth
```

---

### 健康检查

```
GET /health
```

```bash
curl http://localhost:8080/health
# {"status":"ok","authorization_set":true}
```

## 错误处理

| HTTP 状态码 | 场景 |
|-------------|------|
| 400 | 上传了空文件 |
| 401 | Authorization 过期或无效（小米 API 返回 401/403） |
| 404 | 查询的 job_id 不存在 |
| 500 | 异步任务轮询过程中出错（含超时） |
| 502 | 小米 API 返回非 200 业务错误 |
| 504 | 同步模式（wait=true）轮询超时 |

当收到 401 错误时，需要通过 `PUT /auth` 更新有效的 Authorization 头后重试。

## 工作原理

1. 接收上传的音频文件
2. 自动生成 `request_id`（`uuid4().hex`）
3. 按 opus 32kbps 码率估算 `audio_duration`
4. 填充 `device_id`、`app_id` 等固定参数
5. 调用小米 `POST /api/rpcproxy/fasttranscribe` 提交任务，获取 `task_id`（即 `job_id`）
6. **异步模式**：后台协程每隔 `poll_interval` 秒轮询结果，结果写入内存；客户端通过 `GET /jobs/{job_id}` 查询
7. **同步模式**：服务端阻塞等待轮询完成后直接返回结果
8. 收到 `status: RESULT` 后将结果格式化并标记任务完成
