import json
import os
import queue
import tempfile
import time
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image

from DMR.WebService.webapi import WebApi


class WebApiUploadTests(unittest.TestCase):
    def make_api(self, engine=None, ai_client=None):
        send_queue = queue.Queue()
        api = WebApi((send_queue, queue.Queue()), engine=engine, force_login=False,
                     ai_client=ai_client)
        return api, send_queue, api.create_app().test_client()

    def test_upload_center_template_renders(self):
        _, _, client = self.make_api()
        response = client.get('/uploads')
        self.assertEqual(200, response.status_code)
        self.assertIn('视频上传中心'.encode('utf-8'), response.data)
        self.assertIn('追加到已有视频'.encode('utf-8'), response.data)

    def test_upload_seasons_endpoint_uses_selected_account(self):
        api, _, client = self.make_api()
        api.get_upload_account_seasons = Mock(return_value=[{'id': 123, 'name': '直播合集'}])
        response = client.get('/api/upload/seasons?account=account')
        self.assertEqual(200, response.status_code)
        self.assertEqual([{'id': 123, 'name': '直播合集'}], response.get_json()['seasons'])
        api.get_upload_account_seasons.assert_called_once_with('account')

    def test_upload_submissions_endpoint_uses_selected_account(self):
        api, _, client = self.make_api()
        api.get_upload_account_archives = Mock(return_value=[{'bvid': 'BV123', 'title': '最近投稿'}])
        response = client.get('/api/upload/submissions?account=account')
        self.assertEqual(200, response.status_code)
        self.assertEqual('BV123', response.get_json()['submissions'][0]['bvid'])
        api.get_upload_account_archives.assert_called_once_with('account')

    def test_upload_submission_detail_returns_editable_metadata_and_season(self):
        api, _, client = self.make_api()
        api.get_upload_account_archive_detail = Mock(return_value={
            'bvid': 'BV123', 'title': '原稿标题', 'desc': '原稿简介',
            'dynamic': '原稿动态', 'season_id': 9, 'season_title': '直播切片',
        })
        response = client.get('/api/upload/submission?account=account&bvid=BV123')
        self.assertEqual(200, response.status_code)
        self.assertEqual('原稿标题', response.get_json()['submission']['title'])
        self.assertEqual(9, response.get_json()['submission']['season_id'])
        api.get_upload_account_archive_detail.assert_called_once_with('account', 'BV123')

    def test_recent_archives_are_normalized_and_sorted(self):
        api, _, _ = self.make_api()
        api.get_upload_account_files = Mock(return_value={'account': 'account.json'})
        response = {'code': 0, 'data': {'arc_audits': [
            {'Archive': {'bvid': 'BVold', 'title': '较早', 'ptime': 10, 'duration': 20}},
            {'Archive': {'bvid': 'BVnew', 'title': '最新', 'ptime': 20, 'duration': 30}},
        ]}}
        with patch('DMR.Uploader.biliapi.biliapi.get_archives', return_value=response):
            archives = api.get_upload_account_archives('account')
        self.assertEqual(['BVnew', 'BVold'], [item['bvid'] for item in archives])

    def test_archive_detail_normalizes_form_fields_and_collection(self):
        api, _, _ = self.make_api()
        api.get_upload_account_files = Mock(return_value={'account': 'account.json'})
        archive_response = {'code': 0, 'data': {
            'archive': {
                'bvid': 'BV123', 'title': '原标题', 'desc': '原简介', 'dynamic': '原动态',
                'tag': '直播,切片', 'tid': 171, 'copyright': 1, 'source': '',
                'is_only_self': 0, 'attrs': {'is_dolby': 0},
            },
            'subtitle': {'allow': True},
        }}
        membership = {
            'season_id': 9, 'season_title': '热点合集', 'section_title': '七月',
            'episode_title': '原标题',
        }
        with patch('DMR.Uploader.biliapi.biliapi.get_archive_view', return_value=archive_response), \
                patch('DMR.Uploader.biliapi.bili_section.find_video_season', return_value=membership):
            detail = api.get_upload_account_archive_detail('account', 'BV123')
        self.assertEqual('原标题', detail['title'])
        self.assertEqual('原动态', detail['dynamic'])
        self.assertEqual(9, detail['season_id'])
        self.assertEqual('七月', detail['section_title'])

    def test_upload_media_preview_only_serves_library_files(self):
        api, _, client = self.make_api()
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.realpath(os.path.join(temp, 'preview.mp4'))
            with open(path, 'wb') as file:
                file.write(b'preview')
            api._upload_media_paths = Mock(return_value={path})
            response = client.get('/api/upload/media', query_string={'path': path})
            self.assertEqual(200, response.status_code)
            self.assertEqual('video/mp4', response.content_type)
            response.close()
        api._upload_media_paths.assert_called_once_with(refresh=False)

    def test_upload_options_load_independently_from_media_library(self):
        api, _, client = self.make_api()
        api.get_upload_accounts = Mock(return_value=['account'])
        api.get_upload_defaults = Mock(return_value={'account': 'account'})
        api.get_known_seasons = Mock(return_value=[])
        response = client.get('/api/upload/options')
        self.assertEqual(200, response.status_code)
        self.assertEqual(['account'], response.get_json()['accounts'])

    def test_manual_cover_upload_is_normalized_and_tokenized(self):
        api, _, client = self.make_api()
        source = BytesIO()
        Image.new('RGB', (900, 900), 'red').save(source, 'PNG')
        source.seek(0)
        response = client.post('/api/upload/cover/file', data={
            'cover': (source, 'cover.png'),
        }, content_type='multipart/form-data')
        self.assertEqual(200, response.status_code)
        token = response.get_json()['token']
        record = api._resolve_cover_token(token)
        self.assertIsNotNone(record)
        with Image.open(record['path']) as image:
            self.assertEqual((1280, 800), image.size)
            self.assertEqual('JPEG', image.format)
        os.remove(record['path'])

    def test_append_cover_requires_explicit_replace_confirmation(self):
        api, send_queue, client = self.make_api()
        with tempfile.TemporaryDirectory() as temp:
            video = os.path.realpath(os.path.join(temp, 'part.mp4'))
            with open(video, 'wb') as file:
                file.write(b'video')
            token = 'cover-token'
            cover = os.path.join(api.cover_artifact_dir, 'draft_test-cover-token.jpg')
            Image.new('RGB', (1280, 800), 'blue').save(cover, 'JPEG')
            api._register_cover_token(token, cover, 'uploaded')
            api._upload_media_paths = Mock(return_value={video})
            api.get_upload_accounts = Mock(return_value=['account'])
            api.get_upload_defaults = Mock(return_value={'account': 'account'})
            payload = {
                'parts': [{'path': video, 'title': '追加片段'}], 'mode': 'append',
                'bvid': 'BV123abc', 'account': 'account', 'tid': 21, 'copyright': 1,
                'title': '原稿标题', 'submission_loaded_bvid': 'BV123abc',
                'cover_token': token,
            }
            with patch('DMR.WebService.webapi.FFprobe.get_duration', return_value=12):
                denied = client.post('/api/upload/submit', json=payload)
                self.assertEqual(400, denied.status_code)
                payload['cover_replace_confirmed'] = True
                accepted = client.post('/api/upload/submit', json=payload)
            self.assertEqual(200, accepted.status_code)
            message = send_queue.get_nowait()
            self.assertTrue(message.data['args']['cover_required'])
            self.assertEqual(1, len(message.data['managed_artifacts']))
            for path in message.data['managed_artifacts']:
                if os.path.isfile(path):
                    os.remove(path)
            if os.path.isfile(cover):
                os.remove(cover)

    def test_hotspot_cover_context_prefers_reference_clip(self):
        api, _, _ = self.make_api()
        first = os.path.realpath('first.mp4')
        second = os.path.realpath('second.mp4')
        api.get_highlight_results = Mock(return_value=[{'manifest': 'manifest.json'}])
        api._read_highlight_manifest = Mock(return_value={
            'streamer_name': '测试主播',
            'analysis': {'candidates': [
                {'id': 'one', 'start': 1, 'end': 3, 'representative': ['普通热点']},
                {'id': 'two', 'start': 4, 'end': 8, 'representative': ['参考热点'],
                 'subtitle_excerpt': [{'start': 5, 'end': 6, 'text': '我怎么可能在这里输'}]},
            ]},
            'clips': [
                {'id': 'one', 'path': first, 'title': '第一段'},
                {'id': 'two', 'path': second, 'title': '第二段'},
            ],
        })
        context = api._cover_hotspot_context([first, second], reference_path=second)
        self.assertLess(context.index('第二段'), context.index('第一段'))
        self.assertIn('主播：测试主播', context)
        self.assertIn('附近字幕正文：[5.0s] 我怎么可能在这里输', context)

    def test_old_highlight_manifest_reuses_retained_job_subtitle(self):
        api, _, _ = self.make_api()
        with tempfile.TemporaryDirectory() as temp:
            subtitle = os.path.join(temp, 'part.srt')
            with open(subtitle, 'w', encoding='utf-8') as file:
                file.write('1\n00:00:01,000 --> 00:00:02,000\n惊人原话\n')
            video = SimpleNamespace(duration=10, path=os.path.join(temp, 'part.mp4'),
                                    streamer=SimpleNamespace(name='旧任务主播'))
            highlight_task = SimpleNamespace(jobs={'job': {
                'manifest': 'manifest.json',
                'segments': [{'segment_id': 1, 'video': video, 'subtitle': subtitle}],
            }})
            api.engine = SimpleNamespace(task_dict={
                'highlight/test': {'task_type': 'highlight', 'class': highlight_task},
            })
            streamer, lines = api._highlight_source_text(
                {'source_task': '备用名称'}, 'manifest.json',
            )
        self.assertEqual('旧任务主播', streamer)
        self.assertEqual('惊人原话', lines[0]['text'])

    def test_cover_prompt_is_returned_before_image_generation(self):
        ai_client = Mock(available=True)
        ai_client.chat.return_value = json.dumps({
            'title': 'AI投稿标题', 'desc': 'AI投稿简介', 'dynamic': 'AI动态文案',
            'cover_prompt': '模型生成、允许用户编辑的完整提示词',
        }, ensure_ascii=False)
        api, _, _ = self.make_api(ai_client=ai_client)
        api._cover_hotspot_context = Mock(return_value='热点资料')
        api.cover_jobs['job'] = {
            'type': 'prompt', 'status': 'queued', 'error': '', 'token': None, 'prompt': '',
        }
        api._run_cover_prompt_generation(
            'job', ['video.mp4'], '投稿标题', '原简介', '原动态', '补充要求', None,
        )
        self.assertEqual('completed', api.cover_jobs['job']['status'])
        self.assertEqual('模型生成、允许用户编辑的完整提示词', api.cover_jobs['job']['prompt'])
        self.assertEqual({
            'title': 'AI投稿标题', 'desc': 'AI投稿简介', 'dynamic': 'AI动态文案',
        }, api.cover_jobs['job']['metadata'])
        prompt_messages = ai_client.chat.call_args.args[1]
        self.assertIn('热点直播切片', prompt_messages[1]['content'])
        self.assertIn('禁止“高能混剪', prompt_messages[1]['content'])
        self.assertIn('语出惊人', prompt_messages[1]['content'])
        self.assertIn('具体、短、像人写的', prompt_messages[0]['content'])
        ai_client.generate_image.assert_not_called()

    def test_cover_image_endpoint_requires_confirmed_prompt(self):
        ai_client = Mock(available=True)
        api, _, client = self.make_api(ai_client=ai_client)
        path = os.path.realpath('video.mp4')
        api._upload_media_paths = Mock(return_value={path})
        response = client.post('/api/upload/cover/generate', json={'paths': [path]})
        self.assertEqual(400, response.status_code)
        self.assertIn('确认完整生图提示词', response.get_json()['message'])

    def test_upload_library_supports_paging_task_kind_and_modified_sort(self):
        api, _, _ = self.make_api()
        api.get_upload_library = Mock(return_value=[
            {'path': 'a-old.mp4', 'taskname': 'A', 'kind': 'src_video', 'title': 'old', 'group': 'A', 'modified_ts': 1},
            {'path': 'a-new.mp4', 'taskname': 'A', 'kind': 'dm_video', 'title': 'new', 'group': 'A', 'modified_ts': 3},
            {'path': 'a-mid.mp4', 'taskname': 'A', 'kind': 'highlight_clip', 'title': 'mid', 'group': 'A', 'modified_ts': 2},
            {'path': 'b.mp4', 'taskname': 'B', 'kind': 'src_video', 'title': 'b', 'group': 'B', 'modified_ts': 4},
        ])
        page = api.query_upload_library(page=1, page_size=2, taskname='A', sort='modified_desc')
        self.assertEqual(3, page['total'])
        self.assertEqual(2, page['pages'])
        self.assertEqual(['a-new.mp4', 'a-mid.mp4'], [item['path'] for item in page['items']])
        clips = api.query_upload_library(page=1, page_size=24, taskname='A', kind='highlight_clip')
        self.assertEqual(['a-mid.mp4'], [item['path'] for item in clips['items']])
        self.assertEqual({'A': 3, 'B': 1}, {item['name']: item['count'] for item in page['tasknames']})

    def test_upload_library_reuses_short_lived_index(self):
        api, _, _ = self.make_api()
        api._build_upload_library = Mock(return_value=[])
        api.get_upload_library()
        api.get_upload_library()
        api._build_upload_library.assert_called_once_with()

    def test_account_seasons_are_normalized_from_biliapi(self):
        api, _, _ = self.make_api()
        api.get_upload_account_files = Mock(return_value={'account': 'account.json'})
        response = {'code': 0, 'data': {'seasons': [
            {'season': {'id': 2, 'title': '第二个合集'}},
            {'season': {'id': 1, 'title': '第一个合集'}},
            {'season': {'id': 2, 'title': '重复项'}},
        ]}}
        with patch('DMR.Uploader.biliapi.biliapi.get_seasons', return_value=response):
            seasons = api.get_upload_account_seasons('account')
        self.assertEqual([{'id': 2, 'name': '第二个合集'}, {'id': 1, 'name': '第一个合集'}], seasons)

    def test_new_multi_part_upload_is_sent_to_stateless_biliwebapi(self):
        api, send_queue, client = self.make_api()
        with tempfile.TemporaryDirectory() as temp:
            paths = [os.path.join(temp, name) for name in ('one.mp4', 'two.mp4')]
            for path in paths:
                with open(path, 'wb') as file:
                    file.write(b'video')
            real_paths = [os.path.realpath(path) for path in paths]
            api._upload_media_paths = Mock(return_value=set(real_paths))
            api.get_upload_accounts = Mock(return_value=['account'])
            api.get_upload_defaults = Mock(return_value={
                'account': 'account', 'realtime': False, 'concat_video': False,
                'season_id': None, 'section_title': '', 'episode_title': '',
            })
            scheduled_at = time.time() + 10800
            with patch('DMR.WebService.webapi.FFprobe.get_duration', return_value=12):
                response = client.post('/api/upload/submit', json={
                    'parts': [{'path': paths[0], 'title': '精彩开场'},
                              {'path': paths[1], 'title': '高能结尾'}],
                    'mode': 'new', 'title': '跨场次混剪', 'account': 'account',
                    'tid': 21, 'copyright': 1, 'season_id': 123, 'section_title': '直播回放',
                    'episode_title': '高能合集', 'scheduled_at': scheduled_at,
                })
        self.assertEqual(200, response.status_code, response.get_json())
        message = send_queue.get_nowait()
        self.assertEqual('uploader', message.target)
        self.assertEqual('biliwebapi', message.data['engine'])
        self.assertTrue(message.data['stateless'])
        self.assertEqual(real_paths, [file.path for file in message.data['files']])
        self.assertEqual(['精彩开场', '高能结尾'], [file.title for file in message.data['files']])
        self.assertEqual(123, message.data['args']['season_id'])
        self.assertEqual(int(scheduled_at), message.data['args']['scheduled_at'])

    def test_append_upload_keeps_account_visible_and_passes_bvid(self):
        api, send_queue, client = self.make_api()
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, 'part.mp4')
            with open(path, 'wb') as file:
                file.write(b'video')
            api._upload_media_paths = Mock(return_value={os.path.realpath(path)})
            api.get_upload_accounts = Mock(return_value=['account'])
            api.get_upload_defaults = Mock(return_value={
                'account': 'account', 'realtime': False, 'concat_video': False,
                'season_id': None, 'section_title': '', 'episode_title': '',
            })
            with patch('DMR.WebService.webapi.FFprobe.get_duration', return_value=12):
                response = client.post('/api/upload/submit', json={
                    'paths': [path], 'mode': 'append', 'bvid': 'BV1xx411c7mD',
                    'submission_loaded_bvid': 'BV1xx411c7mD', 'title': '原稿标题',
                    'insert_head': True, 'account': 'account',
                })
        self.assertEqual(200, response.status_code, response.get_json())
        message = send_queue.get_nowait()
        self.assertEqual('BV1xx411c7mD', message.data['args']['bvid'])
        self.assertTrue(message.data['args']['insert_head'])
        self.assertTrue(message.data['args']['update_metadata'])
        self.assertTrue(message.data['args']['sync_season'])


