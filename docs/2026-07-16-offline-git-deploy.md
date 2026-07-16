# 离线 Git 直传部署

## 改动内容

- `deploy.ps1` 默认将本地指定分支创建为完整 Git bundle，通过 SSH/SCP 直传到每台服务器；服务器不再执行 `git fetch origin`，因而不需要访问 GitHub。
- 新增 `-Push` 开关。只有显式指定时，脚本才会在离线部署前推送 `$Remote:$Branch`，用于需要时的远端备份。
- 服务器在 bundle 成功上传后，从该 bundle fetch，再执行 `git reset --hard FETCH_HEAD` 与 `git clean -fd`；fetch 失败时不会改变工作树。
- 本地和服务器的临时 bundle 均会在结束时清理。服务器临时文件位于 `/tmp/dmr-deploy-<GUID>.bundle`。

## 生成或使用的文件

- 本地系统临时目录中的 `dmr-deploy-<GUID>.bundle`：仅在部署过程中存在。
- 服务器 `/tmp/dmr-deploy-<GUID>.bundle`：仅在上传与更新过程中存在。

## 使用限制

部署仅发布指定分支的已提交内容；未提交的本地改动不会包含在 bundle 中。服务器仍需安装 Git 且可通过 SSH/SCP 从部署机访问。
