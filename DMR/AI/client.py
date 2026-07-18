import base64
import json
import os
import time

import requests


class AIClientError(RuntimeError):
    pass


class AIClient:
    """OpenAI-compatible client shared by all DMR AI capabilities."""

    BASE_URL_ENV = 'DMR_AI_BASE_URL'
    API_KEY_ENV = 'DMR_AI_API_KEY'

    def __init__(self, config=None, session=None):
        self.config = config or {}
        self.base_url = (os.getenv(self.BASE_URL_ENV) or self.config.get('base_url') or '').rstrip('/')
        self.api_key = os.getenv(self.API_KEY_ENV) or self.config.get('api_key') or ''
        self.timeout = int(self.config.get('timeout', 120) or 120)
        self.capabilities = self.config.get('capabilities') or {}
        self.session = session or requests.Session()

    @property
    def available(self):
        return bool(self.base_url and self.api_key)

    def availability(self):
        missing = []
        if not self.base_url:
            missing.append(f'缺少 {self.BASE_URL_ENV} 或 ai.base_url')
        if not self.api_key:
            missing.append(f'缺少 {self.API_KEY_ENV} 或 ai.api_key')
        return {'available': not missing, 'reason': '；'.join(missing)}

    def _capability(self, name):
        return dict(self.capabilities.get(name) or {})

    def _url(self, path):
        if not self.base_url:
            raise AIClientError(f'未配置 {self.BASE_URL_ENV} 或 ai.base_url')
        return f'{self.base_url}{path}'

    def _headers(self):
        if not self.api_key:
            raise AIClientError(f'未配置 {self.API_KEY_ENV} 或 ai.api_key')
        return {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}

    def chat(self, capability, messages, **overrides):
        options = self._capability(capability)
        options.update({key: value for key, value in overrides.items() if value is not None})
        model = options.pop('model', None)
        if not model:
            raise AIClientError(f'AI capability {capability} 未配置 model')
        timeout = int(options.pop('timeout', self.timeout) or self.timeout)
        payload = {'model': model, 'messages': messages}
        payload.update(options)
        response = self.session.post(
            self._url('/v1/chat/completions'), headers=self._headers(), json=payload, timeout=timeout,
        )
        try:
            response.raise_for_status()
            content = response.json().get('choices', [{}])[0].get('message', {}).get('content', '')
        except Exception as error:
            body = getattr(response, 'text', '')[:1000]
            raise AIClientError(f'AI文本请求失败: {error}; response_body={body}') from error
        if not content:
            raise AIClientError('AI文本接口没有返回内容')
        return content

    @staticmethod
    def _image_data_url(path):
        with open(path, 'rb') as file:
            value = base64.b64encode(file.read()).decode('ascii')
        mime = 'image/png' if os.path.splitext(path)[1].lower() == '.png' else 'image/jpeg'
        return f'data:{mime};base64,{value}'

    @classmethod
    def _find_image_base64(cls, value):
        if isinstance(value, dict):
            if value.get('error'):
                raise AIClientError(f'AI生图接口返回错误: {value.get("error")}')
            for key in ('result', 'b64_json', 'image_base64', 'base64', 'data'):
                item = value.get(key)
                if isinstance(item, str) and len(item) > 100:
                    return item
            for item in value.values():
                found = cls._find_image_base64(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_image_base64(item)
                if found:
                    return found
        return None

    @staticmethod
    def _parse_sse(line):
        if isinstance(line, bytes):
            line = line.decode('utf-8', errors='ignore')
        line = (line or '').strip()
        if line.startswith('data:'):
            line = line[5:].strip()
        if not line or line == '[DONE]' or line.startswith(':'):
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    def generate_image(self, prompt, size, reference_image=None, **overrides):
        options = self._capability('image_generation')
        options.update({key: value for key, value in overrides.items() if value is not None})
        model = options.pop('model', None)
        if not model:
            raise AIClientError('AI capability image_generation 未配置 model')
        timeout = int(options.pop('timeout', self.timeout) or self.timeout)
        retries = max(1, int(options.pop('retries', 3) or 3))
        fallback_non_stream = bool(options.pop('fallback_non_stream', True))
        stream = bool(options.pop('stream', False))
        content = prompt
        if reference_image:
            content = [
                {'type': 'input_text', 'text': prompt},
                {'type': 'input_image', 'image_url': self._image_data_url(reference_image)},
            ]
        tool = {
            'type': 'image_generation', 'size': size, 'output_format': 'png',
            'background': 'opaque', 'moderation': 'auto', 'partial_images': 0,
        }
        for key in ('output_format', 'output_compression', 'background', 'moderation', 'partial_images'):
            if key in options:
                tool[key] = options.pop(key)
        payload = {
            'model': model, 'stream': stream,
            'input': [{'role': 'user', 'content': content}], 'tools': [tool],
        }
        payloads = [payload]
        if stream and fallback_non_stream:
            payloads.append(dict(payload, stream=False))
        last_error = None
        for request_payload in payloads:
            for attempt in range(1, retries + 1):
                try:
                    response = self.session.post(
                        self._url('/v1/responses'), headers=self._headers(), json=request_payload,
                        timeout=timeout, stream=bool(request_payload['stream']),
                    )
                    response.raise_for_status()
                    if request_payload['stream']:
                        for line in response.iter_lines():
                            event = self._parse_sse(line)
                            if event:
                                found = self._find_image_base64(event)
                                if found:
                                    return found
                        raise AIClientError('AI生图接口没有返回有效图片')
                    found = self._find_image_base64(response.json())
                    if found:
                        return found
                    raise AIClientError('AI生图响应中没有图片数据')
                except Exception as error:
                    last_error = error if isinstance(error, AIClientError) else AIClientError(str(error))
                    if attempt < retries:
                        time.sleep(min(2 ** (attempt - 1), 8))
        raise last_error or AIClientError('AI生图请求未执行')
