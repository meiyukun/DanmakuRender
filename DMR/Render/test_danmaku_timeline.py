import unittest

from DMR.Render.danmaku_timeline import (
    TIMELINE_FILTER_PLACEHOLDER,
    bucket_danmaku_density,
    merge_timeline_filter,
    normalize_density,
    parse_ass_time,
    resolve_density_args,
)


class DanmakuTimelineTest(unittest.TestCase):
    def test_parse_ass_time(self):
        self.assertEqual(parse_ass_time("0:00:01.23"), 1.23)
        self.assertEqual(parse_ass_time("12:03:04.56"), 43384.56)
        self.assertIsNone(parse_ass_time("bad"))
        self.assertIsNone(parse_ass_time("0:99:01.00"))

    def test_bucket_danmaku_density(self):
        self.assertEqual(bucket_danmaku_density([], 20, 5), [0, 0, 0, 0])
        self.assertEqual(bucket_danmaku_density([0, 1, 5, 9.9, 20, 21], 20, 5), [2, 2, 0, 1])

    def test_normalize_density_emphasizes_spikes(self):
        normalized = normalize_density(
            [2, 2, 2, 12, 3, 3],
            {
                "peak_percentile": 95,
                "curve_gamma": 0.55,
                "spike_boost": 0.8,
                "min_density_height": 0.04,
            },
        )
        self.assertGreater(normalized[3], normalized[2] * 1.5)
        self.assertGreater(normalized[3], normalized[4])

    def test_resolve_density_args_supports_ratios(self):
        self.assertEqual(resolve_density_args({"sample_interval": 0.002, "smooth_window": 0.01}, 120), (0.24, 5))
        self.assertEqual(resolve_density_args({"sample_interval": 0.002, "smooth_window": 0.01}, 7200), (14.4, 5))
        self.assertEqual(resolve_density_args({"sample_interval": 3, "smooth_window": 1}, 7200), (3.0, 1))

    def test_merge_without_custom_filter(self):
        merged, enabled, warning = merge_timeline_filter(
            None, 1, 2, 60, 1920, 1080, {}, "subtitles=filename='danmaku.ass'"
        )
        self.assertTrue(enabled)
        self.assertIsNone(warning)
        self.assertIn("[0:v]subtitles=filename='danmaku.ass'[base]", merged)
        self.assertIn("[1:v]format=rgba[timeline]", merged)
        self.assertIn("[2:v]format=rgba", merged)
        self.assertIn("[danmaku_timeline_density_progress]", merged)
        self.assertTrue(merged.endswith("[vout]"))

    def test_merge_simple_custom_filter(self):
        merged, enabled, warning = merge_timeline_filter(
            "fps=fps=60,subtitles=filename='{DANMAKU}',drawtext=text=test",
            1,
            2,
            60,
            1920,
            1080,
            {},
            "subtitles=filename='danmaku.ass'",
        )
        self.assertTrue(enabled)
        self.assertIsNone(warning)
        self.assertIn("[0:v]fps=fps=60", merged)
        self.assertIn("drawtext=text=test[base]", merged)

    def test_skip_complex_filter_without_placeholder(self):
        original = "[0:v]scale=1920:1080[vout]"
        merged, enabled, warning = merge_timeline_filter(
            original, 1, 2, 60, 1920, 1080, {}, "subtitles=filename='danmaku.ass'"
        )
        self.assertFalse(enabled)
        self.assertEqual(merged, original)
        self.assertIn("too complex", warning)

    def test_replace_placeholder(self):
        original = "[0:v]scale=1920:1080[base];" + TIMELINE_FILTER_PLACEHOLDER
        merged, enabled, warning = merge_timeline_filter(
            original, 1, 2, 60, 1920, 1080, {}, "subtitles=filename='danmaku.ass'"
        )
        self.assertTrue(enabled)
        self.assertIsNone(warning)
        self.assertNotIn(TIMELINE_FILTER_PLACEHOLDER, merged)
        self.assertIn("[base][timeline]overlay=0:H-h", merged)


if __name__ == "__main__":
    unittest.main()
