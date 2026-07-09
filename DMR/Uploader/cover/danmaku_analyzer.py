import html
import logging
import math
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from os.path import splitext


logger = logging.getLogger(__name__)

_ASS_EFFECT_RE = re.compile(r"\{.*?\}")
_VISIBLE_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_SYMBOL_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_QUESTION_ONLY_RE = re.compile(r"^[?？!！。.,，、~～\s]+$")
_QUESTION_MARK_RE = re.compile(r"[?？]")
_NUMBER_VALUE_RE = re.compile(r"^\d+(?:\.\d+)?%?$")
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\u2600-\u26ff"
    "\u2700-\u27bf"
    "]"
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,12}")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,24}")

_NOISE_WORDS = {
    "哈哈", "哈哈哈", "啊啊", "啊啊啊", "来了", "主播", "弹幕", "直播",
    "可以", "这个", "那个", "什么", "怎么", "不是", "没有", "就是",
    "一下", "真的", "感觉", "哈哈哈哈", "666", "233", "hhh", "www",
    "是的", "对的", "嗯嗯",
}

_GREETING_WORDS = {
    "早上好", "上午好", "中午好", "下午好", "晚上好", "来了", "来啦",
    "开播了", "准时", "签到", "打卡", "每天开心", "点点陪伴之旅",
}

_FAREWELL_WORDS = {
    "拜拜", "再见", "明天见", "下播", "晚安", "回放", "辛苦了",
}

_LOW_VALUE_WORDS = {
    "老板大气", "感谢老板", "谢谢老板",
}

_HOT_LABEL_RULES = [
    ("困惑逆天反应", {"???", "？？？", "?", "？", "看不懂", "没看懂", "什么情况", "啥情况", "这是什么", "啊？"}),
    ("惊叹发现", {"我去", "卧槽", "蛙趣", "哇塞", "鉴定一下", "宝藏", "金色", "古董"}),
    ("策略决策", {"时装", "藏品", "道具", "卖东西", "别锁", "不要锁", "自己摸", "自己玩", "开摸"}),
    ("数据吐槽", {"胜率", "百分之", "6.25", "没上小学"}),
    ("搞笑名场面", {"哈哈", "哈哈哈", "笑死", "绷不住", "乐", "乐了", "蚌埠住", "hhh", "www", "😂", "🤣", "😆", "😹"}),
    ("高能反应", {"高能", "卧槽", "牛", "牛逼", "太强", "神", "绝了", "起飞", "帅", "😱", "😨", "🤯", "🔥"}),
    ("吐槽名场面", {"人机", "毒奶", "这是什么", "选点", "离谱", "逆天"}),
    ("翻车事故", {"寄", "完了", "炸了", "崩了", "坏了", "事故", "翻车", "没了", "死了", "流血", "可惜"}),
    ("争议讨论", {"不是", "凭什么", "离谱", "逆天", "急了", "吵", "节奏", "问题"}),
    ("感动共鸣", {"哭了", "泪目", "感动", "破防", "好听", "温柔", "😭", "🥹", "😢"}),
    ("福利互动", {"抽奖", "红包", "福利", "舰长", "上舰", "礼物", "老板大气", "🎉", "🎁", "💰"}),
]

