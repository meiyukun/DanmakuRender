# 使用基于 Ubuntu 的 Python 3.9 镜像
FROM python:3.9

# 设置工作目录
WORKDIR /app

# 将当前目录下的所有文件复制到容器的 /app 目录
COPY . .
ENV BILIUP_VERSION=v0.2.2
ENV TZ=Asia/Shanghai
# 更新包列表并安装必要的依赖
RUN apt-get update && \
    apt-get install -y nodejs ffmpeg intel-media-va-driver npm && \
    pip install -r requirements.txt && pip install quickjs && \
    wget -O biliup-rs.tar.xz https://github.com/biliup/biliup-rs/releases/download/${BILIUP_VERSION}/biliupR-${BILIUP_VERSION}-x86_64-linux.tar.xz && \
    tar -xf biliup-rs.tar.xz -C . && mv ./biliupR-${BILIUP_VERSION}-x86_64-linux/biliup ./tools/ && rm ./biliup-rs.tar.xz && rm -rf ./biliupR-${BILIUP_VERSION}-x86_64-linux/ && \
    mv ./msyh.ttc /usr/share/fonts && fc-cache -f \
#    wget -O /usr/share/fonts/yahei.ttf https://github.com/chengda/popular-fonts/raw/refs/heads/master/%E5%BE%AE%E8%BD%AF%E9%9B%85%E9%BB%91.ttf && fc-cache -f \
    && apt-get autoremove && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 声明卷
VOLUME ["/app/configs"]

# 设置容器启动时执行的命令
CMD ["python3", "-u", "main.py"]
