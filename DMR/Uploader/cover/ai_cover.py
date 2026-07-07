import base64
import json
import logging
import os
import re
import subprocess
import time
from io import BytesIO

import requests
from PIL import Image

from DMR.Uploader.cover.danmaku_analyzer import analyze_danmaku_files, compact_analysis_for_ai
from DMR.utils import replace_keywords
from DMR.utils.toolsmgr import ToolsList
from DMR.utils.utils import get_tempfile


logger = logging.getLogger(__name__)

DEFAULT_PROMPT = """为B站视频生成一张高点击率完整封面。
视频标题：{TITLE}
主播：{STREAMER.NAME}
弹幕内容分析：{DANMAKU_SUMMARY}
热点弹幕：{HOT_DANMAKU}
关键词：{HOT_KEYWORDS}

要求：画面醒目、清晰、有强烈视觉中心，贴近本次录屏/直播回放的节目内容；包含适合封面的中文标题排版；不要使用真实平台Logo，不要生成二维码。"""

DEFAULT_ANALYSIS_SYSTEM_PROMPT = (
    "你是B站视频运营和直播内容分析助手。根据经过清洗和压缩的弹幕热点，"
    "判断本次录屏/直播回放的节目内容、观众最关注的点，并直接写出可用于AI生图的完整封面提示词。"
    "只返回提示词正文，不要返回JSON，不要返回Markdown。"
)

DEFAULT_ANALYSIS_PROMPT = """视频标题：{TITLE}
主播：{STREAMER.NAME}
弹幕热点资料：
{DANMAKU_COMPACT}

请直接输出一段完整的AI生图提示词，用于生成B站视频封面。
要求：贴近本次视频内容和弹幕热点；如果随生图请求附带直播截图，请明确让生图模型参考截图的画面内容、构图、色调和游戏场景氛围，但不要直接复刻截图；包含画面主体、场景氛围、构图、中文封面标题排版建议；避免真实平台Logo、二维码、真人脸；只返回提示词正文。"""


def _get_api_key(ai_config):
    env_name = ai_config.get("api_key_env") or "DMR_IMAGE_API_KEY"
    key = os.getenv(env_name) if env_name else None
    return key or ai_config.get("api_key") or ""


