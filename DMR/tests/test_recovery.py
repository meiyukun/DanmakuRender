import json
import logging
import os
import queue
import tempfile
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from DMR.Highlight import Highlight
from DMR.Render import Render
from DMR.Uploader import Uploader
from DMR import DanmakuRender
from DMR.Task.highlighttask import HighlightTask
from DMR.Task.liveevents import LiveEvents
from DMR.Task.replaytask import ReplayTask
from DMR.WebService.webapi import WebApi, highlight_output_options, manual_highlight_output_profiles
from DMR.utils import (
    DateTimeDecoder,
    PipeMessage,
    StreamerInfo,
    VideoInfo,
    atomic_json_dump,
)


class WorkingDirectoryTestCase(unittest.TestCase):
    def setUp(self):
        self._old_cwd = os.getcwd()
        self._temp_dir = tempfile.TemporaryDirectory()
        os.chdir(self._temp_dir.name)
        os.makedirs('.temp', exist_ok=True)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._temp_dir.cleanup()


class PersistenceTests(WorkingDirectoryTestCase):
    def test_datetime_decoder_restores_nested_values(self):
        path = os.path.join('.temp', 'state.json')
        expected = datetime(2026, 7, 9, 12, 30)
        atomic_json_dump({'nested': {'ctime': expected}}, path)

        with open(path, encoding='utf-8') as f:
            restored = json.load(f, cls=DateTimeDecoder)

        self.assertEqual(restored['nested']['ctime'], expected)

    def test_uploader_completion_event_preserves_bilibili_archive_metadata(self):
        uploader = Uploader.__new__(Uploader)
        uploader.send_queue = queue.Queue()
        video = VideoInfo(path='part.mkv', ctime=datetime.now())
        uploader._send_completed_result({
            'request_id': 'upload-1', 'source': 'replay/source',
            'completion_result': 'BV123abc', 'engine': 'biliwebapi',
            'upload_group': 'group', 'args': {'account': 'archive-account'},
            'files': [video],
        })
        message = uploader.send_queue.get_nowait()
        self.assertEqual('BV123abc', message.data['result'])
        self.assertEqual('archive-account', message.data['account'])
        self.assertEqual(['part.mkv'], message.data['files'])

    def test_active_render_becomes_interrupted_after_restart(self):
        source = os.path.abspath('source.mp4')
        with open(source, 'wb') as f:
            f.write(b'video')
        video = VideoInfo(
            path=source,
            ctime=datetime.now(),
            streamer=StreamerInfo(name='tester'),
            group_id='group-1',
        )
        task = {
            'uuid': 'render-1',
            'source': 'replay/demo',
            'request_id': 'request-1',
            'mode': 'dmrender',
            'args': {},
            'video': video,
            'output': os.path.abspath('output.mp4'),
            'status': 'rendering',
            'group_id': 'group-1',
        }
        atomic_json_dump({'render-1': task}, '.temp/active_renders.json')

        renderer = Render((None, queue.Queue()))
        try:
            recovered = renderer.failed_tasks['render-1']
            self.assertEqual(recovered['status'], 'interrupted')
            self.assertIsInstance(recovered['video'], VideoInfo)
            self.assertIsInstance(recovered['video'].streamer, StreamerInfo)
        finally:
            renderer.render_executors.shutdown(wait=False)

    def test_completed_render_is_redelivered_without_rerendering(self):
        source = os.path.abspath('source.mp4')
        output = os.path.abspath('output.mp4')
        for path in (source, output):
            with open(path, 'wb') as f:
                f.write(b'video')
        video = VideoInfo(
            path=source,
            ctime=datetime.now(),
            streamer=StreamerInfo(name='tester'),
            group_id='group-1',
        )
        rendered = VideoInfo(
            path=output,
            ctime=datetime.now(),
            streamer=video.streamer,
            group_id='group-1',
        )
        task = {
            'uuid': 'render-1',
            'source': 'replay/demo',
            'request_id': 'request-1',
            'mode': 'dmrender',
            'args': {},
            'video': video,
            'output': output,
            'status': 'completion_pending',
            'completion_result': rendered,
            'group_id': 'group-1',
        }
        atomic_json_dump({'render-1': task}, '.temp/active_renders.json')

        send_queue = queue.Queue()
        renderer = Render((send_queue, queue.Queue()))
        try:
            self.assertEqual(
                renderer.failed_tasks['render-1']['status'],
                'completion_pending',
            )
            self.assertTrue(renderer.retry_task('render-1'))
            result = send_queue.get_nowait()
            self.assertEqual(result.event, 'end')
            self.assertEqual(result.data['output'].path, output)

            renderer._ack_task('request-1')
            self.assertNotIn('render-1', renderer.render_tasks)
        finally:
            renderer.render_executors.shutdown(wait=False)


