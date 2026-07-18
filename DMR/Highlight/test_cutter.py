import unittest
from unittest.mock import Mock, patch

from DMR.Highlight import Highlight, attach_subtitle_excerpts, filter_clip_candidates
from DMR.Highlight.analyzer import select_profile
from DMR.Highlight.cutter import keyframe_extension_seconds, map_range


class HighlightCutterTests(unittest.TestCase):
    def test_candidate_subtitle_excerpt_includes_nearby_spoken_lines(self):
        candidates = [{'id': 'hot', 'start': 100, 'end': 110}]
        subtitles = [
            {'start': 92, 'end': 93, 'text': '太早了'},
            {'start': 95, 'end': 97, 'text': '热点前一句'},
            {'start': 104, 'end': 106, 'text': '这句话语出惊人'},
            {'start': 117, 'end': 118, 'text': '太晚了'},
        ]
        enriched = attach_subtitle_excerpts(candidates, subtitles, padding=6)
        self.assertEqual(
            ['热点前一句', '这句话语出惊人'],
            [item['text'] for item in enriched[0]['subtitle_excerpt']],
        )

    def test_keyframe_extension_counts_both_sides(self):
        pieces = [{'offset': 0, 'start': 98, 'end': 127}]
        self.assertEqual(keyframe_extension_seconds(pieces, 100, 125), 4)

    def test_categories_filter_individual_events_without_aggregation(self):
        candidates = [
            *[{'id': f'funny-{index}', 'category': 'funny'} for index in range(4)],
            *[{'id': f'absurd-{index}', 'category': 'absurd'} for index in range(3)],
            {'id': 'skill-0', 'category': 'skill'},
        ]
        clips = filter_clip_candidates(candidates, ['funny', 'absurd'])
        self.assertEqual(len(clips), 7)
        self.assertEqual([item['id'] for item in clips], [
            'funny-0', 'funny-1', 'funny-2', 'funny-3',
            'absurd-0', 'absurd-1', 'absurd-2',
        ])

    def test_ai_rejected_candidates_remain_available_for_clip_library(self):
        reviewer = Highlight.__new__(Highlight)
        reviewer.logger = Mock()
        reviewer.ai_client = Mock(available=True)
        candidates = [
            {'id': 'keep', 'start': 0, 'end': 10, 'peak_height': 20, 'prominence': 10,
             'category': 'funny', 'score': 10, 'representative': []},
            {'id': 'reject', 'start': 20, 'end': 30, 'peak_height': 18, 'prominence': 8,
             'category': 'absurd', 'score': 8, 'representative': []},
        ]
        reviewer.ai_client.chat.return_value = '''{"candidates": [
            {"id": "keep", "keep": true, "category": "funny", "confidence": 0.9},
            {"id": "reject", "keep": false, "category": "absurd", "confidence": 0.8}
        ]}'''
        reviewed = reviewer._ai_review(candidates, [], {
            'ai': {'enabled': True, 'min_confidence': 0.65},
        })
        self.assertEqual({'keep', 'reject'}, {item['id'] for item in reviewed})
        self.assertTrue(next(item for item in reviewed if item['id'] == 'keep')['ai_keep'])
        self.assertFalse(next(item for item in reviewed if item['id'] == 'reject')['ai_keep'])

    def test_mix_limits_do_not_limit_available_candidate_pool(self):
        candidates = [
            {'id': 'too-long', 'start': 0, 'end': 12, 'category': 'funny'},
            {'id': 'one', 'start': 20, 'end': 25, 'category': 'funny'},
            {'id': 'two', 'start': 30, 'end': 35, 'category': 'funny'},
            {'id': 'reserve', 'start': 40, 'end': 45, 'category': 'funny'},
        ]
        selected = select_profile(candidates, {
            'categories': ['*'], 'max_clips': 2, 'max_total_duration': 10,
        })
        self.assertEqual(['one', 'two'], [item['id'] for item in selected])
        self.assertEqual(4, len(candidates))

    @patch('DMR.Highlight.cutter._next_keyframe', return_value=8.0)
    @patch('DMR.Highlight.cutter._previous_keyframe', return_value=2.0)
    def test_copy_boundaries_align_outward(self, previous, following):
        pieces = map_range([{'path': 'source.mp4', 'offset': 0, 'duration': 12}], 3.2, 6.3, outward=True)
        self.assertEqual((pieces[0]['start'], pieces[0]['end']), (2.0, 8.0))
        previous.assert_called_once_with('source.mp4', 3.2)
        following.assert_called_once_with('source.mp4', 6.3, 12.0)

    @patch('DMR.Highlight.cutter._next_keyframe', return_value=4.0)
    @patch('DMR.Highlight.cutter._previous_keyframe', return_value=8.0)
    def test_multisegment_only_aligns_outer_edges(self, previous, following):
        segments = [
            {'path': 'one.mp4', 'offset': 0, 'duration': 10},
            {'path': 'two.mp4', 'offset': 10, 'duration': 10},
        ]
        pieces = map_range(segments, 8.5, 13.5, outward=True)
        self.assertEqual([(item['start'], item['end']) for item in pieces], [(8.0, 10.0), (0.0, 4.0)])
        previous.assert_called_once_with('one.mp4', 8.5)
        following.assert_called_once_with('two.mp4', 3.5, 10.0)


if __name__ == '__main__':
    unittest.main()
