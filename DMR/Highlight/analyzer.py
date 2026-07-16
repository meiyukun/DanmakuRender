import html
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict


_ASS_TAG_RE = re.compile(r"\{.*?\}")
_VISIBLE_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_BRACKET_EMOTE_RE = re.compile(r"\[([^\[\]\r\n]{1,24})\]")
_SRT_TIME_RE = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)

REACTION_TEXTS = {
    "哈哈", "哈哈哈", "哈哈哈哈", "hhh", "hhhh", "www", "666", "233",
    "???", "？？？", "牛", "牛逼", "卧槽", "我去", "笑死", "乐", "绷不住",
}
REACTION_FAMILIES = {
    "funny": (
        "哈哈", "笑死", "笑不活", "绷", "乐", "hhh", "www", "233", "草", "艹",
        "蚌埠", "小丑", "节目效果",
    ),
    "skill": (
        "帅", "牛", "666", "卧槽", "我超", "我去", "太强", "天秀", "神操作",
        "极限", "无敌", "起飞", "厉害", "好强",
    ),
    "absurd": (
        "???", "啊？", "啊?", "不是哥们", "什么鬼", "离谱", "逆天", "抽象",
        "看不懂", "懵", "人机",
    ),
    "fail": ("寄", "完了", "白给", "下饭", "没了", "死了", "翻车", "可惜", "炸了"),
    "emotional": ("泪目", "哭了", "感动", "破防", "温柔", "好听"),
}
# Named platform emotes are stored as text such as ``[捂脸]`` in raw danmaku.
# Keep social/gift emotes (比心、爱心、红包、互粉...) out of this map so routine
# interaction cannot satisfy the hotspot reaction threshold by itself.
BRACKET_EMOTE_FAMILIES = {
    "funny": (
        "笑哭", "大笑", "偷笑", "憨笑", "坏笑", "奸笑",
        "做鬼脸", "鬼脸", "如花", "调皮", "吐舌", "滑稽", "柴犬",
    ),
    "skill": ("打call", "胜利", "给力"),
    "absurd": (
        "疑问", "发呆", "宕机", "黑脸", "白眼", "斜眼", "皱眉", "擦汗",
        "无语", "尴尬", "裂开", "惊恐", "恐惧", "绝望", "苦涩", "不是吧",
    ),
    "fail": ("打脸", "躺平", "吐血", "晕", "晕倒"),
    "emotional": ("流泪", "泣不成声", "大哭", "委屈", "难过", "哭泣", "破防"),
}
AMBIGUOUS_BRACKET_EMOTES = {
    "666", "捂脸", "呲牙", "鼓掌", "小鼓掌", "酷拽", "点赞", "赞", "微笑",
}
CONTEXT_FAMILY_WORDS = {
    "funny": REACTION_FAMILIES["funny"] + ("好笑", "笑死我了"),
    "skill": REACTION_FAMILIES["skill"] + ("漂亮", "秀到了"),
    "absurd": REACTION_FAMILIES["absurd"] + ("逆天", "离谱", "这也行"),
    "fail": REACTION_FAMILIES["fail"] + ("失误", "空大", "空枪", "空了", "送了"),
    "emotional": REACTION_FAMILIES["emotional"],
}
BENEFIT_WORDS = {
    "福袋", "口令", "抽奖", "红包", "福利", "参与抽奖", "发送口令", "上舰抽",
    "舰长抽", "中奖", "开奖", "礼物抽", "关注抽", "粉丝团",
}
LOW_VALUE_WORDS = {
    "欢迎", "签到", "打卡", "早上好", "晚上好", "主播好", "拜拜", "晚安",
    "下播", "再见", "感谢老板", "谢谢老板", "老板大气", "点点关注", "关注主播",
}
CATEGORY_WORDS = {
    "funny": ("哈哈", "笑死", "绷不住", "乐", "小丑", "节目效果", "hhh", "www",
              "笑哭", "大笑", "偷笑", "憨笑", "坏笑", "做鬼脸"),
    "skill": ("帅", "牛逼", "太强", "神", "操作", "天秀", "极限", "无敌", "起飞",
              "鼓掌", "打call", "胜利", "给力", "酷拽"),
    "absurd": ("逆天", "离谱", "什么情况", "看不懂", "???", "人机", "抽象",
               "疑问", "发呆", "宕机", "黑脸", "白眼", "无语", "尴尬", "裂开"),
    "fail": ("完了", "寄", "翻车", "炸了", "没了", "死了", "可惜", "事故", "打脸", "躺平"),
    "emotional": ("泪目", "哭了", "感动", "破防", "温柔", "好听", "流泪", "泣不成声", "大哭", "委屈"),
}


