import unittest
from unittest.mock import Mock, patch

from DMR.Uploader.biliapi.biliapi import get_archive_view, get_archives, get_seasons
from DMR.Uploader.biliapi.bili_section import find_video_season, sync_video_season


class BiliApiTests(unittest.TestCase):
    def test_get_seasons_uses_creator_seasons_endpoint(self):
        with patch('DMR.Uploader.biliapi.biliapi.read_bilibili_cookies',
                   return_value=('SESSDATA=value; bili_jct=csrf', 'csrf')), \
                patch('DMR.Uploader.biliapi.biliapi._get', return_value={'code': 0}) as get:
            response = get_seasons('account.json', page=2, page_size=50)
        self.assertEqual({'code': 0}, response)
        get.assert_called_once_with('SESSDATA=value; bili_jct=csrf', '/seasons', {'pn': 2, 'ps': 50})

    def test_get_archives_queries_published_creator_submissions(self):
        response = Mock()
        response.json.return_value = {'code': 0}
        with patch('DMR.Uploader.biliapi.biliapi.read_bilibili_cookies',
                   return_value=('SESSDATA=value; bili_jct=csrf', 'csrf')), \
                patch('DMR.Uploader.biliapi.biliapi.requests.get', return_value=response) as get:
            result = get_archives('account.json', page=2, page_size=10)
        self.assertEqual({'code': 0}, result)
        self.assertEqual('https://member.bilibili.com/x/web/archives', get.call_args.args[0])
        self.assertEqual({'status': 'pubed', 'pn': 2, 'ps': 10, 'interactive': 1, 'coop': 1},
                         get.call_args.kwargs['params'])
        self.assertEqual(15, get.call_args.kwargs['timeout'])

    def test_get_archive_view_queries_creator_edit_endpoint(self):
        response = Mock()
        response.json.return_value = {'code': 0}
        with patch('DMR.Uploader.biliapi.biliapi.read_bilibili_cookies',
                   return_value=('SESSDATA=value; bili_jct=csrf', 'csrf')), \
                patch('DMR.Uploader.biliapi.biliapi.requests.get', return_value=response) as get:
            result = get_archive_view('account.json', 'BV123')
        self.assertEqual({'code': 0}, result)
        self.assertEqual('https://member.bilibili.com/x/vupre/web/archive/view', get.call_args.args[0])
        self.assertEqual({'bvid': 'BV123'}, get.call_args.kwargs['params'])

    def test_find_video_season_returns_section_and_episode(self):
        seasons = {'code': 0, 'data': {'total': 1, 'seasons': [
            {'season': {'id': 9, 'title': '热点合集'}},
        ]}}
        season = {'code': 0, 'data': {
            'season': {'id': 9, 'title': '热点合集'},
            'sections': {'sections': [{
                'id': 10, 'title': '七月',
                'episodes': [{'id': 11, 'bvid': 'BV123', 'title': '热点片段'}],
            }]},
        }}
        with patch('DMR.Uploader.biliapi.bili_section.get_seasons', return_value=seasons), \
                patch('DMR.Uploader.biliapi.bili_section.get_season', return_value=season):
            result = find_video_season('account.json', 'BV123')
        self.assertEqual(9, result['season_id'])
        self.assertEqual(10, result['section_id'])
        self.assertEqual(11, result['episode_id'])

    def test_sync_video_season_does_not_write_when_values_are_unchanged(self):
        current = {
            'season_id': 9, 'section_title': '七月', 'episode_title': '热点片段',
            'episode_id': 11,
        }
        with patch('DMR.Uploader.biliapi.bili_section.find_video_season', return_value=current), \
                patch('DMR.Uploader.biliapi.bili_section.delete_episode') as delete, \
                patch('DMR.Uploader.biliapi.bili_section.add_video_to_season') as add:
            result = sync_video_season('account.json', 'BV123', 9, '七月', '热点片段')
        self.assertFalse(result['changed'])
        delete.assert_not_called()
        add.assert_not_called()


if __name__ == '__main__':
    unittest.main()