class WebApiHighlightLibraryTests(unittest.TestCase):
    def make_highlight_api(self, temp, clips):
        manifest_path = os.path.join(temp, 'session.highlights.json')
        with open(manifest_path, 'w', encoding='utf-8') as file:
            json.dump({'version': 2, 'source_task': 'source', 'clips': clips,
                       'outputs': [], 'versions': []}, file)
        job = {'manifest': manifest_path, 'source_task': 'source', 'manual': True,
               'config': {'encoding': {}}}
        highlight_task = SimpleNamespace(jobs={'job': job}, config={'defaults': {'encoding': {}}})
        engine = SimpleNamespace(task_dict={
            'highlight/test': {'task_type': 'highlight', 'class': highlight_task},
        })
        api = WebApi((queue.Queue(), queue.Queue()), engine=engine, force_login=False)
        return api, manifest_path, api.create_app().test_client()

    def test_highlight_workspace_renders_paging_and_lazy_filters(self):
        api = WebApi((queue.Queue(), queue.Queue()), force_login=False)
        client = api.create_app().test_client()
        response = client.get('/highlights')
        self.assertEqual(200, response.status_code)
        self.assertIn(b'id="session-pagination"', response.data)
        self.assertIn(b'id="result-review-filter"', response.data)
        self.assertIn(b'id="clip-search"', response.data)
        self.assertIn(b'id="batch-result-manager"', response.data)

    def test_highlight_results_are_sorted_newest_first(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs = {}
            for index in (1, 2):
                path = os.path.join(temp, f'{index}.highlights.json')
                with open(path, 'w', encoding='utf-8') as file:
                    json.dump({'version': 2, 'source_task': 'source', 'clips': [],
                               'outputs': [], 'versions': []}, file)
                os.utime(path, (index, index))
                jobs[str(index)] = {'manifest': path, 'source_task': 'source', 'manual': False}
            highlight_task = SimpleNamespace(jobs=jobs)
            engine = SimpleNamespace(task_dict={
                'highlight/test': {'task_type': 'highlight', 'class': highlight_task},
            })
            api = WebApi((queue.Queue(), queue.Queue()), engine=engine, force_login=False)
            results = api.get_highlight_results()
        self.assertEqual(['2', '1'], [item['display_name'] for item in results])

    def test_custom_remix_name_is_saved_in_manifest_and_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            clip_path = os.path.join(temp, 'clip.mp4')
            with open(clip_path, 'wb') as file:
                file.write(b'clip')
            api, manifest_path, client = self.make_highlight_api(temp, [
                {'id': 'clip-1', 'path': clip_path, 'duration': 12},
            ])
            with patch('DMR.Highlight.cutter.recompose_clips'):
                response = client.post('/api/highlight/recompose/multi', json={
                    'materials': [{'manifest': manifest_path, 'clip_id': 'clip-1'}],
                    'output_name': '七月爆笑/高能合集',
                })
            self.assertEqual(200, response.status_code, response.get_json())
            version = response.get_json()['version']
            self.assertEqual('七月爆笑/高能合集', version['name'])
            self.assertEqual('七月爆笑／高能合集.mp4', os.path.basename(version['path']))
            with open(manifest_path, encoding='utf-8') as file:
                self.assertEqual('七月爆笑/高能合集', json.load(file)['versions'][0]['name'])

    def test_highlight_thumbnail_is_generated_once_then_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            clip_path = os.path.join(temp, 'clip.mp4')
            with open(clip_path, 'wb') as file:
                file.write(b'clip')
            _, manifest_path, client = self.make_highlight_api(temp, [
                {'id': 'clip-1', 'path': clip_path, 'duration': 12},
            ])

            def create_thumbnail(command, **kwargs):
                with open(command[-1], 'wb') as file:
                    file.write(b'jpeg')
                return SimpleNamespace(returncode=0, stderr=b'')

            query = {'manifest': manifest_path, 'path': clip_path}
            with patch('DMR.WebService.webapi.subprocess.run', side_effect=create_thumbnail) as run:
                first = client.get('/api/highlight/thumbnail', query_string=query)
                self.assertEqual(200, first.status_code, first.get_json())
                first.close()
                second = client.get('/api/highlight/thumbnail', query_string=query)
                self.assertEqual(200, second.status_code, second.get_json())
                second.close()
            run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