def _api_url(ai_config, path):
    base_url = (ai_config.get("base_url") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("AI封面已开启，但未配置 cover_auto.ai.base_url")
    return f"{base_url}{path}"


def _headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _extract_json_object(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _format_list(items, key="text", limit=8):
    values = []
    for item in items[:limit]:
        if isinstance(item, dict):
            values.append(str(item.get(key, "")))
        else:
            values.append(str(item))
    return "、".join([x for x in values if x])


def _clean_text_ai_prompt(content):
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    return content.strip()


def _format_ai_summary_for_log(danmaku_analysis, ai_prompt):
    lines = []
    local_summary = (danmaku_analysis or {}).get("summary")
    if local_summary:
        lines.append(f"本地弹幕摘要: {local_summary}")
    if ai_prompt:
        lines.append(f"文本AI生成的完整生图提示词:\n{ai_prompt}")
    if not lines and danmaku_analysis:
        lines.append(f"有效弹幕数量: {danmaku_analysis.get('total', 0)}")
    return "\n".join(lines)


def _build_prompt_context(video_info, danmaku_analysis=None):
    try:
        context = video_info.copy()
    except Exception:
        context = dict(video_info)

    danmaku_analysis = danmaku_analysis or {}
    local_summary = danmaku_analysis.get("summary") or "无可用弹幕分析"

    context.update({
        "danmaku_summary": local_summary,
        "danmaku_local_summary": local_summary,
        "hot_danmaku": _format_list(danmaku_analysis.get("hot_danmaku", [])),
        "hot_keywords": _format_list(danmaku_analysis.get("hot_keywords", [])),
        "peak_periods": _format_list(danmaku_analysis.get("peak_periods", []), key="period", limit=3),
        "main_topics": "",
        "visual_prompt_hints": "",
        "suggested_cover_text": "",
    })
    return context


def analyze_danmaku_with_ai(ai_config, video_info, danmaku_analysis, api_key):
    if not danmaku_analysis or danmaku_analysis.get("total", 0) <= 0:
        return {}
    if ai_config.get("analysis_mode", "local_then_ai") not in ("local_then_ai", "ai", "force_ai"):
        return {}

    compact = compact_analysis_for_ai(
        danmaku_analysis,
        max_chars=int(ai_config.get("analysis_max_chars", 3500) or 3500),
    )
    if not compact.strip():
        return {}

    try:
        prompt_context = video_info.copy()
    except Exception:
        prompt_context = dict(video_info)
    prompt_context.update({
        "danmaku_compact": compact,
    })
    system_prompt = replace_keywords(
        ai_config.get("analysis_system_prompt") or DEFAULT_ANALYSIS_SYSTEM_PROMPT,
        prompt_context,
    )
    user_prompt = replace_keywords(
        ai_config.get("analysis_prompt") or DEFAULT_ANALYSIS_PROMPT,
        prompt_context,
    )

    timeout = int(ai_config.get("analysis_timeout", ai_config.get("timeout", 120)) or 120)
    payload = {
        "model": ai_config.get("analysis_model") or "gpt-5.4",
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "max_tokens": int(ai_config.get("analysis_max_tokens", 600) or 600),
    }

    response = requests.post(
        _api_url(ai_config, "/v1/chat/completions"),
        headers=_headers(api_key),
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _clean_text_ai_prompt(content)


def _get_target_resolution(video_info):
    resolution = getattr(video_info, "resolution", None)
    if not resolution and isinstance(video_info, dict):
        resolution = video_info.get("resolution")
    if not resolution or len(resolution) < 2:
        return None
    try:
        width, height = int(resolution[0]), int(resolution[1])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _get_video_path(video_info):
    path = getattr(video_info, "path", None)
    if not path and isinstance(video_info, dict):
        path = video_info.get("path")
    return path if path and os.path.exists(path) else None


def _get_video_duration(video_info):
    duration = getattr(video_info, "duration", None)
    if duration is None and isinstance(video_info, dict):
        duration = video_info.get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _get_image_request_sizes(ai_config, video_info):
    if ai_config.get("size"):
        return [ai_config.get("size")]
    if ai_config.get("size_candidates"):
        candidates = ai_config.get("size_candidates")
        if isinstance(candidates, str):
            candidates = [item.strip() for item in candidates.split(",")]
        return [item for item in candidates if item]

    target_resolution = _get_target_resolution(video_info)
    if not target_resolution:
        return ["2048x1152", "1536x1024", "1024x1024"]
    width, height = target_resolution
    ratio = width / height
    if 0.95 <= ratio <= 1.05:
        return ["2048x2048", "1024x1024"]
    if ratio > 1:
        return ["2048x1152", "1536x1024", "1024x1024"]
    return ["1024x1536", "1024x1024"]


def _parse_period_start(period):
    if not period:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", str(period))
    if not match:
        return None
    parts = [int(x) if x is not None else 0 for x in match.groups()]
    if match.group(3) is None:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _reference_frame_second(ai_config, video_info, danmaku_analysis):
    ref_config = ai_config.get("reference_image") or {}
    if ref_config.get("second") is not None:
        try:
            return max(0.0, float(ref_config.get("second")))
        except (TypeError, ValueError):
            pass

    duration = _get_video_duration(video_info)
    source = ref_config.get("source", "peak_danmaku")
    if source == "peak_danmaku":
        for item in (danmaku_analysis or {}).get("peak_periods", []):
            second = _parse_period_start(item.get("period") if isinstance(item, dict) else item)
            if second is not None:
                offset = float(ref_config.get("peak_offset_seconds", 30) or 0)
                second += offset
                if duration:
                    return min(max(0.0, second), max(0.0, duration - 1))
                return max(0.0, second)

    ratio = float(ref_config.get("fallback_position", 0.5) or 0.5)
    ratio = min(max(ratio, 0.0), 1.0)
    if duration:
        return max(0.0, min(duration * ratio, max(0.0, duration - 1)))
    return 1.0


def _extract_reference_frame(ai_config, video_info, danmaku_analysis):
    ref_config = ai_config.get("reference_image") or {}
    enabled = ref_config.get("enabled", ai_config.get("use_reference_image", True))
    if not enabled:
        return None

    video_path = _get_video_path(video_info)
    if not video_path:
        logger.warning("AI封面参考图已开启，但没有可用视频文件路径。")
        return None

    ffmpeg = ToolsList.get("ffmpeg", auto_install=False) or "ffmpeg"
    output_path = get_tempfile(prefix="ai-cover-reference", suffix="jpg")
    second = _reference_frame_second(ai_config, video_info, danmaku_analysis)
    quality = int(ref_config.get("jpeg_quality", 85) or 85)
    quality = min(max(quality, 1), 100)
    scale_width = int(ref_config.get("max_width", 1280) or 1280)
    scale_width = max(scale_width, 320)
    vf = f"scale='min({scale_width},iw)':-2"
    cmds = [
        ffmpeg,
        "-y",
        "-ss",
        f"{second:.3f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-q:v",
        str(max(2, min(31, int(31 - quality * 0.29)))),
        output_path,
    ]
    try:
        proc = subprocess.run(
            cmds,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=int(ref_config.get("timeout", 30) or 30),
            check=False,
        )
        if proc.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
            log_tail = proc.stdout.decode("utf-8", errors="ignore")[-800:]
            logger.warning("AI封面参考图截帧失败，将使用纯文本生图: %s", log_tail)
            return None
        logger.info("AI封面参考图截帧成功: %s 秒, %s", f"{second:.2f}", output_path)
        return output_path
    except Exception as e:
        logger.warning("AI封面参考图截帧失败，将使用纯文本生图: %s", e)
        return None


def _image_data_url(image_path):
    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    ext = os.path.splitext(image_path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    return f"data:{mime};base64,{data}"


def _redact_image_response(data):
    if not isinstance(data, dict):
        return str(type(data).__name__)
    redacted = {}
    for key, value in data.items():
        if key == "data" and isinstance(value, list):
            items = []
            for item in value[:1]:
                if isinstance(item, dict):
                    items.append({
                        item_key: f"<str len={len(item_value)}>" if isinstance(item_value, str) else type(item_value).__name__
                        for item_key, item_value in item.items()
                    })
                else:
                    items.append(type(item).__name__)
            redacted[key] = items
        else:
            redacted[key] = value
    return redacted


def _find_image_base64(data):
    if isinstance(data, dict):
        if data.get("error"):
            raise RuntimeError(f"AI生图接口返回错误: {data.get('error')}")
        for key in ("result", "b64_json", "image_base64", "base64", "data"):
            value = data.get(key)
            if isinstance(value, str) and len(value) > 100:
                return value
        for value in data.values():
            found = _find_image_base64(value)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_image_base64(item)
            if found:
                return found
    return None


def _save_image_from_b64(b64_json, output_path):
    if "," in b64_json and b64_json.lstrip().startswith("data:"):
        b64_json = b64_json.split(",", 1)[1]
    image_bytes = base64.b64decode(b64_json)
    with Image.open(BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        image.save(output_path, format="PNG")
    return output_path


def _image_tool(ai_config, size):
    tool = {
        "type": "image_generation",
        "size": size,
        "output_format": "png",
        "background": "opaque",
        "moderation": "auto",
        "partial_images": 0,
    }
    for key in ("output_format", "output_compression", "background", "moderation", "partial_images"):
        value = ai_config.get(key)
        if value is not None:
            tool[key] = value
    return tool


def _responses_image_payload(ai_config, prompt, size, reference_image=None):
    content = prompt
    if reference_image:
        content = [
            {
                "type": "input_text",
                "text": prompt,
            },
            {
                "type": "input_image",
                "image_url": _image_data_url(reference_image),
            },
        ]
    return {
        "model": ai_config.get("response_model") or ai_config.get("model") or "gpt-5.5",
        "stream": bool(ai_config.get("stream", False)),
        "input": [{
            "role": "user",
            "content": content,
        }],
        "tools": [_image_tool(ai_config, size)],
    }


def _parse_sse_json(line):
    if not line:
        return None
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="ignore")
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line == "[DONE]":
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _request_image_generation(ai_config, api_key, payload, timeout):
    retries = int(ai_config.get("image_retries", 3) or 3)
    retries = max(1, retries)
    payloads = [payload]
    if payload.get("stream", False) and ai_config.get("fallback_non_stream", True):
        non_stream_payload = payload.copy()
        non_stream_payload["stream"] = False
        payloads.append(non_stream_payload)

    last_error = None
    for payload_idx, request_payload in enumerate(payloads):
        if payload_idx > 0:
            logger.warning("流式AI生图失败，将使用非流式Responses请求重试当前尺寸。")
        for attempt in range(1, retries + 1):
            stream = bool(request_payload.get("stream", True))
            try:
                response = requests.post(
                    _api_url(ai_config, "/v1/responses"),
                    headers=_headers(api_key),
                    json=request_payload,
                    timeout=timeout,
                    stream=stream,
                )
                try:
                    response.raise_for_status()
                except requests.HTTPError as e:
                    body = ""
                    try:
                        body = response.text[:1000]
                    except Exception:
                        body = "<无法读取响应体>"
                    raise RuntimeError(f"{e}; response_body={body}") from e
                if stream:
                    data = None
                    for line in response.iter_lines():
                        event = _parse_sse_json(line)
                        if not event:
                            continue
                        if event.get("type") == "error" or event.get("error"):
                            raise RuntimeError(f"AI生图接口返回错误: {event.get('error') or event}")
                        data = event
                        b64_json = _find_image_base64(event)
                        if b64_json:
                            return b64_json
                    if data is None:
                        raise RuntimeError("AI生图接口没有返回有效stream事件")
                    raise RuntimeError(f"AI生图响应中没有图片base64: {_redact_image_response(data)}")
                else:
                    data = response.json()
                    b64_json = _find_image_base64(data)
                    if b64_json:
                        return b64_json
                    raise RuntimeError(f"AI生图响应中没有图片base64: {_redact_image_response(data)}")
            except Exception as e:
                last_error = e
                if attempt >= retries:
                    break
                sleep_seconds = min(2 ** (attempt - 1), 8)
                logger.warning("AI生图请求失败，%s秒后重试(%s/%s): %s", sleep_seconds, attempt, retries, e)
                time.sleep(sleep_seconds)
    if last_error:
        raise last_error
    raise RuntimeError("AI生图请求未执行")


def generate_ai_cover(files, video_info, cover_auto_config):
    ai_config = (cover_auto_config or {}).get("ai") or {}
    if not ai_config.get("enabled", False):
        return None

    api_key = _get_api_key(ai_config)
    if not api_key:
        logger.warning("AI封面已开启，但未找到API密钥，请设置 %s 或 cover_auto.ai.api_key", ai_config.get("api_key_env") or "DMR_IMAGE_API_KEY")
        return None

    danmaku_analysis = {}
    ai_prompt = ""
    if ai_config.get("analyze_danmaku", True):
        paths = []
        for file in files:
            dm_file = getattr(file, "dm_file_id", None) if not isinstance(file, dict) else file.get("dm_file_id")
            if dm_file:
                paths.append(dm_file)
        danmaku_analysis = analyze_danmaku_files(
            paths,
            bucket_seconds=int(ai_config.get("analysis_bucket_seconds", 300) or 300),
            max_items=int(ai_config.get("analysis_max_items", 4000) or 4000),
        )
        if danmaku_analysis.get("total", 0) > 0:
            try:
                ai_prompt = analyze_danmaku_with_ai(ai_config, video_info, danmaku_analysis, api_key)
            except Exception as e:
                logger.warning("AI弹幕分析失败，将使用本地弹幕热点结果生成封面: %s", e)
        summary_log = _format_ai_summary_for_log(danmaku_analysis, ai_prompt)
        if summary_log:
            logger.info("AI封面弹幕热点分析结果:\n%s", summary_log)

    if ai_prompt:
        prompt = ai_prompt
    else:
        prompt_context = _build_prompt_context(video_info, danmaku_analysis)
        prompt_template = ai_config.get("prompt") or DEFAULT_PROMPT
        prompt = replace_keywords(prompt_template, prompt_context)
    logger.info("AI封面生图提示词:\n%s", prompt)
    reference_image = _extract_reference_frame(ai_config, video_info, danmaku_analysis)

    timeout = int(ai_config.get("timeout", 120) or 120)
    sizes = _get_image_request_sizes(ai_config, video_info)
    fallback_size = ai_config.get("fallback_size", "1024x1024")
    if fallback_size and fallback_size not in sizes:
        sizes.append(fallback_size)

    last_error = None
    b64_json = None
    for idx, size in enumerate(sizes):
        image_payload = _responses_image_payload(ai_config, prompt, size, reference_image=reference_image)
        try:
            b64_json = _request_image_generation(ai_config, api_key, image_payload, timeout)
            break
        except Exception as e:
            last_error = e
            if reference_image:
                logger.warning("带参考图AI生图失败，将改用纯文本生图重试当前尺寸 %s: %s", size, e)
                try:
                    image_payload = _responses_image_payload(ai_config, prompt, size, reference_image=None)
                    b64_json = _request_image_generation(ai_config, api_key, image_payload, timeout)
                    break
                except Exception as text_error:
                    last_error = text_error
            if idx >= len(sizes) - 1:
                raise last_error
            logger.warning("AI生图尺寸 %s 失败，将尝试下一个尺寸 %s: %s", size, sizes[idx + 1], last_error)
    if not b64_json and last_error:
        raise last_error

    output_path = get_tempfile(prefix="ai-bili-cover", suffix="png")
    return _save_image_from_b64(b64_json, output_path)