_EMOJI_LABEL_HINTS = {
    "搞笑名场面": {"😂", "🤣", "😆", "😹", "😄", "😁"},
    "高能反应": {"😱", "😨", "😰", "🤯", "🔥", "💥"},
    "困惑逆天反应": {"😳", "😵", "🙃", "🤔", "🧐"},
    "感动共鸣": {"😭", "🥹", "😢", "😿", "💔"},
    "福利互动": {"🎉", "🎊", "🎁", "💰", "💎"},
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


def _format_range(start, end):
    start = max(0, int(start))
    end = max(start + 1, int(end))
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
    if _QUESTION_ONLY_RE.match(text) and _QUESTION_MARK_RE.search(text):
        return "???"
    has_emoji = bool(_EMOJI_RE.search(text))
    if not _VISIBLE_RE.search(text) and not has_emoji:
        return ""
    if _SYMBOL_RE.match(text) and not has_emoji:
        return ""
    compact = re.sub(r"\s+", "", text).lower()
    has_emoji = bool(_EMOJI_RE.search(text))
    if len(text) <= 1 and not has_emoji:
        return ""
    if _NUMBER_VALUE_RE.match(compact):
        return ""
    if compact in _NOISE_WORDS:
        return ""
    if len(compact) >= 3 and len(set(compact)) == 1 and not has_emoji:
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
    for emoji in _EMOJI_RE.findall(str(text or "")):
        yielded.add(emoji)
    if _QUESTION_MARK_RE.search(str(text or "")):
        yielded.add("???")
    for word in _WORD_RE.findall(text):
        word = word.lower()
        if word not in _NOISE_WORDS:
            yielded.add(word)
    for seq in _CJK_RE.findall(text):
        if seq in _NOISE_WORDS:
            continue
        if len(seq) == 2 and len(set(seq)) == 1:
            continue
        if 2 <= len(seq) <= 8:
            yielded.add(seq)
    return yielded


def _is_greeting_or_boilerplate(text):
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if not compact:
        return False
    return any(word.lower() in compact for word in _GREETING_WORDS)


def _is_farewell(text):
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if not compact:
        return False
    return any(word.lower() in compact for word in _FAREWELL_WORDS)


def _is_low_value_text(text):
    compact = re.sub(r"\s+", "", str(text or "")).lower()
    if not compact:
        return False
    if _NUMBER_VALUE_RE.match(compact):
        return True
    return (
        _is_greeting_or_boilerplate(compact)
        or _is_farewell(compact)
        or any(word.lower() in compact for word in _LOW_VALUE_WORDS)
    )


def _is_emoji_token(text):
    text = str(text or "")
    return bool(text) and _EMOJI_RE.fullmatch(text) is not None


def _is_emoji_only_text(text):
    text = re.sub(r"[\ufe0e\ufe0f\u200d\s]", "", str(text or ""))
    if not text:
        return False
    return _EMOJI_RE.sub("", text) == ""


def _choose_hotspot_bucket(bucket_seconds):
    try:
        bucket_seconds = int(bucket_seconds)
    except (TypeError, ValueError):
        bucket_seconds = 300
    return min(60, max(10, bucket_seconds // 20 or 10))


def _smooth_counts(counts):
    if not counts:
        return []
    weights = (1, 2, 3, 2, 1)
    radius = len(weights) // 2
    smoothed = []
    for idx in range(len(counts)):
        total = 0
        weight_total = 0
        for offset, weight in enumerate(weights):
            pos = idx + offset - radius
            if 0 <= pos < len(counts):
                total += counts[pos] * weight
                weight_total += weight
        smoothed.append(total / max(1, weight_total))
    return smoothed


def _local_baseline(values, idx, radius):
    samples = []
    left = max(0, idx - radius)
    right = min(len(values), idx + radius + 1)
    inner_left = max(0, idx - 1)
    inner_right = min(len(values), idx + 2)
    for pos in range(left, right):
        if inner_left <= pos < inner_right:
            continue
        samples.append(values[pos])
    if not samples:
        samples = values
    return sum(samples) / max(1, len(samples))


def _segment_keywords(segment_items, global_keyword_counter, total_items):
    segment_counter = Counter()
    for item in segment_items:
        for token in _keyword_candidates(item["text"]):
            segment_counter[token] += 1

    scored = []
    segment_total = max(1, len(segment_items))
    total_items = max(1, total_items)
    for token, count in segment_counter.items():
        if _is_low_value_text(token):
            continue
        global_count = max(1, global_keyword_counter.get(token, count))
        segment_rate = count / segment_total
        global_rate = global_count / total_items
        lift = segment_rate / max(global_rate, 1 / total_items)
        if lift < 1.0:
            continue
        score = count * (1 + math.log1p(max(0.0, lift - 1)))
        scored.append((score, count, lift, token))
    scored.sort(reverse=True)
    return [
        {"text": token, "count": count, "lift": round(lift, 2)}
        for score, count, lift, token in scored[:8]
    ]


def _classify_segment(segment_items, keywords):
    top_keywords = " ".join(item["text"] for item in keywords[:4])
    question_count = sum(1 for item in segment_items if _QUESTION_MARK_RE.search(item.get("text", "")))
    if any(item.get("text") == "???" for item in keywords[:5]):
        return "困惑逆天反应"
    if question_count >= max(3, len(segment_items) * 0.08):
        return "困惑逆天反应"
    if any(word in top_keywords for word in ("我去", "蛙趣", "哇塞", "鉴定一下")):
        return "惊叹发现"
    if any(word in top_keywords for word in ("时装", "藏品", "卖东西", "别锁", "不要锁", "自己摸", "自己玩", "金色")):
        return "策略决策"
    if any(word in top_keywords for word in ("胜率", "百分之", "没上小学")):
        return "数据吐槽"
    if any(word in top_keywords for word in ("人机", "毒奶", "这是什么选点")):
        return "吐槽名场面"
    if any(word in top_keywords for word in ("流死", "流血", "没了", "可惜", "完了")):
        return "翻车事故"
    emoji_counter = Counter()
    for item in segment_items:
        emoji_counter.update(_EMOJI_RE.findall(item.get("text", "")))
    if emoji_counter:
        label_scores = []
        for label, emojis in _EMOJI_LABEL_HINTS.items():
            score = sum(emoji_counter.get(emoji, 0) for emoji in emojis)
            if score > 0:
                label_scores.append((score, label))
        label_scores.sort(reverse=True)
        if label_scores and label_scores[0][0] >= max(5, len(segment_items) * 0.12):
            return label_scores[0][1]
    text_blob = " ".join(item["text"] for item in segment_items[:200])
    keyword_blob = " ".join(item["text"] for item in keywords)
    blob = f"{text_blob} {keyword_blob}".lower()
    best_label = "集中讨论"
    best_score = 0
    for label, words in _HOT_LABEL_RULES:
        score = sum(1 for word in words if word.lower() in blob)
        if score > best_score:
            best_label = label
            best_score = score
    return best_label


def _is_low_value_segment(segment):
    if segment.get("start", 0) <= 30 and segment.get("label") == "开场问候":
        return True
    if segment.get("label") == "收尾告别":
        return True
    keywords = segment.get("keywords", [])
    if not keywords:
        return False
    greeting_count = sum(1 for item in keywords[:5] if _is_greeting_or_boilerplate(item.get("text", "")))
    return greeting_count >= 3 and segment.get("start", 0) <= 120


def _segment_rank_key(segment):
    score = float(segment.get("score", 0) or 0)
    if segment.get("label") == "开场问候":
        score -= 3.0
    if segment.get("label") == "收尾告别":
        score -= 3.0
    if segment.get("start", 0) <= 60:
        score -= 1.0
    return score, segment.get("count", 0)


def _representative_texts(segment_items, limit=6, skip_low_value=False, skip_emoji_only=False):
    text_counter = Counter(item["text"] for item in segment_items)
    representative = []
    seen = set()
    for item in sorted(segment_items, key=lambda x: (text_counter[x["text"]], len(x["text"])), reverse=True):
        text = item["text"]
        if text in seen:
            continue
        if skip_low_value and _is_low_value_text(text):
            continue
        if skip_emoji_only and _is_emoji_only_text(text):
            continue
        seen.add(text)
        representative.append(text)
        if len(representative) >= limit:
            break
    if representative or not skip_low_value:
        return representative
    return _representative_texts(segment_items, limit=limit, skip_low_value=False, skip_emoji_only=False)


def _merge_hot_segments(segments, bucket_seconds):
    if not segments:
        return []
    segments = sorted(segments, key=lambda item: item["start"])
    merged = [segments[0]]
    max_gap = bucket_seconds * 2
    for segment in segments[1:]:
        prev = merged[-1]
        if segment["start"] <= prev["end"] + max_gap:
            prev["end"] = max(prev["end"], segment["end"])
            if segment["score"] > prev["score"]:
                prev["score"] = segment["score"]
                prev["peak_second"] = segment["peak_second"]
                prev["peak_count"] = segment["peak_count"]
        else:
            merged.append(segment)
    return merged


def _find_hot_segments(items, global_keyword_counter, bucket_seconds, max_segments=5):
    timed_items = [item for item in items if item.get("time") is not None and float(item.get("time") or 0) > 0]
    if len(timed_items) < 8:
        return []

    detail_bucket = _choose_hotspot_bucket(bucket_seconds)
    max_time = max(float(item["time"]) for item in timed_items)
    bucket_count = int(max_time // detail_bucket) + 2
    if bucket_count <= 1:
        return []

    counts = [0] * bucket_count
    for item in timed_items:
        idx = min(bucket_count - 1, max(0, int(float(item["time"]) // detail_bucket)))
        counts[idx] += 1

    smoothed = _smooth_counts(counts)
    mean = sum(smoothed) / len(smoothed)
    variance = sum((value - mean) ** 2 for value in smoothed) / len(smoothed)
    std = math.sqrt(variance) or 1.0
    baseline_radius = max(4, int(120 // detail_bucket))
    min_peak_count = max(3, int(mean + 0.5 * std))

    candidates = []
    for idx, value in enumerate(smoothed):
        if counts[idx] < min_peak_count:
            continue
        left = smoothed[idx - 1] if idx > 0 else -1
        right = smoothed[idx + 1] if idx + 1 < len(smoothed) else -1
        if value < left or value < right:
            continue
        local = _local_baseline(smoothed, idx, baseline_radius)
        prominence = value / max(local, 0.5)
        z_score = (value - mean) / std
        score = z_score * 0.9 + prominence * 1.4 + math.log1p(counts[idx]) * 0.5
        if score < 2.6 and prominence < 1.8:
            continue

        threshold = max(local * 1.25, value * 0.35, 1.0)
        start_idx = idx
        end_idx = idx
        max_expand = max(3, int(180 // detail_bucket))
        while start_idx > 0 and idx - start_idx < max_expand and smoothed[start_idx - 1] >= threshold:
            start_idx -= 1
        while end_idx + 1 < len(smoothed) and end_idx - idx < max_expand and smoothed[end_idx + 1] >= threshold:
            end_idx += 1
        candidates.append({
            "start": start_idx * detail_bucket,
            "end": (end_idx + 1) * detail_bucket,
            "peak_second": idx * detail_bucket,
            "peak_count": counts[idx],
            "score": score,
        })

    merged = _merge_hot_segments(candidates, detail_bucket)
    enriched = []
    for segment in merged:
        segment_items = [
            item for item in timed_items
            if segment["start"] <= float(item["time"]) < segment["end"]
        ]
        if not segment_items:
            continue
        keywords = _segment_keywords(segment_items, global_keyword_counter, len(items))
        unique_text_count = len({item["text"] for item in segment_items})
        repeat_ratio = 1 - unique_text_count / max(1, len(segment_items))
        low_value_ratio = sum(1 for item in segment_items if _is_low_value_text(item["text"])) / max(1, len(segment_items))
        label = _classify_segment(segment_items, keywords)
        if segment["start"] <= 120 and any(_is_greeting_or_boilerplate(item.get("text", "")) for item in keywords[:5]):
            label = "开场问候"
        if segment["start"] >= max_time * 0.9 and (
            low_value_ratio >= 0.35
            or any(_is_farewell(item["text"]) for item in segment_items)
        ):
            label = "收尾告别"
        enriched.append({
            "period": _format_range(segment["start"], segment["end"]),
            "start": int(segment["start"]),
            "end": int(segment["end"]),
            "peak_second": int(segment["peak_second"]),
            "count": len(segment_items),
            "peak_count": int(segment["peak_count"]),
            "score": round(segment["score"], 2),
            "label": label,
            "keywords": keywords,
            "representative_texts": _representative_texts(segment_items, skip_low_value=True, skip_emoji_only=True),
            "unique_text_count": unique_text_count,
            "repeat_ratio": round(repeat_ratio, 2),
        })

    valuable = [segment for segment in enriched if not _is_low_value_segment(segment)]
    valuable.sort(key=_segment_rank_key, reverse=True)
    if len(valuable) >= max_segments:
        return valuable[:max_segments]
    remaining = [segment for segment in enriched if segment not in valuable]
    remaining.sort(key=_segment_rank_key, reverse=True)
    return (valuable + remaining)[:max_segments]


def _persistent_texts(items, bucket_seconds=300, min_count=20, min_bucket_ratio=0.3):
    if not items:
        return set()
    max_time = max(float(item.get("time") or 0) for item in items)
    bucket_total = max(1, int(max_time // bucket_seconds) + 1)
    text_counter = Counter(item["text"] for item in items)
    text_buckets = defaultdict(set)
    for item in items:
        text = item["text"]
        if text_counter[text] < min_count:
            continue
        bucket = int(float(item.get("time") or 0) // bucket_seconds)
        text_buckets[text].add(bucket)
    persistent = set()
    for text, buckets in text_buckets.items():
        if len(buckets) / bucket_total >= min_bucket_ratio or _is_low_value_text(text):
            persistent.add(text)
    return persistent


def analyze_danmaku_files(paths, bucket_seconds=300, max_items=4000):
    items = []
    for path in paths:
        parsed = parse_danmaku_file(path)
        if parsed:
            items.extend(parsed)
    items.sort(key=lambda item: float(item.get("time") or 0))

    text_counter = Counter(item["text"] for item in items)
    persistent_texts = _persistent_texts(items)
    keyword_counter = Counter()
    bucket_counter = Counter()
    for item in items:
        if item["text"] not in persistent_texts and not _is_low_value_text(item["text"]):
            for token in _keyword_candidates(item["text"]):
                if not _is_low_value_text(token):
                    keyword_counter[token] += 1
        if item["time"] is not None:
            bucket = int(float(item["time"]) // bucket_seconds) * bucket_seconds
            bucket_counter[bucket] += 1

    hot_segments = _find_hot_segments(items, keyword_counter, bucket_seconds)

    hot_danmaku = [
        {"text": text, "count": count}
        for text, count in text_counter.most_common(20)
        if text not in persistent_texts and not _is_low_value_text(text) and (count > 1 or len(text) >= 4)
    ][:12]
    hot_keywords = [
        {"text": text, "count": count}
        for text, count in keyword_counter.most_common(20)
        if not _is_emoji_token(text)
    ][:12]
    hot_emojis = [
        {"text": text, "count": count}
        for text, count in keyword_counter.most_common(40)
        if _is_emoji_token(text)
    ][:8]
    if hot_segments:
        peak_periods = [
            {
                "period": item["period"],
                "count": item["count"],
                "start": item["start"],
                "end": item["end"],
                "peak_second": item["peak_second"],
                "score": item["score"],
                "label": item["label"],
            }
            for item in hot_segments[:3]
        ]
    else:
        peak_periods = [
            {"period": _format_period(bucket, bucket_seconds), "count": count}
            for bucket, count in bucket_counter.most_common(5)
            if count > 0
        ][:3]

    representative = []
    seen = set()
    representative_pool = [
        item for item in items
        if item["text"] not in persistent_texts and not _is_low_value_text(item["text"])
    ] or items[:max_items]
    for item in sorted(representative_pool, key=lambda x: (text_counter[x["text"]], len(x["text"])), reverse=True):
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
        if hot_segments:
            peak = "；".join(
                f"{item['period']}（{item['label']}，强度{item['score']}）"
                for item in hot_segments[:3]
            )
        else:
            peak = "、".join(item["period"] for item in peak_periods) or "无明显峰值时段"
        summary = f"弹幕热点关键词：{kw}。高频弹幕：{dm}。互动热点：{peak}。"

    return {
        "total": len(items),
        "summary": summary,
        "hot_danmaku": hot_danmaku,
        "hot_keywords": hot_keywords,
        "hot_emojis": hot_emojis,
        "hot_segments": hot_segments,
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
    hot_emojis = analysis.get("hot_emojis", [])
    if hot_emojis:
        lines.append("高频表情:")
        for item in hot_emojis[:8]:
            lines.append(f"- {item['text']} x{item['count']}")
    lines.append("互动峰值时段:")
    for item in analysis.get("peak_periods", [])[:3]:
        lines.append(f"- {item['period']} x{item['count']}")
    hot_segments = analysis.get("hot_segments", [])
    if hot_segments:
        lines.append("高价值热点片段:")
        for item in hot_segments[:5]:
            keywords = "、".join(keyword["text"] for keyword in item.get("keywords", [])[:5]) or "无明显片段关键词"
            reps = "；".join(item.get("representative_texts", [])[:3]) or "无代表弹幕"
            lines.append(
                f"- {item['period']} {item.get('label', '集中讨论')} "
                f"强度{item.get('score')} 弹幕{item.get('count')} 峰值秒{item.get('peak_second')}"
            )
            lines.append(f"  关键词: {keywords}")
            lines.append(f"  代表弹幕: {reps}")
    lines.append("代表弹幕:")
    for text in analysis.get("representative_texts", [])[:12]:
        lines.append(f"- {text}")

    compact = "\n".join(lines)
    if len(compact) > max_chars:
        compact = compact[:max_chars].rsplit("\n", 1)[0]
    return compact
