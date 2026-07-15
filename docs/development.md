# 开发参考

本文记录项目结构、常用命令和开发环境约定。面向维护者的协作规则见根目录 [AGENTS.md](../AGENTS.md)。

## 项目概览

DanmakuRender 是一个 Python 直播录制、弹幕录制/渲染、转码、上传和清理工具。主程序通过全局配置加载插件和任务配置，支持动态发现 `configs/DMR-*.yml` 任务文件。

主要入口：

- `main.py`：正常运行入口，默认读取 `configs/global.yml`。
- `dryrun.py`：测试运行入口，会把分段时间改短，并将上传延迟设为 24 小时。
- `render_only.py`：手动渲染或转码已有视频。
- `update.py`：更新 release 版本。

核心结构：

- `DMR/Config`：合并默认配置、全局配置和任务配置，并动态检测配置更新。
- `DMR/engine.py`：插件和任务的调度中心，使用队列传递 `PipeMessage`。
- `DMR/Downloader`：直播/视频下载与弹幕下载。
- `DMR/Render`：弹幕渲染、转码和 ffmpeg 调用。
- `DMR/Uploader`：B 站、YouTube 和自定义上传。
- `DMR/Cleaner`：复制、移动、删除等清理动作。
- `DMR/WebService`：可选 WebUI/API。

## 常用命令

```powershell
pip install -r requirements.txt
python main.py --version
python main.py
python main.py --skip_update
python main.py --config configs/global.yml
python dryrun.py --config configs/global.yml
python render_only.py --config configs/global.yml --mode dmrender --input_dir <视频目录>
python render_only.py --config configs/global.yml --transcode --input_dir <视频目录>
```

## 配置约定

- 默认全局配置：`configs/global.yml`；基础默认值：`DMR/Config/default.yml`。
- 任务文件必须匹配 `configs/DMR-*.yml` 才会自动载入；任务名取自 `DMR-` 后的文件名且不可重复。
- 示例配置：`configs/example-直播录制.yml` 与 `configs/example-视频下载.yml`。
- 程序每 60 秒检查任务配置变更；全局配置变更需重启才会生效。

## 外部工具

- Python 依赖见 `requirements.txt`。
- FFmpeg、FFprobe、FFplay 可置于 `tools/`，也可通过 `configs/global.yml` 的 `executable_tools_path` 配置。
- B 站上传需要 `biliup-rs` 或 Web API 登录信息。
- 斗鱼/抖音弹幕功能可能依赖 JavaScript 引擎，如 Node.js 或 QuickJS。
- 渲染参数和硬件编码器在 `render_args` 中配置；需按显卡环境调整 `vencoder` 与 `hwaccel_args`。

## 按范围验证

- 渲染改动：用 `render_only.py` 配合小样本视频验证。
- WebUI 改动：在 `dmr_engine_args.enabled_plugins` 启用 `webservice`；端口见 `webservice_kernel_args.port`。
