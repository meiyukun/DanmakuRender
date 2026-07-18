import json
import logging
import os
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from DMR.utils import (
    DateTimeDecoder, FFprobe, PipeMessage, VideoInfo, atomic_json_dump,
    video_info_from_dict, uuid,
)
from .analyzer import find_hotspots, parse_danmaku, parse_srt, select_profile
from .cutter import render_highlight, write_manifest


DEFAULT_PROFILES = [{
    "id": "all",
    "name": "综合高能",
    "categories": ["*"],
    "max_clips": 12,
    "max_total_duration": 300,
}]


def filter_clip_candidates(candidates, categories):
    """Keep individual hotspot events; categories are filters, never aggregation keys."""
    allowed = set(categories or ["*"])
    return [candidate for candidate in candidates
            if "*" in allowed or candidate.get("category") in allowed]


def attach_subtitle_excerpts(candidates, subtitles, padding=6.0, max_lines=20):
    """给热点候选附加邻近字幕正文，供后续标题和封面文案提炼。"""
    enriched = []
    for candidate in candidates:
        item = dict(candidate)
        start = float(item.get("start", 0)) - max(0, float(padding))
        end = float(item.get("end", 0)) + max(0, float(padding))
        excerpt, seen = [], set()
        for line in subtitles:
            text = str(line.get("text") or "").strip()
            if not text or text in seen:
                continue
            if float(line.get("end", 0)) <= start or float(line.get("start", 0)) >= end:
                continue
            seen.add(text)
            excerpt.append({
                "start": round(float(line.get("start", 0)), 3),
                "end": round(float(line.get("end", 0)), 3),
                "text": text[:300],
            })
            if len(excerpt) >= max(1, int(max_lines)):
                break
        item["subtitle_excerpt"] = excerpt
        enriched.append(item)
    return enriched

DEFAULT_AI_SYSTEM = (
    "你是直播内容剪辑审核员。只能依据候选区间内带时间戳的弹幕、字幕和统计特征，"
    "判断它是否是直播内容本身产生的精彩片段。福袋、口令、抽奖、礼物感谢、欢迎告别、"
    "广告引导等互动必须拒绝。弹幕虽多但主要是各自讨论、缺少短时同步反应的片段也应拒绝；"
    "短时间集中出现哈哈、问号、帅、666等相似反应是重要正向证据。只返回JSON。"
)


