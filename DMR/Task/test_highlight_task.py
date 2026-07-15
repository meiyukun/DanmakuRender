import os
import queue
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from DMR.Config import Config
from DMR.Task.highlighttask import HighlightTask
from DMR.Task.liveevents import LiveEvents
from DMR.utils import PipeMessage, VideoInfo


class HighlightTaskTests(unittest.TestCase):
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

    def test_live_end_publishes_all_segments_once_as_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            event = LiveEvents('source-all', {'common_event_args': {'auto_upload': False, 'auto_clean': False}})
            event.state_file = os.path.join(temp, 'state.json')
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


if __name__ == '__main__':
    unittest.main()
