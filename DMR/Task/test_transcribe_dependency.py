import os
import tempfile
import unittest
from datetime import datetime

from DMR.Task.liveevents import LiveEvents
from DMR.utils import PipeMessage, StreamerInfo, VideoInfo


class TranscribeDependencyTest(unittest.TestCase):
    def test_render_waits_for_requested_subtitle(self):
        with tempfile.TemporaryDirectory() as directory:
            video_path = os.path.join(directory, 'segment.flv')
            open(video_path, 'wb').close()
            video = VideoInfo(
                path=video_path,
                ctime=datetime.now(),
                duration=120,
                title='test',
                streamer=StreamerInfo(name='tester'),
                group_id='group',
                file_id='video',
                dm_file_id=os.path.join(directory, 'segment.ass'),
            )
            config = {
                'common_event_args': {
                    'auto_transcribe': True,
                    'auto_transcode': False,
                    'auto_render': True,
                    'auto_upload': False,
                    'auto_clean': False,
                },
                'transcribe_args': {'engine': 'bilibili', 'bilibili': {}},
                'render_args': {'dmrender': {
                    'burn_subtitle': True,
                    'subtitle_failure_policy': 'continue',
                    'output_dir': directory,
                    'output_name': None,
                    'format': 'mp4',
                }},
            }
            events = LiveEvents('unit-transcribe-dependency', config)
            messages = events.onLiveSegment(PipeMessage('downloader', 'task', 'livesegment', data=video))
            self.assertEqual(['transcriber'], [message.target for message in messages])
            transcribe = messages[0]
            completed = PipeMessage(
                'transcriber', 'task', 'end', request_id=transcribe.request_id,
                data={'subtitle': os.path.splitext(video_path)[0] + '.srt'},
            )
            follow_up = events.onTranscribeEnd(completed)
            self.assertEqual(1, len(follow_up))
            self.assertEqual('render', follow_up[0].target)
            if os.path.exists(events.state_file):
                os.remove(events.state_file)


if __name__ == '__main__':
    unittest.main()
