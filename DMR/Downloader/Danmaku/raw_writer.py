import json
import os
import threading


class RawDanmakuWriter:
    """Compact line-oriented sidecar written before ASS display filtering."""

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)
        self.path = None
        self._file = None
        self._lock = threading.Lock()

    def open(self, path):
        if not self.enabled:
            return
        with self._lock:
            self._close_unlocked()
            self.path = path
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._file = open(path, "w", encoding="utf-8", buffering=1)

    def add(self, danmaku, source_url=None):
        if not self.enabled or self._file is None:
            return False
        attributes = dict(vars(danmaku))
        dtype = str(attributes.get("dtype") or "").lower()
        text = attributes.get("text") or attributes.get("content")
        # Unknown protocol/control messages contain no useful hotspot material and
        # previously accounted for most records in Douyin raw sidecars.
        if dtype in ("", "other", "others") or not text:
            return False
        record = {
            "video_time": attributes.get("time"),
            "type": dtype,
            "sender": {
                "name": attributes.get("uname"),
                "uid": next((attributes.get(key) for key in (
                    "uid", "user_id", "userid", "userId", "open_id", "user_unique_id"
                ) if attributes.get(key) is not None), None),
            },
            "text": str(text),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._file.write(line + "\n")
        return True

    def close(self):
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self):
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
