import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from os.path import splitext


logger = logging.getLogger(__name__)

_ASS_EFFECT_RE = re.compile(r"\{.*?\}")
_VISIBLE_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_SYMBOL_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,12}")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,24}")

_NOISE_WORDS = {
    "哈哈", "哈哈哈", "啊啊", "啊啊啊", "来了", "主播", "弹幕", "直播",
    "可以", "这个", "那个", "什么", "怎么", "不是", "没有", "就是",
    "一下", "真的", "感觉", "哈哈哈哈", "666", "233", "hhh", "www",
}


def _parse_ass_time(time_str):
    nums = re.findall(r"\d+", time_str or "")
    if len(nums) < 4:
        return 0.0
    hours, minutes, seconds, centiseconds = [int(x) for x in nums[:4]]
    return hours * 3600 + minutes * 60 + seconds + centiseconds / 100


def _format_period(seconds, bucket_seconds):
    start = int(seconds)
    end = start + int(bucket_seconds)
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def clean_danmaku_text(text):
    if text is None:
        return ""
    text = html.unescape(str(text))
    text = _ASS_EFFECT_RE.sub("", text)
    text = text.replace("\\N", " ").replace("\\n", " ")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > 80:
        return ""
    if not _VISIBLE_RE.search(text) or _SYMBOL_RE.match(text):
        return ""
    if len(text) <= 1:
        return ""
    compact = re.sub(r"\s+", "", text).lower()
    if compact in _NOISE_WORDS:
        return ""
    if len(compact) >= 3 and len(set(compact)) == 1:
        return ""
    return text


def _parse_ass(path):
    items = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.startswith("Dialogue:"):
                continue
            parts = line.rstrip("\n").split(",", 9)
            if len(parts) < 10:
                continue
            text = clean_danmaku_text(parts[9])
            if text:
                items.append({"time": _parse_ass_time(parts[1]), "text": text})
    return items


def _parse_xml(path):
    items = []
    tree = ET.parse(path)
    root = tree.getroot()
    for elem in root.iter("d"):
        text = clean_danmaku_text(elem.text)
        if not text:
            continue
        time_value = 0.0
        p = elem.attrib.get("p", "")
        if p:
            try:
                time_value = float(p.split(",", 1)[0])
            except ValueError:
                time_value = 0.0
        items.append({"time": time_value, "text": text})
    return items


def _parse_srt(path):
    items = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.isdigit() or "-->" in line:
                continue
            text = clean_danmaku_text(line)
            if text:
                items.append({"time": 0.0, "text": text})
    return items


def parse_danmaku_file(path):
    if not path or not os.path.exists(path):
        return []
    ext = splitext(path)[1].lower()
    try:
        if ext == ".ass":
            return _parse_ass(path)
        if ext == ".xml":
            return _parse_xml(path)
        if ext == ".srt":
            return _parse_srt(path)
        if ext == ".protobuf":
            logger.info("暂不支持解析 protobuf 弹幕文件用于AI封面分析: %s", path)
            return []
        logger.info("暂不支持解析弹幕文件类型用于AI封面分析: %s", path)
    except Exception as e:
        logger.warning("解析弹幕文件失败 %s: %s", path, e)
    return []


def _keyword_candidates(text):
    yielded = set()
    for word in _WORD_RE.findall(text):
        word = word.lower()
        if word not in _NOISE_WORDS:
            yielded.add(word)
    for seq in _CJK_RE.findall(text):
        if seq in _NOISE_WORDS:
            continue
        if 2 <= len(seq) <= 8:
            yielded.add(seq)
    return yielded


def analyze_danmaku_files(paths, bucket_seconds=300, max_items=4000):
    items = []
    for path in paths:
        parsed = parse_danmaku_file(path)
        if parsed:
            items.extend(parsed)
        if len(items) >= max_items:
            items = items[:max_items]
            break

    text_counter = Counter(item["text"] for item in items)
    keyword_counter = Counter()
    bucket_counter = Counter()
    for item in items:
        for token in _keyword_candidates(item["text"]):
            keyword_counter[token] += 1
        if item["time"] is not None:
            bucket = int(float(item["time"]) // bucket_seconds) * bucket_seconds
            bucket_counter[bucket] += 1

    hot_danmaku = [
        {"text": text, "count": count}
        for text, count in text_counter.most_common(20)
        if count > 1 or len(text) >= 4
    ][:12]
    hot_keywords = [
        {"text": text, "count": count}
        for text, count in keyword_counter.most_common(20)
    ][:12]
    peak_periods = [
        {"period": _format_period(bucket, bucket_seconds), "count": count}
        for bucket, count in bucket_counter.most_common(5)
        if count > 0
    ][:3]

    representative = []
    seen = set()
    for item in sorted(items, key=lambda x: (text_counter[x["text"]], len(x["text"])), reverse=True):
        text = item["text"]
        if text in seen:
            continue
        seen.add(text)
        representative.append(text)
        if len(representative) >= 12:
            break

    summary = ""
    if hot_keywords or hot_danmaku:
        kw = "、".join(item["text"] for item in hot_keywords[:8]) or "无明显关键词"
        dm = "；".join(item["text"] for item in hot_danmaku[:5]) or "无明显高频弹幕"
        peak = "、".join(item["period"] for item in peak_periods) or "无明显峰值时段"
        summary = f"弹幕热点关键词：{kw}。高频弹幕：{dm}。互动峰值：{peak}。"

    return {
        "total": len(items),
        "summary": summary,
        "hot_danmaku": hot_danmaku,
        "hot_keywords": hot_keywords,
        "peak_periods": peak_periods,
        "representative_texts": representative,
    }


def compact_analysis_for_ai(analysis, max_chars=3500):
    lines = [
        f"有效弹幕数量: {analysis.get('total', 0)}",
        f"本地摘要: {analysis.get('summary') or '无'}",
        "高频弹幕:",
    ]
    for item in analysis.get("hot_danmaku", [])[:12]:
        lines.append(f"- {item['text']} x{item['count']}")
    lines.append("关键词:")
    for item in analysis.get("hot_keywords", [])[:12]:
        lines.append(f"- {item['text']} x{item['count']}")
    lines.append("互动峰值时段:")
    for item in analysis.get("peak_periods", [])[:3]:
        lines.append(f"- {item['period']} x{item['count']}")
    lines.append("代表弹幕:")
    for text in analysis.get("representative_texts", [])[:12]:
        lines.append(f"- {text}")

    compact = "\n".join(lines)
    if len(compact) > max_chars:
        compact = compact[:max_chars].rsplit("\n", 1)[0]
    return compact
