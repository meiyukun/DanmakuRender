# 2026-07-16 流水线恢复与热点空闲重启修复

## 问题

- 重启后 ReplayTask 能加载并展示流水线 JSON，但旧进程的渲染、转录和上传请求 ID 已失效，界面仍显示等待且不会收到回调。
- Web 只能删除恢复记录，不能选择并恢复。
- 任务页每次轮询都会重建详情 DOM，导致展开项立即收起。
- “空闲后重启”没有统计独立 HighlightTask 的分析、编码、上传和清理状态。

## 改动

- 新增“恢复选中”操作。恢复 ReplayTask 时清除失效等待，根据磁盘现有文件重新判定阶段，并按当前配置重新投递转录、渲染和上传。
- HighlightTask 标记本次启动加载的任务，允许显式恢复中断的依赖、分析、编码、上传或清理阶段。
- 流水线 API 增加 `task_type`、`recovery_id` 和 `can_resume`。
- 任务页轮询后恢复已展开的阶段详情。
- 空闲判定加入 HighlightTask 的准备、依赖等待、分析、编码、重试、上传和清理状态。

## 文件

- `DMR/Task/liveevents.py`
- `DMR/Task/replaytask.py`
- `DMR/Task/highlighttask.py`
- `DMR/WebService/webapi.py`
- `DMR/WebService/templates/tasks.html`
- `DMR/__init__.py`
- `DMR/tests/test_recovery.py`
