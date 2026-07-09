# DanmakuRender-5 —— 一个录制带弹幕直播的小工具（版本5）
结合网络上的代码写的一个能录制带弹幕直播流的小工具，主要用来录制包含弹幕的视频流。     
- 可以录制纯净直播流和弹幕，并且支持在本地预览带弹幕直播流。
- 可以自动渲染弹幕到视频中，并且渲染速度快。
- 支持同时录制多个直播。    
- 支持录播自动上传至B站。     

此版本为全新设计的版本5，包含以下新功能：     
- 支持动态载入配置文件。
- 支持更加复杂的录制、上传、渲染和清理逻辑。
- 支持搬运直播回放或者视频。
- 支持使用webhook与其他录制软件协同。

旧版本可以在分支v1-v4找到。     


## 使用说明
**如果你是纯萌新建议看我B站的专栏安装：https://www.bilibili.com/read/cv26348023**         

### 安装与使用文档      
[**安装文档**](docs/installation.md)       
[**使用文档**](docs/usage.md)     
[**意外停机与自动处理链恢复机制**](docs/crash-recovery.md)

[**服务器录播示例**](https://github.com/SmallPeaches/DanmakuRender/discussions/368)

### 可选参数
程序运行时可以指定以下参数
- `--config` 指定全局配置文件，默认`configs/global.yml`
- `--version` 查看版本号
- `--skip_update` 跳过版本检查

### 部署脚本
项目根目录提供 `deploy.ps1`，用于一键推送本地 `v5` 分支到远端 `my:v5`，然后通过 SSH 登录一台或多台应用服务器，在指定项目目录执行更新。

首次使用时复制示例配置：

```powershell
Copy-Item deploy.remote.example deploy.remote
```

编辑 `deploy.remote`，每行配置一台服务器：

```text
名称|SSH连接|服务器项目目录
prod|root@192.168.1.1|/opt/DanmakuRender
backup|root@192.168.1.2|/opt/DanmakuRender
```

`deploy.remote` 用于保存真实服务器连接信息，已加入 `.gitignore`，不会提交到 Git。

常用命令：

```powershell
# 推送到 my:v5，并更新全部服务器
.\deploy.ps1

# 只更新指定服务器
.\deploy.ps1 -Target prod

# 更新多台指定服务器
.\deploy.ps1 -Target "prod,backup"

# 跳过本地 push，只执行服务器更新
.\deploy.ps1 -SkipPush
```

服务器上的目标目录需要已经是本项目的 Git 仓库，并且可以访问远端 `my`。

## 更多
感谢 THMonster/danmaku, wbt5/real-url, ForgQi/biliup, ForgQi/stream-gears 的工作。     
出现问题欢迎大家提issue讨论。       

**本程序仅供研究学习使用！**
