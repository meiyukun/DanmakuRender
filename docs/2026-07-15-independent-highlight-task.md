# 2026-07-15 独立热点剪辑任务变更日志

## 改动摘要

- 新增 `HighlightTask` 和全局唯一的 `DMH-*.yml` 配置类型；公共参数写在 `defaults`，每个直播任务可在 `targets` 中单独覆盖。
- 从直播任务配置和全局配置中移除 `auto_highlight`、`highlight_args`、`highlight_upload_args` 以及全局 Highlight 插件；旧字段会给出迁移错误。
- 新增直播结束快照、订阅注册、源文件清理持有与释放机制。
- 热点分析严格等待整场直播结束，校验并按顺序纳入该场直播的全部录制分段；不完整快照会失败而不会静默漏段。
- `dryrun.py` 默认安全测试“短时录制→整场结束→热点分析/混剪”，关闭上传、普通渲染、平台转录和清理；支持 `--duration`、`--wait-timeout`、`--ensure-output`、`--allow-upload` 与 `--regular-pipeline`。
- `--ensure-output` 只在短样本没有真实热点时生成带 `test_fallback` 标记的10秒测试片段，用于验证FFmpeg成片链路，不影响正式任务判断。
- 新增 `source.min_segment_duration`，默认忽略短于30秒的录制碎片，并在任务状态和清单记录被忽略分段。
- 原始弹幕 JSONL 改为无版本号的紧凑格式：过滤 `other/others` 和无文本事件，仅保留分段相对 `video_time`、类型、发送者和文本；解析器不再兼容旧 JSONL 字段。
- 热点任务可复用已有弹幕视频/字幕，也可按独立配置请求渲染或转录；等待有超时，失败原因持久化并释放源文件。
- 热点上传配置与普通回放上传完全分离，支持通用上传参数和按输出方案覆盖；默认关闭上传。
- Web API 增加独立热点流水线状态。

## 新增或生成的文件

- `DMR/Task/highlighttask.py`
- `DMR/Task/test_highlight_task.py`
- `configs/DMH-Highlight.yml`
- `configs/example-热点剪辑.yml`
- `docs/2026-07-15-independent-highlight-task.md`

热点分析、切片器、原始弹幕 JSONL 与相应测试沿用并继续维护。
