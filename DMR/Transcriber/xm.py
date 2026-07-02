import json
import os
import subprocess
import time

import requests

from DMR.utils import FFprobe, ToolsList, get_tempfile, uuid


class XMTranscriber:
    BASE_URL = 'https://api.xiaomixiaoai.com'
    LANGUAGE = 'zh-CN'
    REQUEST_ORIGIN = 'RECORD'
    POLL_INTERVAL = 3
    MIN_POLL_TIMEOUT = 120
    POLL_TIMEOUT_RATIO = 0.5
    POLL_TIMEOUT_GRACE = 60

    def __init__(self, auth_file):
        self.auth_file = auth_file

    def transcribe(self, video):
        output = os.path.splitext(video.path)[0] + '.srt'
        if os.path.exists(output):
            return True, output

        audio = get_tempfile(prefix='xm-transcribe', suffix='mp3')
        try:
            self._extract_audio(video.path, audio)
            duration = self._get_duration(video)
            task_id = self._submit_task(audio, duration)
            result = self._fetch_result(task_id, duration)
            srt = self._result_to_srt(result)
            if not srt.strip():
                raise RuntimeError('转录结果为空，未生成有效字幕。')
            with open(output, 'w', encoding='utf-8') as f:
                f.write(srt)
            return True, output
        finally:
            if os.path.exists(audio):
                os.remove(audio)

    def _load_auth(self):
        with open(self.auth_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        missing = [k for k in ('authorization', 'device_id', 'app_id') if not data.get(k)]
        if missing:
            raise ValueError(f'小米转录认证文件缺少字段: {missing}')
        return data

    def _extract_audio(self, video_path, audio_path):
        cmds = [
            ToolsList.get('ffmpeg'),
            '-y',
            '-i', video_path,
            '-vn',
            '-ac', '1',
            '-ar', '16000',
            '-c:a', 'libmp3lame',
            '-b:a', '32k',
            audio_path,
        ]
        proc = subprocess.run(cmds, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            logs = proc.stdout.decode('utf-8', errors='ignore')
            raise RuntimeError(f'提取转录音频失败: {logs[-500:]}')

    def _get_duration(self, video):
        duration = getattr(video, 'duration', None)
        if duration is None or duration <= 0:
            duration = FFprobe.get_duration(video.path)
        return duration if duration and duration > 0 else 0

    def _submit_task(self, audio_path, duration):
        auth = self._load_auth()
        url = f'{self.BASE_URL}/api/rpcproxy/fasttranscribe'
        data = {
            'request_id': uuid(),
            'device_id': auth['device_id'],
            'app_id': auth['app_id'],
            'asr_language_list': json.dumps([self.LANGUAGE], ensure_ascii=False),
            'request_origin': self.REQUEST_ORIGIN,
            'is_enable_speaker_recognition': 'true',
            'audio_duration': str(max(int(duration or 0), 0)),
            'is_enable_polish': 'false',
        }
        headers = {'Authorization': auth['authorization']}
        with open(audio_path, 'rb') as f:
            resp = requests.post(url, headers=headers, data=data, files={'file': (os.path.basename(audio_path), f, 'audio/mpeg')}, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get('code') != 200 or not payload.get('task_id'):
            raise RuntimeError(f'提交小米转录任务失败: {payload}')
        return payload['task_id']

    def _poll_timeout(self, duration):
        if duration <= 0:
            return self.MIN_POLL_TIMEOUT
        return max(self.MIN_POLL_TIMEOUT, int(duration * self.POLL_TIMEOUT_RATIO) + self.POLL_TIMEOUT_GRACE)

    def _fetch_result(self, task_id, duration):
        timeout = self._poll_timeout(duration)
        deadline = time.time() + timeout
        url = f'{self.BASE_URL}/api/rpcproxy/fetchtranscriberesult'
        while time.time() < deadline:
            time.sleep(self.POLL_INTERVAL)
            auth = self._load_auth()
            headers = {'Authorization': auth['authorization']}
            params = {
                'request_id': uuid(),
                'device_id': auth['device_id'],
                'task_id': task_id,
                'app_id': auth['app_id'],
                'request_origin': self.REQUEST_ORIGIN,
            }
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get('code') != 200:
                raise RuntimeError(f'查询小米转录结果失败: {payload}')
            status = payload.get('status')
            if status == 'RESULT':
                return payload.get('result') or {}
            if status == 'PROCESS':
                continue
            raise RuntimeError(f'未知的小米转录任务状态: {payload}')
        raise TimeoutError(f'小米转录任务超时: {task_id}, timeout={timeout}s, duration={duration}s')

    def _result_to_srt(self, result):
        items = []
        for phrase in result.get('phrases') or []:
            speaker = phrase.get('speaker')
            prefix = f'[说话人{speaker}] ' if speaker is not None else ''
            sentences = phrase.get('sentences') or []
            if sentences:
                for sentence in sentences:
                    item = self._subtitle_item(sentence, prefix, fallback=phrase)
                    if item:
                        items.append(item)
            else:
                item = self._subtitle_item(phrase, prefix)
                if item:
                    items.append(item)

        lines = []
        for idx, item in enumerate(items, 1):
            start, end, text = item
            lines.extend([
                str(idx),
                f'{self._format_srt_time(start)} --> {self._format_srt_time(end)}',
                text,
                '',
            ])
        return '\n'.join(lines)

    def _subtitle_item(self, data, prefix='', fallback=None):
        text = (data.get('text') or '').strip()
        fallback = fallback or {}
        offset = data.get('offsetMilliseconds', fallback.get('offsetMilliseconds'))
        duration = data.get('durationMilliseconds', fallback.get('durationMilliseconds'))
        if not text or offset is None or duration is None:
            return None
        start = max(int(offset), 0)
        end = max(start + int(duration), start + 1)
        return start, end, prefix + text

    def _format_srt_time(self, ms):
        seconds, millis = divmod(int(ms), 1000)
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        return f'{hours:02d}:{minute:02d}:{sec:02d},{millis:03d}'