class PipelineRecoveryTests(WorkingDirectoryTestCase):
    def _config(self):
        return {
            'common_event_args': {
                'auto_transcribe': False,
                'auto_transcode': False,
                'auto_render': True,
                'auto_upload': True,
                'auto_clean': False,
            },
            'render_args': {'dmrender': {}},
            'upload_args': {
                'dm_video': [{
                    'engine': 'subprocess',
                    'realtime': True,
                    'min_length': 0,
                }],
            },
            'clean_args': {},
        }

    def test_restored_render_completion_continues_to_upload(self):
        event = LiveEvents('demo', self._config())
        source = VideoInfo(
            path=os.path.abspath('source.mp4'),
            ctime=datetime.now(),
            duration=60,
            group_id='group-1',
            streamer=StreamerInfo(name='tester'),
        )
        event.state_dict = {
            'group-1': [{
                'src_video': {'status': 'ready', 'file': source, 'wait': []},
                'src_video_pre': {'status': None, 'file': None, 'wait': []},
                'dm_video': {'status': 'rendering', 'file': None, 'wait': ['render-request']},
            }],
        }
        event._save_state()

        restored = LiveEvents('demo', self._config())
        output = VideoInfo(
            path=os.path.abspath('rendered.mp4'),
            ctime=datetime.now(),
            duration=60,
            group_id='group-1',
            streamer=StreamerInfo(name='tester'),
        )
        message = type('Message', (), {
            'request_id': 'render-request',
            'data': {'output': output},
            'msg': 'done',
        })()

        result = restored.onRenderEnd(message)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].target, 'uploader')
        self.assertEqual(result[0].data['files'][0].path, output.path)

    def test_disabled_pipeline_recovery_discards_state_and_outbox(self):
        config = self._config()
        source = VideoInfo(
            path=os.path.abspath('source.mp4'), ctime=datetime.now(), duration=60,
            group_id='group-1', streamer=StreamerInfo(name='tester'),
        )
        event = LiveEvents('demo', config)
        event.state_dict = {'group-1': [{'src_video': {'status': 'ready', 'file': source, 'wait': []}}]}
        event._save_state()
        replay = ReplayTask('demo', config, (queue.Queue(), queue.Queue()))
        replay._outbox = {'request-1': {'data': {}}}
        replay._save_outbox()

        config['common_event_args']['recover_pipeline'] = False
        restored = LiveEvents('demo', config)
        disabled_replay = ReplayTask('demo', config, (queue.Queue(), queue.Queue()))

        self.assertEqual(restored.state_dict, {})
        self.assertEqual(disabled_replay._outbox, {})
        self.assertFalse(os.path.exists(restored.state_file))
        self.assertFalse(os.path.exists(disabled_replay.outbox_file))

    def test_webui_exposes_recovered_pipeline_groups(self):
        event = LiveEvents('demo', self._config())
        video = VideoInfo(
            path=os.path.abspath('source.mp4'),
            ctime=datetime.now(),
            duration=60,
            group_id='group-1',
            streamer=StreamerInfo(name='tester'),
        )
        event.state_dict = {
            'group-1': [{
                'src_video': {'status': 'ready', 'file': video, 'wait': []},
                'src_video_pre': {'status': None, 'file': None, 'wait': []},
                'dm_video': {'status': 'uploading', 'file': video, 'wait': ['upload-1']},
            }],
        }
        event.ended_dict = {'group-1': 1}
        event.recovered_group_ids = {'group-1'}

        api = WebApi.__new__(WebApi)
        api.engine = SimpleNamespace(task_dict={
            'demo': {'class': SimpleNamespace(event_class=event)},
        })
        api.logger = logging.getLogger(__name__)

        pipelines = api.get_pipeline_states()

        self.assertEqual(len(pipelines), 1)
        self.assertEqual(pipelines[0]['taskname'], 'demo')
        self.assertEqual(pipelines[0]['summary'], '处理中')
        self.assertTrue(pipelines[0]['recovered'])
        self.assertTrue(pipelines[0]['can_resume'])
        self.assertFalse(pipelines[0]['can_delete'])
        self.assertEqual(pipelines[0]['stages'][1]['waiting'], 1)

    def test_resume_recovered_pipeline_replaces_stale_upload_request(self):
        config = self._config()
        path = os.path.abspath('rendered.mp4')
        with open(path, 'wb') as file:
            file.write(b'video')
        video = VideoInfo(path=path, ctime=datetime.now(), duration=60, group_id='group-1',
                          streamer=StreamerInfo(name='tester'), dtype='dm_video')
        event = LiveEvents('demo', config)
        event.state_dict = {'group-1': [{
            'src_video': {'status': 'ready', 'file': video, 'wait': []},
            'src_video_pre': {'status': None, 'file': None, 'wait': []},
            'dm_video': {'status': 'uploading', 'file': video, 'wait': ['stale-upload']},
            'subtitle': {'status': None, 'file': None, 'wait': [], 'pending_render': None},
        }]}
        event.ended_dict = {'group-1': time.time()}
        event.recovered_group_ids = {'group-1'}

        success, reason, messages = event.resume_recovered_pipeline_state('group-1')

        self.assertTrue(success, reason)
        uploads = [message for message in messages if message.target == 'uploader']
        self.assertEqual(1, len(uploads))
        self.assertNotEqual('stale-upload', uploads[0].request_id)
        self.assertNotIn('group-1', event.recovered_group_ids)

    def test_highlight_job_blocks_idle_restart(self):
        runtime = DanmakuRender.__new__(DanmakuRender)
        highlight = SimpleNamespace(jobs={'source:group': {'status': 'analyzing'}})
        runtime.engine = SimpleNamespace(
            plugin_dict={},
            task_dict={'highlight/Highlight': {'task_type': 'highlight', 'class': highlight}},
        )

        idle, blocking = runtime._get_idle_status()

        self.assertFalse(idle)
        self.assertEqual(['热点剪辑任务 1 个'], blocking)

    def test_paused_recovered_highlight_does_not_block_idle_restart(self):
        runtime = DanmakuRender.__new__(DanmakuRender)
        highlight = SimpleNamespace(
            jobs={'source:group': {'status': 'analyzing'}},
            recovered_job_ids={'source:group'},
        )
        runtime.engine = SimpleNamespace(
            plugin_dict={},
            task_dict={'highlight/Highlight': {'task_type': 'highlight', 'class': highlight}},
        )
        idle, blocking = runtime._get_idle_status()
        self.assertTrue(idle)
        self.assertEqual([], blocking)

    def test_highlight_worker_does_not_auto_run_persisted_tasks(self):
        worker = Highlight.__new__(Highlight)
        worker.stoped = True
        worker.recv_queue = queue.Queue()
        worker.tasks = {'worker-task': {'status': 'interrupted'}}
        worker.executors = Mock()
        worker.logger = logging.getLogger(__name__)
        with patch('DMR.Highlight.threading.Thread') as thread:
            worker.start()
        thread.assert_called_once()
        worker.executors.submit.assert_not_called()

    def test_manual_highlight_task_names_include_tasks_without_sessions(self):
        api = WebApi.__new__(WebApi)
        api.engine = SimpleNamespace(task_dict={
            'replay/Empty': {'task_type': 'replay', 'name': 'Empty', 'class': SimpleNamespace()},
            'highlight/Highlight': {'task_type': 'highlight', 'name': 'Highlight', 'class': SimpleNamespace()},
        })
        self.assertEqual(['Empty'], api.get_replay_task_names())

    def test_manual_highlight_output_options_include_names_and_categories(self):
        options = highlight_output_options({'outputs': [
            {'id': 'all', 'name': '综合高能', 'categories': ['*']},
            {'id': 'funny', 'name': '纯搞笑场面', 'categories': ['funny']},
            {'name': '缺少ID'},
        ]})
        self.assertEqual([
            {'id': 'all', 'name': '综合高能', 'categories': ['*'], 'selected_by_default': True},
            {'id': 'funny', 'name': '纯搞笑场面', 'categories': ['funny'], 'selected_by_default': True},
        ], options[:2])
        self.assertFalse(next(item for item in options if item['id'] == 'skill')['selected_by_default'])
        self.assertEqual(
            {'all', 'funny', 'skill', 'absurd', 'fail', 'emotional'},
            {item['id'] for item in options},
        )

    def test_manual_profiles_do_not_mutate_automatic_output_config(self):
        target = {'outputs': [{
            'id': 'all', 'name': '综合高能', 'categories': ['*'],
            'max_clips': 12, 'max_total_duration': 300,
        }]}
        profiles = manual_highlight_output_profiles(target)
        self.assertEqual(['all'], [item['id'] for item in target['outputs']])
        self.assertEqual(
            {'all', 'funny', 'skill', 'absurd', 'fail', 'emotional'},
            {item['id'] for item in profiles},
        )
        funny = next(item for item in profiles if item['id'] == 'funny')
        self.assertEqual(8, funny['max_clips'])
        self.assertEqual(180, funny['max_total_duration'])

    def test_completed_highlight_is_hidden_from_recovery_list_and_can_be_deleted(self):
        highlight = HighlightTask.__new__(HighlightTask)
        highlight.jobs = {'source:group': {'group_id': 'group', 'status': 'completed', 'outputs': []}}
        highlight.recovered_job_ids = {'source:group'}
        highlight.logger = logging.getLogger(__name__)
        highlight._save = lambda: None
        api = WebApi.__new__(WebApi)
        api.engine = SimpleNamespace(task_dict={
            'highlight/Highlight': {
                'task_type': 'highlight', 'name': 'Highlight', 'class': highlight,
            },
        })
        api.logger = logging.getLogger(__name__)

        pipelines = api.get_pipeline_states()

        self.assertEqual([], pipelines)
        success, reason = highlight.delete_recovered_job('source:group')
        self.assertTrue(success, reason)
        self.assertNotIn('source:group', highlight.jobs)

    def test_nonterminal_recovered_highlight_is_paused_and_deletable_in_web(self):
        highlight = HighlightTask.__new__(HighlightTask)
        highlight.jobs = {'source:group': {
            'group_id': 'group', 'status': 'analyzing', 'segments': [{}], 'outputs': [],
        }}
        highlight.recovered_job_ids = {'source:group'}
        api = WebApi.__new__(WebApi)
        api.engine = SimpleNamespace(task_dict={
            'highlight/Highlight': {
                'task_type': 'highlight', 'name': 'Highlight', 'class': highlight,
            },
        })
        api.logger = logging.getLogger(__name__)
        pipelines = api.get_pipeline_states()
        self.assertEqual(1, len(pipelines))
        self.assertTrue(pipelines[0]['can_resume'])
        self.assertTrue(pipelines[0]['can_delete'])
        self.assertEqual(0, pipelines[0]['active_count'])
        self.assertIn('待恢复', pipelines[0]['summary'])

    def test_deleting_recovered_pipeline_state_removes_persisted_record(self):
        event = LiveEvents('demo', self._config())
        video = VideoInfo(
            path=os.path.abspath('source.mp4'),
            ctime=datetime.now(),
            duration=60,
            group_id='group-1',
            streamer=StreamerInfo(name='tester'),
        )
        event.state_dict = {
            'group-1': [{
                'src_video': {'status': 'ready', 'file': video, 'wait': []},
                'src_video_pre': {'status': None, 'file': None, 'wait': []},
                'dm_video': {'status': 'cleaned', 'file': video, 'wait': []},
            }],
        }
        event.ended_dict = {'group-1': 1}
        event.recovered_group_ids = {'group-1'}
        event._save_state()

        deleted, reason = event.delete_recovered_pipeline_state('group-1')

        self.assertTrue(deleted, reason)
        self.assertNotIn('group-1', event.state_dict)
        self.assertNotIn('group-1', event.ended_dict)
        self.assertNotIn('group-1', event.recovered_group_ids)
        self.assertFalse(os.path.exists(event.state_file))

    def test_load_removes_ended_group_without_follow_up_actions(self):
        config = self._config()
        config['common_event_args']['auto_clean'] = True
        config['clean_args'] = {'dm_video': [{}]}
        event = LiveEvents('demo', config)
        video = VideoInfo(
            path=os.path.abspath('source.mp4'),
            ctime=datetime.now(),
            duration=60,
            group_id='group-1',
            streamer=StreamerInfo(name='tester'),
        )
        event.state_dict = {
            'group-1': [{
                'src_video': {'status': 'ready', 'file': video, 'wait': []},
                'src_video_pre': {'status': None, 'file': None, 'wait': []},
                'dm_video': {'status': 'cleaned', 'file': video, 'wait': []},
            }],
        }
        event.ended_dict = {'group-1': time.time()}
        event._save_state()

        restored = LiveEvents('demo', config)

        self.assertEqual(restored.state_dict, {})
        self.assertEqual(restored.ended_dict, {})
        self.assertFalse(os.path.exists(restored.state_file))

    def test_load_keeps_ended_group_with_pending_upload(self):
        config = self._config()
        event = LiveEvents('demo', config)
        video = VideoInfo(
            path=os.path.abspath('rendered.mp4'),
            ctime=datetime.now(),
            duration=60,
            group_id='group-1',
            streamer=StreamerInfo(name='tester'),
        )
        event.state_dict = {
            'group-1': [{
                'src_video': {'status': None, 'file': None, 'wait': []},
                'src_video_pre': {'status': None, 'file': None, 'wait': []},
                'dm_video': {'status': 'ready', 'file': video, 'wait': []},
            }],
        }
        event.ended_dict = {'group-1': time.time()}
        event._save_state()

        restored = LiveEvents('demo', config)

        self.assertIn('group-1', restored.state_dict)
        self.assertIn('group-1', restored.recovered_group_ids)

    def test_outbox_survives_restart_until_plugin_accepts_task(self):
        send_queue = queue.Queue()
        replay = ReplayTask('demo', self._config(), (send_queue, queue.Queue()))
        video = VideoInfo(
            path=os.path.abspath('source.mp4'),
            ctime=datetime.now(),
            duration=60,
            group_id='group-1',
            streamer=StreamerInfo(name='tester'),
        )
        replay._pipeSend(PipeMessage(
            source='demo',
            target='render',
            event='newtask',
            request_id='request-1',
            data={'video': video, 'files': [video]},
        ))

        restored = ReplayTask('demo', self._config(), (queue.Queue(), queue.Queue()))
        persisted = restored._outbox['request-1']
        self.assertIsInstance(persisted['data']['video'], VideoInfo)
        self.assertIsInstance(persisted['data']['files'][0], VideoInfo)

        restored._accept_outbox_message('request-1')
        self.assertEqual(restored._outbox, {})


if __name__ == '__main__':
    unittest.main()
