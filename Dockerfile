FROM python:3.9-alpine

WORKDIR /app

COPY . .
RUN apk update && apk add --no-cache curl
RUN apk add --no-cache build-essential
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
RUN apk add --no-cache nodejs ffmpeg 
RUN pip3 install --upgrade pip
RUN pip install -r requirements.txt

ENTRYPOINT ["python3", "-u", "main.py"]
