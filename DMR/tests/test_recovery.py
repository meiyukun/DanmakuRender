import json
import os
import queue
import tempfile
import unittest
from datetime import datetime

from DMR.Render import Render
from DMR.Task.liveevents import LiveEvents
from DMR.Task.replaytask import ReplayTask
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
