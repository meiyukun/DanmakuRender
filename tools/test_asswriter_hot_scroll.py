import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from DMR.Downloader.Danmaku.asswriter import AssWriter
from DMR.utils import SimpleDanmaku


def make_writer(path):
    writer = AssWriter(
        description='test', width=640, height=360, dst=10, dmrate=0.8,
        font='Microsoft YaHei', fontsize=20, margin_h=4, margin_w=8,
        dmduration=2, opacity=0.8, auto_fontsize=False,
        outlinecolor='000000', outlinesize=1,
        repeat_danmaku={
            'enabled': True,
            'session_timeout': 2,
            'hot_duration': 4,
            'color_threshold': 2,
            'hot_line_spacing': 1.1,
            'hot_levels': [
                {
                    'threshold': 3, 'font': 'LevelA', 'color': 'ff0000',
                    'font_scale': 1.2, 'count_font_scale': 0.8,
                    'outline_color': '000000', 'outline_size': 1,
                    'suffix': 'HOT', 'pulse': {'enabled': False},
                },
                {
                    'threshold': 5, 'font': 'LevelB', 'color': 'ffd700',
                    'font_scale': 1.4, 'suffix': 'HOT2',
                    'pulse': {'enabled': False},
                },
                {
                    'threshold': 7, 'font': 'LevelC', 'color': 'ffffff',
                    'font_scale': 1.6, 'suffix': 'HOT3',
                    'pulse': {
                        'enabled': True, 'scale': 1.12, 'duration_ms': 500,
                        'glow_size': 4, 'glow_blur': 2,
                    },
                },
            ],
        },
    )
    writer.open(str(path))
    return writer


def dm(time, text='same'):
    return SimpleDanmaku(
        time=float(time), dtype='danmaku', uname='u',
        content=text, text=text, color='ffffff',
    )


