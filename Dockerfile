# 使用基于 Ubuntu 的 Python 3.9 镜像
FROM python:3.9

# 设置工作目录
WORKDIR /app

# 将当前目录下的所有文件复制到容器的 /app 目录
COPY . .

# 更新包列表并安装必要的依赖
RUN apt-get update && \
    apt-get install -y nodejs ffmpeg npm && \
    # 安装 Python 依赖
    pip install -r requirements.txt && pip install quickjs \
    # 清理 apt 缓存
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 声明卷
VOLUME ["/app/config"]

# 设置容器启动时执行的命令
ENTRYPOINT ["python3", "-u", "main.py"]