def _time_value(parts):
    hour, minute, second, fraction = [int(value) for value in parts]
    return hour * 3600 + minute * 60 + second + fraction / (1000 if fraction > 99 else 100)


def _ass_time(value):
    parts = re.findall(r"\d+", value or "")
    if len(parts) < 4:
        return 0.0
    return _time_value(parts[:4])


def _clean_text(value):
    text = html.unescape(str(value or ""))
    text = _ASS_TAG_RE.sub("", text).replace("\\N", " ").replace("\\n", " ")
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) > 120 or not _VISIBLE_RE.search(text):
        return ""
    if re.fullmatch(r"[?？!！。.,，、~～\s]+", text):
        return "???" if "?" in text or "？" in text else ""
    return text


def _reaction_emote_tokens(value):
    raw_text = html.unescape(str(value or ""))
    raw_text = _ASS_TAG_RE.sub("", raw_text).replace("\\N", " ").replace("\\n", " ").lower()
    matched = []
    for emote in _BRACKET_EMOTE_RE.findall(raw_text):
        compact_emote = re.sub(r"\s+", "", emote)
        if compact_emote in AMBIGUOUS_BRACKET_EMOTES:
            matched.append((emote.strip(), "ambiguous"))
            continue
        for family, names in BRACKET_EMOTE_FAMILIES.items():
            if any(name in compact_emote for name in names):
                matched.append((emote.strip(), family))
                break
    return matched


def _clean_danmaku_text(value):
    cleaned = _clean_text(value)
    if cleaned:
        return cleaned
    # Preserve named reactions from otherwise discarded overlong messages.
    tokens = [f"[{emote}]" for emote, _ in _reaction_emote_tokens(value)]
    return "".join(tokens)[:120]


def normalize_campaign_text(value):
    text = _clean_text(value).lower()
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    text = re.sub(r"(.)\1{3,}", r"\1\1\1", text)
    return text


def reaction_family(value, max_chars=16):
    emote_tokens = _reaction_emote_tokens(value)
    bracket_families = [family for _, family in emote_tokens]
    if bracket_families:
        raw_text = html.unescape(str(value or "")).lower()
        context = _BRACKET_EMOTE_RE.sub(" ", raw_text)
        context_scores = {
            family: sum(context.count(word) for word in words)
            for family, words in CONTEXT_FAMILY_WORDS.items()
        }
        context_family, context_score = max(context_scores.items(), key=lambda item: item[1])
        if context_score:
            return context_family
        explicit = [family for family in bracket_families if family != "ambiguous"]
        return Counter(explicit).most_common(1)[0][0] if explicit else "ambiguous"
    cleaned = _clean_text(value).lower()
    compact = re.sub(r"\s+", "", cleaned)
    if not compact or len(compact) > max_chars:
        return None
    if compact == "???" or re.fullmatch(r"[?？]+", compact):
        return "absurd"
    if re.fullmatch(r"哈+|h{2,}|w{2,}|2*3{2,}|6{2,}", compact):
        return "funny" if not compact.startswith("6") else "skill"
    for family, words in REACTION_FAMILIES.items():
        if any(word in compact for word in words):
            return family
    return None


