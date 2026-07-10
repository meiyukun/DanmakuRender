import math
import os
import re
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageColor, ImageDraw


TIMELINE_FILTER_PLACEHOLDER = "{DANMAKU_TIMELINE_FILTER}"


_ASS_DIALOGUE_RE = re.compile(r"^Dialogue:\s*\d+\s*,\s*([^,]+)", re.IGNORECASE)
_ASS_TIME_RE = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2})(?:[.](\d{1,3}))?\s*$")


def parse_ass_time(value: str) -> Optional[float]:
    match = _ASS_TIME_RE.match(value or "")
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        return None
    frac = 0.0
    if fraction:
        frac = int(fraction) / (10 ** len(fraction))
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + frac


def iter_ass_dialogue_start_times(ass_path: str) -> Iterable[float]:
    with open(ass_path, "r", encoding="utf-8-sig", errors="ignore") as ass_file:
        for line in ass_file:
            match = _ASS_DIALOGUE_RE.match(line)
            if not match:
                continue
            start_time = parse_ass_time(match.group(1))
            if start_time is not None:
                yield start_time


def bucket_danmaku_density(start_times: Iterable[float], duration: float, sample_interval: float) -> List[int]:
    duration = max(float(duration or 0), 0.0)
    sample_interval = max(float(sample_interval or 1), 0.1)
    bucket_count = max(1, int(math.ceil(duration / sample_interval)))
    buckets = [0] * bucket_count
    for start_time in start_times:
        if start_time < 0 or start_time > duration:
            continue
        index = min(int(start_time // sample_interval), bucket_count - 1)
        buckets[index] += 1
    return buckets


def resolve_density_args(config: dict, duration: float) -> Tuple[float, int]:
    duration = max(float(duration or 0), 0.0)
    sample_interval = config.get("sample_interval", 0.002)
    smooth_window = config.get("smooth_window", 0.01)

    if str(sample_interval).lower() == "auto":
        sample_interval = 0.002
    sample_interval = float(sample_interval or 0.002)
    if 0 < sample_interval < 1:
        sample_interval = duration * sample_interval
    sample_interval = max(float(sample_interval or 1), 0.1)

    bucket_count = max(1, int(math.ceil(duration / sample_interval)))
    if str(smooth_window).lower() == "auto":
        smooth_window = 0.01
    smooth_window = float(smooth_window or 0.01)
    if 0 < smooth_window < 1:
        smooth_window = bucket_count * smooth_window
    smooth_window = max(int(round(smooth_window or 1)), 1)
    return sample_interval, smooth_window


def smooth_density(values: Sequence[float], smooth_window: int) -> List[float]:
    if not values:
        return [0.0]
    window = max(int(smooth_window or 1), 1)
    if window <= 1:
        return [float(value) for value in values]
    if window % 2 == 0:
        window += 1
    radius = window // 2
    sigma = max(window / 3.0, 0.001)
    weights = [math.exp(-0.5 * ((offset / sigma) ** 2)) for offset in range(-radius, radius + 1)]
    smoothed = []
    for index in range(len(values)):
        weighted_sum = 0.0
        weight_sum = 0.0
        for offset, weight in zip(range(-radius, radius + 1), weights):
            sample_index = min(max(index + offset, 0), len(values) - 1)
            weighted_sum += float(values[sample_index]) * weight
            weight_sum += weight
        smoothed.append(weighted_sum / weight_sum)
    return smoothed


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(float(value) for value in values)
    percentile = max(0.0, min(float(percentile), 100.0))
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * percentile / 100.0
    left = int(math.floor(pos))
    right = min(left + 1, len(sorted_values) - 1)
    ratio = pos - left
    return sorted_values[left] * (1 - ratio) + sorted_values[right] * ratio


def normalize_density(values: Sequence[float], config: Optional[dict] = None) -> List[float]:
    if not values:
        return [0.0]
    config = config or {}
    min_height = max(0.0, min(float(config.get("min_density_height", 0.04)), 1.0))
    spike_boost = max(0.0, float(config.get("spike_boost", 0.8)))
    peak_percentile = float(config.get("peak_percentile", 95))
    curve_gamma = max(0.05, float(config.get("curve_gamma", 0.55)))

    enhanced = []
    previous = float(values[0])
    for value in values:
        value = float(value)
        spike = max(value - previous, 0.0)
        enhanced.append(value + spike * spike_boost)
        previous = value

    scale = _percentile(enhanced, peak_percentile)
    if scale <= 0:
        return [max(min_height, 0.02)] * len(values)

    normalized = []
    for value in enhanced:
        ratio = max(0.0, min(value / scale, 1.0))
        normalized.append(max(min_height, ratio ** curve_gamma))
    return normalized


def _rgba(color: str, opacity: float) -> Tuple[int, int, int, int]:
    rgb = ImageColor.getrgb(color or "#FFFFFF")
    alpha = max(0, min(int(float(opacity) * 255), 255))
    return rgb[0], rgb[1], rgb[2], alpha


def _catmull_rom(p0: float, p1: float, p2: float, p3: float, t: float) -> float:
    return 0.5 * (
        (2 * p1)
        + (-p0 + p2) * t
        + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
        + (-p0 + 3 * p1 - 3 * p2 + p3) * t * t * t
    )


def _interpolate(values: Sequence[float], x: int, width: int) -> float:
    if len(values) == 1 or width <= 1:
        return values[0] if values else 0.0
    pos = x * (len(values) - 1) / (width - 1)
    index = int(math.floor(pos))
    ratio = pos - index
    p0 = values[max(index - 1, 0)]
    p1 = values[index]
    p2 = values[min(index + 1, len(values) - 1)]
    p3 = values[min(index + 2, len(values) - 1)]
    return max(0.0, min(_catmull_rom(p0, p1, p2, p3, ratio), 1.0))


def generate_timeline_png(
    ass_path: str,
    output_path: str,
    duration: float,
    resolution: Tuple[int, int],
    config: dict,
    progress_output_path: Optional[str] = None,
) -> str:
    width, video_height = resolution
    width = max(int(width or 1920), 1)
    video_height = max(int(video_height or 1080), 1)
    timeline_height = max(1, int(video_height * float(config.get("height_ratio", 0.075))))
    bar_height = max(1, int(video_height * float(config.get("bar_height_ratio", 0.008))))
    margin_bottom = max(0, int(video_height * float(config.get("margin_bottom_ratio", 0.012))))
    graph_height = max(1, timeline_height - bar_height - margin_bottom)

    sample_interval, smooth_window = resolve_density_args(config, duration)
    density = bucket_danmaku_density(
        iter_ass_dialogue_start_times(ass_path),
        duration,
        sample_interval,
    )
    normalized = normalize_density(smooth_density(density, smooth_window), config)

    image = Image.new("RGBA", (width, timeline_height), (0, 0, 0, 0))
    progress_image = Image.new("RGBA", (width, timeline_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    progress_draw = ImageDraw.Draw(progress_image, "RGBA")
    draw.rectangle(
        (0, 0, width, timeline_height),
        fill=_rgba(config.get("background_color", "#000000"), config.get("background_opacity", 0.28)),
    )

    points = [(0, graph_height)]
    for x in range(width):
        value = _interpolate(normalized, x, width)
        y = graph_height - int(value * (graph_height - 1))
        points.append((x, y))
    points.append((width - 1, graph_height))
    density_opacity = float(config.get("opacity", 0.55))
    draw.polygon(points, fill=_rgba(config.get("density_color", "#FFFFFF"), density_opacity * 0.45))
    progress_draw.polygon(points, fill=_rgba(config.get("density_color", "#FFFFFF"), density_opacity))

    bar_y = max(0, timeline_height - margin_bottom - bar_height)
    draw.rectangle(
        (0, bar_y, width, bar_y + bar_height),
        fill=_rgba(config.get("density_color", "#FFFFFF"), min(config.get("opacity", 0.55), 0.32)),
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    image.save(output_path, "PNG")
    if progress_output_path:
        os.makedirs(os.path.dirname(progress_output_path), exist_ok=True)
        progress_image.save(progress_output_path, "PNG")
    return output_path


def ffmpeg_color(color: str, opacity: float = 1.0) -> str:
    rgb = ImageColor.getrgb(color or "#FFFFFF")
    alpha = max(0.0, min(float(opacity), 1.0))
    return "0x%02X%02X%02X@%.3f" % (rgb[0], rgb[1], rgb[2], alpha)


def ffmpeg_rgb_components(color: str) -> Tuple[int, int, int]:
    rgb = ImageColor.getrgb(color or "#FFFFFF")
    return rgb[0], rgb[1], rgb[2]


def has_explicit_filter_labels(filter_complex: str) -> bool:
    return bool(re.search(r"\[[^\]]+\]", filter_complex or ""))


def build_timeline_overlay_filter(
    base_label: str,
    timeline_input_index: int,
    timeline_progress_input_index: int,
    duration: float,
    output_width: int,
    output_height: int,
    config: dict,
) -> str:
    output_width = max(1, int(output_width or 1920))
    bar_height = max(1, int(output_height * float(config.get("bar_height_ratio", 0.008))))
    margin_bottom = max(0, int(output_height * float(config.get("margin_bottom_ratio", 0.012))))
    y_expr = max(0, output_height - margin_bottom - bar_height)
    duration = max(float(duration or 0), 0.001)
    progress_r, progress_g, progress_b = ffmpeg_rgb_components(config.get("progress_color", "#00A1D6"))
    progress_alpha = f"if(lte(X,W*min(max(T,0)/{duration:.6f},1)),255,0)"
    density_alpha = f"if(lte(X,W*min(max(T,0)/{duration:.6f},1)),alpha(X,Y),0)"
    return (
        f"[{timeline_input_index}:v]format=rgba[timeline];"
        f"[{base_label}][timeline]overlay=0:H-h:shortest=1[danmaku_timeline_ov];"
        f"[{timeline_progress_input_index}:v]format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{density_alpha}'[danmaku_timeline_density_progress];"
        f"[danmaku_timeline_ov][danmaku_timeline_density_progress]overlay=0:H-h:shortest=1[danmaku_timeline_density_ov];"
        f"color=c=black@0.0:s={output_width}x{bar_height}:d={duration:.6f},format=rgba,"
        f"geq=r='{progress_r}':g='{progress_g}':b='{progress_b}':a='{progress_alpha}'[danmaku_timeline_progress];"
        f"[danmaku_timeline_density_ov][danmaku_timeline_progress]overlay=0:{y_expr}:shortest=1[vout]"
    )


def merge_timeline_filter(
    filter_complex: Optional[str],
    timeline_input_index: int,
    timeline_progress_input_index: int,
    duration: float,
    output_width: int,
    output_height: int,
    config: dict,
    default_danmaku_filter: str,
    base_post_filter: Optional[str] = None,
) -> Tuple[Optional[str], bool, Optional[str]]:
    overlay_filter = build_timeline_overlay_filter(
        "base",
        timeline_input_index,
        timeline_progress_input_index,
        duration,
        output_width,
        output_height,
        config,
    )
    base_post_filter = f",{base_post_filter}" if base_post_filter else ""

    if not filter_complex:
        return f"[0:v]{default_danmaku_filter}{base_post_filter}[base];{overlay_filter}", True, None

    filter_complex = filter_complex.replace("\n", "").replace("\r", "")
    if TIMELINE_FILTER_PLACEHOLDER in filter_complex:
        return filter_complex.replace(TIMELINE_FILTER_PLACEHOLDER, overlay_filter), True, None

    if ";" in filter_complex or has_explicit_filter_labels(filter_complex):
        return (
            filter_complex,
            False,
            "danmaku_timeline enabled but advanced filter_complex is too complex to merge; "
            f"add {TIMELINE_FILTER_PLACEHOLDER} to opt in.",
        )

    return f"[0:v]{filter_complex}{base_post_filter}[base];{overlay_filter}", True, None
