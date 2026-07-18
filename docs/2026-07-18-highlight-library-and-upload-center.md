# 跨场次热点素材库与通用上传中心

- 自动和手动热点任务统一使用独立场次输出目录，均保存独立小片段、初始混剪和 version 2 清单。
- 热点素材库按生成时间倒序展示，增加真实片段复选框、跨场次素材篮、跨场次混剪和热点场次批量删除。
- 跨场次素材参数不兼容时自动统一编码，只处理所选小片段，不处理原始整场直播。
- 新增通用上传中心，可从原始录屏、弹幕版录屏、热点混剪、热点小片段和自定义混剪选择并排序多个分P。
- 上传表单支持新投稿、向已有 BV 追加分P、标题、简介、动态、分区、标签、可见性、定时发布和加入合集；追加分P也可选择账号，合集既可从已知配置选择，也可直接填写ID。
- `biliwebapi` 增加已有 BV 初始化能力；Web 提交的无状态上传完成后自动清理任务记录。

新增文件：

- `DMR/WebService/templates/uploads.html`
- `DMR/WebService/static/uploads.css`
- `DMR/WebService/static/uploads.js`
- `DMR/WebService/test_webapi.py`
- `DMR/Uploader/test_uploader.py`
- `docs/2026-07-18-highlight-library-and-upload-center.md`
