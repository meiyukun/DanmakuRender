import argparse
import base64
import os
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from DMR.AI import AIClient
from DMR.Config import Config


def save_image(b64_json, output_path):
    if ',' in b64_json and b64_json.lstrip().startswith('data:'):
        b64_json = b64_json.split(',', 1)[1]
    with Image.open(BytesIO(base64.b64decode(b64_json))) as image:
        image = image.convert('RGB')
        image.save(output_path, format='PNG')
        return image.size


def main():
    parser = argparse.ArgumentParser(description='通过统一AI组件测试封面生图接口')
    parser.add_argument('--config', default='configs/global.yml', help='全局配置文件路径')
    parser.add_argument('--base-url', help='仅本次测试覆盖 DMR_AI_BASE_URL')
    parser.add_argument('--api-key', help='仅本次测试覆盖 DMR_AI_API_KEY；不建议写入命令历史')
    parser.add_argument('--size', default='1536x1024', help='生图尺寸')
    parser.add_argument('--prompt', default='Draw a simple red circle on a white background. No text.')
    parser.add_argument('--reference-image', help='可选参考图路径')
    parser.add_argument('--output', help='输出图片路径，默认写入 .temp')
    args = parser.parse_args()

    if args.base_url:
        os.environ['DMR_AI_BASE_URL'] = args.base_url
    if args.api_key:
        os.environ['DMR_AI_API_KEY'] = args.api_key
    config = Config(args.config)
    client = AIClient(config.get_config('ai'))
    if not client.available:
        raise RuntimeError(client.availability()['reason'])

    output_path = Path(args.output or f'.temp/test-ai-cover-{args.size.replace("x", "_")}.png')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f'POST {client.base_url}/v1/responses')
    b64_json = client.generate_image(args.prompt, args.size, reference_image=args.reference_image)
    image_size = save_image(b64_json, output_path)
    print(f'image_size={image_size[0]}x{image_size[1]}')
    print(f'saved={output_path}')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