def parse_danmaku(path, offset=0.0, allowed_types=None):
    if not path or not os.path.exists(path):
        return []
    ext = os.path.splitext(path)[1].lower()
    items = []
    allowed_types = set(allowed_types or ["danmaku", "emoticon"])
    if ext == ".ass":
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as file:
            for line in file:
                if not line.startswith("Dialogue:"):
                    continue
                parts = line.rstrip("\n").split(",", 9)
                if len(parts) < 10:
                    continue
                text = _clean_danmaku_text(parts[9])
                if text:
                    items.append({"time": offset + _ass_time(parts[1]), "text": text, "dtype": "danmaku"})
    elif ext == ".xml":
        root = ET.parse(path).getroot()
        for node in root.iter("d"):
            text = _clean_danmaku_text(node.text)
            if not text:
                continue
            try:
                timestamp = float(node.attrib.get("p", "0").split(",", 1)[0])
            except ValueError:
                timestamp = 0.0
            items.append({"time": offset + timestamp, "text": text, "dtype": "danmaku"})
    elif ext == ".jsonl":
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as file:
            for line in file:
                try:
                    record = json.loads(line)
                    dtype = str(record.get("type") or "other")
                    if allowed_types and dtype not in allowed_types:
                        continue
                    text = _clean_danmaku_text(record.get("text"))
                    timestamp = float(record.get("video_time"))
                    if not text:
                        continue
                    sender = record.get("sender") or {}
                    items.append({
                        "time": offset + timestamp,
                        "text": text,
                        "dtype": dtype,
                        "uname": sender.get("name"),
                        "uid": sender.get("uid"),
                    })
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
    return items


def parse_srt(path, offset=0.0):
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as file:
        content = file.read().replace("\r\n", "\n")
    entries = []
    for block in re.split(r"\n\s*\n", content):
        match = _SRT_TIME_RE.search(block)
        if not match:
            continue
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        text = _clean_text(" ".join(block[match.end():].strip().splitlines()))
        if text:
            entries.append({"start": offset + start, "end": offset + end, "text": text})
    return entries


def _entropy(counter):
    total = sum(counter.values())
    if total <= 0:
        return 0.0
    return -sum((count / total) * math.log(count / total, 2) for count in counter.values())


