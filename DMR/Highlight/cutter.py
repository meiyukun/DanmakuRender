import copy
import json
import os
import subprocess
import tempfile
from datetime import datetime

from DMR.utils import ToolsList, replace_keywords, safe_filename, uuid


def _run(command, logger):
    logger.debug("highlight ffmpeg args: %s", " ".join(str(value) for value in command))
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if process.returncode != 0:
        raise RuntimeError(process.stdout.decode("utf-8", errors="ignore"))


def _has_audio(path):
    ffprobe = ToolsList.get("ffprobe") or "ffprobe"
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return True


def map_range(segments, start, end):
    mapped = []
    for segment in segments:
        segment_start = float(segment["offset"])
        segment_end = segment_start + float(segment["duration"])
        overlap_start = max(start, segment_start)
        overlap_end = min(end, segment_end)
        if overlap_end > overlap_start:
            mapped.append({
                "path": segment["path"],
                "start": overlap_start - segment_start,
                "end": overlap_end - segment_start,
            })
    return mapped


def _escape_concat(path):
    return os.path.abspath(path).replace("'", "'\\''")


def render_outputs(segments, profiles, output_dir, base_info, config, logger):
    ffmpeg = config.get("ffmpeg") or ToolsList.get("ffmpeg") or "ffmpeg"
    os.makedirs(output_dir, exist_ok=True)
    format_name = config.get("format", "mp4")
    vencoder = config.get("vencoder", "libx264")
    aencoder = config.get("aencoder", "aac")
    vencoder_args = list(config.get("vencoder_args") or ["-crf", "20", "-preset", "medium"])
    aencoder_args = list(config.get("aencoder_args") or ["-b:a", "192k"])
    resolution = config.get("output_resolution") or base_info.resolution
    if resolution and isinstance(resolution, (list, tuple)):
        resolution = f"{int(resolution[0])}x{int(resolution[1])}"
    fps = int(config.get("output_fps", 30))
    outputs = []

    with tempfile.TemporaryDirectory(prefix="dmr-highlight-", dir=".temp") as temp_dir:
        clip_cache = {}
        for profile in profiles:
            clip_paths = []
            for clip in profile["clips"]:
                key = (clip["start"], clip["end"])
                if key not in clip_cache:
                    pieces = map_range(segments, clip["start"], clip["end"])
                    if not pieces:
                        continue
                    piece_paths = []
                    for piece_index, piece in enumerate(pieces):
                        piece_path = os.path.join(temp_dir, f"clip-{len(clip_cache)}-{piece_index}.{format_name}")
                        video_filter = f"fps={fps},format=yuv420p"
                        if resolution:
                            width, height = resolution.lower().split("x", 1)
                            video_filter = (
                                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,{video_filter}"
                            )
                        command = [
                            ffmpeg, "-y", "-ss", str(piece["start"]), "-to", str(piece["end"]),
                            "-i", piece["path"],
                        ]
                        if _has_audio(piece["path"]):
                            command += ["-map", "0:v:0", "-map", "0:a:0"]
                        else:
                            command += [
                                "-f", "lavfi", "-t", str(piece["end"] - piece["start"]),
                                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                                "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                            ]
                        command += [
                            "-vf", video_filter,
                            "-c:v", vencoder, *vencoder_args, "-c:a", aencoder, *aencoder_args,
                            "-ar", "48000", "-ac", "2", piece_path,
                        ]
                        _run(command, logger)
                        piece_paths.append(piece_path)
                    if len(piece_paths) == 1:
                        clip_cache[key] = piece_paths[0]
                    else:
                        clip_path = os.path.join(temp_dir, f"clip-{len(clip_cache)}-joined.{format_name}")
                        list_path = os.path.join(temp_dir, f"clip-{len(clip_cache)}.txt")
                        with open(list_path, "w", encoding="utf-8") as file:
                            file.write("\n".join(f"file '{_escape_concat(path)}'" for path in piece_paths))
                        _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", clip_path], logger)
                        clip_cache[key] = clip_path
                clip_paths.append(clip_cache[key])
            if not clip_paths:
                continue

            output_name = profile.get("output_name")
            if output_name:
                context = base_info.copy()
                context["highlight"] = {"id": profile["id"], "name": profile["name"]}
                output_name = replace_keywords(output_name, context, replace_invalid=True)
            else:
                output_name = f"{os.path.splitext(os.path.basename(base_info.path))[0]}-{profile['name']}"
            output_path = safe_filename(os.path.join(output_dir, f"{output_name}.{format_name}"))
            list_path = os.path.join(temp_dir, f"profile-{profile['id']}.txt")
            with open(list_path, "w", encoding="utf-8") as file:
                file.write("\n".join(f"file '{_escape_concat(path)}'" for path in clip_paths))
            partial = output_path + ".part." + format_name
            _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", partial], logger)
            os.replace(partial, output_path)
            video = copy.deepcopy(base_info)
            video.path = output_path
            video.file_id = uuid()
            video.dtype = f"highlight_{profile['id']}"
            video.size = os.path.getsize(output_path)
            video.duration = sum(clip["end"] - clip["start"] for clip in profile["clips"])
            video.highlight_profile = profile["id"]
            video.highlight_name = profile["name"]
            video.highlight_categories = profile.get("categories", ["*"])
            outputs.append(video)
    return outputs


def write_manifest(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, default=str)
    os.replace(temporary, path)
    return path
