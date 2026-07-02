# 小米小爱语音转录 API 文档

**Base URL:** `https://api.xiaomixiaoai.com`

---

## 认证

所有接口均需在请求头中携带 `Authorization` 字段，格式如下：

```
Authorization: DAA-TOKEN-V1 client_id:{app_id},api_key:{api_key},access_token:{access_token},signature:{signature}
```

| 字段 | 说明 |
|------|------|
| `client_id` | 应用 ID，与表单中的 `app_id` 一致 |
| `api_key` | API 密钥 |
| `access_token` | 访问令牌 |
| `signature` | 请求签名 |

---

## 1. 提交转录任务

### `POST /api/rpcproxy/fasttranscribe`

上传音频文件并创建语音转文字任务。

### 请求头

| 字段 | 值 |
|------|-----|
| `Content-Type` | `multipart/form-data; boundary={boundary}` |
| `Authorization` | 见上方认证说明 |

### 请求体（multipart/form-data）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `request_id` | string | 是 | 本次请求的唯一 ID，32 位十六进制字符串 |
| `device_id` | string | 是 | 设备 ID，16 位十六进制字符串 |
| `app_id` | string | 是 | 应用 ID |
| `asr_language_list` | string | 是 | 识别语言列表，JSON 数组字符串，如 `["zh-CN"]` |
| `request_origin` | string | 是 | 请求来源，如 `RECORD` |
| `is_enable_speaker_recognition` | string | 是 | 是否启用说话人识别，`true` / `false` |
| `audio_duration` | string | 是 | 音频时长（秒） |
| `file` | file | 是 | 音频文件，支持 opus 格式，文件名示例：`tempEncodeOpusFile*.opus` |
| `is_enable_polish` | string | 是 | 是否启用文字润色，`true` / `false` |

### 响应

**HTTP 200**

```json
{
  "code": 200,
  "request_id": "78edfefa875a49f08667f49439e6ea25",
  "message": "success",
  "task_id": "e6a26da6d28c470dbd73280e2687168d",
  "file_id": "https://cnbj1.fds.api.xiaomi.com/simu-inter/..."
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | 状态码，200 表示成功 |
| `request_id` | string | 回显请求 ID |
| `message` | string | 结果描述 |
| `task_id` | string | 转录任务 ID，用于后续轮询查询 |
| `file_id` | string | 音频文件存储地址（带签名的临时 URL） |

---

## 2. 查询转录结果

### `GET /api/rpcproxy/fetchtranscriberesult`

轮询查询转录任务的处理状态和结果。

### 请求头

| 字段 | 值 |
|------|-----|
| `Authorization` | 见上方认证说明 |

### 查询参数（Query String）

| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `request_id` | string | 是 | 本次轮询的唯一 ID（每次轮询生成新 ID） |
| `device_id` | string | 是 | 设备 ID |
| `task_id` | string | 是 | 来自 `fasttranscribe` 响应的任务 ID |
| `app_id` | string | 是 | 应用 ID |
| `request_origin` | string | 是 | 请求来源，如 `RECORD` |

### 响应

#### 处理中（status: PROCESS）

```json
{
  "request_id": "78edfefa875a49f08667f49439e6ea25",
  "code": 200,
  "status": "PROCESS",
  "task_id": "e6a26da6d28c470dbd73280e2687168d",
  "file_id": "https://cnbj1.fds.api.xiaomi.com/simu-inter/..."
}
```

#### 完成（status: RESULT）

```json
{
  "request_id": "78edfefa875a49f08667f49439e6ea25",
  "code": 200,
  "status": "RESULT",
  "task_id": "e6a26da6d28c470dbd73280e2687168d",
  "file_id": "https://cnbj1.fds.api.xiaomi.com/simu-inter/...",
  "result": {
    "durationMilliseconds": 125396,
    "phrases": [
      {
        "speaker": 1,
        "speakerName": "",
        "offsetMilliseconds": 2370,
        "durationMilliseconds": 20975,
        "text": "领导你好，你好...",
        "sentences": [
          {
            "text": "领导你好，你好...",
            "offsetMilliseconds": 2370,
            "durationMilliseconds": 20975,
            "words": [
              {
                "text": "领导",
                "offsetMilliseconds": 2370,
                "durationMilliseconds": 250
              }
            ]
          }
        ]
      }
    ]
  }
}
```

### 响应字段说明

**顶层字段**

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | 状态码，200 表示成功 |
| `request_id` | string | 原始提交任务的 request_id |
| `status` | string | `PROCESS`（处理中）/ `RESULT`（已完成） |
| `task_id` | string | 任务 ID |
| `file_id` | string | 音频文件存储地址 |
| `result` | object | 转录结果，仅 status 为 `RESULT` 时存在 |

**result 对象**

| 字段 | 类型 | 说明 |
|------|------|------|
| `durationMilliseconds` | int | 音频总时长（毫秒） |
| `phrases` | array | 说话片段列表，按说话人分段 |

**phrases 元素**

| 字段 | 类型 | 说明 |
|------|------|------|
| `speaker` | int | 说话人编号（从 1 开始） |
| `speakerName` | string | 说话人名称（可为空） |
| `offsetMilliseconds` | int | 片段起始时间（毫秒） |
| `durationMilliseconds` | int | 片段时长（毫秒） |
| `text` | string | 片段完整文本 |
| `sentences` | array | 句子列表 |

**sentences 元素**

| 字段 | 类型 | 说明 |
|------|------|------|
| `text` | string | 句子文本 |
| `offsetMilliseconds` | int | 句子起始时间（毫秒） |
| `durationMilliseconds` | int | 句子时长（毫秒） |
| `words` | array | 词级时间戳列表 |

**words 元素**

| 字段 | 类型 | 说明 |
|------|------|------|
| `text` | string | 词或标点 |
| `offsetMilliseconds` | int | 起始时间（毫秒） |
| `durationMilliseconds` | int | 时长（毫秒） |

---

## 使用流程

```
1. 调用 POST /api/rpcproxy/fasttranscribe
   上传音频文件，获取 task_id

2. 轮询 GET /api/rpcproxy/fetchtranscriberesult?task_id={task_id}&...
   每次使用新的 request_id
   - status = "PROCESS" -> 继续等待后重试
   - status = "RESULT"  -> 从 result 字段取转录内容
```
