FROM linuxserver/ffmpeg

# 设置工作目录
WORKDIR /app

# 将当前目录下的所有文件复制到容器的 /app 目录
COPY . .
ARG TARGETARCH
ENV BILIUP_VERSION=v1.1.28
ENV TZ=Asia/Shanghai
EXPOSE 5000
# 使用 linuxserver/ffmpeg 作为基础镜像


# 更新软件源并安装必要的依赖
RUN apt-get update && apt-get install -y \
    software-properties-common wget \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update

# 安装 Python 3.9
RUN apt-get install -y python3.9 python3.9-distutils python3.9-dev



# 安装 pip
RUN wget https://bootstrap.pypa.io/get-pip.py && \
    python3.9 get-pip.py && \
    rm get-pip.py

# 设置 Python 3.9 为默认 Python 版本
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.9 1


# 更新包列表并安装DMR必要的依赖
RUN apt-get install -y fontconfig nodejs npm && pip install --ignore-installed -r requirements.txt && pip install --use-pep517 quickjs

RUN if [ "$TARGETARCH" = "amd64" ]; then \
        BILIUP_ARCH="x86_64"; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        BILIUP_ARCH="aarch64"; \
    else \
        echo "Unsupported architecture: $TARGETARCH" && exit 1; \
    fi && \
    wget -O biliup-rs.tar.xz https://github.com/biliup/biliup/releases/download/${BILIUP_VERSION}/biliupR-${BILIUP_VERSION}-${BILIUP_ARCH}-linux.tar.xz && \
    tar -xf biliup-rs.tar.xz -C . && \
    mv ./biliupR-${BILIUP_VERSION}-${BILIUP_ARCH}-linux/biliup ./tools/ && \
    rm ./biliup-rs.tar.xz && \
    rm -rf ./biliupR-${BILIUP_VERSION}-${BILIUP_ARCH}-linux/ && \
    mv ./fonts/* /usr/share/fonts && fc-cache -f

#    wget -O /usr/share/fonts/yahei.ttf https://github.com/chengda/popular-fonts/raw/refs/heads/master/%E5%BE%AE%E8%BD%AF%E9%9B%85%E9%BB%91.ttf && fc-cache -f \

RUN apt-get autoremove && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 声明卷
VOLUME ["/app/configs"]

# 设置容器启动时执行的命令
ENTRYPOINT ["python3", "-u", "main.py"]