import queue
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from DMR.Uploader import Uploader
from DMR.Uploader.biliwebapi import BiliWebApi
from DMR.utils import VideoInfo


class UploaderTests(unittest.TestCase):
    def test_stateless_completion_is_removed_without_ack(self):
        uploader = Uploader.__new__(Uploader)
        uploader._lock = __import__('threading').Lock()
        task = {'uuid': 'task', 'source': 'engine', 'request_id': 'request',
                'files': [], 'engine': 'biliwebapi', 'args': {}, 'stateless': True}
        uploader.upload_tasks = {'task': task}
        uploader.failed_tasks = {}
        uploader.save_active_tasks = Mock()
        uploader._send_completed_result = Mock()
        uploader._gather(task, 'info', 'BV123')
        self.assertNotIn('task', uploader.upload_tasks)
        uploader._send_completed_result.assert_called_once_with(task)

    def test_append_at_head_keeps_selected_multi_part_order(self):
        uploader = BiliWebApi.__new__(BiliWebApi)
        uploader.insert_head = True
        uploader.videos = SimpleNamespace(bvid='BV1xx411c7mD', videos=['existing'])
        uploader.get_remote_data = Mock(return_value=uploader.videos)

        def upload_file(filepath, videos, **_kwargs):
            videos.videos.insert(0, filepath)
            return True, videos.bvid

        uploader.upload_file = Mock(side_effect=upload_file)
        uploader.submit = Mock(return_value={'data': {'bvid': 'BV1xx411c7mD'}})
        files = [VideoInfo(path=name, ctime=datetime.now()) for name in ('one.mp4', 'two.mp4')]
        status, bvid = uploader.upload(files, realtime=False, concat_video=False)
        self.assertTrue(status)
        self.assertEqual('BV1xx411c7mD', bvid)
        self.assertEqual(['one.mp4', 'two.mp4', 'existing'], uploader.videos.videos)

    def test_absolute_schedule_does_not_depend_on_source_file_time(self):
        uploader = BiliWebApi.__new__(BiliWebApi)
        scheduled_at = int(time.time() + 10800)
        video = VideoInfo(path='old.mp4', ctime=datetime.now() - timedelta(days=30), title='old')
        submission = uploader.videoinfo_to_videos(video, {
            'scheduled_at': scheduled_at, 'title': '定时投稿', 'tid': 21, 'copyright': 1,
        })
        self.assertEqual(scheduled_at, submission.dtime)

    def test_web_upload_passes_custom_part_title_to_biliwebapi(self):
        uploader = BiliWebApi.__new__(BiliWebApi)
        uploader.insert_head = False
        uploader.videos = SimpleNamespace(bvid='BV1xx411c7mD', videos=[])
        uploader.get_remote_data = Mock(return_value=uploader.videos)
        uploader.upload_file = Mock(return_value=(True, 'BV1xx411c7mD'))
        uploader.submit = Mock(return_value={'data': {'bvid': 'BV1xx411c7mD'}})
        file = VideoInfo(path='source.mp4', ctime=datetime.now(), dtype='web_upload', title='自定义分P')
        uploader.upload([file], realtime=False, concat_video=False)
        self.assertEqual('自定义分P', uploader.upload_file.call_args.kwargs['part_title'])

    def test_append_upload_applies_cover_before_final_edit(self):
        uploader = BiliWebApi.__new__(BiliWebApi)
        uploader.insert_head = False
        uploader.videos = SimpleNamespace(bvid='BV1xx411c7mD', videos=[], cover='old-cover')
        uploader.get_remote_data = Mock(return_value=uploader.videos)
        uploader.cover_up = Mock(return_value='new-cover-url')
        uploader.upload_file = Mock(return_value=(True, 'BV1xx411c7mD'))
        uploader.submit = Mock(return_value={'data': {'bvid': 'BV1xx411c7mD'}})
        file = VideoInfo(path='source.mp4', ctime=datetime.now(), dtype='web_upload', title='追加分P')
        uploader.upload([file], realtime=False, concat_video=False,
                        cover='managed.jpg', cover_required=True)
        uploader.cover_up.assert_called_once_with('managed.jpg')
        self.assertEqual('new-cover-url', uploader.submit.call_args.kwargs['videos'].cover)

    def test_append_upload_applies_editable_metadata_before_final_edit(self):
        uploader = BiliWebApi.__new__(BiliWebApi)
        uploader.insert_head = False
        remote = SimpleNamespace(
            bvid='BV1xx411c7mD', videos=[], cover='old-cover', title='原标题', desc='原简介',
            desc_v2=[], dynamic='原动态', tag='旧标签', tid=21, copyright=1,
            source='', is_only_self=0,
        )
        uploader.videos = remote
        uploader.get_remote_data = Mock(return_value=remote)
        uploader.upload_file = Mock(return_value=(True, remote.bvid))
        uploader.submit = Mock(return_value={'data': {'bvid': remote.bvid}})
        file = VideoInfo(path='source.mp4', ctime=datetime.now(), dtype='web_upload', title='追加分P')
        uploader.upload(
            [file], realtime=False, concat_video=False, update_metadata=True,
            title='新标题', desc='新简介', dynamic='新动态', tag='热点,直播',
            tid=171, copyright=1, source='', is_only_self=1,
        )
        submitted = uploader.submit.call_args.kwargs['videos']
        self.assertEqual('新标题', submitted.title)
        self.assertEqual('新简介', submitted.desc)
        self.assertEqual([{'raw_text': '新简介', 'biz_id': '', 'type': 1}], submitted.desc_v2)
        self.assertEqual('新动态', submitted.dynamic)
        self.assertEqual('热点,直播', submitted.tag)
        self.assertEqual(171, submitted.tid)
        self.assertEqual(1, submitted.is_only_self)

    def test_stateless_completion_removes_managed_cover(self):
        uploader = Uploader.__new__(Uploader)
        uploader._lock = __import__('threading').Lock()
        uploader.logger = Mock()
        with tempfile.TemporaryDirectory(dir='.temp') as temp:
            uploader._managed_artifact_root = Mock(return_value=os.path.realpath(temp))
            cover = os.path.join(temp, 'cover.jpg')
            with open(cover, 'wb') as file:
                file.write(b'cover')
            task = {'uuid': 'task', 'source': 'engine', 'request_id': 'request',
                    'files': [], 'engine': 'biliwebapi', 'args': {}, 'stateless': True,
                    'managed_artifacts': [cover]}
            uploader.upload_tasks = {'task': task}
            uploader.failed_tasks = {}
            uploader.save_active_tasks = Mock()
            uploader._send_completed_result = Mock()
            uploader._gather(task, 'info', 'BV123')
            self.assertFalse(os.path.exists(cover))


if __name__ == '__main__':
    unittest.main()
