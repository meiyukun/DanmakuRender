import json
import os
import tempfile
import unittest

from DMR.Downloader.Danmaku.raw_writer import RawDanmakuWriter
from DMR.Highlight.analyzer import parse_danmaku
from DMR.Highlight.analyzer import _representative
from DMR.utils import GiftDanmaku, SimpleDanmaku


class RawDanmakuTests(unittest.TestCase):
    def test_writer_saves_only_compact_hotspot_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "segment.danmaku.jsonl")
            writer = RawDanmakuWriter()
            writer.open(path)
            writer.add(SimpleDanmaku(
                time=12.5, timestamp=1700000000, dtype="danmaku", uname="测试用户",
                content="哈哈哈", color="ff00ff", uid=123, medal={"name": "粉丝牌", "level": 8},
            ), source_url="https://example.test/live/1")
            writer.close()

            with open(path, encoding="utf-8") as file:
                record = json.loads(file.readline())
            self.assertEqual(12.5, record["video_time"])
            self.assertEqual("测试用户", record["sender"]["name"])
            self.assertEqual(123, record["sender"]["uid"])
            self.assertEqual("danmaku", record["type"])
            self.assertEqual("哈哈哈", record["text"])
            self.assertEqual(
                {"video_time", "type", "sender", "text"},
                set(record),
            )
            self.assertNotIn("version", record)
            self.assertNotIn("timestamp", record)
            self.assertNotIn("color", record)
            self.assertNotIn("data", record)

    def test_writer_drops_other_and_empty_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "segment.danmaku.jsonl")
            writer = RawDanmakuWriter()
            writer.open(path)
            self.assertFalse(writer.add(SimpleDanmaku(
                time=1, timestamp=1700000000, dtype="other", uname="", content="",
                raw_data="large protocol descriptor",
            )))
            self.assertFalse(writer.add(SimpleDanmaku(
                time=2, timestamp=1700000001, dtype="entry", uname="用户", content="",
            )))
            self.assertTrue(writer.add(SimpleDanmaku(
                time=3, timestamp=1700000002, dtype="entry", uname="用户", content="用户来了",
            )))
            writer.close()
            with open(path, encoding="utf-8") as file:
                records = [json.loads(line) for line in file]
            self.assertEqual(1, len(records))
            self.assertEqual("entry", records[0]["type"])

    def test_parser_does_not_fall_back_to_legacy_content_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "segment.danmaku.jsonl")
            with open(path, "w", encoding="utf-8") as file:
                file.write(json.dumps({
                    "video_time": 1, "type": "danmaku",
                    "sender": {"name": "旧用户", "uid": 1}, "content": "旧格式文本",
                }, ensure_ascii=False) + "\n")
            self.assertEqual([], parse_danmaku(path))

    def test_parser_uses_current_text_records_and_ignores_gifts_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "segment.danmaku.jsonl")
            writer = RawDanmakuWriter()
            writer.open(path)
            writer.add(SimpleDanmaku(
                time=1, timestamp=1700000000, dtype="danmaku", uname="甲", content="帅",
            ))
            writer.add(GiftDanmaku(
                time=2, timestamp=1700000001, uname="乙", content="礼物",
                gift_name="花", gift_count=1,
            ))
            writer.close()

            default_items = parse_danmaku(path)
            all_items = parse_danmaku(path, allowed_types=["danmaku", "gift"])
            self.assertEqual(["帅"], [item["text"] for item in default_items])
            self.assertEqual(2, len(all_items))

    def test_ai_representatives_only_contain_time_and_text(self):
        items = [
            {"time": 1.5, "text": "哈哈", "dtype": "danmaku", "uname": "用户", "uid": 123},
            {"time": 2.0, "text": "哈哈", "dtype": "emoticon", "uname": "用户2"},
        ]
        representatives = _representative(items)
        self.assertTrue(representatives)
        self.assertTrue(all(set(item) == {"time", "text"} for item in representatives))


if __name__ == "__main__":
    unittest.main()
