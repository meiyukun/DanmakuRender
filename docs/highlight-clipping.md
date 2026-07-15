# 独立热点剪辑任务

热点剪辑已从直播录制流水线拆分为独立任务类型。整个程序共用唯一一份 `DMH-*.yml` 热点配置：直播任务只在整场直播结束时发布一份不可变的视频组快照；全局热点任务订阅多个直播任务，独立负责依赖准备、弹幕分析、混剪、上传和结果状态。若同时放置多份 `DMH-*.yml`，配置加载器会拒绝并报告冲突。

一场直播使用同一个 `group_id` 聚合全部录制分段。分段完成事件只负责追加素材，不会启动热点分析；只有下载器确认整场直播结束后，直播任务才会发布包含分段总数的完整快照。热点任务校验快照完整性，按 `segment_id` 排序，并把所有分段首尾拼成统一时间轴后再检测热点。

完整性校验后，`source.min_segment_duration` 会过滤过短的异常重连或收尾碎片，默认30秒。过滤发生在建立分析时间轴之前；被忽略分段的编号、路径、时长和原因会写入任务状态及热点清单，不会静默丢失。设为0可关闭过滤，单个直播任务也可在 `targets` 中覆盖。

## 配置与生命周期

- `configs/DMH-Highlight.yml` 是所有直播任务共用的配置，默认不上传；`configs/example-热点剪辑.yml` 是带完整备注的模板。
- `defaults` 提供通用的 source/preprocess/analysis/encoding/outputs/upload/clean 配置；`targets.<直播任务名>` 递归覆盖默认值，列表整体替换，`~` 清空字段。
- 全局热点任务统一订阅 `targets` 中启用的直播任务。每个直播任务可单独覆盖公共参数；生成成片和清单后立即释放对应源文件，后续上传不再占用源文件。
- 状态依次为 `preparing`、`waiting_dependencies`、`analyzing`、`output_ready`、`uploading`、`completed/failed`，失败原因会持久化。
- `src_video` 剪无弹幕原版，`dm_video` 剪弹幕版；缺少弹幕版或字幕时可通过 `preprocess` 请求渲染/转录。字幕支持 disabled、available、prefer、required 及超时。

## 分析与隐私

热点优先读取录制阶段保存的原始 JSONL 弹幕，缺失时可回退 ASS。分析结合局部稳健基线、峰值显著度、峰谷边界、相邻峰合并和“哈哈、问号、帅、666”等短时重复反应，过滤长期重复的福袋/口令互动。AI 复核仅接收带相对时间的文本，不发送昵称、用户 ID 或弹幕类型。

## 相关文件

- 独立任务：`DMR/Task/highlighttask.py`
- 分析与编码：`DMR/Highlight/`
- 原始弹幕：`DMR/Downloader/Danmaku/raw_writer.py`
- 配置加载：`DMR/Config/__init__.py`