class HotScrollAssTest(unittest.TestCase):
    def test_long_scrolling_content_is_not_ellipsized(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'long.ass'
            writer = make_writer(path)
            long_text = '这是一条非常长的滚动热点弹幕内容' * 8
            writer.add(dm(0.0, long_text))
            writer.add(dm(0.1, long_text))
            writer.add(dm(0.2, long_text))
            writer.close()

            text = path.read_text(encoding='utf-8')
            self.assertIn(long_text, text)
            self.assertNotIn('…', text)

    def test_count_refresh_keeps_position_and_style_until_next_flight(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'hot.ass'
            writer = make_writer(path)
            writer.add(dm(0.0))
            writer.add(dm(0.1))
            writer.add(dm(0.2))
            writer.add(dm(1.0))
            writer.add(dm(2.0))

            self.assertEqual(len(writer._hot_flights), 1)
            first = writer._hot_flights[0]
            self.assertEqual([count for _, count in first['updates']], [3, 4, 5])
            self.assertEqual(first['style']['font'], 'LevelA')

            writer.add(dm(4.3, 'other'))
            self.assertEqual(len(writer._hot_flights), 1)
            writer.add(dm(4.5))
            self.assertEqual(len(writer._hot_flights), 2)
            self.assertEqual(writer._hot_flights[1]['style']['font'], 'LevelB')
            writer.close()

            text = path.read_text(encoding='utf-8')
            hot_lines = [line for line in text.splitlines() if line.startswith('Dialogue: 2,')]
            self.assertGreaterEqual(len(hot_lines), 4)
            first_flight = hot_lines[:3]
            self.assertTrue(all(r'\fnLevelA' in line for line in first_flight))
            self.assertIn(r'\fnLevelB', hot_lines[3])

            starts = []
            for line in first_flight:
                match = re.search(r'\\move\((-?\d+),\d+,-?\d+,\d+,0,\d+\)', line)
                self.assertIsNotNone(match)
                starts.append(int(match.group(1)))
            self.assertEqual(starts[0], 640)
            self.assertLess(starts[1], 640)
            self.assertLess(starts[2], starts[1])

    def test_hotspot_can_launch_before_previous_tail_fully_exits(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'drain.ass'
            writer = make_writer(path)
            for lane in range(writer._ntracks):
                writer._track_tails[lane] = dm(0, f'ordinary-{lane}')

            writer.add(dm(0.0))
            writer.add(dm(0.05))
            writer.add(dm(0.1))
            self.assertEqual(len(writer._hot_flights), 0)

            writer.add(dm(1.5, 'trigger'))
            self.assertEqual(len(writer._hot_flights), 1)
            self.assertLess(writer._hot_flights[0]['start'], 2.0)
            writer.close()

    def test_ordinary_danmaku_can_follow_an_active_hotspot(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'blocked.ass'
            writer = make_writer(path)
            writer.add(dm(0.0))
            writer.add(dm(0.1))
            writer.add(dm(0.2))
            flight = writer._hot_flights[0]
            follow_time = next(
                timestamp for timestamp in (1.2, 1.7, 2.2, 2.7, 3.2)
                if writer._tail_distance(flight['tail'], timestamp) >= writer.margin_w
            )
            blocked = dm(follow_time, 'blocked')
            for lane in range(writer._ntracks):
                if lane not in flight['lanes']:
                    writer._track_tails[lane] = blocked

            ordinary = dm(follow_time, 'ordinary-following-hotspot')
            self.assertTrue(writer.add(ordinary))
            self.assertTrue(any(writer._track_tails[lane] is ordinary for lane in flight['lanes']))
            writer.close()

    def test_timeout_starts_after_flight_leaves_screen(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'timeout.ass'
            writer = make_writer(path)
            writer.add(dm(0.0))
            writer.add(dm(0.1))
            writer.add(dm(0.2))
            first_end = writer._hot_flights[0]['end']

            writer.add(dm(first_end + 2.1, 'trigger'))
            writer.add(dm(first_end + 2.2))
            session = writer._repeat_sessions[writer._normalize_repeat_text('same')]
            self.assertEqual(session['count'], 1)
            writer.close()

    def test_pending_hotspots_launch_by_heat(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'priority.ass'
            writer = make_writer(path)
            for lane in range(writer._ntracks):
                writer._track_tails[lane] = dm(0, f'ordinary-{lane}')

            for index in range(3):
                writer.add(dm(0.1 + index * 0.01, 'lower'))
            for index in range(4):
                writer.add(dm(0.2 + index * 0.01, 'higher'))
            self.assertEqual(len(writer._hot_flights), 0)

            writer.add(dm(2.1, 'trigger'))
            self.assertTrue(writer._hot_flights)
            self.assertEqual(
                writer._hot_flights[0]['key'],
                writer._normalize_repeat_text('higher'),
            )
            writer.close()

    def test_pulse_restarts_on_every_in_flight_count_update(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'pulse.ass'
            writer = make_writer(path)
            for lane in range(writer._ntracks):
                writer._track_tails[lane] = dm(0, f'ordinary-{lane}')

            # 先在轨道繁忙期间累积到启用脉冲的等级，再发射并连续更新计数。
            for index in range(7):
                writer.add(dm(0.10 + index * 0.01, 'pulse-hot'))
            writer.add(dm(2.1, 'launch-trigger'))
            writer.add(dm(2.3, 'pulse-hot'))
            writer.add(dm(2.6, 'pulse-hot'))
            writer.close()

            text = path.read_text(encoding='utf-8')
            hot_lines = [
                line for line in text.splitlines()
                if line.startswith('Dialogue: 2,') and 'pulse-hot' in line
            ]
            self.assertEqual(len(hot_lines), 3)
            self.assertIn('×7', hot_lines[0])
            self.assertIn('×8', hot_lines[1])
            self.assertIn('×9', hot_lines[2])
            self.assertTrue(all(r'\fscx112\fscy112' in line for line in hot_lines))
            self.assertTrue(all(r'\t(0,' in line for line in hot_lines))


if __name__ == '__main__':
    unittest.main()
