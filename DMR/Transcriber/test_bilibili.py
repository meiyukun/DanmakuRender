import os
import tempfile
import unittest
import json
import time
import threading
from types import SimpleNamespace
from unittest.mock import patch

from DMR.Transcriber.bilibili import BilibiliTranscriber, SubtitleUnavailableError


class BilibiliTranscriberTest(unittest.TestCase):
    def test_subtitle_json_to_srt(self):
        data = {
            'body': [
                {'from': 1.23, 'to': 4.56, 'content': '第一句'},
                {'from': 3661.001, 'to': 3662.2, 'content': '第二句'},
            ]
        }
        result = BilibiliTranscriber._to_srt(data)
        self.assertIn('00:00:01,230 --> 00:00:04,560', result)
        self.assertIn('01:01:01,001 --> 01:01:02,200', result)
        self.assertIn('第一句', result)

    def test_prefers_ready_ai_chinese_subtitle(self):
        selected = BilibiliTranscriber._select_subtitle([
            {'lan': 'zh-CN', 'subtitle_url': '//manual', 'ai_status': None},
            {'lan': 'ai-zh', 'subtitle_url': '//pending', 'ai_status': 1},
            {'lan': 'ai-zh', 'subtitle_url': '//ready', 'ai_status': 2},
        ])
        self.assertEqual('//ready', selected['subtitle_url'])

    def test_atomic_write_replaces_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'test.srt')
            BilibiliTranscriber._atomic_write(path, 'content')
            with open(path, encoding='utf-8') as file:
                self.assertEqual('content', file.read())
            self.assertFalse(os.path.exists(path + '.tmp'))

    def test_rejects_expired_cookie_before_interactive_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'login.json')
            with open(path, 'w', encoding='utf-8') as file:
                json.dump({'cookie_info': {'cookies': [
                    {'name': 'SESSDATA', 'value': 'secret', 'expires': time.time() - 1},
                    {'name': 'bili_jct', 'value': 'csrf'},
                ]}}, file)
            with self.assertRaisesRegex(RuntimeError, '已过期'):
                BilibiliTranscriber._validate_cookie_file({'cookies': path})

    def test_same_group_reuses_uploader_for_multiple_parts(self):
        BilibiliTranscriber._pool.clear()
        fake = object()
        upload = {'cookies': os.path.abspath('same-account.json'), 'account': 'test', 'limit': 1}
        engine = BilibiliTranscriber(group_segments=True)
        with patch('DMR.Transcriber.bilibili.BiliWebApi', return_value=fake) as constructor:
            first, _, first_pooled = engine._get_uploader(upload, SimpleNamespace(group_id='live'))
            second, _, second_pooled = engine._get_uploader(upload, SimpleNamespace(group_id='live'))
        self.assertIs(first, second)
        self.assertIs(first, fake)
        self.assertTrue(first_pooled and second_pooled)
        constructor.assert_called_once()
        BilibiliTranscriber._pool.clear()

    def test_accessible_video_without_subtitle_requests_reupload(self):
        engine = BilibiliTranscriber(
            poll_interval_ratio=0, min_poll_interval=1, max_poll_interval=1,
            poll_timeout=60, reupload_after_ratio=0,
            min_reupload_after=10, max_reupload_after=10,
        )
        view = {'code': 0, 'data': {'aid': 1, 'pages': [{'cid': 2, 'part': 'part'}]}}
        player = {'code': 0, 'data': {'subtitle': {'subtitles': []}}}
        with patch.object(engine, '_get_json', side_effect=[view, player]), \
                patch('DMR.Transcriber.bilibili.time.time', side_effect=[0, 0, 0, 11]):
            with self.assertRaises(SubtitleUnavailableError):
                engine._wait_for_subtitle('BV1', 'part', 120, object())

    def test_transcribe_reuploads_after_missing_subtitle(self):
        class FakeUploader:
            def __init__(self):
                self.calls = 0
                self.videos = SimpleNamespace(videos=[])
                self._session = SimpleNamespace(cookies={})

            def upload(self, files, **kwargs):
                self.calls += 1
                self.videos.videos.append({'title': os.path.splitext(os.path.basename(files[0].path))[0]})
                return True, 'BV-retry'

        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'video.flv')
            open(source, 'wb').close()
            proxies = [os.path.join(directory, 'attempt-1.mp4'), os.path.join(directory, 'attempt-2.mp4')]
            fake = FakeUploader()
            engine = BilibiliTranscriber(max_reuploads=1, keep_raw_json=False)

            def make_proxy(_, path, duration):
                with open(path, 'wb') as file:
                    file.write(b'proxy')

            with patch.object(engine, '_validate_cookie_file'), \
                    patch.object(engine, '_get_uploader', return_value=(fake, threading.Lock(), True)), \
                    patch.object(engine, '_make_proxy', side_effect=make_proxy), \
                    patch.object(engine, '_wait_for_subtitle', side_effect=[
                        SubtitleUnavailableError('missing'),
                        ({'body': [{'from': 0, 'to': 1, 'content': 'ok'}]}, 1, 2),
                    ]), \
                    patch('DMR.Transcriber.bilibili.get_tempfile', side_effect=proxies):
                status, result = engine.transcribe(SimpleNamespace(
                    path=source, duration=120, group_id='group', resolution=None,
                ))

            self.assertTrue(status)
            self.assertEqual(2, fake.calls)
            self.assertEqual(2, result['upload_attempts'])
            self.assertTrue(os.path.exists(os.path.splitext(source)[0] + '.srt'))
            self.assertFalse(any(os.path.exists(path) for path in proxies))

    def test_transcribe_reuses_subtitle_from_existing_upload_without_proxy(self):
        fake_uploader = SimpleNamespace(
            _session=SimpleNamespace(cookies={'SESSDATA': 'ready'}),
            stop=lambda: None,
        )
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'video.flv')
            open(source, 'wb').close()
            engine = BilibiliTranscriber(
                account='bilibili', keep_raw_json=False,
                existing_upload={'bvid': 'BV-existing', 'part_title': 'video'},
            )
            with patch.object(engine, '_validate_cookie_file'), \
                    patch.object(engine, '_wait_for_subtitle', return_value=(
                        {'body': [{'from': 0, 'to': 1, 'content': '已有字幕'}]}, 11, 22,
                    )) as wait, \
                    patch.object(engine, '_get_uploader') as upload_proxy, \
                    patch('DMR.Transcriber.bilibili.BiliWebApi', return_value=fake_uploader):
                status, result = engine.transcribe(SimpleNamespace(path=source, duration=60))
            self.assertTrue(status)
            self.assertTrue(result['reused_existing_upload'])
            self.assertEqual(0, result['upload_attempts'])
            wait.assert_called_once()
            upload_proxy.assert_not_called()
            with open(os.path.splitext(source)[0] + '.srt', encoding='utf-8') as file:
                self.assertIn('已有字幕', file.read())


if __name__ == '__main__':
    unittest.main()
