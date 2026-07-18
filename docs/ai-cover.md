# AI 封面与统一 AI 配置

DanmakuRender 使用统一 AI 组件处理封面内容分析、Responses 生图和热点候选复核。业务模块不再分别配置接口地址、密钥或模型。

## 全局配置

在全局 YAML 中配置公共接口和各项能力使用的模型：

```yaml
ai:
  base_url: ''
  api_key: ''
  timeout: 120
  capabilities:
    cover_analysis:
      model: gpt-5.4
      max_tokens: 1200
    highlight_review:
      model: gpt-5.4
      max_tokens: 1800
    image_generation:
      model: gpt-5.5
      stream: False
      retries: 3
      fallback_non_stream: True
```

推荐在项目根目录 `.env` 中保存本机凭据：

```dotenv
DMR_AI_BASE_URL=https://your-ai-proxy.example.com
DMR_AI_API_KEY=your-api-key
```

固定只读取这两个变量名，不提供 `api_key_env` 或 `base_url_env`。优先级为：操作系统环境变量、根目录 `.env`、全局 YAML、默认空值。`.env` 已被 Git 忽略，不应提交。

`base_url` 不要包含结尾 `/v1`。配置变更需要重启程序。

## 自动上传封面

上传目标仍通过局部业务配置决定是否启用封面及提示词：

```yaml
upload_args:
  bilibili:
    cover_auto:
      enabled: True
      ai:
        enabled: True
        analyze_danmaku: True
        use_reference_image: True
        size: 1536x1024
        analysis_prompt: |
          视频标题：{TITLE}
          主播：{STREAMER.NAME}
          弹幕热点资料：
          {DANMAKU_COMPACT}

          请直接输出一段完整的AI生图提示词。
        prompt: |
          为B站视频生成一张高点击率完整封面。
          视频标题：{TITLE}
          弹幕内容分析：{DANMAKU_SUMMARY}
```

- `analyze_danmaku`：清洗并压缩弹幕热点，再由文本 AI 整理完整生图提示词。
- `use_reference_image`：从真实视频画面截帧并随提示词提交。
- `size`、`size_candidates`、`fallback_size`：封面业务使用的生图尺寸。
- `analysis_prompt`：文本分析提示词。
- `prompt`：文本分析失败或无有效弹幕时的本地兜底提示词。
- `analysis_system_prompt`、`analysis_mode`、`analysis_max_items`、`analysis_max_chars`、`analysis_bucket_seconds`、`reference_image`：可选业务参数。

URL、Key、模型、超时、最大输出和请求重试不得再写入 `cover_auto.ai`，否则配置解析会提示迁移错误。

AI 生图失败时，自动上传流程仍会回退到本地视频帧叠字封面。

## 上传中心

上传中心支持三种封面模式：

- 不设置：新投稿使用平台默认行为，追加分P保留原稿封面。
- 上传封面：接受 JPEG、PNG、WebP，最大 10MB。
- AI 生成：汇总当前投稿信息、所选热点和用户补充要求，通过一次文本 AI 调用同时生成标题、简介、动态和完整生图提示词。四项都会填入页面并允许用户编辑；用户确认提示词后才调用生图接口。可附带任一待上传视频的指定时间参考帧。

手动上传和 AI 生成结果都会统一转换为 1280×800、16:10 JPEG，页面预览即为最终上传构图。AI 每次生成一张；用户可继续修改已确认的提示词并重生成。

追加分P选择新封面时必须明确确认；分P与封面通过同一次稿件编辑提交生效。显式选择的封面若校验或平台上传失败，整个上传任务失败，不会静默无封面继续。

## 弹幕、热点和参考图

自动封面只向文本 AI 发送清洗压缩后的弹幕摘要，不上传完整弹幕文件。上传中心选择热点小片段、自动混剪或自定义混剪时，会从可信热点清单汇总：

- 热点标题、类别、AI 置信度和复核理由；
- 起止时间和代表弹幕；
- 混剪实际包含的热点片段 ID。

上下文限制约 3500 字符，参考帧所属热点优先。

自动封面的参考帧默认取弹幕峰值附近，找不到峰值时取视频中间。上传中心则由用户选择视频和时间点，服务端使用 FFmpeg 截帧，因此源文件即使无法在浏览器播放，也可通过手动时间截取。

## 接口与日志

文本能力使用 `POST /v1/chat/completions`，生图使用 `POST /v1/responses` 和 `image_generation` 工具。统一客户端负责流式/非流式响应、重试和错误处理。

日志可以记录分析摘要、最终提示词和失败原因，但不会输出 API Key、Authorization 或图片 base64。
