import base64
import json
import os
import threading
from datetime import date, datetime


def _json_safe(value, depth=0):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if depth >= 8:
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth + 1) for item in value]
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value), depth + 1)
    return str(value)


class RawDanmakuWriter:
    """Line-oriented lossless sidecar written before ASS display filtering."""

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
        record = {
            "version": 1,
            "video_time": attributes.get("time"),
            "timestamp": attributes.get("timestamp"),
            "received_at": datetime.now().timestamp(),
            "type": attributes.get("dtype"),
            "sender": {
                "name": attributes.get("uname"),
                "uid": next((attributes.get(key) for key in (
                    "uid", "user_id", "userid", "userId", "open_id", "user_unique_id"
                ) if attributes.get(key) is not None), None),
            },
            "color": attributes.get("color"),
            "content": attributes.get("content"),
            "text": attributes.get("text"),
            "source_url": source_url,
            "class": danmaku.__class__.__name__,
            "data": attributes,
        }
        line = json.dumps(_json_safe(record), ensure_ascii=False, separators=(",", ":"))
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