def find_campaigns(items, bucket_seconds=5, min_duration=120, dominant_ratio=0.35):
    """Find sustained repeated commands; reaction-only text can never become a campaign."""
    buckets = defaultdict(Counter)
    raw_by_normalized = defaultdict(Counter)
    for item in items:
        normalized = normalize_campaign_text(item["text"])
        if not normalized or reaction_family(item["text"]) or len(normalized) < 2:
            continue
        bucket = int(float(item["time"]) // bucket_seconds)
        buckets[bucket][normalized] += 1
        raw_by_normalized[normalized][item["text"]] += 1

    active = defaultdict(list)
    for bucket, counter in buckets.items():
        total = sum(counter.values())
        if total < 3:
            continue
        text, count = counter.most_common(1)[0]
        keyword = any(word in text for word in BENEFIT_WORDS)
        if count / total >= dominant_ratio or (keyword and count >= 2):
            active[text].append(bucket)

    campaigns = []
    min_buckets = max(1, int(math.ceil(min_duration / bucket_seconds)))
    for text, bucket_ids in active.items():
        bucket_ids.sort()
        runs = []
        current = [bucket_ids[0]]
        for bucket in bucket_ids[1:]:
            if bucket - current[-1] <= 3:
                current.append(bucket)
            else:
                runs.append(current)
                current = [bucket]
        runs.append(current)
        for run in runs:
            span = run[-1] - run[0] + 1
            if span < min_buckets and not (
                any(word in text for word in BENEFIT_WORDS) and span >= max(6, min_buckets // 2)
            ):
                continue
            start = run[0] * bucket_seconds
            end = (run[-1] + 1) * bucket_seconds
            campaigns.append({
                "text": text,
                "display_text": raw_by_normalized[text].most_common(1)[0][0],
                "start": start,
                "end": end,
                "duration": end - start,
            })
    return campaigns


def _in_campaign(item, campaigns):
    normalized = normalize_campaign_text(item["text"])
    return any(
        campaign["text"] == normalized and campaign["start"] <= item["time"] < campaign["end"]
        for campaign in campaigns
    )


def _gaussian(values, sigma):
    if sigma <= 0:
        return [float(value) for value in values]
    radius = max(1, int(math.ceil(sigma * 3)))
    weights = [math.exp(-0.5 * (offset / sigma) ** 2) for offset in range(-radius, radius + 1)]
    output = []
    for index in range(len(values)):
        total = weight_total = 0.0
        for offset, weight in zip(range(-radius, radius + 1), weights):
            position = min(max(index + offset, 0), len(values) - 1)
            total += values[position] * weight
            weight_total += weight
        output.append(total / weight_total)
    return output


def _median(values):
    values = sorted(float(value) for value in values)
    if not values:
        return 0.0
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def _baseline(values, index, radius=120, exclusion=30):
    samples = [
        values[position]
        for position in range(max(0, index - radius), min(len(values), index + radius + 1))
        if abs(position - index) > exclusion
    ] or values
    median = _median(samples)
    mad = _median([abs(value - median) for value in samples])
    return median, max(mad * 1.4826, 0.5)


def _peak_bases(values, peak, max_distance=180):
    left_border = max(0, peak - max_distance)
    right_border = min(len(values) - 1, peak + max_distance)
    left = min(range(left_border, peak + 1), key=lambda pos: values[pos])
    right = min(range(peak, right_border + 1), key=lambda pos: values[pos])
    contour = max(values[left], values[right])
    return left, right, max(0.0, values[peak] - contour)


def _boundary_threshold(curve, peak, config):
    baseline, spread = _baseline(
        curve,
        peak,
        radius=int(config.get("baseline_radius_seconds", 120)),
        exclusion=int(config.get("baseline_exclusion_seconds", 30)),
    )
    peak_delta = max(0.0, curve[peak] - baseline)
    threshold = baseline + max(
        float(config.get("boundary_threshold_mad", 1.0)) * spread,
        float(config.get("boundary_threshold_ratio", 0.50)) * peak_delta,
    )
    return baseline, spread, threshold


def _find_quiet_boundary(curve, origin, direction, threshold, quiet_seconds, max_distance):
    quiet = 0
    position = origin
    stop = max(-1, origin - max_distance - 1) if direction < 0 else min(len(curve), origin + max_distance + 1)
    for position in range(origin, stop, direction):
        if curve[position] <= threshold:
            quiet += 1
            if quiet >= quiet_seconds:
                if direction < 0:
                    return position + quiet_seconds, True
                return position - quiet_seconds + 1, True
        else:
            quiet = 0
    return max(0, min(len(curve) - 1, origin + direction * max_distance)), False


def _same_peak_event(previous, current, curve, config):
    gap = current["peak"] - previous["peak"]
    if gap <= 0 or gap > int(config.get("peak_merge_max_gap_seconds", 45)):
        return False
    baseline = min(previous["baseline"], current["baseline"])
    smaller_peak = min(previous["peak_height"], current["peak_height"])
    if smaller_peak <= baseline:
        return True
    # A momentary dip should not split a broad reaction episode. Only treat the
    # peaks as separate events after the curve has stayed near baseline for a
    # configurable period.
    release = baseline + float(config.get("peak_merge_valley_ratio", 0.25)) * (
        smaller_peak - baseline
    )
    required_quiet = max(1, int(config.get("peak_merge_quiet_seconds", 6)))
    quiet = 0
    for value in curve[previous["peak"]:current["peak"] + 1]:
        if value <= release:
            quiet += 1
            if quiet >= required_quiet:
                return False
        else:
            quiet = 0
    return True


def _excess_quantile_boundary(curve, start, end, baseline, quantile):
    """Return the position containing a quantile of density above baseline."""
    start = max(0, int(start))
    end = min(len(curve) - 1, int(end))
    if end <= start:
        return start
    excess = [max(0.0, curve[position] - baseline) for position in range(start, end + 1)]
    total = sum(excess)
    if total <= 0:
        return start if quantile <= 0.5 else end
    target = total * min(max(float(quantile), 0.0), 1.0)
    accumulated = 0.0
    for position, value in zip(range(start, end + 1), excess):
        accumulated += value
        if accumulated >= target:
            return position
    return end


def _segment_items(items, start, end):
    return [item for item in items if start <= item["time"] < end]


def _is_low_value(text):
    compact = normalize_campaign_text(text)
    return any(word in compact for word in LOW_VALUE_WORDS) or any(word in compact for word in BENEFIT_WORDS)


def _classify(items):
    def semantic_text(value):
        return _BRACKET_EMOTE_RE.sub(
            lambda match: "" if re.sub(r"\s+", "", match.group(1)).lower()
            in AMBIGUOUS_BRACKET_EMOTES else match.group(0),
            value,
        )
    blob = " ".join(semantic_text(item["text"]) for item in items).lower()
    scores = {
        category: sum(blob.count(word.lower()) for word in words)
        for category, words in CATEGORY_WORDS.items()
    }
    category, score = max(scores.items(), key=lambda item: item[1])
    return category if score else "other_content"


def _representative(items, limit=20):
    counter = Counter(item["text"] for item in items if not _is_low_value(item["text"]))
    top = {text for text, _ in counter.most_common(8)}
    selected = []
    for item in items:
        if item["text"] in top and item not in selected:
            selected.append({"time": item["time"], "text": item["text"]})
        if len(selected) >= limit:
            break
    return selected


def _reaction_metrics(items, config):
    total = len(items)
    if not total:
        return {
            "reaction_count": 0, "reaction_ratio": 0.0, "dominant_reaction": None,
            "dominant_reaction_ratio": 0.0, "short_message_ratio": 0.0,
            "discussion_ratio": 0.0, "reaction_span": 0.0,
        }
    max_chars = int(config.get("reaction_max_chars", 16))
    short_chars = int(config.get("short_message_chars", 8))
    discussion_chars = int(config.get("discussion_min_chars", 12))
    families = Counter()
    short_count = discussion_count = 0
    reaction_times = []
    for item in items:
        compact = normalize_campaign_text(item["text"])
        family = reaction_family(item["text"], max_chars=max_chars)
        if family:
            families[family] += 1
            reaction_times.append(float(item["time"]))
        elif len(compact) >= discussion_chars:
            discussion_count += 1
        if len(compact) <= short_chars:
            short_count += 1
    reaction_count = sum(families.values())
    dominant, dominant_count = families.most_common(1)[0] if families else (None, 0)
    return {
        "reaction_count": reaction_count,
        "reaction_ratio": reaction_count / total,
        "dominant_reaction": dominant,
        "dominant_reaction_ratio": dominant_count / total,
        "short_message_ratio": short_count / total,
        "discussion_ratio": discussion_count / total,
        "reaction_span": max(reaction_times) - min(reaction_times) if len(reaction_times) > 1 else 0.0,
    }


def find_hotspots(items, duration, config=None):
    config = config or {}
    campaigns = find_campaigns(
        items,
        bucket_seconds=int(config.get("campaign_bucket_seconds", 5)),
        min_duration=int(config.get("campaign_min_duration", 120)),
        dominant_ratio=float(config.get("campaign_dominant_ratio", 0.35)),
    )
    filtered = [item for item in items if not _in_campaign(item, campaigns)]
    bucket_count = max(1, int(math.ceil(float(duration or 0))))
    counts = [0] * bucket_count
    for item in filtered:
        if 0 <= item["time"] < duration:
            counts[min(bucket_count - 1, int(item["time"]))] += 1

    peak_curve = _gaussian(counts, float(config.get("peak_smoothing_seconds", 5)))
    boundary_curve = _gaussian(counts, float(config.get("boundary_smoothing_seconds", 2)))
    min_messages = int(config.get("min_peak_messages", 8))
    min_z = float(config.get("min_peak_z", 3.0))
    min_prominence_ratio = float(config.get("min_prominence_ratio", 1.0))
    peaks = []
    for index in range(1, bucket_count - 1):
        value = peak_curve[index]
        if value < peak_curve[index - 1] or value < peak_curve[index + 1]:
            continue
        nearby_count = sum(counts[max(0, index - 5):min(bucket_count, index + 6)])
        if nearby_count < min_messages:
            continue
        baseline, spread = _baseline(
            peak_curve, index,
            radius=int(config.get("baseline_radius_seconds", 120)),
            exclusion=int(config.get("baseline_exclusion_seconds", 30)),
        )
        z_score = (value - baseline) / spread
        left, right, prominence = _peak_bases(
            peak_curve, index, int(config.get("prominence_search_seconds", 180)),
        )
        prominence_ratio = prominence / max(baseline, 1.0)
        rise_window = boundary_curve[max(0, index - 20):index]
        rising = boundary_curve[index] - min(rise_window or [boundary_curve[index]])
        if z_score < min_z or prominence_ratio < min_prominence_ratio or rising <= 0:
            continue
        peaks.append({
            "peak": index, "peak_height": value, "baseline": baseline, "spread": spread,
            "z_score": z_score, "prominence": prominence,
            "prominence_ratio": prominence_ratio, "nearby_count": nearby_count,
        })

    peak_groups = []
    for peak in peaks:
        if peak_groups and _same_peak_event(peak_groups[-1][-1], peak, peak_curve, config):
            peak_groups[-1].append(peak)
        else:
            peak_groups.append([peak])

    candidates = []
    quiet_seconds = max(1, int(config.get("boundary_quiet_seconds", 4)))
    search_seconds = max(quiet_seconds, int(config.get("boundary_search_seconds", 60)))
    pre_roll = max(0, int(config.get("pre_roll_seconds", 10)))
    post_roll = int(config.get("post_roll_seconds", 2))
    start_quantile = float(config.get("boundary_start_quantile", 0.05))
    end_quantile = float(config.get("boundary_end_quantile", 0.92))
    min_clip = int(config.get("min_clip_seconds", 10))
    max_clip = int(config.get("max_clip_seconds", 45))
    for group_index, group in enumerate(peak_groups):
        strongest = max(group, key=lambda item: (item["peak_height"], item["prominence"]))
        boundary_stats = [_boundary_threshold(boundary_curve, item["peak"], config) for item in group]
        thresholds = [item[2] for item in boundary_stats]
        threshold = min(thresholds)
        event_baseline = min(item[0] for item in boundary_stats)
        onset, left_found = _find_quiet_boundary(
            boundary_curve, group[0]["peak"], -1, threshold, quiet_seconds, search_seconds,
        )
        decay, right_found = _find_quiet_boundary(
            boundary_curve, group[-1]["peak"], 1, threshold, quiet_seconds, search_seconds,
        )
        body_start = _excess_quantile_boundary(
            boundary_curve, onset, decay, event_baseline, start_quantile,
        )
        body_start = min(body_start, group[0]["peak"])
        body_end = _excess_quantile_boundary(
            boundary_curve, onset, decay, event_baseline, end_quantile,
        )
        start = max(0, body_start - pre_roll)
        end = min(duration, max(body_end, group[-1]["peak"]) + post_roll)
        clipped_by_max = False
        if end - start > max_clip:
            clipped_by_max = True
            # Preserve the detected beginning and trim the low-energy tail. For
            # exceptionally broad build-ups, still keep substantially more
            # context than the old peak-(pre_roll+quiet) anchor allowed.
            if strongest["peak"] > start + max_clip:
                peak_position_ratio = min(max(
                    float(config.get("max_clip_peak_position_ratio", 0.65)), 0.1,
                ), 0.9)
                start = max(0, strongest["peak"] - max_clip * peak_position_ratio)
            end = min(duration, start + max_clip)
        if end - start < min_clip:
            missing = min_clip - (end - start)
            start = max(0, start - math.ceil(missing / 2))
            end = min(duration, start + min_clip)
            start = max(0, end - min_clip)
        segment = _segment_items(filtered, start, end)
        if not segment:
            continue
        text_counter = Counter(normalize_campaign_text(item["text"]) for item in segment)
        diversity = len(text_counter) / len(segment)
        top_ratio = text_counter.most_common(1)[0][1] / len(segment)
        low_value_ratio = sum(_is_low_value(item["text"]) for item in segment) / len(segment)
        reaction = _reaction_metrics(segment, config)
        reaction_signal = (
            reaction["reaction_count"] >= int(config.get("reaction_min_count", 5))
            and (
                reaction["reaction_ratio"] >= float(config.get("reaction_min_ratio", 0.18))
                or reaction["dominant_reaction_ratio"] >= float(config.get("dominant_reaction_min_ratio", 0.10))
            )
        )
        if bool(config.get("require_reaction_signal", True)) and not reaction_signal:
            continue
        if reaction["discussion_ratio"] > float(config.get("discussion_max_ratio", 0.75)):
            continue
        if low_value_ratio >= 0.55:
            continue
        if not reaction_signal and (top_ratio >= 0.72 or (_entropy(text_counter) < 0.8 and len(segment) >= 12)):
            continue
        density_score = strongest["z_score"] * math.log1p(strongest["nearby_count"]) + strongest["prominence_ratio"]
        reaction_boost = 1.0 + float(config.get("reaction_score_weight", 1.5)) * (
            reaction["reaction_ratio"] + reaction["dominant_reaction_ratio"]
        )
        score = density_score * reaction_boost
        category = reaction["dominant_reaction"] or _classify(segment)
        candidates.append({
            "id": f"peak-{strongest['peak']}", "start": round(start, 3), "end": round(end, 3),
            "peak": strongest["peak"], "peaks": [item["peak"] for item in group],
            "peak_cluster": group_index, "peak_height": round(strongest["peak_height"], 3),
            "z_score": round(strongest["z_score"], 3),
            "prominence": round(strongest["prominence"], 3),
            "prominence_ratio": round(strongest["prominence_ratio"], 3),
            "score": round(score, 3), "message_count": len(segment),
            "diversity": round(diversity, 3), "top_ratio": round(top_ratio, 3),
            "category": category, "representative": _representative(segment),
            "reaction_count": reaction["reaction_count"],
            "reaction_ratio": round(reaction["reaction_ratio"], 3),
            "dominant_reaction": reaction["dominant_reaction"],
            "dominant_reaction_ratio": round(reaction["dominant_reaction_ratio"], 3),
            "short_message_ratio": round(reaction["short_message_ratio"], 3),
            "discussion_ratio": round(reaction["discussion_ratio"], 3),
            "reaction_span": round(reaction["reaction_span"], 3),
            "boundary_threshold": round(threshold, 3),
            "event_baseline": round(event_baseline, 3),
            "body_start": round(body_start, 3), "body_end": round(body_end, 3),
            "boundary_confidence": "both" if left_found and right_found else "partial",
            "clipped_by_max": clipped_by_max,
        })

    chronological = sorted(candidates, key=lambda item: item["start"])
    for previous, current in zip(chronological, chronological[1:]):
        if current["start"] < previous["end"]:
            left_peak, right_peak = sorted((previous["peak"], current["peak"]))
            split = min(range(left_peak, right_peak + 1), key=lambda pos: boundary_curve[pos])
            previous["end"] = round(min(previous["end"], split), 3)
            current["start"] = round(max(current["start"], split), 3)
    candidates = [item for item in chronological if item["end"] - item["start"] >= min_clip]
    candidates.sort(key=lambda item: (item["score"], item["peak_height"], item["prominence"]), reverse=True)
    return {"campaigns": campaigns, "candidates": candidates, "raw_count": len(items), "filtered_count": len(filtered)}


def select_profile(candidates, profile):
    categories = profile.get("categories", ["*"])
    selected = [
        item for item in candidates
        if "*" in categories or item.get("category") in categories
    ][:int(profile.get("max_clips", 12))]
    limit = float(profile.get("max_total_duration", 300))
    kept = []
    total = 0.0
    for item in selected:
        duration = item["end"] - item["start"]
        if kept and total + duration > limit:
            continue
        kept.append(item)
        total += duration
    if len(kept) > 1:
        strongest = kept[0]
        kept = [strongest] + sorted(kept[1:], key=lambda item: item["start"])
    return kept
