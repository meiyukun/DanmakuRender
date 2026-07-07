import argparse
import base64
import json
import os
import sys
from io import BytesIO
from pathlib import Path

import requests
import yaml
from PIL import Image


def load_ai_config(config_path, upload_key):
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    upload_args = config.get("upload_args", {})
    if upload_key:
        target = upload_args[upload_key]
    elif "dm_video" in upload_args:
        target = upload_args["dm_video"]
    elif "bilibili" in upload_args:
        target = upload_args["bilibili"]
    else:
        raise KeyError("未找到 upload_args.dm_video 或 upload_args.bilibili")
    return target.get("cover_auto", {}).get("ai", {})


def find_image_base64(data):
    if isinstance(data, dict):
        if data.get("error"):
            raise RuntimeError(f"接口返回错误: {data.get('error')}")
        for key in ("result", "b64_json", "image_base64", "base64", "data"):
            value = data.get(key)
            if isinstance(value, str) and len(value) > 100:
                return value
        for value in data.values():
            found = find_image_base64(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_image_base64(item)
            if found:
                return found
    return None


def parse_sse_json(line):
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="ignore")
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line == "[DONE]":
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def save_image(b64_json, output_path):
    if "," in b64_json and b64_json.lstrip().startswith("data:"):
        b64_json = b64_json.split(",", 1)[1]
    image_bytes = base64.b64decode(b64_json)
    with Image.open(BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        image.save(output_path, format="PNG")
        return image.size


def main():
    parser = argparse.ArgumentParser(description="测试AI封面生图接口")
    parser.add_argument("--config", default="configs/DMR-Test.yml", help="配置文件路径")
    parser.add_argument("--upload-key", default=None, help="upload_args 下的键，例如 dm_video 或 bilibili")
    parser.add_argument("--base-url", default=None, help="覆盖 cover_auto.ai.base_url")
    parser.add_argument("--api-key", default=None, help="覆盖API密钥；不建议写入命令历史")
    parser.add_argument("--api-key-env", default=None, help="覆盖API密钥环境变量名")
    parser.add_argument("--size", default="1536x1024", help="生图尺寸，例如 1024x1024")
    parser.add_argument("--stream", action="store_true", help="使用流式Responses")
    parser.add_argument("--non-stream", action="store_true", help="使用非流式Responses")
    parser.add_argument("--prompt", default="Draw a simple red circle on a white background. No text.", help="测试提示词")
    parser.add_argument("--reference-image", default=None, help="参考图路径；设置后以 input_image 随提示词一起提交")
    parser.add_argument("--output", default=None, help="输出图片路径，默认写入 .temp")
    parser.add_argument("--timeout", type=int, default=None, help="请求超时时间")
    args = parser.parse_args()

    ai_config = load_ai_config(args.config, args.upload_key)
    base_url = (args.base_url or ai_config.get("base_url") or "https://cliproxy.hub.zhongyangufen.com.cn").rstrip("/")
    env_name = args.api_key_env or ai_config.get("api_key_env") or "DMR_IMAGE_API_KEY"
    api_key = args.api_key or os.getenv(env_name) or ai_config.get("api_key")
    if not api_key:
        raise RuntimeError(f"未找到API密钥，请设置环境变量 {env_name} 或配置 api_key")

    if args.stream and args.non_stream:
        raise ValueError("--stream 和 --non-stream 不能同时使用")
    stream = bool(ai_config.get("stream", False))
    if args.stream:
        stream = True
    if args.non_stream:
        stream = False

    size = args.size or ai_config.get("size") or "1024x1024"
    timeout = args.timeout or int(ai_config.get("timeout", 120) or 120)
    output_path = Path(args.output or f".temp/test-ai-cover-{size.replace('x', '_')}.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tool = {
        "type": "image_generation",
        "size": size,
    }
    for key in ("output_format", "output_compression", "background", "moderation", "partial_images"):
        value = ai_config.get(key)
        if value is not None:
            tool[key] = value
    tool.setdefault("output_format", "png")
    tool.setdefault("background", "opaque")
    tool.setdefault("moderation", "auto")
    tool.setdefault("partial_images", 0)

    content = args.prompt
    if args.reference_image:
        image_path = Path(args.reference_image)
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        content = [
            {
                "type": "input_text",
                "text": args.prompt,
            },
            {
                "type": "input_image",
                "image_url": f"data:{mime};base64,{image_b64}",
            },
        ]

    payload = {
        "model": ai_config.get("response_model") or ai_config.get("model") or "gpt-5.5",
        "stream": stream,
        "input": [{"role": "user", "content": content}],
        "tools": [tool],
    }

    print(f"POST {base_url}/v1/responses")
    print(f"stream={stream} size={size} model={payload['model']} reference_image={bool(args.reference_image)}")
    response = requests.post(
        f"{base_url}/v1/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        stream=stream,
        timeout=timeout,
    )
    print(f"status={response.status_code}")
    if response.status_code >= 400:
        print(response.text[:2000])
        response.raise_for_status()

    b64_json = None
    if stream:
        event_count = 0
        event_types = []
        for raw in response.iter_lines():
            event = parse_sse_json(raw)
            if not event:
                continue
            event_count += 1
            event_type = event.get("type")
            if event_type and event_type not in event_types:
                event_types.append(event_type)
                print(f"event={event_type}")
            b64_json = find_image_base64(event)
            if b64_json:
                break
        print(f"event_count={event_count}")
    else:
        data = response.json()
        print(f"response_keys={list(data.keys())}")
        b64_json = find_image_base64(data)

    if not b64_json:
        raise RuntimeError("响应中未找到图片base64")

    image_size = save_image(b64_json, output_path)
    print(f"image_base64_len={len(b64_json)}")
    print(f"image_size={image_size[0]}x{image_size[1]}")
    print(f"saved={output_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
