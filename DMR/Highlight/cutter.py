import copy
import json
import os
import subprocess
import tempfile
from types import SimpleNamespace

from DMR.utils import ToolsList, replace_keywords, safe_filename, uuid


MAX_COPY_KEYFRAME_EXTENSION_SECONDS = 4.0


def _run(command, logger):
    logger.debug("highlight ffmpeg args: %s", " ".join(str(value) for value in command))
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if process.returncode != 0:
        raise RuntimeError(process.stdout.decode("utf-8", errors="ignore"))


def _probe(path, entries="stream=index,codec_type,codec_name,profile,width,height,pix_fmt,r_frame_rate,sample_rate,channels,channel_layout"):
    ffprobe = ToolsList.get("ffprobe") or "ffprobe"
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", entries, "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="ignore"))
    return json.loads(result.stdout or b"{}")


def _stream_signature(path):
    streams = _probe(path).get("streams") or []
    signature = []
    for stream in streams:
        if stream.get("codec_type") not in ("video", "audio"):
            continue
        signature.append(tuple((key, str(stream.get(key, ""))) for key in sorted(stream)))
    return tuple(signature)


def _previous_keyframe(path, position):
    if position <= 0:
        return 0.0
    ffprobe = ToolsList.get("ffprobe") or "ffprobe"
    interval_start = max(0.0, float(position) - 30.0)
    result = subprocess.run([
        ffprobe, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
        "-read_intervals", f"{interval_start}%{float(position) + 0.001}",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0", path,
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    values = []
    for value in result.stdout.decode("utf-8", errors="ignore").splitlines():
        try:
            values.append(float(value.strip().split(",")[0]))
        except ValueError:
            pass
    return max((value for value in values if value <= position + 0.01), default=0.0)


def _next_keyframe(path, position, duration):
    if position >= duration:
        return float(duration)
    ffprobe = ToolsList.get("ffprobe") or "ffprobe"
    result = subprocess.run([
        ffprobe, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
        "-read_intervals", f"{max(0.0, float(position) - 0.01)}%+30",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "csv=p=0", path,
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    for value in result.stdout.decode("utf-8", errors="ignore").splitlines():
        try:
            timestamp = float(value.strip().split(",")[0])
            if timestamp >= position - 0.01:
                return min(timestamp, float(duration))
        except ValueError:
            pass
    return float(duration)


def map_range(segments, start, end, outward=False):
    mapped = []
    for segment in segments:
        segment_start = float(segment["offset"])
        duration = float(segment["duration"])
        segment_end = segment_start + duration
        overlap_start, overlap_end = max(start, segment_start), min(end, segment_end)
        if overlap_end <= overlap_start:
            continue
        local_start, local_end = overlap_start - segment_start, overlap_end - segment_start
        if outward:
            if overlap_start == start:
                local_start = _previous_keyframe(segment["path"], local_start)
            if overlap_end == end:
                local_end = _next_keyframe(segment["path"], local_end, duration)
        mapped.append({"path": segment["path"], "start": local_start, "end": local_end,
                       "offset": segment_start})
    return mapped


def keyframe_extension_seconds(pieces, requested_start, requested_end):
    """Return total duration added before and after a requested clip."""
    if not pieces:
        return 0.0
    effective_start = pieces[0]["offset"] + pieces[0]["start"]
    effective_end = pieces[-1]["offset"] + pieces[-1]["end"]
    return max(0.0, requested_start - effective_start) + max(0.0, effective_end - requested_end)


def _escape_concat(path):
    return os.path.abspath(path).replace("'", "'\\''")


def _concat(paths, output, ffmpeg, logger):
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8", dir=".temp") as file:
        file.write("\n".join(f"file '{_escape_concat(path)}'" for path in paths))
        list_path = file.name
    try:
        partial = output + ".part" + os.path.splitext(output)[1]
        _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", partial], logger)
        os.replace(partial, output)
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def _encode_piece(piece, output, ffmpeg, config, base_info, logger):
    resolution = config.get("output_resolution") or base_info.resolution
    if resolution and isinstance(resolution, (list, tuple)):
        resolution = f"{int(resolution[0])}x{int(resolution[1])}"
    video_filter = f"fps={int(config.get('output_fps', 30))},format=yuv420p"
    if resolution:
        width, height = str(resolution).lower().split("x", 1)
        video_filter = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,{video_filter}")
    streams = _probe(piece["path"]).get("streams") or []
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    command = [ffmpeg, "-y", "-ss", str(piece["start"]), "-t", str(piece["end"] - piece["start"]),
               "-i", piece["path"]]
    if not has_audio:
        command += ["-f", "lavfi", "-t", str(piece["end"] - piece["start"]),
                    "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    command += ["-map", "0:v:0"]
    if has_audio:
        command += ["-map", "0:a:0"]
    else:
        command += ["-map", "1:a:0", "-shortest"]
    command += ["-vf", video_filter, "-c:v", config.get("vencoder", "libx264"),
                *list(config.get("vencoder_args") or ["-crf", "20", "-preset", "medium"])]
    command += ["-c:a", config.get("aencoder", "aac"),
                *list(config.get("aencoder_args") or ["-b:a", "192k"]), "-ar", "48000", "-ac", "2"]
    _run(command + [output], logger)


def render_highlight(segments, candidates, selected, output_dir, base_info, config, mix, logger):
    """Persist one physical file per hotspot event, then create one initial mix.

    Candidate categories are metadata only. This function intentionally never
    concatenates candidates by category before writing the clip library.
    """
    ffmpeg = config.get("ffmpeg") or ToolsList.get("ffmpeg") or "ffmpeg"
    format_name = config.get("format", "mp4")
    clip_dir = os.path.join(output_dir, "clips")
    os.makedirs(clip_dir, exist_ok=True)
    requested_mode = str(config.get("mode", "copy")).lower()
    signatures = [_stream_signature(item["path"]) for item in segments]
    effective_mode = "copy" if requested_mode == "copy" and len(set(signatures)) == 1 else "reencode"
    if requested_mode == "copy" and effective_mode != "copy" and config.get("copy_fallback", "reencode") != "reencode":
        raise RuntimeError("源分段编码参数不一致，无法无重编码拼接")
    outward = effective_mode == "copy" and config.get("keyframe_alignment", "outward") == "outward"
    aligned_pieces = {}
    if outward:
        for candidate in candidates:
            clip_id = str(candidate.get("id"))
            pieces = map_range(segments, candidate["start"], candidate["end"], outward=True)
            aligned_pieces[clip_id] = pieces
            extension = keyframe_extension_seconds(pieces, candidate["start"], candidate["end"])
            if extension > MAX_COPY_KEYFRAME_EXTENSION_SECONDS + 1e-6:
                effective_mode = "reencode"
                outward = False
                aligned_pieces = {}
                break
    clip_records, clip_paths = [], {}
    selected_ids = {str(item.get("id")) for item in selected}
    for index, candidate in enumerate(sorted(candidates, key=lambda item: item["start"]), 1):
        clip_id = str(candidate.get("id") or f"clip-{index:03d}")
        pieces = aligned_pieces.get(clip_id) if outward else None
        if pieces is None:
            pieces = map_range(segments, candidate["start"], candidate["end"], outward=False)
        if not pieces:
            continue
        filename = safe_filename(os.path.join(clip_dir, f"{index:03d}-{clip_id}.{format_name}"))
        piece_paths = []
        with tempfile.TemporaryDirectory(prefix="dmr-highlight-piece-", dir=".temp") as temp_dir:
            for piece_index, piece in enumerate(pieces):
                piece_path = filename if len(pieces) == 1 else os.path.join(temp_dir, f"{piece_index}.{format_name}")
                if effective_mode == "copy":
                    _run([ffmpeg, "-y", "-ss", str(piece["start"]), "-t", str(piece["end"] - piece["start"]),
                          "-i", piece["path"], "-map", "0:v:0", "-map", "0:a?", "-c", "copy", piece_path], logger)
                else:
                    _encode_piece(piece, piece_path, ffmpeg, config, base_info, logger)
                piece_paths.append(piece_path)
            if len(piece_paths) > 1:
                _concat(piece_paths, filename, ffmpeg, logger)
        effective_start = pieces[0]["offset"] + pieces[0]["start"]
        effective_end = pieces[-1]["offset"] + pieces[-1]["end"]
        record = {"id": clip_id, "sequence": index, "path": filename,
                  "category": candidate.get("category"),
                  "score": candidate.get("score", 0), "title": candidate.get("ai_title", ""),
                  "ai_reviewed": candidate.get("ai_reviewed", False),
                  "ai_keep": candidate.get("ai_keep", True),
                  "ai_status": candidate.get("ai_status", "disabled"),
                  "ai_confidence": candidate.get("ai_confidence"),
                  "ai_category": candidate.get("ai_category"),
                  "ai_reason": candidate.get("ai_reason", ""),
                  "auto_selected": clip_id in selected_ids,
                  "requested_start": candidate["start"], "requested_end": candidate["end"],
                  "effective_start": effective_start, "effective_end": effective_end,
                  "duration": effective_end - effective_start, "encoding_mode": effective_mode}
        clip_records.append(record)
        clip_paths[clip_id] = filename
    ordered_paths = [clip_paths[str(item.get("id"))] for item in selected if str(item.get("id")) in clip_paths]
    outputs = []
    if ordered_paths:
        name = mix.get("output_name")
        if name:
            context = base_info.copy(); context["highlight"] = {"id": mix["id"], "name": mix["name"]}
            name = replace_keywords(name, context, replace_invalid=True)
        else:
            name = f"{os.path.splitext(os.path.basename(base_info.path))[0]}-{mix['name']}"
        output_path = safe_filename(os.path.join(output_dir, f"{name}.{format_name}"))
        _concat(ordered_paths, output_path, ffmpeg, logger)
        video = copy.deepcopy(base_info)
        video.path, video.file_id, video.dtype = output_path, uuid(), f"highlight_{mix['id']}"
        video.size = os.path.getsize(output_path)
        video.duration = sum(next(record["duration"] for record in clip_records if record["id"] == str(item["id"])) for item in selected if str(item.get("id")) in clip_paths)
        video.highlight_profile, video.highlight_name = mix["id"], mix["name"]
        video.highlight_categories = mix.get("categories", ["*"])
        outputs.append(video)
    return outputs, clip_records, effective_mode


def recompose_clips(paths, output, config, logger):
    ffmpeg = config.get("ffmpeg") or ToolsList.get("ffmpeg") or "ffmpeg"
    signatures = [_stream_signature(path) for path in paths]
    if len(set(signatures)) == 1:
        try:
            _concat(paths, output, ffmpeg, logger)
            return output
        except Exception:
            logger.warning("热点素材媒体参数无法直接拼接，自动统一编码。", exc_info=True)
    first_streams = _probe(paths[0]).get("streams") or []
    first_video = next((stream for stream in first_streams if stream.get("codec_type") == "video"), {})
    base_info = SimpleNamespace(resolution=(first_video.get("width") or 1920, first_video.get("height") or 1080))
    with tempfile.TemporaryDirectory(prefix="dmr-highlight-remix-", dir=".temp") as temp_dir:
        normalized = []
        extension = os.path.splitext(output)[1] or ".mp4"
        for index, path in enumerate(paths):
            duration_info = _probe(path, "format=duration").get("format") or {}
            duration = float(duration_info.get("duration") or 0)
            if duration <= 0:
                raise RuntimeError(f"无法取得热点素材时长: {path}")
            normalized_path = os.path.join(temp_dir, f"{index:03d}{extension}")
            _encode_piece({"path": path, "start": 0.0, "end": duration}, normalized_path,
                          ffmpeg, config, base_info, logger)
            normalized.append(normalized_path)
        _concat(normalized, output, ffmpeg, logger)
    return output


def write_manifest(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=str)
    os.replace(temporary, path)
    return path
