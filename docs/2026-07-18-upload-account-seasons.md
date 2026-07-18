# 上传中心账号与合集自动加载

- 修复上传配置中的默认账号不在下拉选项时，浏览器将账号选择清空的问题；此时自动回退到第一个可用账号。
- 上传账号从 `.login_info/*.json` 中识别，要求登录文件包含 `SESSDATA` 和 `bili_jct`。
- 在 `DMR/Uploader/biliapi/biliapi.py` 增加创作者平台 `/seasons` 只读查询。
- 上传中心会在首次打开及切换账号时自动读取该账号的合集，显示合集标题和ID。
- 远程查询失败时保留配置文件中已知的合集，并继续允许手动填写合集ID。

新增文件：

- `DMR/Uploader/test_biliapi.py`
- `docs/2026-07-18-upload-account-seasons.md`
