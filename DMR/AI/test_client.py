import os
import unittest
from unittest.mock import Mock, patch

from DMR.AI import AIClient
from DMR.Config import Config


class AIClientTests(unittest.TestCase):
    def config(self):
        return {
            'base_url': 'https://yaml.example', 'api_key': 'yaml-key', 'timeout': 30,
            'capabilities': {
                'cover_analysis': {'model': 'text-model', 'max_tokens': 123},
                'image_generation': {'model': 'image-model', 'retries': 1},
            },
        }

    def test_fixed_environment_variables_override_yaml(self):
        with patch.dict(os.environ, {
            'DMR_AI_BASE_URL': 'https://env.example/', 'DMR_AI_API_KEY': 'env-key',
        }, clear=False):
            client = AIClient(self.config())
        self.assertEqual('https://env.example', client.base_url)
        self.assertEqual('env-key', client.api_key)

    def test_chat_uses_capability_model(self):
        response = Mock(text='')
        response.raise_for_status.return_value = None
        response.json.return_value = {'choices': [{'message': {'content': 'result'}}]}
        session = Mock()
        session.post.return_value = response
        with patch.dict(os.environ, {}, clear=True):
            client = AIClient(self.config(), session=session)
        self.assertEqual('result', client.chat('cover_analysis', [{'role': 'user', 'content': 'x'}]))
        request = session.post.call_args
        self.assertEqual('https://yaml.example/v1/chat/completions', request.args[0])
        self.assertEqual('text-model', request.kwargs['json']['model'])
        self.assertEqual(123, request.kwargs['json']['max_tokens'])

    def test_image_response_is_parsed_by_shared_client(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {'data': [{'b64_json': 'a' * 200}]}
        session = Mock()
        session.post.return_value = response
        with patch.dict(os.environ, {}, clear=True):
            client = AIClient(self.config(), session=session)
        self.assertEqual('a' * 200, client.generate_image('prompt', '1024x1024'))
        self.assertEqual('https://yaml.example/v1/responses', session.post.call_args.args[0])

    def test_config_rejects_custom_env_variable_names(self):
        with self.assertRaisesRegex(ValueError, 'api_key_env'):
            Config._validate_local_ai_transport({'ai': {'api_key_env': 'CUSTOM_KEY'}})

    def test_config_loads_project_root_dotenv_without_override(self):
        with patch('DMR.Config.load_dotenv') as loader:
            Config('configs/global.yml')
        path = loader.call_args.args[0]
        self.assertEqual('.env', path.name)
        self.assertEqual(False, loader.call_args.kwargs['override'])


if __name__ == '__main__':
    unittest.main()
