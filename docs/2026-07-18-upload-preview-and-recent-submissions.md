# 上传素材预览与最近投稿选择

- 上传素材库的每个视频项目增加预览按钮，使用登录保护的 `/api/upload/media` 接口按需读取素材文件。
- 预览弹窗支持播放、进度条、Esc或点击遮罩关闭，并提供在新窗口打开原文件的入口。
- 在 `DMR/Uploader/biliapi/biliapi.py` 增加创作者中心 `/x/web/archives` 已发布稿件查询。
- 追加到已有稿件时，可从所选账号最近30个已发布投稿中选择，显示标题、BV号和发布时间。
- 最近投稿按账号在当前页面缓存；切换账号自动刷新，同时继续支持手动填写BV号。

新增文件：

- `docs/2026-07-18-upload-preview-and-recent-submissions.md`
