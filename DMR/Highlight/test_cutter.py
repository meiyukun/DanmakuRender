import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from DMR.Highlight import Highlight, attach_subtitle_excerpts, filter_clip_candidates
from DMR.Highlight.analyzer import select_profile
from DMR.Highlight.cutter import keyframe_extension_seconds, map_range, map_ranges, materialize_clip, normalize_ranges, render_highlight


class HighlightCutterTests(unittest.TestCase):
    def test_multiple_ranges_are_sorted_merged_and_mapped_with_range_indexes(self):
        ranges = normalize_ranges([
            {'start': 12, 'end': 14}, {'start': 2, 'end': 5}, {'start': 5, 'end': 7},
        ])
        self.assertEqual([{'start': 2.0, 'end': 7.0}, {'start': 12.0, 'end': 14.0}], ranges)
        pieces = map_ranges([
            {'path': 'one.mp4', 'offset': 0, 'duration': 10},
            {'path': 'two.mp4', 'offset': 10, 'duration': 10},
        ], ranges)
        self.assertEqual([0, 1], [item['range_index'] for item in pieces])
        self.assertEqual([(2, 7), (2, 4)], [(item['start'], item['end']) for item in pieces])

    def test_multiple_ranges_reject_short_retained_section(self):
        with self.assertRaisesRegex(ValueError, '不得短于'):
            normalize_ranges([{'start': 1, 'end': 1.5}, {'start': 3, 'end': 5}])

    def test_multiple_ranges_reject_more_than_fifty_sections(self):
        with self.assertRaisesRegex(ValueError, '最多保留 50'):
            normalize_ranges([{'start': index * 2, 'end': index * 2 + 1}
                              for index in range(51)])

    def test_multiple_ranges_reject_non_finite_boundaries(self):
        for value in (float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '起止时间无效'):
                normalize_ranges([{'start': 1, 'end': value}])

    @patch('DMR.Highlight.cutter._concat')
    @patch('DMR.Highlight.cutter._run')
    def test_materialize_clip_keeps_flattened_piece_order(self, run, concat):
        clip = {'id': 'jump-cut', 'pieces': [
            {'path': 'one.mp4', 'start': 2, 'end': 4, 'range_index': 0},
            {'path': 'two.mp4', 'start': 1, 'end': 3, 'range_index': 0},
            {'path': 'two.mp4', 'start': 8, 'end': 11, 'range_index': 1},
        ]}
        os.makedirs('.temp', exist_ok=True)
        with tempfile.TemporaryDirectory() as output_dir:
            output = os.path.join(output_dir, 'result.mp4')
            materialize_clip(clip, output, {'mode': 'copy'}, Mock(),
                             SimpleNamespace(resolution=(1920, 1080)))
        self.assertEqual(['one.mp4', 'two.mp4', 'two.mp4'],
                         [call.args[0][call.args[0].index('-i') + 1] for call in run.call_args_list])
        self.assertEqual(1, concat.call_count)

    def test_virtual_clips_store_cross_segment_ranges_without_files(self):
        segments = [
            {'path': 'one.mp4', 'offset': 0, 'duration': 10},
            {'path': 'two.mp4', 'offset': 10, 'duration': 10},
        ]
        with tempfile.TemporaryDirectory() as output_dir:
            outputs, records, _ = render_highlight(
                segments, [{'id': 'cross', 'start': 8, 'end': 13, 'category': 'funny'}], [],
                output_dir, SimpleNamespace(path='one.mp4', resolution=(1920, 1080)),
                {'format': 'mp4'}, {'id': 'all', 'name': 'all'}, Mock(),
                virtual=True, generate_mix=False,
            )
            self.assertFalse(os.path.isdir(os.path.join(output_dir, 'clips')))
        self.assertFalse(outputs)
        self.assertEqual('virtual', records[0]['storage'])
        self.assertNotIn('path', records[0])
        self.assertEqual([(8, 10), (0, 3)],
                         [(piece['start'], piece['end']) for piece in records[0]['pieces']])

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

    @patch('DMR.Highlight.cutter._encode_piece')
    @patch('DMR.Highlight.cutter._run')
    @patch('DMR.Highlight.cutter._stream_signature', return_value=('same',))
    @patch('DMR.Highlight.cutter.map_range')
    def test_only_candidate_exceeding_six_second_extension_is_reencoded(
            self, mapped, signature, run, encode_piece):
        def ranges(segments, start, end, outward=False):
            if not outward:
                return [{'path': 'source.mp4', 'offset': 0, 'start': start, 'end': end}]
            if start == 10:
                return [{'path': 'source.mp4', 'offset': 0, 'start': 7, 'end': 23}]
            return [{'path': 'source.mp4', 'offset': 0, 'start': 26, 'end': 43}]
        mapped.side_effect = ranges
        candidates = [
            {'id': 'copy', 'start': 10, 'end': 20, 'category': 'funny'},
            {'id': 'encode', 'start': 30, 'end': 40, 'category': 'funny'},
        ]
        os.makedirs('.temp', exist_ok=True)
        with tempfile.TemporaryDirectory() as output_dir:
            outputs, records, mode = render_highlight(
                [{'path': 'source.mp4', 'offset': 0, 'duration': 60}], candidates, [],
                output_dir, SimpleNamespace(path='source.mp4', resolution=(1920, 1080)),
                {'mode': 'copy', 'copy_fallback': 'reencode', 'keyframe_alignment': 'outward',
                 'format': 'mp4'}, {'id': 'all', 'name': 'all'}, Mock(),
            )
        self.assertFalse(outputs)
        self.assertEqual(['copy', 'reencode'], [item['encoding_mode'] for item in records])
        self.assertEqual('mixed', mode)
        self.assertEqual(1, run.call_count)
        self.assertEqual(1, encode_piece.call_count)

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
