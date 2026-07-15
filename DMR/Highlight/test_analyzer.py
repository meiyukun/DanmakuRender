import unittest

from DMR.Highlight.analyzer import find_hotspots, select_profile


class HighlightAnalyzerTests(unittest.TestCase):
    def test_peak_is_kept_and_sustained_command_is_removed(self):
        items = []
        for second in range(0, 600, 10):
            items.append({'time': second, 'text': '普通聊天'})
        for second in range(190, 211):
            for index in range(5):
                items.append({'time': second + index / 10, 'text': f'太强了{index}'})
        for second in range(300, 481, 2):
            items.extend([
                {'time': second, 'text': '今晚好运口令'},
                {'time': second + 0.2, 'text': '今晚好运口令'},
                {'time': second + 0.4, 'text': '今晚好运口令'},
            ])

        result = find_hotspots(items, 600, {
            'min_peak_messages': 8,
            'min_peak_z': 2.5,
            'min_prominence_ratio': 0.8,
        })

        self.assertTrue(result['campaigns'])
        hotspot = next(item for item in result['candidates'] if 185 <= item['peak'] <= 215)
        self.assertLess(hotspot['end'] - hotspot['start'], 45)
        self.assertFalse(hotspot['clipped_by_max'])
        self.assertFalse(any(300 <= item['peak'] <= 480 for item in result['candidates']))

    def test_close_peaks_in_same_density_episode_are_grouped(self):
        items = [{'time': second, 'text': '普通聊天'} for second in range(0, 240, 15)]
        for second in range(95, 121):
            count = 7 if second in range(98, 104) or second in range(110, 116) else 3
            for index in range(count):
                items.append({'time': second + index / 10, 'text': f'笑死{index}'})

        result = find_hotspots(items, 240, {
            'min_peak_messages': 8,
            'min_peak_z': 2.0,
            'min_prominence_ratio': 0.5,
        })

        around_event = [item for item in result['candidates'] if 90 <= item['peak'] <= 125]
        self.assertEqual(1, len(around_event))
        self.assertGreaterEqual(len(around_event[0]['peaks']), 2)

    def test_returned_candidate_ranges_never_overlap(self):
        items = [{'time': second, 'text': '普通聊天'} for second in range(0, 300, 15)]
        for begin in (90, 125):
            for second in range(begin, begin + 10):
                for index in range(6):
                    items.append({'time': second + index / 10, 'text': f'高能{begin}-{index}'})

        result = find_hotspots(items, 300, {
            'min_peak_messages': 8,
            'min_peak_z': 2.0,
            'min_prominence_ratio': 0.5,
            'peak_merge_max_gap_seconds': 20,
        })
        chronological = sorted(result['candidates'], key=lambda item: item['start'])
        self.assertTrue(all(a['end'] <= b['start'] for a, b in zip(chronological, chronological[1:])))

    def test_dense_independent_discussion_is_not_a_hotspot(self):
        items = []
        for second in range(90, 111):
            for index in range(8):
                items.append({
                    'time': second + index / 10,
                    'text': f'我觉得这个角色的第{index}套天赋应该换一种搭配',
                })

        result = find_hotspots(items, 240, {
            'min_peak_messages': 8,
            'min_peak_z': 2.0,
            'min_prominence_ratio': 0.5,
        })
        self.assertFalse(result['candidates'])

    def test_short_repeated_reactions_are_positive_not_campaigns(self):
        items = []
        reactions = ['哈哈哈哈', '笑死', '？？？', '帅', '666666']
        for second in range(95, 111):
            for index in range(8):
                items.append({'time': second + index / 10, 'text': reactions[index % len(reactions)]})

        result = find_hotspots(items, 240, {
            'min_peak_messages': 8,
            'min_peak_z': 2.0,
            'min_prominence_ratio': 0.5,
        })
        hotspot = next(item for item in result['candidates'] if 90 <= item['peak'] <= 115)
        self.assertFalse(result['campaigns'])
        self.assertGreater(hotspot['reaction_ratio'], 0.7)
        self.assertIn(hotspot['dominant_reaction'], {'funny', 'skill', 'absurd'})

    def test_profile_puts_strongest_first_then_chronological(self):
        candidates = [
            {'start': 100, 'end': 120, 'category': 'funny', 'peak_height': 10},
            {'start': 20, 'end': 40, 'category': 'funny', 'peak_height': 9},
            {'start': 60, 'end': 80, 'category': 'skill', 'peak_height': 8},
        ]
        selected = select_profile(candidates, {
            'categories': ['*'], 'max_clips': 3, 'max_total_duration': 100,
        })
        self.assertEqual([100, 20, 60], [item['start'] for item in selected])


if __name__ == '__main__':
    unittest.main()
