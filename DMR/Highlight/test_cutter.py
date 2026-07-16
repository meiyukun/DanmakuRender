import unittest
from unittest.mock import patch

from DMR.Highlight import filter_clip_candidates
from DMR.Highlight.cutter import keyframe_extension_seconds, map_range


class HighlightCutterTests(unittest.TestCase):
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
