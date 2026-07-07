# AI 封面生成说明

DanmakuRender 的 B 站自动封面支持使用 AI 生成图片。流程为：

1. 手动配置 `cover` 时，直接使用手动封面。
2. 开启 `cover_auto.enabled` 后，会先尝试 AI 封面。
3. AI 失败时，回退到本地视频帧叠字封面。

## 生图接口

图像生成使用 OpenAI Responses 兼容接口：

```http
POST /v1/responses
```

请求中使用 `image_generation` 内置工具：

```json
{
  "model": "gpt-5.5",
  "stream": false,
  "input": [
    {
      "role": "user",
      "content": "为B站视频生成一张高点击率完整封面。"
    }
  ],
  "tools": [
    {
      "type": "image_generation",
      "size": "1536x1024",
      "output_format": "png",
      "background": "opaque",
      "moderation": "auto",
      "partial_images": 0
    }
  ]
}
```

注意：中转服务会把 `image_generation` 的生图后端固定为 `gpt-image-2`，配置中的 `response_model` 只是 `/v1/responses` 的 `model` 字段。

## 关键配置

配置位置：

```yaml
upload_args:
  bilibili:
    cover_auto:
      enabled: True
      ai:
        enabled: True
```

常用 AI 配置：

```yaml
base_url: 'https://your-ai-proxy.example.com'
api_key_env: DMR_IMAGE_API_KEY
api_key: ''
response_model: gpt-5.5
analysis_model: gpt-5.5
analyze_danmaku: True
use_reference_image: True
size: 1536x1024
timeout: 120
image_retries: 3
analysis_prompt: |
  视频标题：{TITLE}
  主播：{STREAMER.NAME}
  弹幕热点资料：
  {DANMAKU_COMPACT}

  请直接输出一段完整的AI生图提示词，用于生成B站视频封面。
prompt: |
  为B站视频生成一张高点击率完整封面。
  视频标题：{TITLE}
  弹幕内容分析：{DANMAKU_SUMMARY}
```

参数说明：

- `base_url`：中转 API 地址，不要带结尾 `/v1`。
- `api_key_env`：优先读取的环境变量名。
- `api_key`：明文密钥兜底，不建议提交到仓库。
- `response_model`：`/v1/responses` 的 `model` 字段；生图后端由中转固定为 `gpt-image-2`。
- `analysis_model`：文本 AI 分析弹幕并生成完整生图提示词的模型。
- `analyze_danmaku`：是否读取弹幕热点，让文本 AI 生成更贴近本次视频内容的完整生图提示词。
- `use_reference_image`：是否从直播/视频真实画面截一帧作为生图参考图。
- `size`：固定生图尺寸。设置后只尝试这个尺寸。
- `timeout`：单次请求超时时间，单位秒。
- `image_retries`：每个尺寸的生图请求重试次数。
- `analysis_prompt`：文本 AI 的用户提示词。文本 AI 成功时，它的输出会直接作为最终生图提示词。
- `prompt`：本地兜底生图提示词。文本 AI 失败、未启用弹幕分析或没有有效弹幕时使用。

以下高级参数仍可在 `cover_auto.ai` 中手动添加，但默认配置文件不再展示：`stream`、`fallback_non_stream`、`size_candidates`、`fallback_size`、`output_format`、`output_compression`、`background`、`moderation`、`partial_images`、`analysis_system_prompt`、`analysis_mode`、`analysis_max_items`、`analysis_max_chars`、`analysis_max_tokens`、`analysis_bucket_seconds`、`reference_image`。

## 尺寸选择

当 `size` 和 `size_candidates` 都为空时，程序按视频比例自动选择官方支持尺寸：

- 横屏：`2048x1152`、`1536x1024`、`1024x1024`
- 竖屏：`1024x1536`、`1024x1024`
- 方图：`2048x2048`、`1024x1024`
- 无法获取分辨率：`2048x1152`、`1536x1024`、`1024x1024`

如果中转对大尺寸不稳定，最稳的配置是固定使用：

```yaml
size: 1024x1024
```

## 弹幕分析

开启 `analyze_danmaku` 后，程序会先清洗弹幕文件，只保留用于分析的视频内容线索：

- 去除 ASS 样式、坐标、移动特效等无关内容。
- 过滤空内容、纯符号、明显噪声和过短重复内容。
- 提取高频弹幕、关键词、互动峰值时段和代表弹幕。
- 只把压缩后的热点摘要发送给文本 AI，不上传原始完整弹幕文件。

文本 AI 的提示词可配置。文本 AI 成功时，它返回的正文会直接作为最终 AI 生图 prompt，不再要求 JSON，也不会再由程序拼接字段：

```yaml
analysis_prompt: |
  视频标题：{TITLE}
  主播：{STREAMER.NAME}
  弹幕热点资料：
  {DANMAKU_COMPACT}

  请直接输出一段完整的AI生图提示词，用于生成B站视频封面。
```

`prompt` 是本地兜底生图提示词模板。文本 AI 失败、未启用弹幕 AI 分析或无有效弹幕时才使用：

```yaml
prompt: |
  为B站视频生成一张高点击率完整封面。
  视频标题：{TITLE}
  弹幕内容分析：{DANMAKU_SUMMARY}
  热点弹幕：{HOT_DANMAKU}
  关键词：{HOT_KEYWORDS}
```

## 参考图

开启 `use_reference_image` 后，AI 封面生成会从待上传视频截取一张真实画面，并在 `/v1/responses` 的 `input` 中以 `input_image` 形式随提示词一起提交。这样生成结果能参考直播画面的构图、色调和内容。

截帧策略：

- 有弹幕峰值时，默认取第一个峰值窗口起点后 30 秒。
- 没有弹幕峰值时，默认取视频中间帧。
- 参考图默认缩放到最大宽度 1280，JPEG 质量 85。

如果截帧失败、视频路径不可用，程序会直接改用纯文本生图；如果带参考图生图失败，会在同一尺寸下自动降级为纯文本生图。

## 日志

AI 封面流程会输出：

- 弹幕热点分析结果。
- 文本 AI 成功生成的完整生图提示词。
- 最终发送给生图模型的提示词。
- 参考图截帧成功或失败信息。
- 每个尺寸的失败和重试信息。

日志不会输出 API key 或图片 base64 内容。
