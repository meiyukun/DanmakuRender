# AGENTS.md

## 项目概览

DanmakuRender 是一个 Python 直播录制、弹幕录制/渲染、转码、上传和清理工具。主程序通过全局配置加载插件和任务配置，支持动态发现 `configs/DMR-*.yml` 任务文件。

主要入口：

- `main.py`：正常运行入口，默认读取 `configs/global.yml`。
- `dryrun.py`：测试运行入口，会把分段时间改短，并将上传延迟设置为 24 小时。
- `render_only.py`：手动渲染或转码已有视频。
- `update.py`：更新 release 版本。

核心包结构：

- `DMR/Config`：合并默认配置、全局配置和任务配置，动态检测配置更新。
- `DMR/engine.py`：插件和任务的调度中心，使用队列传递 `PipeMessage`。
- `DMR/Downloader`：直播/视频下载与弹幕下载。
- `DMR/Render`：弹幕渲染、转码和 ffmpeg 调用。
- `DMR/Uploader`：B站、YouTube 和自定义上传。
- `DMR/Cleaner`：复制、移动、删除等清理动作。
- `DMR/WebService`：可选 WebUI/API。

## 常用命令

安装依赖：

```powershell
pip install -r requirements.txt
```

查看版本：

```powershell
python main.py --version
```

正常运行：

```powershell
python main.py
```

跳过更新检查运行：

```powershell
python main.py --skip_update
```

指定全局配置：

```powershell
python main.py --config configs/global.yml
```

测试录制流程：

```powershell
python dryrun.py --config configs/global.yml
```

手动渲染：

```powershell
python render_only.py --config configs/global.yml --mode dmrender --input_dir <视频目录>
```

手动转码：

```powershell
python render_only.py --config configs/global.yml --transcode --input_dir <视频目录>
```

## 配置约定

- 默认全局配置是 `configs/global.yml`，基础默认值在 `DMR/Config/default.yml`。
- 任务文件必须匹配 `configs/DMR-*.yml` 才会被自动载入。
- 任务名来自文件名中 `DMR-` 后面的部分，任务名不要重复。
- 示例配置在 `configs/example-直播录制.yml` 和 `configs/example-视频下载.yml`。
- 程序运行时会每 60 秒检查任务配置变更；全局配置变更需要重启生效。
- `configs/`、`.login_info/`、`logs/`、`.temp/` 往往包含本机状态或用户私有配置，修改前要确认目的。

## 外部工具和运行环境

- 依赖 Python 包列在 `requirements.txt`。
- FFmpeg、FFprobe、FFplay 可放在 `tools/`，也可通过 `configs/global.yml` 的 `executable_tools_path` 指定。
- B站上传需要 `biliup-rs` 或 Web API 登录信息。
- 斗鱼/抖音弹幕相关能力可能依赖 JavaScript 引擎，例如 nodejs 或 quickjs。
- 渲染参数和硬件编码器设置在 `render_args` 中；不同显卡环境可能需要改 `vencoder` 和 `hwaccel_args`。

## 验证建议

- 配置解析相关改动优先用 `python main.py --version` 做基础导入检查，再用目标配置实例化运行。
- 录制/上传/清理属于有副作用流程，测试前确认任务配置、输出目录、上传延迟和清理策略。
- `dryrun.py` 会触发实际录制和可能的上传逻辑，文档说明上传会延迟 24 小时发布，但仍需谨慎。
- 渲染相关改动可用 `render_only.py` 对小样本视频验证。
- WebUI 改动需要启用 `dmr_engine_args.enabled_plugins` 中的 `webservice`，默认端口见 `webservice_kernel_args.port`。

## 代码风格与维护注意

- 项目当前以标准库线程、队列和普通类为主，优先沿用现有插件/任务/消息队列结构。
- YAML 配置合并逻辑集中在 `DMR/Config/__init__.py`，不要在业务模块里重复实现配置默认值合并。
- 新增插件或任务事件时，检查 `DMR/engine.py` 的 `pipeSend` 路由和对应插件队列消费逻辑。
- 文件路径和用户可见文本中已有大量中文，编辑时保持 UTF-8。
- 避免提交本机账号、cookies、日志、临时文件和真实任务配置中的隐私信息。

## 当前工作区状态

初始化时检测到以下未提交改动，后续修改时不要误覆盖：

- 已修改：`DMR/Uploader/biliwebapi.py`
- 已修改：`configs/global.yml`
- 已新增：`DMR/utils/ai/README.md`
- 未跟踪：`configs/DMR-Test2.yml.a`
- 未跟踪：一个以 `configs/DMR-羊羊.yml。` 形式命名的配置文件，文件名末尾包含中文句号
