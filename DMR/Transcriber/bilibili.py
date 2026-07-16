import copy
import json
import logging
import os
import subprocess
import threading
import time

import requests

from DMR.Uploader.biliwebapi import BiliWebApi
from DMR.utils import FFprobe, ToolsList, get_tempfile

logger = logging.getLogger(__name__)


class SubtitleUnavailableError(RuntimeError):
    """稿件已经可以访问，但宽限期内没有生成可下载字幕。"""


class BilibiliTranscriber:
    VIEW_URL = 'https://api.bilibili.com/x/web-interface/view'
    PLAYER_URL = 'https://api.bilibili.com/x/player/wbi/v2'
    _pool = {}
    _pool_lock = threading.Lock()

    def __init__(self, **config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
        })

    def transcribe(self, video):
        output = os.path.splitext(video.path)[0] + '.srt'
        raw_output = os.path.splitext(video.path)[0] + '.subtitle.json'
        if os.path.exists(output) and not self.config.get('overwrite', False):
            return True, {'subtitle': output, 'skipped': True}

        uploader = None
        pooled = False
        try:
            upload = self._upload_config()
            self._validate_cookie_file(upload)
            duration = self._duration(video)
            existing = self.config.get('existing_upload') or {}
            if existing.get('bvid') and existing.get('part_title'):
                existing_uploader = BiliWebApi(
                    cookies=upload.get('cookies'), account=upload.get('account'),
                    limit=upload.get('limit', 3),
                )
                previous_timeout = self.config.get('poll_timeout')
                self.config['poll_timeout'] = max(
                    float(self.config.get('existing_upload_timeout', 300)), 1
                )
                try:
                    self.session.cookies.update(existing_uploader._session.cookies)
                    subtitle_data, aid, cid = self._wait_for_subtitle(
                        existing['bvid'], existing['part_title'], duration, self.session,
                    )
                    if self.config.get('keep_raw_json', False):
                        self._atomic_json_dump(subtitle_data, raw_output)
                    self._atomic_write(output, self._to_srt(subtitle_data))
                    return True, {
                        'subtitle': output,
                        'raw_subtitle': raw_output if self.config.get('keep_raw_json', False) else None,
                        'bvid': existing['bvid'], 'aid': aid, 'cid': cid,
                        'upload_attempts': 0, 'reused_existing_upload': True,
                    }
                except Exception as error:
                    logger.info('已上传稿件暂未取得字幕，将回退字幕代理视频: %s', error)
                finally:
                    if previous_timeout is None:
                        self.config.pop('poll_timeout', None)
                    else:
                        self.config['poll_timeout'] = previous_timeout
                    existing_uploader.stop()
            uploader, upload_lock, pooled = self._get_uploader(upload, video)
            max_reuploads = max(int(self.config.get('max_reuploads', 2)), 0)
            subtitle_data = aid = cid = bvid = None
            attempts = 0
            for attempt in range(max_reuploads + 1):
                attempts = attempt + 1
                proxy = get_tempfile(prefix='bilibili-transcribe', suffix='mp4')
                try:
                    self._make_proxy(video.path, proxy, duration)
                    proxy_info = copy.deepcopy(video)
                    proxy_info.path = proxy
                    proxy_info.size = os.path.getsize(proxy)
                    proxy_info.resolution = (
                        int(self.config.get('proxy_video', {}).get('width', 640)),
                        int(self.config.get('proxy_video', {}).get('height', 360)),
                    )
                    with upload_lock:
                        status, bvid = uploader.upload(files=[proxy_info], **upload)
                        if not status or not bvid:
                            raise RuntimeError(f'B站字幕代理视频上传失败: {bvid}')
                        uploaded_part = uploader.videos.videos[-1]
                        part_title = uploaded_part.get('title')
                        self.session.cookies.update(uploader._session.cookies)
                    try:
                        subtitle_data, aid, cid = self._wait_for_subtitle(
                            bvid, part_title, duration, self.session,
                        )
                        break
                    except SubtitleUnavailableError:
                        if attempt >= max_reuploads:
                            raise
                        logger.warning(
                            'B站未生成分P字幕，正在进行第 %s/%s 次重新上传: %s',
                            attempt + 1, max_reuploads, video.path,
                        )
                finally:
                    if os.path.exists(proxy) and not self.config.get('keep_proxy', False):
                        os.remove(proxy)

            if self.config.get('keep_raw_json', False):
                self._atomic_json_dump(subtitle_data, raw_output)
            self._atomic_write(output, self._to_srt(subtitle_data))
            return True, {
                'subtitle': output,
                'raw_subtitle': raw_output if self.config.get('keep_raw_json', False) else None,
                'bvid': bvid,
                'aid': aid,
                'cid': cid,
                'upload_attempts': attempts,
            }
        finally:
            if uploader is not None and not pooled:
                uploader.stop()

    def _duration(self, video):
        duration = getattr(video, 'duration', 0) or 0
        if duration <= 0:
            duration = FFprobe.get_duration(video.path) or 0
        return float(duration)

    def _make_proxy(self, source, output, duration=None):
        cfg = self.config.get('proxy_video') or {}
        width = int(cfg.get('width', 640))
        height = int(cfg.get('height', 360))
        fps = max(float(cfg.get('fps', 1)), 0.1)
        audio_bitrate = str(cfg.get('audio_bitrate', '64k'))
        background = str(cfg.get('background', 'black'))
        audio_codec = str(cfg.get('audio_codec', 'aac'))
        codecs = ['copy', 'aac'] if audio_codec == 'auto' else [audio_codec]
        last_logs = ''
        for codec in codecs:
            cmds = [
            ToolsList.get('ffmpeg'), '-y',
            '-f', 'lavfi', '-i', f'color=c={background}:s={width}x{height}:r={fps}',
            '-i', source,
            '-map', '0:v:0', '-map', '1:a:0',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'stillimage',
            '-pix_fmt', 'yuv420p', '-r', str(fps), '-g', str(max(1, int(fps * 10))),
            '-c:a', codec,
            ]
            if codec != 'copy':
                cmds += [
                    '-af', 'asetpts=N/SR/TB',
                    '-ac', str(int(cfg.get('audio_channels', 1))),
                    '-ar', str(int(cfg.get('audio_sample_rate', 16000))),
                    '-b:a', audio_bitrate,
                ]
            cmds += ['-shortest']
            if duration and float(duration) > 0:
                cmds += ['-t', str(float(duration))]
            cmds += ['-movflags', '+faststart', output]
            proc = subprocess.run(cmds, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if proc.returncode == 0:
                return
            last_logs = proc.stdout.decode('utf-8', errors='ignore')
        raise RuntimeError(f'生成B站字幕代理视频失败: {last_logs[-1000:]}')

    def _upload_config(self):
        supplied = copy.deepcopy(self.config.get('upload') or {})
        # 对外只保留账号与Cookie路径；其余投稿参数使用字幕任务专用的安全默认值。
        if 'account' in self.config:
            supplied['account'] = self.config.get('account')
        if 'cookies' in self.config:
            supplied['cookies'] = self.config.get('cookies')
        defaults = {
            'account': 'bilibili',
            'cookies': None,
            'line': 'AUTO',
            'limit': 3,
            'realtime': True,
            'concat_video': False,
            'copyright': 1,
            'source': '',
            'tid': 21,
            'cover': '',
            'title': '[录屏] {TITLE}',
            'desc': '游戏录像。',
            'dynamic': '',
            'tag': '游戏',
            'dtime': 0,
            'dolby': 0,
            'hires': 0,
            'no_reprint': 1,
            'is_only_self': 1,
            'charging_pay': 0,
            'open_subtitle': False,
            'no_disturbance': 1,
            'extra_kwargs': {},
            'cover_auto': None,
            'season_id': None,
            'section_title': None,
            'episode_title': None,
            'epid': None,
        }
        defaults.update(supplied)
        if not defaults.get('cookies') and not defaults.get('account'):
            raise ValueError('B站字幕任务必须配置 upload.account 或 upload.cookies')
        return defaults

    def _get_uploader(self, upload, video):
        if not self.config.get('group_segments', True):
            return BiliWebApi(
                cookies=upload.get('cookies'), account=upload.get('account'),
                limit=upload.get('limit', 3),
            ), threading.Lock(), False
        group_id = getattr(video, 'upload_group_id', None) or getattr(video, 'group_id', None)
        if not group_id:
            return BiliWebApi(
                cookies=upload.get('cookies'), account=upload.get('account'),
                limit=upload.get('limit', 3),
            ), threading.Lock(), False
        identity = os.path.abspath(upload.get('cookies') or os.path.join(
            '.login_info', f"{upload.get('account')}.json"
        ))
        key = (identity, str(group_id))
        with self._pool_lock:
            now = time.time()
            expire = max(float(self.config.get('uploader_expire', 7 * 24 * 3600)), 0)
            if expire:
                for old_key, old_entry in list(self._pool.items()):
                    if now - old_entry['ctime'] > expire and not old_entry['lock'].locked():
                        old_entry['uploader'].stop()
                        self._pool.pop(old_key, None)
            entry = self._pool.get(key)
            if entry is None:
                entry = {
                    'uploader': BiliWebApi(
                        cookies=upload.get('cookies'), account=upload.get('account'),
                        limit=upload.get('limit', 3),
                    ),
                    'lock': threading.Lock(),
                    'ctime': now,
                }
                self._pool[key] = entry
            else:
                entry['ctime'] = now
            return entry['uploader'], entry['lock'], True

    @classmethod
    def close_all(cls):
        with cls._pool_lock:
            entries = list(cls._pool.values())
            cls._pool.clear()
        for entry in entries:
            entry['uploader'].stop()

    @staticmethod
    def _validate_cookie_file(upload):
        cookie_file = upload.get('cookies') or os.path.join(
            '.login_info', f"{upload.get('account')}.json"
        )
        if not os.path.exists(cookie_file):
            raise FileNotFoundError(f'B站登录文件不存在: {cookie_file}')
        with open(cookie_file, 'r', encoding='utf-8') as file:
            data = json.load(file)
        cookies = (data.get('cookie_info') or {}).get('cookies') or []
        values = {item.get('name'): item for item in cookies}
        missing = [name for name in ('SESSDATA', 'bili_jct') if not values.get(name, {}).get('value')]
        if missing:
            raise ValueError(f'B站登录文件缺少Cookie: {missing}')
        expires = values['SESSDATA'].get('expires')
        if expires and float(expires) <= time.time():
            expired_at = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(expires)))
            raise RuntimeError(f'B站登录Cookie已过期（{expired_at}）: {cookie_file}')

    def _wait_for_subtitle(self, bvid, part_title, duration, session):
        ratio = max(float(self.config.get('poll_interval_ratio', 0.3)), 0)
        minimum = max(float(self.config.get('min_poll_interval', 30)), 1)
        maximum = max(float(self.config.get('max_poll_interval', 300)), minimum)
        interval = min(max(duration * ratio, minimum), maximum)
        timeout = max(float(self.config.get('poll_timeout', 86400)), interval)
        retry_ratio = max(float(self.config.get('reupload_after_ratio', 0.3)), 0)
        retry_minimum = max(float(self.config.get('min_reupload_after', 600)), interval)
        retry_maximum = max(float(self.config.get('max_reupload_after', 3600)), retry_minimum)
        reupload_after = min(max(duration * retry_ratio, retry_minimum), retry_maximum)
        deadline = time.time() + timeout
        last_state = None
        accessible_since = None
        while time.time() < deadline:
            view = self._get_json(session, self.VIEW_URL, {'bvid': bvid})
            if view.get('code') == 0 and view.get('data'):
                data = view['data']
                pages = data.get('pages') or []
                page = next((item for item in pages if item.get('part') == part_title), None)
                if page:
                    cid = page['cid']
                    aid = data.get('aid')
                    player = self._get_json(
                        session,
                        self.PLAYER_URL,
                        {'bvid': bvid, 'aid': aid, 'cid': cid},
                        referer=f'https://www.bilibili.com/video/{bvid}',
                    )
                    last_state = player
                    if player.get('code') == 0:
                        if accessible_since is None:
                            accessible_since = time.time()
                        subtitles = ((player.get('data') or {}).get('subtitle') or {}).get('subtitles') or []
                        selected = self._select_subtitle(subtitles)
                        if selected:
                            url = selected['subtitle_url']
                            if url.startswith('//'):
                                url = 'https:' + url
                            subtitle = self._get_json(session, url, None, referer=f'https://www.bilibili.com/video/{bvid}')
                            if subtitle.get('body'):
                                return subtitle, aid, cid
                        if time.time() - accessible_since >= reupload_after:
                            raise SubtitleUnavailableError(
                                f'B站稿件已可访问但未生成字幕，将重新上传: '
                                f'bvid={bvid}, cid={cid}, waited={reupload_after}s'
                            )
            else:
                last_state = view
            time.sleep(min(interval, max(0, deadline - time.time())))
        raise TimeoutError(f'等待B站AI字幕超时: bvid={bvid}, timeout={timeout}s, last={last_state}')

    @staticmethod
    def _select_subtitle(subtitles):
        ready = [item for item in subtitles if item.get('subtitle_url') and item.get('ai_status') in (None, 2)]
        for item in ready:
            if item.get('lan') == 'ai-zh':
                return item
        return ready[0] if ready else None

    @staticmethod
    def _get_json(session, url, params=None, referer=None):
        headers = {'Referer': referer} if referer else None
        response = session.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()

    @classmethod
    def _to_srt(cls, subtitle):
        lines = []
        for index, item in enumerate(subtitle.get('body') or [], 1):
            content = str(item.get('content') or '').strip()
            if not content:
                continue
            lines.extend([
                str(index),
                f"{cls._format_time(item.get('from', 0))} --> {cls._format_time(item.get('to', 0))}",
                content,
                '',
            ])
        result = '\n'.join(lines)
        if not result.strip():
            raise RuntimeError('B站返回的字幕为空。')
        return result

    @staticmethod
    def _format_time(value):
        milliseconds = max(round(float(value or 0) * 1000), 0)
        seconds, millis = divmod(milliseconds, 1000)
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        return f'{hours:02d}:{minute:02d}:{sec:02d},{millis:03d}'

    @staticmethod
    def _atomic_write(path, content):
        temp = path + '.tmp'
        with open(temp, 'w', encoding='utf-8', newline='\n') as file:
            file.write(content)
        os.replace(temp, path)

    @staticmethod
    def _atomic_json_dump(data, path):
        temp = path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temp, path)
