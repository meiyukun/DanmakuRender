FROM python:3.9-alpine

WORKDIR /app

COPY . .
RUN apk update && apk add --no-cache curl build-base nodejs ffmpeg 
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
RUN pip install -r requirements.txt
VOLUME ["/app/config"]
ENTRYPOINT ["python3", "-u", "main.py"]
