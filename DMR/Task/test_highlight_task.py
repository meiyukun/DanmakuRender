import os
import queue
import logging
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from DMR.Config import Config
from DMR.Task.highlighttask import HighlightTask
from DMR.Task.liveevents import LiveEvents
from DMR.utils import PipeMessage, VideoInfo


class HighlightTaskTests(unittest.TestCase):
    def test_automatic_and_manual_runs_both_use_session_directories(self):
        task = HighlightTask.__new__(HighlightTask)
        task.taskname = 'Highlight'
        task._worker_recv = queue.Queue()
        task._save = Mock()
        base = {'source_task': 'Test', 'group_id': 'group', 'status': 'preparing',
                'request_id': 'request', 'segments': [{'segment_id': 1, 'video': VideoInfo(
                    path=os.path.join('live', 'source.mp4'), ctime=datetime.now(), duration=60)}],
                'source_segment_count': 1, 'ignored_segments': [], 'dependencies': {},
                'config': {'source': {'subtitle': 'available'}, 'encoding': {'output_dir': 'outputs'},
                           'analysis': {}, 'outputs': []}}
        automatic = dict(base, manual=False, run_id=None)
        task._dispatch_if_ready('auto', automatic)
        auto_message = task._worker_recv.get_nowait()
        self.assertTrue(auto_message.data['args']['output_dir'].endswith(os.path.join('group-automatic')))
        manual = dict(base, manual=True, run_id='run')
        task._dispatch_if_ready('manual', manual)
        manual_message = task._worker_recv.get_nowait()
        self.assertTrue(manual_message.data['args']['output_dir'].endswith(os.path.join('group-manual-run')))

    def test_completed_highlight_result_can_be_removed_by_manifest(self):
        task = HighlightTask.__new__(HighlightTask)
        task.jobs = {'job': {'status': 'completed', 'manifest': os.path.abspath('result.highlights.json')}}
        task.recovered_job_ids = {'job'}
        task._save = Mock()
        task.logger = Mock()
        success, reason = task.delete_completed_job_by_manifest(os.path.abspath('result.highlights.json'))
        self.assertTrue(success, reason)
        self.assertNotIn('job', task.jobs)
        self.assertNotIn('job', task.recovered_job_ids)
        task._save.assert_called_once()

    def test_live_event_registry_can_be_built_after_highlight_decoupling(self):
        with tempfile.TemporaryDirectory() as temp:
            event = LiveEvents('registry', {'common_event_args': {'auto_upload': False, 'auto_clean': False}})
            event.state_file = os.path.join(temp, 'state.json')
            self.assertIn('downloader/liveend', event.event_dict)
            self.assertIn('highlight/subscribe', event.event_dict)
            self.assertIs(event.event_dict['tick'].__func__, event.onTick.__func__)

    def test_empty_internal_event_does_not_emit_info_log(self):
        event = LiveEvents('empty-log', {'common_event_args': {'auto_upload': False, 'auto_clean': False}})
        with self.assertLogs(event.logger, level='DEBUG') as captured:
            event.defaultEvent(PipeMessage('task', 'replay/empty-log', 'accepted', msg=''))
        self.assertFalse(any('INFO' in line for line in captured.output))
        self.assertIn('收到无文本事件', captured.output[0])

    def test_config_classifies_dmh_and_deep_merges_target(self):
        config = Config('configs/global.yml')
        self.assertIn('Highlight', config.get_highlighttasks())
        target = config.get_highlight_config('Highlight')['targets']['Test']
        self.assertEqual('src_video', target['source']['video'])
        self.assertEqual(45, target['analysis']['detection']['max_clip_seconds'])
        self.assertEqual(10, target['analysis']['detection']['pre_roll_seconds'])
        self.assertGreater(target['analysis']['detection']['boundary_end_quantile'], 0)
        self.assertLessEqual(target['analysis']['detection']['boundary_end_quantile'], 1)
        self.assertEqual('biliwebapi', target['upload']['common']['engine'])

    def test_subscription_hold_and_release(self):
        event = LiveEvents('source', {'common_event_args': {'auto_upload': False, 'auto_clean': False}})
        event.onHighlightSubscribe(PipeMessage('highlight/cutter', 'replay/source', 'highlight/subscribe',
                                               data={'highlight_task': 'cutter'}))
        self.assertIn('cutter', event.highlight_subscribers)
        event.highlight_holds['group'] = {'cutter'}
        event.onHighlightRelease(PipeMessage('highlight/cutter', 'replay/source', 'highlight/release',
                                             data={'highlight_task': 'cutter', 'group_id': 'group'}))
        self.assertNotIn('group', event.highlight_holds)

    def test_duplicate_source_notification_is_idempotent(self):
        config = {'runtime': {}, 'targets': {'source': {'enabled': True, 'source': {}, 'analysis': {},
                                                        'encoding': {}, 'outputs': [], 'upload': {'enabled': False}}}}
        with tempfile.TemporaryDirectory() as temp, patch('DMR.Task.highlighttask.os.path.join', side_effect=os.path.join):
            task = HighlightTask('test', config, (queue.Queue(), queue.Queue()))
            task.state_file = os.path.join(temp, 'state.json')
            task._worker_recv = queue.Queue()
            message = PipeMessage('replay/source', 'highlight/test', 'source_ready',
                                  data={'source_task': 'source', 'group_id': 'group',
                                        'session_ended': True, 'segment_count': 1,
                                        'video_states': [{'src_video': {'file': VideoInfo(
                                            path='missing.mp4', ctime=datetime.now(), group_id='group',
                                            segment_id=1)}}]})
            task._accept_source(message)
            task._accept_source(message)
            self.assertEqual(1, len(task.jobs))

    def test_short_source_segment_is_recorded_and_excluded(self):
        config = {'runtime': {}, 'targets': {'source': {
            'enabled': True, 'source': {'min_segment_duration': 30}, 'analysis': {},
            'encoding': {}, 'outputs': [], 'upload': {'enabled': False},
        }}}
        with tempfile.TemporaryDirectory() as temp:
            task = HighlightTask('short-filter', config, (queue.Queue(), queue.Queue()))
            task.state_file = os.path.join(temp, 'state.json')
            states = []
            for segment_id, duration in ((1, 8), (2, 60)):
                video = VideoInfo(path=f'{segment_id}.mp4', ctime=datetime.now(), group_id='group',
                                  segment_id=segment_id, duration=duration)
                states.append({'src_video': {'file': video}})
            task._accept_source(PipeMessage(
                'replay/source', 'highlight/short-filter', 'source_ready',
                data={'source_task': 'source', 'group_id': 'group', 'session_ended': True,
                      'segment_count': 2, 'video_states': states},
            ))
            job = task.jobs['source:group']
            self.assertEqual(2, job['source_segment_count'])
            self.assertEqual(1, job['segment_count'])
            self.assertEqual(1, job['ignored_segments'][0]['segment_id'])
            self.assertEqual(2, job['segments'][0]['segment_id'])

    def test_existing_bilibili_upload_creates_subtitle_dependency_without_preprocess_config(self):
        config = {'runtime': {}, 'targets': {'source': {
            'enabled': True, 'source': {'subtitle': 'prefer', 'min_segment_duration': 0},
            'preprocess': {'transcribe': {}}, 'analysis': {}, 'encoding': {},
            'outputs': [], 'upload': {'enabled': False},
        }}}
        with tempfile.TemporaryDirectory() as temp:
            task = HighlightTask('existing-subtitle', config, (queue.Queue(), queue.Queue()))
            task.state_file = os.path.join(temp, 'state.json')
            video = VideoInfo(path='part.flv', ctime=datetime.now(), group_id='group',
                              segment_id=1, duration=60, src_video_id='BV-existing',
                              src_video_upload={'bvid': 'BV-existing', 'part_title': 'part',
                                                'account': 'archive-account'})
            task._accept_source(PipeMessage(
                'replay/source', 'highlight/existing-subtitle', 'source_ready',
                data={'source_task': 'source', 'group_id': 'group', 'session_ended': True,
                      'segment_count': 1, 'video_states': [{'src_video': {'file': video}}]},
            ))
            message = task.send_queue.get_nowait()
            self.assertEqual('transcriber', message.target)
            self.assertEqual('bilibili', message.data['engine'])
            self.assertEqual('BV-existing', message.data['args']['existing_upload']['bvid'])
            self.assertEqual('archive-account', message.data['args']['account'])

    def test_nonterminal_recovered_highlight_can_be_cancelled_and_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            task = HighlightTask.__new__(HighlightTask)
            task.taskname = 'delete-recovered'
            task.jobs = {'source:group': {
                'status': 'analyzing', 'request_id': 'old-worker-request',
            }}
            task.recovered_job_ids = {'source:group'}
            task._worker_recv = queue.Queue()
            task.state_file = os.path.join(temp, 'state.json')
            task.logger = logging.getLogger(__name__)
            success, reason = task.delete_recovered_job('source:group')
            self.assertTrue(success, reason)
            self.assertNotIn('source:group', task.jobs)
            discard = task._worker_recv.get_nowait()
            self.assertEqual('discard', discard.event)
            self.assertEqual('old-worker-request', discard.request_id)

    def test_late_subtitle_result_after_timeout_does_not_dispatch_again(self):
        config = {'runtime': {}, 'targets': {}}
        with tempfile.TemporaryDirectory() as temp:
            task = HighlightTask('late-subtitle', config, (queue.Queue(), queue.Queue()))
            task.state_file = os.path.join(temp, 'state.json')
            task.jobs = {'source:group': {
                'segments': [{'segment_id': 1, 'subtitle': None}],
                'dependencies': {'subtitle-request': {
                    'kind': 'subtitle', 'segment_id': 1, 'status': 'timeout',
                }},
            }}
            with patch.object(task, '_dispatch_if_ready') as dispatch:
                task._on_dependency(PipeMessage(
                    'transcriber', 'highlight/late-subtitle', 'end', request_id='subtitle-request',
                    data={'subtitle': os.path.join(temp, 'late.srt')},
                ))
            self.assertIsNone(task.jobs['source:group']['segments'][0]['subtitle'])
            dispatch.assert_not_called()

    def test_subtitle_dependency_uses_returned_subtitle_path(self):
        config = {'runtime': {}, 'targets': {}}
        with tempfile.TemporaryDirectory() as temp:
            task = HighlightTask('subtitle-result', config, (queue.Queue(), queue.Queue()))
            task.state_file = os.path.join(temp, 'state.json')
            task.jobs = {'source:group': {
                'segments': [{'segment_id': 1, 'subtitle': None}],
                'dependencies': {'subtitle-request': {
                    'kind': 'subtitle', 'segment_id': 1, 'status': 'waiting',
                }},
            }}
            with patch.object(task, '_dispatch_if_ready') as dispatch:
                task._on_dependency(PipeMessage(
                    'transcriber', 'highlight/subtitle-result', 'end', request_id='subtitle-request',
                    data={'subtitle': os.path.join(temp, 'part.srt'), 'engine': 'bilibili'},
                ))
            segment = task.jobs['source:group']['segments'][0]
            self.assertEqual(os.path.join(temp, 'part.srt'), segment['subtitle'])
            self.assertEqual('ready', task.jobs['source:group']['dependencies']['subtitle-request']['status'])
            dispatch.assert_called_once()

    def test_live_end_publishes_all_segments_once_as_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            event = LiveEvents('source-all', {'common_event_args': {'auto_upload': False, 'auto_clean': False}})
            event.state_file = os.path.join(temp, 'state.json')
            event.session_file = os.path.join(temp, 'sessions.json')
            event.state_dict = {'session': []}
            for segment_id in (3, 1, 2):
                video = VideoInfo(path=f'{segment_id}.mp4', ctime=datetime.now(), group_id='session',
                                  segment_id=segment_id)
                event.state_dict['session'].append({
                    'src_video': {'status': 'ready', 'file': video, 'wait': []},
                    'src_video_pre': {'status': None, 'file': None, 'wait': []},
                    'dm_video': {'status': None, 'file': None, 'wait': []},
                    'subtitle': {'status': None, 'file': None, 'wait': [], 'pending_render': None},
                })
            event.highlight_subscribers.add('Highlight')
            messages = event.onLiveEnd(PipeMessage('downloader', 'replay/source-all', 'liveend', data='session'))
            source_messages = [message for message in messages if message.event == 'source_ready']
            self.assertEqual(1, len(source_messages))
            snapshot = source_messages[0].data
            self.assertTrue(snapshot['session_ended'])
            self.assertEqual(3, snapshot['segment_count'])
            self.assertEqual(3, len(snapshot['video_states']))
            event.state_dict['session'][0]['src_video']['file'].path = 'changed.mp4'
            self.assertEqual('3.mp4', snapshot['video_states'][0]['src_video']['file'].path)
            self.assertIn('session', event.completed_sessions)
            self.assertEqual(3, len(event.completed_sessions['session']['video_states']))
            self.assertEqual([], event.get_completed_session('session', [])['video_states'])

    def test_bilibili_upload_result_is_persisted_in_completed_session(self):
        with tempfile.TemporaryDirectory() as temp:
            event = LiveEvents('upload-metadata', {
                'common_event_args': {'auto_upload': False, 'auto_clean': False},
            })
            event.state_file = os.path.join(temp, 'state.json')
            event.session_file = os.path.join(temp, 'sessions.json')
            video = VideoInfo(path='part.mkv', ctime=datetime.now(), group_id='session',
                              segment_id=1, duration=60)
            event.state_dict = {'session': [{
                'src_video': {'status': 'uploading', 'file': video, 'wait': ['upload-1']},
                'src_video_pre': {'status': None, 'file': None, 'wait': []},
                'dm_video': {'status': None, 'file': None, 'wait': []},
                'subtitle': {'status': None, 'file': None, 'wait': []},
            }]}
            event.ended_dict = {'session': time.time()}
            event._archive_session('session', event.state_dict['session'])
            event.onUploadEnd(PipeMessage(
                'uploader', 'replay/upload-metadata', 'end', request_id='upload-1',
                data={'result': 'BV123abc', 'engine': 'biliwebapi', 'account': 'archive-account'},
            ))
            restored = event.completed_sessions['session']['video_states'][0]['src_video']['file']
            self.assertEqual('BV123abc', restored.src_video_id)
            self.assertEqual('part', restored.src_video_upload['part_title'])
            self.assertEqual('archive-account', restored.src_video_upload['account'])

    def test_manual_runs_use_unique_job_ids_and_reject_parallel_duplicate(self):
        target = {'source': {'min_segment_duration': 0}, 'analysis': {}, 'encoding': {},
                  'outputs': [], 'upload': {'enabled': False}}
        with tempfile.TemporaryDirectory() as temp:
            task = HighlightTask('manual', {'runtime': {}, 'targets': {}}, (queue.Queue(), queue.Queue()))
            task.state_file = os.path.join(temp, 'state.json')
            task._worker_recv = queue.Queue()
            video = VideoInfo(path='source.mp4', ctime=datetime.now(), group_id='group',
                              segment_id=1, duration=60)
            data = {'source_task': 'source', 'group_id': 'group', 'session_ended': True,
                    'segment_count': 1, 'video_states': [{'src_video': {'file': video}}],
                    'manual': True, 'run_id': 'run1', 'target_config': target}
            task._accept_source(PipeMessage('web', 'highlight/manual', 'source_ready', data=data))
            data['run_id'] = 'run2'
            task._accept_source(PipeMessage('web', 'highlight/manual', 'source_ready', data=data))
            self.assertIn('source:group:manual:run1', task.jobs)
            self.assertNotIn('source:group:manual:run2', task.jobs)

    def test_legacy_scan_groups_stable_segments_and_records_danmaku(self):
        with tempfile.TemporaryDirectory() as temp:
            rendered_dir = os.path.join(temp, 'rendered')
            os.makedirs(rendered_dir)
            paths = [os.path.join(temp, f'part-{index}.mkv') for index in (1, 2)]
            for index, path in enumerate(paths):
                with open(path, 'wb') as file:
                    file.write(b'video')
                ended = time.time() - 600 + index * 70
                os.utime(path, (ended, ended))
            with open(os.path.splitext(paths[0])[0] + '.danmaku.jsonl', 'w', encoding='utf-8') as file:
                file.write('{}\n')
            rendered_path = os.path.join(rendered_dir, 'fixed.mp4')
            with open(rendered_path, 'wb') as file:
                file.write(b'rendered')
            event = LiveEvents('legacy', {
                'common_event_args': {'auto_upload': False, 'auto_clean': False},
                'download_args': {'output_dir': temp, 'stop_wait_time': 5},
                'render_args': {'dmrender': {'output_dir': rendered_dir,
                                             'output_name': 'fixed', 'format': 'mp4'}},
            })
            event.session_file = os.path.join(temp, 'sessions.json')
            with patch('DMR.Task.liveevents.FFprobe.get_duration', return_value=60):
                added = event.scan_legacy_sessions()
            self.assertEqual(1, added)
            session = next(iter(event.completed_sessions.values()))
            self.assertTrue(session['inferred'])
            self.assertEqual(2, len(session['video_states']))
            first = session['video_states'][0]['src_video']['file']
            self.assertTrue(first.raw_dm_file_id.endswith('.danmaku.jsonl'))
            dm_video = session['video_states'][0]['dm_video']['file']
            self.assertEqual(os.path.abspath(rendered_path), dm_video.path)


if __name__ == '__main__':
    unittest.main()