class Highlight:
    def __init__(self, pipe, nhighlights=1, state_name=None, **kwargs):
        self.send_queue, self.recv_queue = pipe
        self.logger = logging.getLogger("DMR.Highlight")
        self.stoped = True
        self.tasks = {}
        self.failed_tasks = {}
        self.executors = ThreadPoolExecutor(max_workers=max(1, int(nhighlights)))
        self._lock = threading.Lock()
        self.ai_client = kwargs.get('ai_client')
        suffix = f"_{state_name}" if state_name else ""
        self.active_file = f".temp/active_highlights{suffix}.json"
        self.failed_file = f".temp/failed_highlights{suffix}.json"
        self._load_tasks()

    def _load_json(self, path):
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file, cls=DateTimeDecoder)
        except Exception as error:
            self.logger.warning("恢复热点任务失败 %s: %s", path, error)
            return {}

    @staticmethod
    def _restore_task(task):
        for segment in task.get("segments", []):
            if isinstance(segment.get("video"), dict):
                segment["video"] = video_info_from_dict(segment["video"])
        result = task.get("result")
        if isinstance(result, dict):
            result["outputs"] = [
                video_info_from_dict(output) if isinstance(output, dict) else output
                for output in result.get("outputs", [])
            ]
        return task

    def _load_tasks(self):
        self.tasks = {key: self._restore_task(value) for key, value in self._load_json(self.active_file).items()}
        self.failed_tasks = {key: self._restore_task(value) for key, value in self._load_json(self.failed_file).items()}
        for task in self.tasks.values():
            if task.get("status") != "completion_pending":
                task["status"] = "interrupted"

    def _save(self):
        atomic_json_dump(self.tasks, self.active_file)
        atomic_json_dump(self.failed_tasks, self.failed_file)

    def _send(self, event, message, task=None, data=None):
        self.send_queue.put(PipeMessage(
            source="highlight",
            target=task.get("source", "engine") if task else "engine",
            event=event,
            request_id=task.get("request_id") if task else None,
            msg=message,
            data=data,
        ))

    def _monitor(self):
        while not self.stoped:
            message = self.recv_queue.get()
            try:
                if message.event == "newtask":
                    self.add_task(message)
                elif message.event == "ack":
                    self.ack(message.request_id)
                elif message.event == "discard":
                    self.discard(message.request_id)
                elif message.event == "exit":
                    break
            except Exception as error:
                self.logger.exception("热点消息处理失败: %s", error)

    def start(self):
        self.stoped = False
        threading.Thread(target=self._monitor, daemon=True).start()
        if self.tasks:
            self.logger.info("Loaded %s paused highlight worker tasks; waiting for explicit recovery.", len(self.tasks))

    def add_task(self, message):
        with self._lock:
            for task in list(self.tasks.values()) + list(self.failed_tasks.values()):
                if message.request_id and task.get("request_id") == message.request_id:
                    self._send("accepted", "热点任务已存在", task)
                    return
            data = message.data or {}
            task = {
                "uuid": uuid(), "source": message.source, "request_id": message.request_id,
                "taskname": data.get("taskname"), "source_task": data.get("source_task"),
                "group_id": data.get("group_id"),
                "segments": data.get("segments") or [], "config": data.get("args") or {},
                "source_segment_count": data.get("source_segment_count"),
                "ignored_segments": data.get("ignored_segments") or [],
                "subtitle_status": data.get("subtitle_status") or {}, "status": "waiting",
            }
            self.tasks[task["uuid"]] = task
            self._save()
            self._send("accepted", "热点任务已持久化", task)
            self.executors.submit(self._run_task, task)

    def ack(self, request_id):
        with self._lock:
            for task_id, task in list(self.tasks.items()):
                if task.get("request_id") == request_id and task.get("status") == "completion_pending":
                    self.tasks.pop(task_id, None)
                    self._save()
                    return

    def discard(self, request_id):
        """Remove a paused/replaced worker task without deleting generated videos."""
        with self._lock:
            removed = False
            for collection in (self.tasks, self.failed_tasks):
                for task_id, task in list(collection.items()):
                    if task.get("request_id") == request_id:
                        collection.pop(task_id, None)
                        removed = True
            if removed:
                self._save()

    @staticmethod
    def _extract_json(text):
        try:
            return json.loads(text)
        except Exception:
            match = re.search(r"\{.*\}", text or "", re.S)
            return json.loads(match.group(0)) if match else None

    def _ai_review(self, candidates, subtitles, config):
        ai = config.get("ai") or {}
        if not ai.get("enabled") or not candidates:
            return [dict(candidate, ai_reviewed=False, ai_keep=True, ai_status="disabled")
                    for candidate in candidates]
        if not self.ai_client or not self.ai_client.available:
            self.logger.warning("热点AI未配置完整，使用严格本地结果兜底")
            fallback_ids = {str(item.get("id")) for item in self._strict_fallback(candidates)}
            return [dict(candidate, ai_reviewed=False,
                         ai_keep=str(candidate.get("id")) in fallback_ids, ai_status="fallback")
                    for candidate in candidates]
        allowed = set((config.get("categories") or {
            "funny": {}, "skill": {}, "absurd": {}, "fail": {},
            "emotional": {}, "other_content": {},
        }).keys())
        reviewed = []
        try:
            batch_size = max(1, int(ai.get("batch_size", 8)))
            for offset in range(0, len(candidates), batch_size):
                batch = candidates[offset:offset + batch_size]
                material = []
                for candidate in batch:
                    subtitle_lines = [
                        line for line in subtitles
                        if line["end"] > candidate["start"] and line["start"] < candidate["end"]
                    ][:30]
                    material.append({
                        "id": candidate["id"], "start": candidate["start"], "end": candidate["end"],
                        "peak_height": candidate["peak_height"], "prominence": candidate["prominence"],
                        "local_category": candidate["category"],
                        "reaction_count": candidate.get("reaction_count", 0),
                        "reaction_ratio": candidate.get("reaction_ratio", 0),
                        "dominant_reaction": candidate.get("dominant_reaction"),
                        "dominant_reaction_ratio": candidate.get("dominant_reaction_ratio", 0),
                        "short_message_ratio": candidate.get("short_message_ratio", 0),
                        "discussion_ratio": candidate.get("discussion_ratio", 0),
                        "reaction_span": candidate.get("reaction_span", 0),
                        "danmaku": candidate.get("representative", [])[:30], "subtitles": subtitle_lines,
                    })
                prompt = {
                    "allowed_categories": sorted(allowed),
                    "required_schema": {"candidates": [{
                        "id": "peak-id", "keep": True, "category": "funny",
                        "confidence": 0.8, "title": "片段标题", "reason": "判断依据",
                    }]},
                    "candidates": material,
                }
                content = self.ai_client.chat('highlight_review', [
                    {"role": "system", "content": ai.get("system_prompt") or DEFAULT_AI_SYSTEM},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ])
                result = self._extract_json(content) or {}
                decisions = {item.get("id"): item for item in result.get("candidates", [])}
                for candidate in batch:
                    decision = decisions.get(candidate["id"])
                    candidate = dict(candidate)
                    confidence = float((decision or {}).get("confidence", 0))
                    keep = bool(decision and decision.get("keep") and
                                confidence >= float(ai.get("min_confidence", 0.65)))
                    category = (decision or {}).get("category")
                    if category not in allowed:
                        category = None
                    candidate.update({
                        "ai_reviewed": bool(decision), "ai_keep": keep,
                        "ai_status": "approved" if keep else "rejected",
                        "ai_confidence": confidence,
                        "ai_category": category,
                        "ai_title": (decision or {}).get("title", ""),
                        "ai_reason": (decision or {}).get("reason", ""),
                    })
                    if keep and category:
                        candidate["category"] = category
                    reviewed.append(candidate)
            reviewed.sort(
                key=lambda item: (item.get("score", 0), item.get("ai_confidence", 0), item["peak_height"]),
                reverse=True,
            )
            return reviewed
        except Exception as error:
            self.logger.warning("热点AI复核失败，使用严格本地结果兜底: %s", error)
            fallback_ids = {str(item.get("id")) for item in self._strict_fallback(candidates)}
            return [dict(candidate, ai_reviewed=False,
                         ai_keep=str(candidate.get("id")) in fallback_ids, ai_status="fallback")
                    for candidate in candidates]

    @staticmethod
    def _strict_fallback(candidates):
        return [
            candidate for candidate in candidates
            if candidate.get("z_score", 0) >= 4 and candidate.get("prominence_ratio", 0) >= 1.5
        ]

    def _run_task(self, task):
        task["status"] = "analyzing"
        with self._lock:
            self._save()
        try:
            config = task["config"]
            segments = []
            danmaku = []
            subtitles = []
            danmaku_sources = []
            offset = 0.0
            base_info = None
            for item in sorted(task["segments"], key=lambda value: value.get("segment_id", 0)):
                video = item.get("video")
                if not isinstance(video, VideoInfo) or not video.path or not os.path.exists(video.path):
                    continue
                duration = FFprobe.get_duration(video.path) or video.duration or 0
                if duration <= 0:
                    continue
                base_info = base_info or video
                segments.append({
                    "path": video.path, "duration": duration, "offset": offset,
                    "subtitle": item.get("subtitle"),
                })
                danmaku_source = str(config.get("danmaku_source", "auto")).lower()
                raw_dm_file = item.get("raw_dm_file")
                ass_dm_file = item.get("dm_file")
                if danmaku_source == "raw":
                    selected_dm_file = raw_dm_file
                elif danmaku_source == "ass":
                    selected_dm_file = ass_dm_file
                else:
                    selected_dm_file = raw_dm_file if raw_dm_file and os.path.exists(raw_dm_file) else ass_dm_file
                if danmaku_source == "raw" and (not selected_dm_file or not os.path.exists(selected_dm_file)):
                    raise RuntimeError(f"热点任务要求原始弹幕，但文件不存在: {raw_dm_file}")
                danmaku_sources.append({
                    "segment_id": item.get("segment_id"),
                    "source": "raw" if selected_dm_file == raw_dm_file else "ass",
                    "path": selected_dm_file,
                })
                danmaku.extend(parse_danmaku(
                    selected_dm_file, offset, allowed_types=config.get("danmaku_types")
                ))
                subtitles.extend(parse_srt(item.get("subtitle"), offset))
                offset += duration
            if not segments:
                raise RuntimeError("热点任务没有可用视频分段")

            analysis = find_hotspots(danmaku, offset, config.get("detection") or {})
            analysis["danmaku_sources"] = danmaku_sources
            candidates = self._ai_review(analysis["candidates"], subtitles, config)
            candidates = attach_subtitle_excerpts(candidates, subtitles)
            analysis["candidates"] = candidates
            # dryrun-only end-to-end validation: real analysis still runs first; when the
            # short sample contains no hotspot, encode one marked test clip to verify FFmpeg.
            fallback_seconds = min(offset, max(0, float(config.get("test_fallback_clip_seconds", 0))))
            if not candidates and fallback_seconds > 0:
                candidates = [{
                    "id": "dryrun-fallback", "start": 0.0, "end": fallback_seconds,
                    "peak": 0.0, "peak_height": 0, "prominence": 0, "score": 0,
                    "category": "other_content", "test_fallback": True,
                    "ai_reviewed": False, "ai_keep": True, "ai_status": "test_fallback",
                }]
            auto_candidates = [candidate for candidate in candidates if candidate.get("ai_keep", True)]
            requested_profiles = []
            for profile in config.get("outputs") or DEFAULT_PROFILES:
                profile = dict(profile)
                profile_id = str(profile.get("id", ""))
                if not re.fullmatch(r"[a-z0-9_-]+", profile_id):
                    raise ValueError(f"热点输出方案ID不合法: {profile_id}")
                profile.setdefault("name", profile_id)
                requested_profiles.append(profile)

            all_profile = next((item for item in requested_profiles if item["id"] == "all"), None)
            categories = {category for item in requested_profiles for category in item.get("categories", [])}
            wildcard = "*" in categories or bool(all_profile)
            limit_profile = all_profile or (config.get("all_output") or {})
            combined_profile = {
                "id": "all" if wildcard else (requested_profiles[0]["id"] if len(requested_profiles) == 1 else "mix"),
                "name": ("综合高能" if wildcard else requested_profiles[0]["name"] if len(requested_profiles) == 1 else "自选类别混剪"),
                "categories": ["*"] if wildcard else sorted(categories),
                "max_clips": int(limit_profile.get("max_clips", 12)),
                "max_total_duration": float(limit_profile.get("max_total_duration", 300)),
                "output_name": limit_profile.get("output_name"),
            }
            # The clip library intentionally keeps every local hotspot candidate. AI review,
            # category profiles and duration/count limits only decide the initial automatic mix.
            clip_candidates = candidates
            output_candidates = filter_clip_candidates(auto_candidates, combined_profile["categories"])
            selected = select_profile(output_candidates, combined_profile) if requested_profiles else []

            output_dir = config.get("output_dir") or os.path.dirname(base_info.path) + "（高能混剪）"
            manifest_path = os.path.join(
                output_dir,
                f"{os.path.splitext(os.path.basename(base_info.path))[0]}.highlights.json",
            )
            task["status"] = "rendering"
            with self._lock:
                self._save()
            if clip_candidates:
                try:
                    outputs, clip_records, encoding_mode = render_highlight(
                        segments, clip_candidates, selected, output_dir, base_info, config, combined_profile, self.logger
                    )
                except Exception:
                    if (str(config.get("mode", "copy")).lower() != "copy" or
                            config.get("copy_fallback", "reencode") != "reencode"):
                        raise
                    self.logger.warning("无重编码裁切或拼接失败，整场回退重新编码", exc_info=True)
                    fallback_config = dict(config)
                    fallback_config["mode"] = "reencode"
                    outputs, clip_records, encoding_mode = render_highlight(
                        segments, clip_candidates, selected, output_dir, base_info,
                        fallback_config, combined_profile, self.logger,
                    )
            else:
                outputs, clip_records, encoding_mode = [], [], config.get("mode", "copy")
            for output in outputs:
                output.highlight_manifest = manifest_path
            streamer = getattr(base_info, "streamer", None)
            streamer_name = (streamer.get("name") if isinstance(streamer, dict)
                             else getattr(streamer, "name", None))
            manifest = {
                "version": 2, "taskname": task.get("taskname"), "source_task": task.get("source_task"),
                "group_id": task.get("group_id"),
                "streamer_name": str(streamer_name or task.get("source_task") or ""),
                "subtitle_status": task.get("subtitle_status"), "analysis": analysis,
                "source_segments": [{
                    "path": item.get("path"), "duration": item.get("duration"),
                    "offset": item.get("offset"), "subtitle": item.get("subtitle"),
                } for item in segments],
                "source_segment_count": task.get("source_segment_count", len(task.get("segments", []))),
                "analyzed_segment_count": len(task.get("segments", [])),
                "ignored_segments": task.get("ignored_segments", []),
                "selected_candidates": auto_candidates,
                "clip_candidates": [str(item.get("id")) for item in clip_candidates],
                "auto_selected_candidates": [str(item.get("id")) for item in selected],
                "requested_outputs": [item["id"] for item in requested_profiles],
                "initial_mix": {"id": combined_profile["id"],
                                "clip_ids": [str(item.get("id")) for item in selected]},
                "encoding_mode": encoding_mode,
                "clips": clip_records,
                "versions": [],
                "outputs": [{
                    "path": output.path, "dtype": output.dtype, "duration": output.duration,
                } for output in outputs],
            }
            write_manifest(manifest_path, manifest)
            result = {"outputs": outputs, "manifest": manifest_path,
                      "clip_count": len(clip_records)}
            with self._lock:
                task["status"] = "completion_pending"
                task["result"] = result
                self._save()
            if outputs:
                message = "热点混剪完成"
            elif clip_records:
                message = f"已保留 {len(clip_records)} 个候选素材，未生成自动混剪"
            else:
                message = "未发现满足条件的热点片段"
            self._send("end", message, task, result)
        except Exception as error:
            self.logger.exception("热点混剪失败: %s", error)
            with self._lock:
                task["status"] = "failed"
                task["failure_reason"] = str(error)
                self.tasks.pop(task["uuid"], None)
                self.failed_tasks[task["uuid"]] = task
                self._save()
            self._send("error", f"热点混剪失败: {error}", task, {"error": str(error)})

    def stop(self):
        self.stoped = True
        if self.recv_queue is not None:
            self.recv_queue.put(PipeMessage(source="highlight", target="highlight", event="exit"))
        with self._lock:
            self._save()
        self.executors.shutdown(wait=False)
        self.logger.info("Highlight stopped.")
