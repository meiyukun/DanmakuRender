import logging
import threading
import queue
import secrets
import os
import yaml
import glob
import json
import math
import mimetypes
import re
import time
import hashlib
import subprocess
import shutil
import base64
import tempfile
from flask import Flask, request, render_template, redirect, url_for, flash, session, send_file
from functools import wraps
from copy import deepcopy
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageOps
from werkzeug.serving import make_server

from DMR.utils import *


MANUAL_HIGHLIGHT_CATEGORIES = {
    'funny': '纯搞笑场面',
    'skill': '帅气操作',
    'absurd': '逆天场面',
    'fail': '翻车失误',
    'emotional': '情绪高光',
}


def manual_highlight_output_profiles(target):
    """Add standard category profiles for manual runs without changing auto jobs."""
    profiles = [deepcopy(item) for item in target.get('outputs') or [] if isinstance(item, dict)]
    configured_ids = {str(item.get('id')) for item in profiles if item.get('id')}
    if 'all' not in configured_ids:
        profiles.insert(0, {
            'id': 'all', 'name': '综合高能', 'categories': ['*'],
            'max_clips': 12, 'max_total_duration': 300, 'output_name': None,
        })
        configured_ids.add('all')
    all_profile = next((item for item in profiles if item.get('id') == 'all'), {})
    max_clips = min(max(int(all_profile.get('max_clips', 12)), 1), 8)
    max_duration = min(max(float(all_profile.get('max_total_duration', 300)), 1), 180)
    for category, name in MANUAL_HIGHLIGHT_CATEGORIES.items():
        if category not in configured_ids:
            profiles.append({
                'id': category, 'name': name, 'categories': [category],
                'max_clips': max_clips, 'max_total_duration': max_duration,
                'output_name': None,
            })
    return profiles


def highlight_output_options(target):
    """Return the user-selectable output profiles without exposing upload settings."""
    options = []
    configured_ids = {
        str(item.get('id')) for item in target.get('outputs') or []
        if isinstance(item, dict) and item.get('id')
    }
    for item in manual_highlight_output_profiles(target):
        if not isinstance(item, dict) or not item.get('id'):
            continue
        options.append({
            'id': str(item['id']),
            'name': str(item.get('name') or item['id']),
            'categories': [str(value) for value in item.get('categories') or []],
            'selected_by_default': str(item['id']) in configured_ids,
        })
    return options


class WebApi:
    def __init__(
            self,
            pipe:Tuple[queue.Queue, queue.Queue],
            engine=None,
            runtime_controller=None,
            ai_client=None,
            host='0.0.0.0',
            port=5000,
            force_login=True,
            username='admin',
            password='admin',
            **kwargs,
        ) -> None:
        self.send_queue, self.recv_queue = pipe
        self.engine = engine
        self.runtime_controller = runtime_controller
        self.ai_client = ai_client
        self.kwargs = kwargs
        
        # WebAPI Config
        self.host = host
        self.port = port
        self.force_login = force_login
        self.username = username
        self.password = password
        
        # 强制使用高强度随机密钥，每次启动自动生成，极大提高安全性
        # 注意：这意味着每次重启程序后，所有已登录用户都需要重新登录
        self.secret_key = secrets.token_hex(32)
        self.logger = logging.getLogger(__name__)
        self.stoped = True
        self.highlight_scans = {}
        self.highlight_scan_lock = threading.Lock()
        self.highlight_thumbnail_lock = threading.Lock()
        self.upload_library_cache = {'time': 0.0, 'items': []}
        self.upload_library_lock = threading.Lock()
        self.cover_token_lock = threading.Lock()
        self.cover_tokens = {}
        self.cover_job_lock = threading.Lock()
        self.cover_jobs = {}
        self.cover_executor = ThreadPoolExecutor(max_workers=2)
        self.highlight_operation_lock = threading.Lock()
        self.highlight_operation_file = os.path.join('.temp', 'highlight_operations.json')
        try:
            with open(self.highlight_operation_file, 'r', encoding='utf-8') as file:
                self.highlight_operations = json.load(file)
        except (OSError, ValueError):
            self.highlight_operations = {}
        for operation in self.highlight_operations.values():
            if operation.get('status') in ('queued', 'running'):
                operation.update({'status': 'failed', 'error': '程序重启，后台生成操作已中断。'})
        self.highlight_operation_executor = ThreadPoolExecutor(max_workers=1)
        self.cover_artifact_dir = os.path.realpath(os.path.join('.temp', 'upload_covers'))
        os.makedirs(self.cover_artifact_dir, exist_ok=True)

        self.webapp = None
        self.webserver = None
        self.webapp_thread = None

    def login_required(self, f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if self.force_login and 'logged_in' not in session:
                return redirect(url_for('login', next=request.url))
            return f(*args, **kwargs)
        return decorated_function

    def create_app(self):
        # Silence Flask/Werkzeug non-error logs
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        
        app = Flask(__name__, template_folder='templates')
        app.secret_key = self.secret_key
        app.logger = self.logger

        @app.route('/login', methods=['GET', 'POST'])
        def login():
            if request.method == 'POST':
                username = request.form['username']
                password = request.form['password']
                if username == self.username and password == self.password:
                    session['logged_in'] = True
                    return redirect(url_for('index'))
                else:
                    return render_template('login.html', error='Invalid credentials')
            return render_template('login.html')

        @app.route('/logout')
        def logout():
            session.pop('logged_in', None)
            return redirect(url_for('login'))

        @app.route('/')
        @self.login_required
        def index():
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return self.get_tasks_data()
            return render_template('index.html', **self.get_tasks_data())

        # 保留之前的兼容性
        @app.route('/api/put_message', methods=['POST'])
        @self.login_required
        def api_v1_func():
            req_data = request.get_json()
            message = PipeMessage(**req_data['data'])
            self.send_queue.put(message)
            return 'success', 200

        @app.route('/tasks')
        @self.login_required
        def tasks_page():
            return render_template('tasks.html', **self.get_tasks_data())

        @app.route('/highlights')
        @self.login_required
        def highlights_page():
            return render_template('highlights.html', sessions=self.get_highlight_sessions(),
                                   tasknames=self.get_replay_task_names())

        @app.route('/uploads')
        @self.login_required
        def upload_center_page():
            return render_template('uploads.html')

        @app.route('/api/upload/options')
        @self.login_required
        def upload_options_api():
            ai_cover = (self.ai_client.availability() if self.ai_client
                        else {'available': False, 'reason': '统一AI客户端不可用'})
            return {'status': 'success', 'accounts': self.get_upload_accounts(),
                    'defaults': self.get_upload_defaults(),
                    'seasons': self.get_known_seasons(), 'ai_cover': ai_cover}

        @app.route('/api/upload/library')
        @self.login_required
        def upload_library_api():
            try:
                return {'status': 'success', **self.query_upload_library(
                    page=request.args.get('page', 1), page_size=request.args.get('page_size', 24),
                    taskname=request.args.get('taskname', ''), kind=request.args.get('kind', '*'),
                    query=request.args.get('query', ''), sort=request.args.get('sort', 'modified_desc'),
                )}
            except ValueError as error:
                return {'status': 'error', 'message': str(error)}, 400

        @app.route('/api/upload/seasons')
        @self.login_required
        def upload_seasons_api():
            account = str(request.args.get('account') or '').strip()
            try:
                return {'status': 'success', 'seasons': self.get_upload_account_seasons(account)}
            except ValueError as error:
                return {'status': 'error', 'message': str(error)}, 400
            except Exception as error:
                self.logger.warning('获取B站合集列表失败: %s', error)
                return {'status': 'error', 'message': f'获取合集失败：{error}'}, 502

        @app.route('/api/upload/submissions')
        @self.login_required
        def upload_submissions_api():
            account = str(request.args.get('account') or '').strip()
            try:
                return {'status': 'success', 'submissions': self.get_upload_account_archives(account)}
            except ValueError as error:
                return {'status': 'error', 'message': str(error)}, 400
            except Exception as error:
                self.logger.warning('获取B站最近投稿失败: %s', error)
                return {'status': 'error', 'message': f'获取最近投稿失败：{error}'}, 502

        @app.route('/api/upload/submission')
        @self.login_required
        def upload_submission_api():
            account = str(request.args.get('account') or '').strip()
            bvid = str(request.args.get('bvid') or '').strip()
            if not re.fullmatch(r'BV[a-zA-Z0-9]+', bvid):
                return {'status': 'error', 'message': '请提供有效的 BV 号。'}, 400
            try:
                return {'status': 'success',
                        'submission': self.get_upload_account_archive_detail(account, bvid)}
            except ValueError as error:
                return {'status': 'error', 'message': str(error)}, 400
            except Exception as error:
                self.logger.warning('获取B站稿件详情失败: %s', error)
                return {'status': 'error', 'message': f'获取稿件详情失败：{error}'}, 502

        @app.route('/api/upload/media')
        @self.login_required
        def upload_media_api():
            requested = request.args.get('path') or ''
            virtual = self._decode_virtual_material(requested)
            if virtual:
                try:
                    manifest = self._read_highlight_manifest(virtual['manifest'])
                    clip = next(item for item in manifest.get('clips') or []
                                if str(item.get('id')) == virtual['clip_id'])
                    piece = (clip.get('pieces') or [])[0]
                    real_path = os.path.realpath(piece['path'])
                    if real_path not in self._highlight_source_paths(manifest):
                        raise ValueError
                except Exception:
                    return {'status': 'error', 'message': '虚拟热点素材不存在。'}, 404
            else:
                real_path = os.path.realpath(requested)
            if real_path not in self._upload_media_paths(refresh=False) or not os.path.isfile(real_path):
                if not virtual:
                    return {'status': 'error', 'message': '视频不存在或不属于上传素材库。'}, 404
            mime_type = {
                '.mkv': 'video/x-matroska', '.flv': 'video/x-flv', '.ts': 'video/mp2t',
                '.m2ts': 'video/mp2t', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
            }.get(os.path.splitext(real_path)[1].lower()) or mimetypes.guess_type(real_path)[0] or 'application/octet-stream'
            return send_file(real_path, conditional=True, mimetype=mime_type,
                             as_attachment=False, download_name=os.path.basename(real_path))

        @app.route('/api/upload/cover/file', methods=['POST'])
        @self.login_required
        def upload_cover_file_api():
            uploaded = request.files.get('cover')
            if not uploaded or not uploaded.filename:
                return {'status': 'error', 'message': '请选择封面图片。'}, 400
            if request.content_length and request.content_length > 10 * 1024 * 1024:
                return {'status': 'error', 'message': '封面图片不能超过 10MB。'}, 400
            token = secrets.token_urlsafe(24)
            raw_path = os.path.join(self.cover_artifact_dir, f'draft_{token}.upload')
            output_path = os.path.join(self.cover_artifact_dir, f'draft_{token}.jpg')
            try:
                uploaded.save(raw_path)
                if os.path.getsize(raw_path) > 10 * 1024 * 1024:
                    raise ValueError('封面图片不能超过 10MB。')
                self._normalize_cover(raw_path, output_path)
                self._register_cover_token(token, output_path, 'uploaded')
                return {'status': 'success', 'token': token,
                        'preview_url': f'/api/upload/cover/media?token={token}'}
            except Exception as error:
                for path in (raw_path, output_path):
                    if os.path.isfile(path):
                        os.remove(path)
                return {'status': 'error', 'message': f'封面图片无效: {error}'}, 400
            finally:
                if os.path.isfile(raw_path):
                    os.remove(raw_path)

        @app.route('/api/upload/cover/reference', methods=['POST'])
        @self.login_required
        def upload_cover_reference_api():
            payload = request.get_json(silent=True) or {}
            real_path = os.path.realpath(payload.get('path') or '')
            if real_path not in self._upload_media_paths() or not os.path.isfile(real_path):
                return {'status': 'error', 'message': '参考视频不存在或不允许访问。'}, 400
            try:
                second = float(payload.get('second') or 0)
                duration = float(FFprobe.get_duration(real_path) or 0)
                if not math.isfinite(second) or second < 0 or (duration > 0 and second >= duration):
                    raise ValueError
            except (TypeError, ValueError):
                return {'status': 'error', 'message': '参考帧时间无效。'}, 400
            token = secrets.token_urlsafe(24)
            raw_path = os.path.join(self.cover_artifact_dir, f'reference_{token}.png')
            output_path = os.path.join(self.cover_artifact_dir, f'reference_{token}.jpg')
            ffmpeg = ToolsList.get('ffmpeg', auto_install=False) or 'ffmpeg'
            try:
                process = subprocess.run([
                    ffmpeg, '-y', '-ss', f'{second:.3f}', '-i', real_path,
                    '-frames:v', '1', '-vf', "scale='min(1280,iw)':-2", raw_path,
                ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45, check=False)
                if process.returncode != 0 or not os.path.isfile(raw_path):
                    raise RuntimeError(process.stdout.decode('utf-8', errors='ignore')[-600:])
                self._normalize_cover(raw_path, output_path)
                self._register_cover_token(token, output_path, 'reference',
                                           {'source_path': real_path, 'second': second})
                return {'status': 'success', 'token': token, 'second': second,
                        'preview_url': f'/api/upload/cover/media?token={token}'}
            except Exception as error:
                if os.path.isfile(output_path):
                    os.remove(output_path)
                return {'status': 'error', 'message': f'截取参考帧失败: {error}'}, 400
            finally:
                if os.path.isfile(raw_path):
                    os.remove(raw_path)

        @app.route('/api/upload/cover/prompt', methods=['POST'])
        @self.login_required
        def upload_cover_prompt_api():
            payload = request.get_json(silent=True) or {}
            if not self.ai_client or not self.ai_client.available:
                reason = (self.ai_client.availability().get('reason') if self.ai_client
                          else '统一AI客户端不可用')
                return {'status': 'error', 'message': reason}, 400
            paths = [os.path.realpath(path) for path in (payload.get('paths') or [])]
            allowed = self._upload_media_paths()
            if not 1 <= len(paths) <= 100 or any(path not in allowed for path in paths):
                return {'status': 'error', 'message': '待上传视频选择无效。'}, 400
            title = str(payload.get('title') or '').strip()[:80]
            current_desc = str(payload.get('desc') or '')[:2000]
            current_dynamic = str(payload.get('dynamic') or '')[:233]
            custom_prompt = str(payload.get('prompt') or '').strip()
            if len(custom_prompt) > 2000:
                return {'status': 'error', 'message': '补充提示词不能超过 2000 个字符。'}, 400
            reference = self._resolve_cover_token(payload.get('reference_token'), kind='reference') \
                if payload.get('reference_token') else None
            if payload.get('reference_token') and not reference:
                return {'status': 'error', 'message': '参考帧令牌无效或已过期。'}, 400
            if reference and os.path.realpath(reference.get('source_path') or '') not in paths:
                return {'status': 'error', 'message': '参考帧来源必须仍在待上传分P列表中。'}, 400
            job_id = secrets.token_urlsafe(24)
            with self.cover_job_lock:
                self.cover_jobs[job_id] = {
                    'type': 'prompt', 'status': 'queued', 'error': '', 'token': None,
                    'prompt': '', 'metadata': {},
                }
            self.cover_executor.submit(
                self._run_cover_prompt_generation, job_id, paths, title, current_desc,
                current_dynamic, custom_prompt, reference,
            )
            return {'status': 'queued', 'job_id': job_id}

        @app.route('/api/upload/cover/generate', methods=['POST'])
        @self.login_required
        def upload_cover_generate_api():
            payload = request.get_json(silent=True) or {}
            if not self.ai_client or not self.ai_client.available:
                reason = (self.ai_client.availability().get('reason') if self.ai_client
                          else '统一AI客户端不可用')
                return {'status': 'error', 'message': reason}, 400
            paths = [os.path.realpath(path) for path in (payload.get('paths') or [])]
            allowed = self._upload_media_paths()
            if not 1 <= len(paths) <= 100 or any(path not in allowed for path in paths):
                return {'status': 'error', 'message': '待上传视频选择无效。'}, 400
            confirmed_prompt = str(payload.get('confirmed_prompt') or '').strip()
            if not confirmed_prompt:
                return {'status': 'error', 'message': '请先确认完整生图提示词。'}, 400
            if len(confirmed_prompt) > 8000:
                return {'status': 'error', 'message': '完整生图提示词不能超过 8000 个字符。'}, 400
            reference = self._resolve_cover_token(payload.get('reference_token'), kind='reference') \
                if payload.get('reference_token') else None
            if payload.get('reference_token') and not reference:
                return {'status': 'error', 'message': '参考帧令牌无效或已过期。'}, 400
            if reference and os.path.realpath(reference.get('source_path') or '') not in paths:
                return {'status': 'error', 'message': '参考帧来源必须仍在待上传分P列表中。'}, 400
            job_id = secrets.token_urlsafe(24)
            with self.cover_job_lock:
                self.cover_jobs[job_id] = {
                    'type': 'image', 'status': 'queued', 'error': '', 'token': None,
                    'prompt': confirmed_prompt,
                }
            self.cover_executor.submit(
                self._run_cover_image_generation, job_id, confirmed_prompt, reference,
            )
            return {'status': 'queued', 'job_id': job_id}

        @app.route('/api/upload/cover/generate/status')
        @self.login_required
        def upload_cover_generate_status_api():
            job_id = request.args.get('job_id')
            with self.cover_job_lock:
                job = deepcopy(self.cover_jobs.get(job_id))
            if not job:
                return {'status': 'error', 'message': '生成任务不存在或已失效。'}, 404
            if job.get('token'):
                job['preview_url'] = f'/api/upload/cover/media?token={job["token"]}'
            return job

        @app.route('/api/upload/cover/media')
        @self.login_required
        def upload_cover_media_api():
            record = self._resolve_cover_token(request.args.get('token'))
            if not record:
                return {'status': 'error', 'message': '封面不存在或令牌已过期。'}, 404
            return send_file(record['path'], conditional=True, mimetype='image/jpeg')

        @app.route('/api/upload/submit', methods=['POST'])
        @self.login_required
        def upload_submit_api():
            payload = request.get_json(silent=True) or {}
            submitted_parts = payload.get('parts')
            if submitted_parts is None:
                submitted_parts = [{'path': path} for path in (payload.get('paths') or [])]
            if not isinstance(submitted_parts, list) or not 1 <= len(submitted_parts) <= 100:
                return {'status': 'error', 'message': '请选择 1 至 100 个视频文件。'}, 400
            normalized_parts = []
            materialized_paths = set()
            for part in submitted_parts:
                if not isinstance(part, dict) or not part.get('path'):
                    return {'status': 'error', 'message': '分P信息无效。'}, 400
                requested_path = part['path']
                virtual = self._decode_virtual_material(requested_path)
                path = self._export_virtual_material(virtual) if virtual else os.path.realpath(requested_path)
                if virtual:
                    materialized_paths.add(path)
                part_title = (str(part.get('title')).strip() if 'title' in part
                              else os.path.splitext(os.path.basename(path))[0])
                if not part_title or len(part_title) > 80:
                    return {'status': 'error', 'message': '每个分P名称必须为 1 至 80 个字符。'}, 400
                normalized_parts.append({'path': path, 'title': part_title})
            allowed = self._upload_media_paths() | materialized_paths
            real_paths = [part['path'] for part in normalized_parts]
            if len(set(real_paths)) != len(real_paths) or any(path not in allowed for path in real_paths):
                return {'status': 'error', 'message': '包含重复、无效或不允许上传的文件。'}, 400
            mode = payload.get('mode', 'new')
            bvid = str(payload.get('bvid') or '').strip()
            if mode not in ('new', 'append') or (mode == 'append' and not re.fullmatch(r'BV[a-zA-Z0-9]+', bvid)):
                return {'status': 'error', 'message': '追加分P时必须填写有效的 BV 号。'}, 400
            loaded_bvid = str(payload.get('submission_loaded_bvid') or '').strip()
            if mode == 'append' and loaded_bvid.casefold() != bvid.casefold():
                return {'status': 'error', 'message': '请先成功读取目标稿件的完整信息，再提交追加。'}, 400
            title = str(payload.get('title') or '').strip()
            if not title:
                return {'status': 'error', 'message': '投稿必须填写标题。'}, 400
            if len(title) > 80:
                return {'status': 'error', 'message': '投稿标题不能超过 80 个字符。'}, 400
            account = str(payload.get('account') or 'bilibili')
            if account not in self.get_upload_accounts():
                return {'status': 'error', 'message': '上传账号不存在。'}, 400
            try:
                tid = int(payload.get('tid') or 21)
                copyright_type = int(payload.get('copyright') or 1)
                season_id = int(payload.get('season_id')) if payload.get('season_id') else None
                scheduled_at = float(payload.get('scheduled_at') or 0)
            except (TypeError, ValueError):
                return {'status': 'error', 'message': '分区、稿件类型、合集或定时发布参数无效。'}, 400
            if tid <= 0 or copyright_type not in (1, 2) or (season_id is not None and season_id <= 0):
                return {'status': 'error', 'message': '分区、稿件类型或合集 ID 无效。'}, 400
            source = str(payload.get('source') or '').strip()
            if copyright_type == 2 and not source:
                return {'status': 'error', 'message': '转载稿件必须填写转载来源。'}, 400
            if not math.isfinite(scheduled_at):
                return {'status': 'error', 'message': '定时发布时间无效。'}, 400
            cover_record = self._resolve_cover_token(payload.get('cover_token')) \
                if payload.get('cover_token') else None
            if payload.get('cover_token') and not cover_record:
                return {'status': 'error', 'message': '所选封面不存在或已过期。'}, 400
            if mode == 'append' and cover_record and payload.get('cover_replace_confirmed') is not True:
                return {'status': 'error', 'message': '追加分P替换原稿封面必须显式确认。'}, 400
            files = []
            for index, part in enumerate(normalized_parts):
                path = part['path']
                duration = FFprobe.get_duration(path)
                ctime = datetime.fromtimestamp(os.path.getmtime(path))
                files.append(VideoInfo(path=path, file_id=uuid(), dtype='web_upload',
                                       size=os.path.getsize(path), duration=duration, ctime=ctime,
                                       title=part['title'], segment_id=index + 1))
            if scheduled_at and scheduled_at <= time.time() + 9000:
                return {'status': 'error', 'message': '定时发布时间必须至少晚于当前时间 2.5 小时。'}, 400
            dtime = max(0, int(scheduled_at - files[0].ctime.timestamp())) if scheduled_at else 0
            request_id = uuid()
            managed_artifacts = []
            managed_cover = None
            if cover_record:
                managed_cover = os.path.join(self.cover_artifact_dir, f'task_{request_id}.jpg')
                shutil.copy2(cover_record['path'], managed_cover)
                managed_artifacts.append(managed_cover)
            args = merge_dict(self.get_upload_defaults(), {
                'account': account, 'title': title, 'desc': str(payload.get('desc') or ''),
                'dynamic': str(payload.get('dynamic') or ''), 'tag': str(payload.get('tag') or ''),
                'tid': tid, 'copyright': copyright_type, 'source': source,
                'is_only_self': 1 if payload.get('is_only_self') else 0, 'dtime': dtime,
                'scheduled_at': int(scheduled_at) if scheduled_at else 0,
                'season_id': season_id,
                'section_title': str(payload.get('section_title') or ''),
                'episode_title': str(payload.get('episode_title') or ''),
                'insert_head': bool(payload.get('insert_head')), 'bvid': bvid if mode == 'append' else None,
                'update_metadata': mode == 'append', 'sync_season': mode == 'append',
                'realtime': False, 'concat_video': False,
            })
            args.pop('cover_auto', None)
            args.pop('cover', None)
            args.pop('cover_required', None)
            if managed_cover:
                args['cover'] = managed_cover
                args['cover_required'] = True
            self.send_queue.put(PipeMessage(
                source='engine', target='uploader', event='newtask', request_id=request_id,
                data={'taskname': 'Web上传中心', 'files': files, 'engine': 'biliwebapi',
                      'stateless': True, 'upload_group': f'web_upload_{request_id}', 'args': args,
                      'managed_artifacts': managed_artifacts},
            ))
            return {'status': 'success', 'request_id': request_id, 'part_count': len(files)}

        @app.route('/api/highlight/sessions')
        @self.login_required
        def highlight_sessions_api():
            return {'sessions': self.get_highlight_sessions(), 'tasknames': self.get_replay_task_names()}

        @app.route('/api/highlight/manual/config')
        @self.login_required
        def highlight_manual_config_api():
            taskname = request.args.get('taskname')
            highlight_info = next((info for info in self.engine.task_dict.values()
                                   if info.get('task_type') == 'highlight'), None) if self.engine else None
            if not highlight_info:
                return {'status': 'error', 'message': '热点任务未加载。'}, 404
            config = highlight_info['class'].config
            target = merge_dict(deepcopy(config.get('defaults') or {}),
                                deepcopy(config.get('targets', {}).get(taskname) or {}))
            (target.get('analysis', {}).get('ai') or {}).pop('api_key', None)
            (target.get('upload', {}).get('common') or {}).pop('cookies', None)
            target.setdefault('upload', {})['enabled'] = False
            output_options = highlight_output_options(target)
            return {'status': 'success', 'yaml': yaml.safe_dump(target, allow_unicode=True, sort_keys=False),
                    'source': target.get('source') or {},
                    'output_ids': [item['id'] for item in output_options if item['selected_by_default']],
                    'output_options': output_options}

        @app.route('/api/highlight/sessions/scan', methods=['POST'])
        @self.login_required
        def highlight_sessions_scan_api():
            taskname = (request.get_json(silent=True) or {}).get('taskname')
            task_info = self.engine.task_dict.get(f'replay/{taskname}') if self.engine else None
            if not task_info:
                return {'status': 'error', 'message': '录制任务不存在。'}, 404
            with self.highlight_scan_lock:
                current = self.highlight_scans.get(taskname) or {}
                if current.get('status') == 'running':
                    return {'status': 'running'}
                self.highlight_scans[taskname] = {'status': 'running', 'added': 0, 'error': ''}
            def scan():
                try:
                    added = task_info['class'].event_class.scan_legacy_sessions()
                    result = {'status': 'completed', 'added': added, 'error': ''}
                except Exception as error:
                    self.logger.exception('扫描旧录像失败: %s', taskname)
                    result = {'status': 'failed', 'added': 0, 'error': str(error)}
                with self.highlight_scan_lock:
                    self.highlight_scans[taskname] = result
            threading.Thread(target=scan, daemon=True, name=f'highlight-scan-{taskname}').start()
            return {'status': 'running'}

        @app.route('/api/highlight/sessions/scan/status')
        @self.login_required
        def highlight_sessions_scan_status_api():
            taskname = request.args.get('taskname')
            with self.highlight_scan_lock:
                result = dict(self.highlight_scans.get(taskname) or {'status': 'idle', 'added': 0, 'error': ''})
            if result['status'] in ('completed', 'failed'):
                result.update({'sessions': self.get_highlight_sessions(),
                               'tasknames': self.get_replay_task_names()})
            return result

        @app.route('/api/highlight/manual', methods=['POST'])
        @self.login_required
        def highlight_manual_api():
            payload = request.get_json(silent=True) or {}
            taskname, session_id = payload.get('taskname'), payload.get('session_id')
            replay_info = self.engine.task_dict.get(f'replay/{taskname}') if self.engine else None
            highlight_info = next((info for info in self.engine.task_dict.values()
                                   if info.get('task_type') == 'highlight'), None) if self.engine else None
            if not replay_info or not highlight_info:
                return {'status': 'error', 'message': '录制任务或热点任务未加载。'}, 404
            session = replay_info['class'].event_class.get_completed_session(
                session_id, payload.get('selected_paths'))
            if not session or not session.get('video_states'):
                return {'status': 'error', 'message': '直播场次不存在或未选择有效分段。'}, 400
            highlight_task = highlight_info['class']
            base = merge_dict(deepcopy(highlight_task.config.get('defaults') or {}),
                              deepcopy(highlight_task.config.get('targets', {}).get(taskname) or {}))
            override_text = payload.get('override_yaml') or '{}'
            try:
                override = yaml.safe_load(override_text) or {}
                if not isinstance(override, dict):
                    raise ValueError('覆盖参数必须是 YAML 对象。')
                target = merge_dict(base, override)
            except Exception as error:
                return {'status': 'error', 'message': f'临时参数无效: {error}'}, 400
            source_override = payload.get('source') or {}
            target['source'] = merge_dict(target.get('source') or {}, source_override)
            output_ids = payload.get('output_ids') or []
            if not isinstance(output_ids, list) or not output_ids:
                return {'status': 'error', 'message': '请至少选择一个输出方案。'}, 400
            manual_outputs = manual_highlight_output_profiles(target)
            available_ids = {str(item.get('id')) for item in manual_outputs if item.get('id')}
            unknown_ids = [str(value) for value in output_ids if str(value) not in available_ids]
            if unknown_ids:
                return {'status': 'error', 'message': f'包含无效的输出方案: {unknown_ids}'}, 400
            selected_ids = {str(value) for value in output_ids}
            target['outputs'] = [item for item in manual_outputs
                                 if str(item.get('id')) in selected_ids]
            target.setdefault('upload', {})['enabled'] = bool(payload.get('upload_confirmed'))
            source = target.setdefault('source', {})
            if source.get('video', 'src_video') not in ('src_video', 'dm_video'):
                return {'status': 'error', 'message': 'source.video 仅支持 src_video 或 dm_video。'}, 400
            if source.get('subtitle', 'prefer') not in ('disabled', 'available', 'prefer', 'required'):
                return {'status': 'error', 'message': 'source.subtitle 策略无效。'}, 400
            if source.get('video', 'src_video') == 'dm_video':
                missing_dm = [index + 1 for index, state in enumerate(session['video_states'])
                              if not (state.get('dm_video') or {}).get('file')]
                if missing_dm and not (target.get('preprocess') or {}).get('dmrender'):
                    return {'status': 'error', 'message':
                            f'分段 {missing_dm} 未找到任务配置目录中的弹幕版视频，且未配置 preprocess.dmrender。'}, 400
            active = [job for job in highlight_task.jobs.values()
                      if job.get('source_task') == taskname and job.get('group_id') == session_id and
                      job.get('status') not in ('completed', 'failed')]
            if active:
                return {'status': 'error', 'message': '该场直播已有正在运行的热点任务。'}, 409
            run_id = uuid(8)
            highlight_task.recv_queue.put(PipeMessage(
                source='web', target=f'highlight/{highlight_task.taskname}', event='source_ready',
                data={'source_task': taskname, 'group_id': session_id, 'session_ended': True,
                      'segment_count': len(session['video_states']), 'video_states': session['video_states'],
                      'manual': True, 'run_id': run_id, 'target_config': target},
            ))
            return {'status': 'success', 'run_id': run_id,
                    'job_id': f'{taskname}:{session_id}:manual:{run_id}'}

        @app.route('/api/highlight/results')
        @self.login_required
        def highlight_results_api():
            return {'status': 'success', 'results': self.get_highlight_results()}

        @app.route('/api/highlight/media')
        @self.login_required
        def highlight_media_api():
            manifest_path, media_path = request.args.get('manifest'), request.args.get('path')
            allowed = self._highlight_media_paths(manifest_path)
            real_path = os.path.realpath(media_path or '')
            if real_path not in allowed or not os.path.isfile(real_path):
                return {'status': 'error', 'message': '文件不存在或不属于该热点任务。'}, 404
            return send_file(real_path, conditional=True)

        @app.route('/api/highlight/clip/media')
        @self.login_required
        def highlight_clip_media_api():
            try:
                manifest = self._read_highlight_manifest(request.args.get('manifest'))
                clip = next(item for item in manifest.get('clips') or []
                            if str(item.get('id')) == str(request.args.get('clip_id')))
                if request.args.get('segment') is not None:
                    source = (manifest.get('source_segments') or [])[int(request.args.get('segment'))]
                else:
                    source = (clip.get('pieces') or [])[int(request.args.get('piece', 0))]
                real_path = os.path.realpath(source.get('path') or '')
                allowed = self._highlight_source_paths(manifest)
                if real_path not in allowed or not os.path.isfile(real_path):
                    raise ValueError('热点源视频不存在。')
                return send_file(real_path, conditional=True)
            except Exception as error:
                return {'status': 'error', 'message': str(error)}, 404

        @app.route('/api/highlight/clip/range', methods=['POST'])
        @self.login_required
        def highlight_clip_range_api():
            payload = request.get_json(silent=True) or {}
            try:
                manifest_path = payload.get('manifest')
                manifest = self._read_highlight_manifest(manifest_path)
                clip = next(item for item in manifest.get('clips') or []
                            if str(item.get('id')) == str(payload.get('clip_id')))
                if clip.get('storage') != 'virtual':
                    raise ValueError('只有虚拟热点片段可以调整范围。')
                revision = int(payload.get('revision'))
                if revision != int(clip.get('revision', 1)):
                    raise ValueError('片段已在其他页面修改，请刷新后重试。')
                segments = manifest.get('source_segments') or []
                total = max((float(item.get('offset', 0)) + float(item.get('duration', 0))
                             for item in segments), default=0)
                requested_ranges = payload.get('ranges')
                if requested_ranges is None:
                    requested_ranges = [{'start': float(payload.get('start')),
                                         'end': float(payload.get('end'))}]
                from DMR.Highlight.cutter import map_range, map_ranges, normalize_ranges, write_manifest
                ranges = normalize_ranges(requested_ranges, minimum_duration=1.0, max_ranges=50)
                if ranges[-1]['end'] > total + .001:
                    raise ValueError('保留区间必须位于整场直播内。')
                for value in ranges:
                    mapped = map_range(segments, value['start'], value['end'], outward=False)
                    mapped_duration = sum(float(item['end']) - float(item['start']) for item in mapped)
                    if not mapped or mapped_duration < value['end'] - value['start'] - 1e-3:
                        raise ValueError('保留区间包含缺失的视频分段。')
                pieces = map_ranges(segments, ranges, outward=False)
                start, end = ranges[0]['start'], ranges[-1]['end']
                duration = sum(value['end'] - value['start'] for value in ranges)
                clip.update({'requested_start': start, 'requested_end': end,
                             'effective_start': start, 'effective_end': end,
                             'ranges': ranges, 'duration': duration, 'pieces': pieces,
                             'revision': revision + 1, 'export_stale': bool(clip.get('export_path'))})
                initial = manifest.setdefault('initial_mix', {})
                if str(clip.get('id')) in [str(value) for value in initial.get('clip_ids') or []]:
                    initial['stale'] = True
                write_manifest(manifest_path, manifest)
                return {'status': 'success', 'clip': clip, 'initial_mix': initial}
            except Exception as error:
                return {'status': 'error', 'message': str(error)}, 409

        @app.route('/api/highlight/initial-mix/generate', methods=['POST'])
        @self.login_required
        def highlight_initial_mix_generate_api():
            payload = request.get_json(silent=True) or {}
            try:
                manifest_path = os.path.realpath(payload.get('manifest') or '')
                self._read_highlight_manifest(manifest_path)
                operation_id = uuid()
                operation = {'id': operation_id, 'kind': 'initial_mix', 'manifest': manifest_path,
                             'status': 'queued', 'created_at': datetime.now().isoformat()}
                with self.highlight_operation_lock:
                    self.highlight_operations[operation_id] = operation
                    atomic_json_dump(self.highlight_operations, self.highlight_operation_file)
                self.highlight_operation_executor.submit(self._generate_initial_mix, operation_id)
                return {'status': 'success', 'operation_id': operation_id}, 202
            except Exception as error:
                return {'status': 'error', 'message': str(error)}, 400

        @app.route('/api/highlight/operation/status')
        @self.login_required
        def highlight_operation_status_api():
            with self.highlight_operation_lock:
                operation = deepcopy(self.highlight_operations.get(request.args.get('id')))
            if not operation:
                return {'status': 'error', 'message': '操作不存在。'}, 404
            return {'status': 'success', 'operation': operation}

        @app.route('/api/highlight/thumbnail')
        @self.login_required
        def highlight_thumbnail_api():
            manifest_path, media_path = request.args.get('manifest'), request.args.get('path')
            allowed = self._highlight_media_paths(manifest_path)
            real_path = os.path.realpath(media_path or '')
            if real_path not in allowed or not os.path.isfile(real_path):
                return {'status': 'error', 'message': '文件不存在或不属于该热点任务。'}, 404
            try:
                stat = os.stat(real_path)
                cache_key = hashlib.sha256(
                    f'{real_path}|{stat.st_size}|{stat.st_mtime_ns}'.encode('utf-8')
                ).hexdigest()
                cache_dir = os.path.realpath(os.path.join('.temp', 'highlight-thumbnails'))
                thumbnail_path = os.path.join(cache_dir, f'{cache_key}.jpg')
                with self.highlight_thumbnail_lock:
                    if not os.path.isfile(thumbnail_path):
                        os.makedirs(cache_dir, exist_ok=True)
                        temporary_path = os.path.join(cache_dir, f'{cache_key}.part.jpg')
                        command = [
                            ToolsList.get('ffmpeg') or 'ffmpeg', '-hide_banner', '-loglevel', 'error',
                            '-y', '-ss', '0.5', '-i', real_path, '-frames:v', '1',
                            '-vf', 'scale=480:-2', '-q:v', '4', temporary_path,
                        ]
                        result = subprocess.run(command, capture_output=True, timeout=30, check=False)
                        if result.returncode != 0 or not os.path.isfile(temporary_path):
                            if os.path.isfile(temporary_path):
                                os.remove(temporary_path)
                            message = result.stderr.decode('utf-8', errors='replace').strip()
                            raise RuntimeError(message or '无法提取视频预览图。')
                        os.replace(temporary_path, thumbnail_path)
                return send_file(thumbnail_path, mimetype='image/jpeg', conditional=True, max_age=86400)
            except Exception as error:
                return {'status': 'error', 'message': f'生成预览图失败: {error}'}, 500

        @app.route('/api/highlight/clip/thumbnail')
        @self.login_required
        def highlight_clip_thumbnail_api():
            try:
                manifest_path, clip_id = request.args.get('manifest'), request.args.get('clip_id')
                manifest = self._read_highlight_manifest(manifest_path)
                clip = next(item for item in manifest.get('clips') or [] if str(item.get('id')) == str(clip_id))
                if int(request.args.get('revision', clip.get('revision', 1))) != int(clip.get('revision', 1)):
                    raise ValueError('缩略图修订号已过期。')
                piece = (clip.get('pieces') or [])[0]
                source = os.path.realpath(piece['path'])
                if source not in self._highlight_source_paths(manifest) or not os.path.isfile(source):
                    raise ValueError('热点源视频不存在。')
                stat = os.stat(source)
                key = hashlib.sha256(f'{source}|{stat.st_size}|{stat.st_mtime_ns}|{clip_id}|{clip.get("revision", 1)}'.encode()).hexdigest()
                cache_dir = os.path.realpath(os.path.join('.temp', 'highlight-thumbnails'))
                target = os.path.join(cache_dir, key + '.jpg')
                with self.highlight_thumbnail_lock:
                    if not os.path.isfile(target):
                        os.makedirs(cache_dir, exist_ok=True)
                        temporary = target + '.part.jpg'
                        command = [ToolsList.get('ffmpeg') or 'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                                   '-ss', str(piece['start']), '-i', source, '-frames:v', '1',
                                   '-vf', 'scale=480:-2', '-q:v', '4', temporary]
                        result = subprocess.run(command, capture_output=True, timeout=30, check=False)
                        if result.returncode or not os.path.isfile(temporary):
                            raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))
                        os.replace(temporary, target)
                return send_file(target, mimetype='image/jpeg', conditional=True, max_age=86400)
            except Exception as error:
                return {'status': 'error', 'message': str(error)}, 404

        @app.route('/api/highlight/recompose', methods=['POST'])
        @self.login_required
        def highlight_recompose_api():
            payload = request.get_json(silent=True) or {}
            manifest_path = payload.get('manifest')
            try:
                manifest = self._read_highlight_manifest(manifest_path)
                clip_map = {str(item['id']): item for item in manifest.get('clips') or []}
                clip_ids = [str(value) for value in payload.get('clip_ids') or []]
                if not clip_map:
                    raise ValueError('该场直播没有通过审核的热点片段，无法进行拼接。')
                if not clip_ids:
                    raise ValueError('请至少勾选一个热点片段。')
                if any(value not in clip_map for value in clip_ids):
                    raise ValueError('片段选择无效。')
                paths = [clip_map[value]['path'] for value in clip_ids]
                if any(os.path.realpath(path) not in self._highlight_media_paths(manifest_path) for path in paths):
                    raise ValueError('片段文件不属于该热点任务。')
                versions = manifest.setdefault('versions', [])
                used_numbers = [int(match.group(1)) for item in versions
                                if (match := re.fullmatch(r'custom-v(\d+)', str(item.get('id', ''))))]
                version_id = f"custom-v{max(used_numbers, default=0) + 1}"
                extension = os.path.splitext(paths[0])[1] or '.mp4'
                version_name, file_stem = self._highlight_output_name(payload.get('output_name'), version_id)
                output = safe_filename(os.path.join(os.path.dirname(manifest_path), f'{file_stem}{extension}'))
                highlight_info = next((info for info in self.engine.task_dict.values()
                                       if info.get('task_type') == 'highlight'), None)
                highlight_task = (highlight_info or {}).get('class')
                job = next((item for item in getattr(highlight_task, 'jobs', {}).values()
                            if os.path.realpath(item.get('manifest') or '') == os.path.realpath(manifest_path)), None)
                config = deepcopy((job or {}).get('config', {}).get('encoding') or
                                  getattr(highlight_task, 'config', {}).get('defaults', {}).get('encoding', {}))
                from DMR.Highlight.cutter import recompose_clips, write_manifest
                recompose_clips(paths, output, config, self.logger)
                record = {'id': version_id, 'name': version_name, 'path': output, 'clip_ids': clip_ids,
                          'duration': sum(float(clip_map[value].get('duration', 0)) for value in clip_ids),
                          'uploaded': False, 'created_at': datetime.now().isoformat()}
                versions.append(record)
                write_manifest(manifest_path, manifest)
                return {'status': 'success', 'version': record}
            except Exception as error:
                self.logger.exception('热点片段重新拼接失败')
                return {'status': 'error', 'message': str(error)}, 400

        @app.route('/api/highlight/recompose/multi', methods=['POST'])
        @self.login_required
        def highlight_recompose_multi_api():
            payload = request.get_json(silent=True) or {}
            materials = payload.get('materials') or []
            if not isinstance(materials, list) or not 1 <= len(materials) <= 100:
                return {'status': 'error', 'message': '请选择 1 至 100 个热点小片段。'}, 400
            try:
                resolved = []
                for material in materials:
                    manifest_path = material.get('manifest') if isinstance(material, dict) else None
                    manifest = self._read_highlight_manifest(manifest_path)
                    clip_id = str(material.get('clip_id'))
                    clip = next((item for item in manifest.get('clips') or []
                                 if str(item.get('id')) == clip_id), None)
                    if not clip:
                        raise ValueError(f'热点小片段不存在: {clip_id}')
                    if clip.get('storage') != 'virtual' and os.path.realpath(clip.get('path', '')) not in self._highlight_media_paths(manifest_path):
                        raise ValueError(f'热点小片段文件不存在: {clip_id}')
                    requested_revision = material.get('revision')
                    if requested_revision is not None and int(requested_revision) != int(clip.get('revision', 1)):
                        raise ValueError(f'热点小片段已被修改: {clip_id}')
                    resolved.append({'manifest': os.path.realpath(manifest_path), 'clip': clip})
                host_path = resolved[0]['manifest']
                host = self._read_highlight_manifest(host_path)
                versions = host.setdefault('versions', [])
                used_numbers = [int(match.group(1)) for item in versions
                                if (match := re.fullmatch(r'custom-v(\d+)', str(item.get('id', ''))))]
                version_id = f'custom-v{max(used_numbers, default=0) + 1}'
                first_path = resolved[0]['clip'].get('path') or ((resolved[0]['clip'].get('pieces') or [{}])[0].get('path'))
                extension = os.path.splitext(first_path or '')[1] or '.mp4'
                version_name, file_stem = self._highlight_output_name(payload.get('output_name'), version_id)
                output = safe_filename(os.path.join(os.path.dirname(host_path), f'{file_stem}{extension}'))
                highlight_info = next((info for info in self.engine.task_dict.values()
                                       if info.get('task_type') == 'highlight'), None)
                highlight_task = (highlight_info or {}).get('class')
                job = next((item for item in getattr(highlight_task, 'jobs', {}).values()
                            if os.path.realpath(item.get('manifest') or '') == host_path), None)
                config = deepcopy((job or {}).get('config', {}).get('encoding') or
                                  getattr(highlight_task, 'config', {}).get('defaults', {}).get('encoding', {}))
                from DMR.Highlight.cutter import materialize_clip, recompose_clips, write_manifest
                with tempfile.TemporaryDirectory(prefix='dmr-highlight-web-', dir='.temp') as temp_dir:
                    paths = []
                    for index, item in enumerate(resolved):
                        clip = item['clip']
                        if clip.get('storage') == 'virtual':
                            path = os.path.join(temp_dir, f'{index:03d}{extension}')
                            materialize_clip(clip, path, config, self.logger)
                        else:
                            path = clip['path']
                        paths.append(path)
                    recompose_clips(paths, output, config, self.logger)
                record = {
                    'id': version_id, 'name': version_name, 'path': output,
                    'clip_ids': [str(item['clip']['id']) for item in resolved],
                    'materials': [{'manifest': item['manifest'], 'clip_id': str(item['clip']['id']),
                                   'revision': int(item['clip'].get('revision', 1))}
                                  for item in resolved],
                    'duration': sum(float(item['clip'].get('duration', 0)) for item in resolved),
                    'uploaded': False, 'created_at': datetime.now().isoformat(),
                }
                versions.append(record)
                write_manifest(host_path, host)
                return {'status': 'success', 'version': record}
            except Exception as error:
                self.logger.exception('跨热点场次片段拼接失败')
                return {'status': 'error', 'message': str(error)}, 400

        @app.route('/api/highlight/artifact/delete', methods=['POST'])
        @self.login_required
        def highlight_artifact_delete_api():
            payload = request.get_json(silent=True) or {}
            try:
                manifest_path = payload.get('manifest')
                manifest = self._read_highlight_manifest(manifest_path)
                kind = payload.get('kind')
                if kind not in ('clip', 'version'):
                    raise ValueError('文件类型无效。')
                collection = 'clips' if kind == 'clip' else 'versions'
                artifact_id = str(payload.get('id'))
                item = next((value for value in manifest.get(collection, []) if str(value.get('id')) == artifact_id), None)
                if not item:
                    raise ValueError('文件记录不存在。')
                path_value = item.get('path') or item.get('export_path')
                if path_value:
                    path = os.path.realpath(path_value)
                    if path not in self._highlight_media_paths(manifest_path):
                        raise ValueError('文件不属于该热点任务。')
                    if os.path.exists(path):
                        os.remove(path)
                elif item.get('storage') != 'virtual':
                    raise ValueError('文件记录缺少路径。')
                manifest[collection].remove(item)
                from DMR.Highlight.cutter import write_manifest
                write_manifest(manifest_path, manifest)
                return {'status': 'success'}
            except Exception as error:
                return {'status': 'error', 'message': str(error)}, 400

        @app.route('/api/highlight/result/delete', methods=['POST'])
        @self.login_required
        def highlight_result_delete_api():
            payload = request.get_json(silent=True) or {}
            manifest_path = payload.get('manifest')
            try:
                self._read_highlight_manifest(manifest_path)
                real_manifest = os.path.realpath(manifest_path)
                highlight_info = next((info for info in self.engine.task_dict.values()
                                       if info.get('task_type') == 'highlight' and any(
                                           os.path.realpath(job.get('manifest') or '') == real_manifest
                                           for job in getattr(info.get('class'), 'jobs', {}).values()
                                       )), None)
                highlight_task = (highlight_info or {}).get('class')
                if not highlight_task:
                    raise ValueError('找不到该热点场次对应的任务记录。')
                allowed = self._highlight_media_paths(manifest_path)
                success, reason = highlight_task.delete_completed_job_by_manifest(manifest_path)
                if not success:
                    raise ValueError(reason)
                failed = []
                for path in sorted(allowed, key=len, reverse=True):
                    try:
                        if os.path.isfile(path):
                            os.remove(path)
                    except OSError as error:
                        failed.append(f'{os.path.basename(path)}: {error}')
                try:
                    if os.path.isfile(real_manifest):
                        os.remove(real_manifest)
                except OSError as error:
                    failed.append(f'{os.path.basename(real_manifest)}: {error}')
                for directory in (os.path.join(os.path.dirname(real_manifest), 'clips'),
                                  os.path.dirname(real_manifest)):
                    try:
                        if os.path.isdir(directory) and not os.listdir(directory):
                            os.rmdir(directory)
                    except OSError:
                        pass
                if failed:
                    self.logger.warning('热点场次记录已删除，但部分文件删除失败: %s', failed)
                    return {'status': 'partial', 'message': '场次已移除，但部分文件被占用，未能删除。',
                            'failed': failed}
                return {'status': 'success'}
            except Exception as error:
                return {'status': 'error', 'message': str(error)}, 400

        @app.route('/api/highlight/version/upload', methods=['POST'])
        @self.login_required
        def highlight_version_upload_api():
            payload = request.get_json(silent=True) or {}
            if payload.get('confirmed') is not True:
                return {'status': 'error', 'message': '上传必须显式确认。'}, 400
            try:
                manifest_path, version_id = payload.get('manifest'), str(payload.get('id'))
                manifest = self._read_highlight_manifest(manifest_path)
                version = next((item for item in manifest.get('versions', [])
                                if str(item.get('id')) == version_id), None)
                if not version or os.path.realpath(version.get('path', '')) not in self._highlight_media_paths(manifest_path):
                    raise ValueError('混剪版本不存在。')
                highlight_info = next((info for info in self.engine.task_dict.values()
                                       if info.get('task_type') == 'highlight'), None)
                highlight_task = (highlight_info or {}).get('class')
                job = next((item for item in getattr(highlight_task, 'jobs', {}).values()
                            if os.path.realpath(item.get('manifest') or '') == os.path.realpath(manifest_path)), None)
                if not job:
                    raise ValueError('找不到该版本对应的热点任务，无法取得上传配置。')
                upload = job.get('config', {}).get('upload') or {}
                args = deepcopy(upload.get('common') or {})
                args = merge_dict(args, deepcopy((upload.get('outputs') or {}).get('highlight_custom') or {}))
                if not args.get('engine'):
                    raise ValueError('未配置热点上传引擎。')
                args = highlight_task._replace_task_keywords(args, job)
                video = VideoInfo(path=version['path'], file_id=uuid(), dtype='highlight_custom',
                                  size=os.path.getsize(version['path']), duration=version.get('duration'),
                                  title=os.path.basename(version['path']), ctime=datetime.now())
                request_id = uuid()
                self.send_queue.put(PipeMessage(
                    source=f'web/highlight/{highlight_task.taskname}', target='uploader', event='newtask',
                    request_id=request_id, data={'taskname': highlight_task.taskname, 'files': [video],
                    'engine': args['engine'], 'stateless': True,
                    'upload_group': f'{highlight_task.taskname}_{manifest.get("group_id")}_{version_id}',
                    'args': args},
                ))
                version['upload_requested'] = True
                version['upload_request_id'] = request_id
                from DMR.Highlight.cutter import write_manifest
                write_manifest(manifest_path, manifest)
                return {'status': 'success', 'request_id': request_id}
            except Exception as error:
                return {'status': 'error', 'message': str(error)}, 400

        @app.route('/api/tasks')
        @self.login_required
        def tasks_api():
            return self.get_tasks_data()

        @app.route('/api/pipeline_states/delete_batch', methods=['POST'])
        @self.login_required
        def pipeline_states_delete_batch():
            payload = request.get_json(silent=True) or {}
            items = payload.get('items')
            if not isinstance(items, list) or not items:
                return {'status': 'error', 'message': '请选择要删除的流水线记录。'}, 400

            deleted = []
            skipped = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                taskname = item.get('taskname')
                task_type = item.get('task_type', 'replay')
                group_id = item.get('group_id')
                recovery_id = item.get('recovery_id') or group_id
                task_info = self.engine.task_dict.get(f'{task_type}/{taskname}') if self.engine else None
                task = task_info.get('class') if task_info else None
                if task_type == 'highlight' and task and recovery_id:
                    success, reason = task.delete_recovered_job(recovery_id)
                elif task and group_id:
                    success, reason = task.event_class.delete_recovered_pipeline_state(group_id)
                else:
                    skipped.append({'taskname': taskname, 'group_id': group_id, 'reason': '记录不存在。'})
                    continue
                result = {'taskname': taskname, 'task_type': task_type, 'group_id': group_id,
                          'recovery_id': recovery_id}
                if success:
                    deleted.append(result)
                else:
                    result['reason'] = reason
                    skipped.append(result)

            return {'status': 'success', 'deleted': deleted, 'skipped': skipped}

        @app.route('/api/pipeline_states/resume_batch', methods=['POST'])
        @self.login_required
        def pipeline_states_resume_batch():
            payload = request.get_json(silent=True) or {}
            items = payload.get('items')
            if not isinstance(items, list) or not items:
                return {'status': 'error', 'message': '请选择要恢复的流水线记录。'}, 400
            resumed, skipped = [], []
            for item in items:
                taskname = item.get('taskname') if isinstance(item, dict) else None
                task_type = item.get('task_type', 'replay') if isinstance(item, dict) else 'replay'
                recovery_id = item.get('recovery_id') or item.get('group_id') if isinstance(item, dict) else None
                task_info = self.engine.task_dict.get(f'{task_type}/{taskname}') if self.engine else None
                task = task_info.get('class') if task_info else None
                if task_type == 'highlight' and task:
                    success, reason = task.resume_recovered_job(recovery_id)
                elif task:
                    success, reason = task.resume_recovered_pipeline(recovery_id)
                else:
                    success, reason = False, '任务不存在。'
                result = {'taskname': taskname, 'task_type': task_type, 'group_id': item.get('group_id'),
                          'recovery_id': recovery_id}
                if success:
                    resumed.append(result)
                else:
                    result['reason'] = reason
                    skipped.append(result)
            return {'status': 'success', 'resumed': resumed, 'skipped': skipped}

        @app.route('/api/restart/status')
        @self.login_required
        def restart_status_api():
            return self.get_restart_status()

        @app.route('/api/restart/request', methods=['POST'])
        @self.login_required
        def restart_request_api():
            if not self.runtime_controller:
                return {'status': 'error', 'message': 'Runtime controller not available.'}, 503
            self.runtime_controller.request_restart('webui', mode='idle')
            return {'status': 'success', 'restart': self.get_restart_status()}

        @app.route('/api/restart/force', methods=['POST'])
        @self.login_required
        def restart_force_api():
            if not self.runtime_controller:
                return {'status': 'error', 'message': 'Runtime controller not available.'}, 503
            self.runtime_controller.request_restart('webui', mode='force')
            return {'status': 'success', 'restart': self.get_restart_status()}

        @app.route('/api/restart/cancel', methods=['POST'])
        @self.login_required
        def restart_cancel_api():
            if not self.runtime_controller:
                return {'status': 'error', 'message': 'Runtime controller not available.'}, 503
            cancelled = self.runtime_controller.cancel_restart()
            return {
                'status': 'success',
                'cancelled': cancelled,
                'restart': self.get_restart_status(),
            }

        @app.route('/api/check_config', methods=['POST'])
        @self.login_required
        def check_config_api():
            try:
                content = request.json.get('content')
                yaml.safe_load(content)
                return {'valid': True, 'message': '配置文件格式正确 (Valid YAML)'}
            except Exception as e:
                return {'valid': False, 'message': f'配置文件格式错误: {e}'}

        @app.route('/config')
        @self.login_required
        def config_list():
            configs = []
            config_dir = 'configs'
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            
            files = glob.glob(os.path.join(config_dir, '*.yml'))
            for f in files:
                filename = os.path.basename(f)
                is_global = filename.lower() == 'global.yml'
                if is_global:
                    taskname = '全局配置'
                elif filename.startswith('DMR-'):
                    taskname = filename[4:-4]
                elif filename.endswith('.yml'):
                    taskname = filename[:-4]
                else:
                    taskname = filename
                
                configs.append({
                    'filename': filename,
                    'taskname': taskname,
                    'is_global': is_global,
                })

            configs.sort(key=lambda item: (not item['is_global'], item['filename']))
            
            return render_template('config_list.html', configs=configs)

        @app.route('/config/create', methods=['GET', 'POST'])
        @self.login_required
        def config_create():
            return config_edit(filename=None)

        @app.route('/config/edit/<filename>', methods=['GET', 'POST'])
        @self.login_required
        def config_edit(filename):
            config_dir = 'configs'
            content = ""
            check_result = None
            saved_global = False
            
            if filename:
                filepath = os.path.join(config_dir, filename)
                if not os.path.exists(filepath):
                    flash(f'File {filename} not found.', 'error')
                    return redirect(url_for('config_list'))
                
                if request.method == 'GET':
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()

            if request.method == 'POST':
                content = request.form['content']
                # Normalize line endings to avoid triple spacing (CRLF -> LF)
                content = content.replace('\r\n', '\n')
                action = request.form['action']
                new_filename = request.form.get('new_filename', filename)
                
                # Validation
                try:
                    yaml.safe_load(content)
                    valid = True
                    check_result = "YAML Format OK"
                except Exception as e:
                    valid = False
                    check_result = f"YAML Error: {e}"
                
                if action == 'save':
                    if filename and filename.startswith('example-'):
                        flash('示例文件不支持修改。', 'error')
                        return redirect(url_for('config_list'))
                    
                    if valid:
                        if not new_filename.endswith('.yml'):
                             new_filename += '.yml'
                        
                        save_path = os.path.join(config_dir, new_filename)
                        try:
                            with open(save_path, 'w', encoding='utf-8') as f:
                                f.write(content)
                            if os.path.basename(save_path).lower() == 'global.yml':
                                saved_global = True
                                flash('全局配置已保存，重启程序后生效。', 'success')
                                return render_template(
                                    'config_edit.html',
                                    filename=new_filename,
                                    content=content,
                                    check_result=check_result,
                                    saved_global=saved_global,
                                    restart_status=self.get_restart_status(),
                                )
                            flash(f'Config {new_filename} saved successfully.', 'success')
                            return redirect(url_for('config_list'))
                        except Exception as e:
                            flash(f'Error saving file: {e}', 'error')
                    else:
                        flash('Invalid YAML format. Please fix errors before saving.', 'error')

            return render_template(
                'config_edit.html',
                filename=filename,
                content=content,
                check_result=check_result,
                saved_global=saved_global,
                restart_status=self.get_restart_status(),
            )

        @app.route('/config/delete/<filename>', methods=['POST'])
        @self.login_required
        def config_delete(filename):
            config_dir = 'configs'
            if filename:
                if filename.lower() == 'global.yml':
                    flash('global.yml 不支持删除。', 'error')
                    return redirect(url_for('config_list'))

                if filename.startswith('example-'):
                    flash('示例文件不支持删除。', 'error')
                    return redirect(url_for('config_list'))
                
                filepath = os.path.join(config_dir, filename)
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        flash(f'Config {filename} deleted successfully.', 'success')
                    except Exception as e:
                        flash(f'Error deleting file: {e}', 'error')
                else:
                    flash(f'File {filename} not found.', 'error')
            return redirect(url_for('config_list'))

        @app.route('/api/failed_uploads/retry/<uuid>', methods=['POST'])
        @self.login_required
        def failed_uploads_retry(uuid):
            if self.engine and 'uploader' in self.engine.plugin_dict:
                uploader = self.engine.plugin_dict['uploader']['class']
                if uploader:
                    if uploader.retry_task(uuid):
                        return {'status': 'success', 'message': 'Task retry scheduled.'}
                    else:
                        return {
                            'status': 'error',
                            'message': uploader.last_retry_error or 'Task not found or failed to retry.',
                        }
            return {'status': 'error', 'message': 'Uploader not available.'}

        @app.route('/api/failed_uploads/delete/<uuid>', methods=['POST'])
        @self.login_required
        def failed_uploads_delete(uuid):
            if self.engine and 'uploader' in self.engine.plugin_dict:
                uploader = self.engine.plugin_dict['uploader']['class']
                if uploader:
                    if uploader.delete_failed_task(uuid):
                        return {'status': 'success', 'message': 'Task deleted.'}
                    else:
                        return {'status': 'error', 'message': 'Task not found.'}
            return {'status': 'error', 'message': 'Uploader not available.'}

        @app.route('/api/failed_uploads/delete_batch', methods=['POST'])
        @self.login_required
        def failed_uploads_delete_batch():
            req_data = request.get_json(silent=True) or {}
            uuids = req_data.get('uuids') or []
            if not isinstance(uuids, list):
                return {'status': 'error', 'message': 'Invalid uuids.'}, 400
            if self.engine and 'uploader' in self.engine.plugin_dict:
                uploader = self.engine.plugin_dict['uploader']['class']
                if uploader:
                    deleted = []
                    missing = []
                    for task_uuid in uuids:
                        if uploader.delete_failed_task(task_uuid):
                            deleted.append(task_uuid)
                        else:
                            missing.append(task_uuid)
                    return {'status': 'success', 'deleted': deleted, 'missing': missing}
            return {'status': 'error', 'message': 'Uploader not available.'}

        @app.route('/api/failed_renders/retry/<uuid>', methods=['POST'])
        @self.login_required
        def failed_renders_retry(uuid):
            if self.engine and 'render' in self.engine.plugin_dict:
                render = self.engine.plugin_dict['render']['class']
                if render:
                    if render.retry_task(uuid):
                        return {'status': 'success', 'message': 'Task retry scheduled.'}
                    else:
                        return {
                            'status': 'error',
                            'message': render.last_retry_error or 'Task not found or failed to retry.',
                        }
            return {'status': 'error', 'message': 'Render not available.'}

        @app.route('/api/failed_renders/delete/<uuid>', methods=['POST'])
        @self.login_required
        def failed_renders_delete(uuid):
            if self.engine and 'render' in self.engine.plugin_dict:
                render = self.engine.plugin_dict['render']['class']
                if render:
                    if render.delete_failed_task(uuid):
                        return {'status': 'success', 'message': 'Task deleted.'}
                    else:
                        return {'status': 'error', 'message': 'Task not found.'}
            return {'status': 'error', 'message': 'Render not available.'}

        @app.route('/api/failed_renders/delete_batch', methods=['POST'])
        @self.login_required
        def failed_renders_delete_batch():
            req_data = request.get_json(silent=True) or {}
            uuids = req_data.get('uuids') or []
            if not isinstance(uuids, list):
                return {'status': 'error', 'message': 'Invalid uuids.'}, 400
            if self.engine and 'render' in self.engine.plugin_dict:
                render = self.engine.plugin_dict['render']['class']
                if render:
                    deleted = []
                    missing = []
                    for task_uuid in uuids:
                        if render.delete_failed_task(task_uuid):
                            deleted.append(task_uuid)
                        else:
                            missing.append(task_uuid)
                    return {'status': 'success', 'deleted': deleted, 'missing': missing}
            return {'status': 'error', 'message': 'Render not available.'}

        return app

    def get_tasks_data(self):
        # Get Recording Tasks
        recording_tasks = []
        if self.engine and 'downloader' in self.engine.plugin_dict:
            downloader = self.engine.plugin_dict['downloader']['class']
            if downloader:
                for taskname, task in downloader.download_tasks.items():
                    # Basic info
                    status = 1 if not getattr(task, 'stoped', True) else 0
                    duration = "未开播"
                    
                    if status == 1:
                        if hasattr(task, 'segment_start_time'):
                             delta = datetime.now() - task.segment_start_time
                             duration = str(delta).split('.')[0]
                    
                    recording_tasks.append({
                        'name': taskname,
                        'platform': task.plat,
                        'url': task.url,
                        'status': status,
                        'duration': duration
                    })

        # Get Upload Tasks
        upload_tasks_list = []
        failed_tasks_list = []
        if self.engine and 'uploader' in self.engine.plugin_dict:
            uploader = self.engine.plugin_dict['uploader']['class']
            if uploader:
                for uuid, task in uploader.upload_tasks.items():
                    upload_tasks_list.append({
                        'files': [{'name': os.path.basename(f.path)} for f in task.get('files', [])],
                        'account': task.get('args', {}).get('account', 'Unknown'),
                        'engine': task.get('engine', 'Unknown'),
                        'is_sync': bool(task.get('stream_queue')),
                        'status': task.get('status', 'waiting')
                    })
                
                for uuid, task in uploader.failed_tasks.items():
                    failed_tasks_list.append({
                        'uuid': uuid,
                        'files': [{'name': os.path.basename(f.path)} for f in task.get('files', [])],
                        'account': task.get('args', {}).get('account', 'Unknown'),
                        'engine': task.get('engine', 'Unknown'),
                        'command': task.get('command'),
                        'status': task.get('status', 'failed'),
                        'failure_reason': task.get('failure_reason', ''),
                    })

        # Get Render Tasks
        render_tasks_list = []
        failed_renders_list = []
        if self.engine and 'render' in self.engine.plugin_dict:
            render = self.engine.plugin_dict['render']['class']
            if render:
                for uuid, task in render.render_tasks.items():
                    video_path = task.get('video').path if task.get('video') else 'Unknown'
                    render_tasks_list.append({
                        'video': os.path.basename(video_path),
                        'output': os.path.basename(task.get('output', 'Unknown')),
                        'mode': task.get('mode', 'Unknown'),
                        'status': task.get('status', 'waiting')
                    })
                
                for uuid, task in render.failed_tasks.items():
                    video_path = task.get('video').path if task.get('video') else 'Unknown'
                    failed_renders_list.append({
                        'uuid': uuid,
                        'video': os.path.basename(video_path),
                        'output': os.path.basename(task.get('output', 'Unknown')),
                        'mode': task.get('mode', 'Unknown'),
                        'status': task.get('status', 'failed'),
                        'failure_reason': task.get('failure_reason', ''),
                    })
        
        return {
            'recording_tasks': recording_tasks, 
            'upload_tasks': upload_tasks_list, 
            'failed_tasks': failed_tasks_list,
            'render_tasks': render_tasks_list, 
            'failed_renders': failed_renders_list,
            'pipeline_states': self.get_pipeline_states(),
            'restart_status': self.get_restart_status(),
        }

    def _known_highlight_manifests(self):
        paths = set()
        if self.engine:
            for info in self.engine.task_dict.values():
                if info.get('task_type') == 'highlight':
                    paths.update(os.path.realpath(job.get('manifest'))
                                 for job in getattr(info.get('class'), 'jobs', {}).values()
                                 if job.get('manifest'))
        return paths

    def _read_highlight_manifest(self, path):
        real_path = os.path.realpath(path or '')
        if (real_path not in self._known_highlight_manifests() or
                not real_path.endswith('.highlights.json') or not os.path.isfile(real_path)):
            raise ValueError('热点清单不存在。')
        with open(real_path, 'r', encoding='utf-8') as file:
            manifest = json.load(file)
        if manifest.get('version') not in (1, 2, 3):
            raise ValueError('热点清单版本不受支持。')
        return manifest

    @staticmethod
    def _highlight_output_name(requested_name, version_id):
        """Return the user-facing remix name and a filesystem-safe stem."""
        name = str(requested_name or '').strip()
        if len(name) > 80:
            raise ValueError('混剪名称不能超过 80 个字符。')
        if not name:
            name = version_id
        file_stem = replace_invalid_chars(name).strip().rstrip('. ')
        return name, file_stem or version_id

    def _highlight_media_paths(self, manifest_path):
        try:
            manifest = self._read_highlight_manifest(manifest_path)
        except Exception:
            return set()
        paths = []
        paths.extend(item.get('path') for item in manifest.get('clips') or [])
        paths.extend(item.get('path') for item in manifest.get('outputs') or [])
        paths.extend(item.get('path') for item in manifest.get('versions') or [])
        paths.extend(item.get('export_path') for item in manifest.get('clips') or [])
        return {os.path.realpath(path) for path in paths if path}

    @staticmethod
    def _highlight_source_paths(manifest):
        paths = [item.get('path') for item in manifest.get('source_segments') or []]
        for clip in manifest.get('clips') or []:
            paths.extend(item.get('path') for item in clip.get('pieces') or [])
        return {os.path.realpath(path) for path in paths if path}

    @staticmethod
    def _encode_virtual_material(manifest, clip):
        payload = json.dumps({'manifest': os.path.realpath(manifest), 'clip_id': str(clip.get('id')),
                              'revision': int(clip.get('revision', 1))}, ensure_ascii=False).encode('utf-8')
        return 'highlight-virtual:' + base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')

    @staticmethod
    def _decode_virtual_material(value):
        if not str(value).startswith('highlight-virtual:'):
            return None
        try:
            encoded = str(value).split(':', 1)[1]
            encoded += '=' * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded).decode('utf-8'))
            return {'manifest': os.path.realpath(payload['manifest']), 'clip_id': str(payload['clip_id']),
                    'revision': int(payload['revision'])}
        except Exception:
            return None

    def _export_virtual_material(self, descriptor):
        manifest = self._read_highlight_manifest(descriptor['manifest'])
        clip = next(item for item in manifest.get('clips') or []
                    if str(item.get('id')) == descriptor['clip_id'])
        if int(clip.get('revision', 1)) != descriptor['revision']:
            raise ValueError('热点片段范围已修改，请刷新素材库后重新选择。')
        current = clip.get('export_path')
        if current and int(clip.get('export_revision', 0)) == descriptor['revision'] and os.path.isfile(current):
            return os.path.realpath(current)
        highlight_info = next((info for info in self.engine.task_dict.values()
                               if info.get('task_type') == 'highlight'), None) if self.engine else None
        highlight_task = (highlight_info or {}).get('class')
        job = next((item for item in getattr(highlight_task, 'jobs', {}).values()
                    if os.path.realpath(item.get('manifest') or '') == descriptor['manifest']), None)
        config = deepcopy((job or {}).get('config', {}).get('encoding') or
                          getattr(highlight_task, 'config', {}).get('defaults', {}).get('encoding', {}))
        extension = config.get('format', 'mp4')
        export_dir = os.path.join(os.path.dirname(descriptor['manifest']), 'exports')
        os.makedirs(export_dir, exist_ok=True)
        target = safe_filename(os.path.join(export_dir, f"{clip.get('sequence', 0):03d}-{clip['id']}.{extension}"))
        temporary = target + '.new.' + extension
        from DMR.Highlight.cutter import materialize_clip, write_manifest
        materialize_clip(clip, temporary, config, self.logger)
        os.replace(temporary, target)
        clip.update({'export_path': target, 'export_revision': descriptor['revision'], 'export_stale': False})
        write_manifest(descriptor['manifest'], manifest)
        self.upload_library_cache['time'] = 0
        return os.path.realpath(target)

    def _generate_initial_mix(self, operation_id):
        with self.highlight_operation_lock:
            operation = self.highlight_operations[operation_id]
            operation['status'] = 'running'
            atomic_json_dump(self.highlight_operations, self.highlight_operation_file)
        try:
            manifest_path = operation['manifest']
            manifest = self._read_highlight_manifest(manifest_path)
            clips = {str(item.get('id')): item for item in manifest.get('clips') or []}
            clip_ids = [str(value) for value in (manifest.get('initial_mix') or {}).get('clip_ids') or []]
            selected = [clips[value] for value in clip_ids if value in clips]
            if not selected:
                raise ValueError('系统推荐列表为空，无法生成混剪。')
            highlight_info = next((info for info in self.engine.task_dict.values()
                                   if info.get('task_type') == 'highlight'), None) if self.engine else None
            highlight_task = (highlight_info or {}).get('class')
            job = next((item for item in getattr(highlight_task, 'jobs', {}).values()
                        if os.path.realpath(item.get('manifest') or '') == manifest_path), None)
            config = deepcopy((job or {}).get('config', {}).get('encoding') or
                              getattr(highlight_task, 'config', {}).get('defaults', {}).get('encoding', {}))
            extension = config.get('format', 'mp4')
            initial = manifest.setdefault('initial_mix', {})
            revision = int(initial.get('revision', 0)) + 1
            output = safe_filename(os.path.join(os.path.dirname(manifest_path),
                                                f'系统推荐混剪-v{revision}.{extension}'))
            from DMR.Highlight.cutter import materialize_clip, recompose_clips, write_manifest
            with tempfile.TemporaryDirectory(prefix='dmr-highlight-initial-', dir='.temp') as temp_dir:
                paths = []
                for index, clip in enumerate(selected):
                    if clip.get('storage') == 'virtual':
                        path = os.path.join(temp_dir, f'{index:03d}.{extension}')
                        materialize_clip(clip, path, config, self.logger)
                    else:
                        path = clip['path']
                    paths.append(path)
                recompose_clips(paths, output, config, self.logger)
            record = {'path': output, 'dtype': 'highlight_initial',
                      'duration': sum(float(item.get('duration', 0)) for item in selected),
                      'revision': revision, 'created_at': datetime.now().isoformat(),
                      'clip_revisions': {str(item['id']): int(item.get('revision', 1)) for item in selected}}
            manifest.setdefault('outputs', []).append(record)
            initial.update({'status': 'generated', 'stale': False, 'revision': revision})
            write_manifest(manifest_path, manifest)
            with self.highlight_operation_lock:
                operation.update({'status': 'completed', 'output': record, 'completed_at': datetime.now().isoformat()})
                atomic_json_dump(self.highlight_operations, self.highlight_operation_file)
        except Exception as error:
            self.logger.exception('生成系统推荐热点混剪失败')
            with self.highlight_operation_lock:
                operation.update({'status': 'failed', 'error': str(error),
                                  'completed_at': datetime.now().isoformat()})
                atomic_json_dump(self.highlight_operations, self.highlight_operation_file)

    def get_highlight_results(self):
        results, seen = [], set()
        if not self.engine:
            return results
        for info in self.engine.task_dict.values():
            if info.get('task_type') != 'highlight':
                continue
            for job in getattr(info.get('class'), 'jobs', {}).values():
                manifest_path = job.get('manifest')
                if not manifest_path or os.path.realpath(manifest_path) in seen:
                    continue
                try:
                    manifest = self._read_highlight_manifest(manifest_path)
                except Exception:
                    continue
                seen.add(os.path.realpath(manifest_path))
                results.append({'manifest': manifest_path, 'taskname': manifest.get('taskname'),
                                'source_task': manifest.get('source_task') or job.get('source_task'),
                                'display_name': os.path.basename(manifest_path).removesuffix('.highlights.json'),
                                'created_at': datetime.fromtimestamp(os.path.getmtime(manifest_path)).isoformat(),
                                'run_type': 'manual' if job.get('manual') else 'automatic',
                                'manifest_version': manifest.get('version', 1),
                                'group_id': manifest.get('group_id'), 'encoding_mode': manifest.get('encoding_mode'),
                                'detected_candidate_count': len((manifest.get('analysis') or {}).get('candidates') or []),
                                'approved_candidate_count': len(manifest.get('selected_candidates') or []),
                                'clips': manifest.get('clips') or [], 'outputs': manifest.get('outputs') or [],
                                'versions': manifest.get('versions') or [],
                                'initial_mix': manifest.get('initial_mix') or {},
                                'source_segments': [{
                                    'index': index, 'offset': float(segment.get('offset') or 0),
                                    'duration': float(segment.get('duration') or 0),
                                } for index, segment in enumerate(manifest.get('source_segments') or [])],
                                'timeline_duration': max((
                                    float(segment.get('offset') or 0) + float(segment.get('duration') or 0)
                                    for segment in manifest.get('source_segments') or []
                                ), default=0)})
        results.sort(key=lambda item: item.get('created_at') or '', reverse=True)
        return results

    def get_highlight_sessions(self):
        sessions = []
        if not self.engine:
            return sessions
        highlight_loaded = any(info.get('task_type') == 'highlight'
                               for info in self.engine.task_dict.values())
        for task_key, task_info in self.engine.task_dict.items():
            if task_info.get('task_type', 'replay') != 'replay':
                continue
            taskname = task_info.get('name', task_key.split('/', 1)[-1])
            event_class = getattr(task_info.get('class'), 'event_class', None)
            if not event_class:
                continue
            for session_id, session in event_class.completed_sessions.items():
                segments = []
                available = True
                for state in session.get('video_states', []):
                    video = ((state.get('src_video') or {}).get('file') or
                             (state.get('src_video_pre') or {}).get('file'))
                    path = getattr(video, 'path', '') if video else ''
                    exists = bool(path and os.path.exists(path))
                    available = available and exists
                    segments.append({
                        'segment_id': getattr(video, 'segment_id', len(segments) + 1),
                        'path': path, 'name': os.path.basename(path) if path else '',
                        'duration': float(getattr(video, 'duration', 0) or 0),
                        'exists': exists,
                        'has_raw_danmaku': bool(getattr(video, 'raw_dm_file_id', None) and
                                                os.path.exists(video.raw_dm_file_id)),
                        'has_ass': bool(getattr(video, 'dm_file_id', None) and os.path.exists(video.dm_file_id)),
                    })
                sessions.append({
                    'taskname': taskname, 'session_id': session_id,
                    'title': session.get('title') or '', 'started_at': session.get('started_at'),
                    'duration': session.get('duration', 0), 'inferred': bool(session.get('inferred')),
                    'segment_count': len(segments), 'segments': segments,
                    'available': available and bool(segments), 'highlight_loaded': highlight_loaded,
                })
        sessions.sort(key=lambda item: item.get('started_at') or '', reverse=True)
        return sessions

    def get_upload_account_files(self):
        accounts = {}
        for path in glob.glob(os.path.join('.login_info', '*.json')):
            try:
                with open(path, 'r', encoding='utf-8') as file:
                    payload = json.load(file)
                cookies = (payload.get('cookie_info') or {}).get('cookies') or []
                cookie_names = {str(item.get('name')) for item in cookies if isinstance(item, dict)}
                if {'SESSDATA', 'bili_jct'}.issubset(cookie_names):
                    accounts[os.path.splitext(os.path.basename(path))[0]] = os.path.realpath(path)
            except Exception:
                continue
        return accounts

    def get_upload_accounts(self):
        return sorted(self.get_upload_account_files())

    def get_upload_account_seasons(self, account):
        account_files = self.get_upload_account_files()
        cookie_file = account_files.get(account)
        if not cookie_file:
            raise ValueError('上传账号不存在或登录信息无效。')
        from DMR.Uploader.biliapi.biliapi import get_seasons
        raw_seasons, page = [], 1
        while True:
            response = get_seasons(cookie_file, page=page, page_size=100)
            if response.get('code') != 0:
                raise RuntimeError(response.get('message') or response)
            data = response.get('data') or {}
            page_items = data.get('seasons') or []
            raw_seasons.extend(page_items)
            try:
                total = int(data.get('total') or len(raw_seasons))
            except (TypeError, ValueError):
                total = len(raw_seasons)
            if not page_items or len(raw_seasons) >= total:
                break
            page += 1
            if page > 100:
                raise RuntimeError('合集数量异常，已停止继续翻页。')
        seasons, seen = [], set()
        for item in raw_seasons:
            season = item.get('season') if isinstance(item, dict) else None
            if not isinstance(season, dict):
                continue
            try:
                season_id = int(season.get('id'))
            except (TypeError, ValueError):
                continue
            if season_id in seen:
                continue
            seen.add(season_id)
            seasons.append({'id': season_id, 'name': str(season.get('title') or f'合集 {season_id}')})
        return seasons

    def get_upload_account_archives(self, account, limit=30):
        account_files = self.get_upload_account_files()
        cookie_file = account_files.get(account)
        if not cookie_file:
            raise ValueError('上传账号不存在或登录信息无效。')
        from DMR.Uploader.biliapi.biliapi import get_archives
        response = get_archives(cookie_file, page=1, page_size=min(max(int(limit), 1), 100), status='pubed')
        if response.get('code') != 0:
            raise RuntimeError(response.get('message') or response)
        submissions = []
        for item in (response.get('data') or {}).get('arc_audits') or []:
            archive = item.get('Archive') if isinstance(item, dict) else None
            if not isinstance(archive, dict) or not archive.get('bvid'):
                continue
            timestamp = archive.get('ptime') or archive.get('dtime') or archive.get('ctime') or 0
            try:
                timestamp = int(timestamp)
            except (TypeError, ValueError):
                timestamp = 0
            submissions.append({
                'bvid': str(archive['bvid']), 'title': str(archive.get('title') or archive['bvid']),
                'published_at': datetime.fromtimestamp(timestamp).isoformat() if timestamp > 0 else None,
                'timestamp': timestamp, 'duration': int(archive.get('duration') or 0),
            })
        submissions.sort(key=lambda item: item['timestamp'], reverse=True)
        return submissions[:limit]

    def get_upload_account_archive_detail(self, account, bvid):
        account_files = self.get_upload_account_files()
        cookie_file = account_files.get(account)
        if not cookie_file:
            raise ValueError('上传账号不存在或登录信息无效。')
        from DMR.Uploader.biliapi.biliapi import get_archive_view
        from DMR.Uploader.biliapi.bili_section import find_video_season
        response = get_archive_view(cookie_file, bvid)
        if response.get('code') != 0:
            raise RuntimeError(response.get('message') or response)
        data = response.get('data') or {}
        archive = data.get('archive') or {}
        if not archive or str(archive.get('bvid') or '').casefold() != str(bvid).casefold():
            raise RuntimeError('平台未返回目标稿件的编辑信息。')
        membership = find_video_season(cookie_file, bvid) or {}
        try:
            dtime = int(archive.get('dtime') or 0)
        except (TypeError, ValueError):
            dtime = 0
        subtitle = data.get('subtitle') or {}
        attrs = archive.get('attrs') or {}
        return {
            'bvid': str(archive.get('bvid') or bvid),
            'title': str(archive.get('title') or ''),
            'desc': str(archive.get('desc') or ''),
            'dynamic': str(archive.get('dynamic') or ''),
            'tag': str(archive.get('tag') or ''),
            'tid': int(archive.get('tid') or 21),
            'copyright': int(archive.get('copyright') or 1),
            'source': str(archive.get('source') or ''),
            'cover': str(archive.get('cover') or ''),
            'scheduled_at': dtime if dtime > time.time() else 0,
            'is_only_self': bool(archive.get('is_only_self')),
            'open_subtitle': bool(subtitle.get('allow')),
            'dolby': int(attrs.get('is_dolby') or 0),
            'no_reprint': int(archive.get('no_reprint') or 0),
            'charging_pay': int(archive.get('charging_pay') or 0),
            'season_id': membership.get('season_id'),
            'season_title': str(membership.get('season_title') or ''),
            'section_title': str(membership.get('section_title') or ''),
            'episode_title': str(membership.get('episode_title') or archive.get('title') or ''),
        }

    def get_upload_defaults(self):
        defaults = {
            'engine': 'biliwebapi', 'account': 'bilibili', 'retry': 3, 'timeout': 0,
            'limit': 3, 'line': None, 'task_upload_lock': True, 'realtime': False,
            'concat_video': False, 'copyright': 1, 'source': '', 'tid': 21, 'title': '', 'desc': '',
            'dynamic': '', 'tag': '直播回放,热点剪辑', 'open_subtitle': False,
            'dolby': 0, 'hires': 0, 'no_reprint': 0, 'is_only_self': 0,
            'charging_pay': 0, 'no_disturbance': 0, 'season_id': None,
            'section_title': '', 'episode_title': '', 'extra_kwargs': {},
        }
        if self.engine:
            info = next((item for item in self.engine.task_dict.values()
                         if item.get('task_type') == 'highlight'), None)
            common = (((getattr((info or {}).get('class'), 'config', {}) or {}).get('defaults') or {})
                      .get('upload') or {}).get('common') or {}
            defaults = merge_dict(defaults, deepcopy(common))
        defaults.pop('cookies', None)
        defaults['engine'] = 'biliwebapi'
        return defaults

    @staticmethod
    def _normalize_cover(source_path, output_path):
        with Image.open(source_path) as image:
            image.load()
            if image.width * image.height > 40_000_000:
                raise ValueError('图片像素总量不能超过 4000 万。')
            if image.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('仅支持 JPEG、PNG 或 WebP。')
            image = ImageOps.exif_transpose(image).convert('RGB')
            image = ImageOps.fit(image, (1280, 800), method=Image.Resampling.LANCZOS)
            image.save(output_path, 'JPEG', quality=92, optimize=True)
        return output_path

    def _register_cover_token(self, token, path, kind, metadata=None):
        real_path = os.path.realpath(path)
        if os.path.commonpath((self.cover_artifact_dir, real_path)) != self.cover_artifact_dir:
            raise ValueError('封面文件不在受管理目录中。')
        with self.cover_token_lock:
            self.cover_tokens[token] = {
                'path': real_path, 'kind': kind, 'created_at': time.time(), **(metadata or {}),
            }

    def _resolve_cover_token(self, token, kind=None):
        if not token:
            return None
        with self.cover_token_lock:
            record = deepcopy(self.cover_tokens.get(str(token)))
        if not record or (kind and record.get('kind') != kind):
            return None
        if time.time() - float(record.get('created_at', 0)) > 24 * 3600:
            return None
        real_path = os.path.realpath(record.get('path') or '')
        try:
            managed = os.path.commonpath((self.cover_artifact_dir, real_path)) == self.cover_artifact_dir
        except ValueError:
            managed = False
        return record if managed and os.path.isfile(real_path) else None

    def _highlight_source_text(self, manifest, manifest_path):
        """读取热点清单对应的主播名称和已有字幕，不触发新的转写。"""
        streamer_name = str(manifest.get('streamer_name') or '').strip()
        source_segments = list(manifest.get('source_segments') or [])
        if self.engine and (not source_segments or not streamer_name):
            target = os.path.realpath(manifest_path or '')
            for info in self.engine.task_dict.values():
                if info.get('task_type') != 'highlight':
                    continue
                job = next((value for value in getattr(info.get('class'), 'jobs', {}).values()
                            if os.path.realpath(value.get('manifest') or '') == target), None)
                if not job:
                    continue
                if not source_segments:
                    offset = 0.0
                    for segment in sorted(job.get('segments') or [],
                                          key=lambda value: value.get('segment_id', 0)):
                        video = segment.get('video') or segment.get('source_video')
                        duration = float(getattr(video, 'duration', 0) or 0)
                        if duration <= 0 and getattr(video, 'path', None):
                            duration = float(FFprobe.get_duration(video.path) or 0)
                        source_segments.append({
                            'offset': offset, 'duration': duration,
                            'subtitle': segment.get('subtitle'),
                        })
                        offset += duration
                if not streamer_name:
                    for segment in job.get('segments') or []:
                        video = segment.get('video') or segment.get('source_video')
                        streamer = getattr(video, 'streamer', None)
                        streamer_name = str(
                            (streamer.get('name') if isinstance(streamer, dict)
                             else getattr(streamer, 'name', None)) or ''
                        ).strip()
                        if streamer_name:
                            break
                break
        streamer_name = streamer_name or str(manifest.get('source_task') or '').strip()
        subtitles = []
        from DMR.Highlight.analyzer import parse_srt
        for segment in source_segments:
            subtitle = segment.get('subtitle')
            if subtitle and not isinstance(subtitle, str):
                subtitle = getattr(subtitle, 'path', None)
            try:
                subtitles.extend(parse_srt(subtitle, float(segment.get('offset') or 0)))
            except (TypeError, ValueError, OSError):
                continue
        return streamer_name, subtitles

    def _cover_hotspot_context(self, paths, reference_path=None, max_chars=3500):
        selected = {os.path.realpath(path): index for index, path in enumerate(paths)}
        contexts = []
        for result in self.get_highlight_results():
            try:
                manifest = self._read_highlight_manifest(result.get('manifest'))
            except Exception:
                continue
            streamer_name, subtitles = self._highlight_source_text(manifest, result.get('manifest'))
            clips = {str(item.get('id')): item for item in manifest.get('clips') or []}
            candidates = {str(item.get('id')): item
                          for item in (manifest.get('analysis') or {}).get('candidates') or []}
            source_sets = []
            for clip_id, clip in clips.items():
                source_sets.append((clip.get('path') or clip.get('export_path'), [clip_id]))
            initial_ids = [str(value) for value in (manifest.get('initial_mix') or {}).get('clip_ids') or []]
            for output in manifest.get('outputs') or []:
                source_sets.append((output.get('path'), initial_ids))
            for version in manifest.get('versions') or []:
                source_sets.append((version.get('path'), [str(value) for value in version.get('clip_ids') or []]))
            for media_path, clip_ids in source_sets:
                real_media = os.path.realpath(media_path or '')
                if real_media not in selected:
                    continue
                for clip_id in clip_ids:
                    clip, candidate = clips.get(clip_id, {}), candidates.get(clip_id, {})
                    representative = candidate.get('representative') or candidate.get('representative_texts') or []
                    kept_ranges = clip.get('ranges') or [{
                        'start': float(clip.get('requested_start') or candidate.get('start') or 0),
                        'end': float(clip.get('requested_end') or candidate.get('end') or 0),
                    }]
                    candidate_start = float(kept_ranges[0]['start'])
                    candidate_end = float(kept_ranges[-1]['end'])
                    representative = [item for item in representative if not isinstance(item, dict) or any(
                        float(value['start']) <= float(item.get('time', value['start'])) <= float(value['end'])
                        for value in kept_ranges
                    )]
                    subtitle_pool = candidate.get('subtitle_excerpt') or subtitles
                    nearby_subtitles = [line for line in subtitle_pool if any(
                        float(line.get('end', 0)) > float(value['start']) - 6
                        and float(line.get('start', 0)) < float(value['end']) + 6
                        for value in kept_ranges
                    )][:20]
                    contexts.append({
                        'priority': 0 if reference_path and real_media == os.path.realpath(reference_path) else 1,
                        'order': selected[real_media], 'id': clip_id,
                        'streamer_name': streamer_name,
                        'title': clip.get('title') or candidate.get('ai_title') or '',
                        'category': clip.get('ai_category') or clip.get('category') or candidate.get('category') or '',
                        'confidence': clip.get('ai_confidence'),
                        'reason': clip.get('ai_reason') or candidate.get('ai_reason') or '',
                        'start': candidate_start, 'end': candidate_end,
                        'representative': representative[:8],
                        'subtitles': nearby_subtitles[:12],
                    })
        contexts.sort(key=lambda item: (item['priority'], item['order']))
        lines, seen = [], set()
        for item in contexts:
            unique = (item['order'], item['id'])
            if unique in seen:
                continue
            seen.add(unique)
            subtitle_text = ' / '.join(
                f"[{float(value.get('start', 0)):.1f}s] {str(value.get('text') or '').strip()[:120]}"
                for value in item['subtitles'] if str(value.get('text') or '').strip()
            )[:1000]
            line = (
                f"主播：{item['streamer_name'] or '未知'}；热点 {item['id']}；"
                f"标题：{item['title'] or '未命名'}；类别：{item['category'] or '未知'}；"
                f"时间：{item['start']}-{item['end']}秒；置信度：{item['confidence']}；"
                f"理由：{item['reason'] or '无'}；代表弹幕：{'、'.join(map(str, item['representative'])) or '无'}；"
                f"附近字幕正文：{subtitle_text or '无'}"
            )
            if sum(len(value) + 1 for value in lines) + len(line) > max_chars:
                break
            lines.append(line)
        return '\n'.join(lines)

    def _run_cover_prompt_generation(self, job_id, paths, title, current_desc,
                                     current_dynamic, custom_prompt, reference):
        def update(**values):
            with self.cover_job_lock:
                if job_id in self.cover_jobs:
                    self.cover_jobs[job_id].update(values)
        try:
            update(status='analyzing')
            reference_path = reference.get('source_path') if reference else None
            hotspot = self._cover_hotspot_context(paths, reference_path=reference_path)
            material_names = '、'.join(os.path.splitext(os.path.basename(path))[0] for path in paths[:12])
            base_prompt = (
                '请为这次B站热点直播切片投稿，一次性生成标题、简介、动态文案和16:10中文封面的完整生图提示词。\n'
                f'当前投稿标题：{title or "未填写"}\n当前简介：{current_desc or "未填写"}\n'
                f'当前动态：{current_dynamic or "未填写"}\n'
                f'视频素材：{material_names}\n热点资料：\n{hotspot or "没有可用的热点资料"}\n'
                f'用户补充要求：{custom_prompt or "无"}\n'
                '生成风格要求：\n'
                '1. 标题像真实热门直播切片，优先抓住一个最有传播力的具体事件、反转、反应或金句；建议18至32个汉字。'
                '已提供主播名称时自然带出主播名；附近字幕中若有语出惊人、反差强烈或能独立成立的原话，可优先提炼为亮点。'
                '引用台词必须忠于字幕正文，不得补写字幕中没有的话。可使用口语化悬念，但不要标题党，'
                '不要编造主播、游戏、人物、结果或台词。\n'
                '2. 禁止“高能混剪、精彩瞬间、不容错过、震撼来袭、全程高能、笑不活了”等空泛套话；'
                '不要堆叠感叹号、书名号、标签和关键词，不要把素材文件名机械拼进标题。\n'
                '3. 简介只写2至4个短句：一句交代发生了什么，一句点出最好看的看点；有多段时可再概括一行。'
                '不要写运营分析、创作说明、免责声明或冗长背景。\n'
                '4. 动态只写一句自然口语，直接抛出最大看点，引导点开即可；不要重复整段简介，不要自动添加话题标签。\n'
                '5. 标题不超过80字，简介不超过2000字，动态不超过233字。信息不足时宁可保守概括，不得虚构。\n'
                '6. 封面提示词必须具体描述主体、动作、场景、表情、构图、光色和文字排版。封面大字控制在4至10个汉字，'
                '可从真实字幕金句或热点反应中提炼，不要直接照搬投稿标题；画面醒目清晰、主体突出，避免元素堆砌。'
                '不要使用真实平台Logo、二维码或虚构真人脸。'
            )
            if reference:
                base_prompt += '\n生图时会附带视频参考帧，请参考其内容、构图和场景氛围，但不要直接复刻。'
            content = self.ai_client.chat('cover_analysis', [
                {'role': 'system', 'content': (
                    '你是熟悉中文直播热点切片的短视频编辑。写法要具体、短、像人写的，'
                    '核心是准确提炼这段直播为什么值得点开，不使用模板化运营套话。'
                    '用户补充要求优先，但不能据此虚构素材中没有的事实。只返回一个JSON对象，不要Markdown。格式必须是：'
                    '{"title":"投稿标题","desc":"投稿简介","dynamic":"动态文案",'
                    '"cover_prompt":"可直接用于AI生图的完整提示词"}'
                )},
                {'role': 'user', 'content': base_prompt},
            ]).strip()
            content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content, flags=re.I | re.S).strip()
            try:
                generated = json.loads(content)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', content, re.S)
                generated = json.loads(match.group(0)) if match else None
            if not isinstance(generated, dict) or not str(generated.get('cover_prompt') or '').strip():
                raise ValueError('AI未返回有效的标题、简介、动态和封面提示词JSON。')
            metadata = {
                'title': str(generated.get('title') or title).strip()[:80],
                'desc': str(generated.get('desc') or current_desc).strip()[:2000],
                'dynamic': str(generated.get('dynamic') or current_dynamic).strip()[:233],
            }
            final_prompt = str(generated['cover_prompt']).strip()[:8000]
            update(status='completed', prompt=final_prompt, metadata=metadata)
        except Exception as error:
            self.logger.exception('上传中心AI封面提示词生成失败')
            update(status='failed', error=str(error))

    def _run_cover_image_generation(self, job_id, confirmed_prompt, reference):
        def update(**values):
            with self.cover_job_lock:
                if job_id in self.cover_jobs:
                    self.cover_jobs[job_id].update(values)
        try:
            update(status='generating')
            b64_image = self.ai_client.generate_image(
                confirmed_prompt, '1536x1024', reference_image=reference.get('path') if reference else None,
            )
            if b64_image.lstrip().startswith('data:'):
                b64_image = b64_image.split(',', 1)[1]
            token = secrets.token_urlsafe(24)
            raw_path = os.path.join(self.cover_artifact_dir, f'generated_{token}.png')
            output_path = os.path.join(self.cover_artifact_dir, f'generated_{token}.jpg')
            try:
                with open(raw_path, 'wb') as file:
                    file.write(base64.b64decode(b64_image))
                self._normalize_cover(raw_path, output_path)
            finally:
                if os.path.isfile(raw_path):
                    os.remove(raw_path)
            self._register_cover_token(token, output_path, 'generated')
            update(status='completed', token=token, prompt=confirmed_prompt)
        except Exception as error:
            self.logger.exception('上传中心AI封面生成失败')
            update(status='failed', error=str(error))

    def get_known_seasons(self):
        seasons = set()
        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'season_id' and item:
                        try:
                            seasons.add(int(item))
                        except (TypeError, ValueError):
                            pass
                    else:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
        if self.engine:
            for info in self.engine.task_dict.values():
                walk(getattr(info.get('class'), 'config', {}))
        return [{'id': value, 'name': f'合集 {value}'} for value in sorted(seasons)]

    def get_upload_library(self, refresh=False):
        with self.upload_library_lock:
            if (not refresh and self.upload_library_cache['time'] and
                    time.monotonic() - self.upload_library_cache['time'] < 15):
                return list(self.upload_library_cache['items'])
            items = self._build_upload_library()
            self.upload_library_cache = {'time': time.monotonic(), 'items': items}
            return list(items)

    def _build_upload_library(self):
        items, seen = [], set()
        def add(path, kind, taskname, group, title, duration=0, created_at=None):
            path = getattr(path, 'path', path)
            if not path or not os.path.isfile(path):
                return
            real = os.path.realpath(path)
            if real in seen:
                return
            seen.add(real)
            stat = os.stat(real)
            default_part_title = (title if str(kind).startswith('highlight_') and title
                                  else os.path.splitext(os.path.basename(real))[0])
            items.append({'path': real, 'kind': kind, 'taskname': taskname, 'group': group,
                          'title': title or os.path.basename(real),
                          'part_title': str(default_part_title)[:80],
                          'duration': max(0, float(duration or 0)), 'size': stat.st_size,
                          'modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                          'modified_ts': stat.st_mtime,
                          'created_at': created_at or datetime.fromtimestamp(stat.st_mtime).isoformat()})
        if self.engine:
            for task_key, info in self.engine.task_dict.items():
                if info.get('task_type', 'replay') != 'replay':
                    continue
                taskname = info.get('name', task_key.split('/', 1)[-1])
                event = getattr(info.get('class'), 'event_class', None)
                for session_id, session in getattr(event, 'completed_sessions', {}).items():
                    group = f'{taskname} · {session.get("started_at") or session_id}'
                    for state in session.get('video_states', []):
                        for dtype, label in (('src_video', '原始录屏'), ('src_video_pre', '转码前录屏'),
                                             ('dm_video', '弹幕版录屏')):
                            video = (state.get(dtype) or {}).get('file')
                            add(video, dtype, taskname, group,
                                f'{label} · {os.path.basename(getattr(video, "path", ""))}',
                                getattr(video, 'duration', 0))
            for result in self.get_highlight_results():
                taskname = result.get('source_task') or result.get('taskname') or '热点任务'
                group = f'{"自动" if result.get("run_type") == "automatic" else "手动"}热点 · {result.get("display_name")}'
                for output in result.get('outputs') or []:
                    add(output.get('path'), 'highlight_mix', taskname, group, '热点混剪成片',
                        output.get('duration'), result.get('created_at'))
                for clip in result.get('clips') or []:
                    title = clip.get('title') or f'热点小片段 {clip.get("sequence") or clip.get("id")}'
                    if clip.get('storage') == 'virtual':
                        token = self._encode_virtual_material(result.get('manifest'), clip)
                        timestamp = os.path.getmtime(result.get('manifest'))
                        items.append({'path': token, 'kind': 'highlight_clip', 'taskname': taskname,
                                      'group': group, 'title': title, 'part_title': str(title)[:80],
                                      'duration': max(0, float(clip.get('duration') or 0)), 'size': 0,
                                      'modified_at': datetime.fromtimestamp(timestamp).isoformat(),
                                      'modified_ts': timestamp, 'created_at': result.get('created_at'),
                                      'virtual': True})
                    else:
                        add(clip.get('path'), 'highlight_clip', taskname, group, title,
                            clip.get('duration'), result.get('created_at'))
                for version in result.get('versions') or []:
                    add(version.get('path'), 'highlight_version', taskname, group,
                        f'自定义混剪 {version.get("name") or version.get("id")}',
                        version.get('duration'), version.get('created_at'))
        items.sort(key=lambda item: item.get('modified_ts', 0), reverse=True)
        return items

    def query_upload_library(self, page=1, page_size=24, taskname='', kind='*', query='', sort='modified_desc'):
        try:
            page, page_size = int(page), int(page_size)
        except (TypeError, ValueError):
            raise ValueError('分页参数无效。')
        if page < 1 or not 1 <= page_size <= 100:
            raise ValueError('页码必须大于0，每页数量必须在1到100之间。')
        sorters = {
            'modified_desc': (lambda item: item.get('modified_ts', 0), True),
            'modified_asc': (lambda item: item.get('modified_ts', 0), False),
            'name_asc': (lambda item: (item.get('title') or '').casefold(), False),
            'name_desc': (lambda item: (item.get('title') or '').casefold(), True),
        }
        if sort not in sorters:
            raise ValueError('排序方式无效。')
        records = self.get_upload_library()
        task_counts = {}
        for item in records:
            name = item.get('taskname') or '未归属任务'
            task_counts[name] = task_counts.get(name, 0) + 1
        taskname = str(taskname or '').strip()
        scoped = [item for item in records if not taskname or item.get('taskname') == taskname]
        kind_counts = {}
        for item in scoped:
            item_kind = item.get('kind') or 'unknown'
            kind_counts[item_kind] = kind_counts.get(item_kind, 0) + 1
        kind = str(kind or '*')
        if kind != '*':
            scoped = [item for item in scoped if item.get('kind') == kind]
        query = str(query or '').strip().casefold()
        if query:
            scoped = [item for item in scoped if query in ' '.join((
                str(item.get('title') or ''), str(item.get('group') or ''),
                str(item.get('taskname') or ''), os.path.basename(item.get('path') or ''),
            )).casefold()]
        key, reverse = sorters[sort]
        scoped.sort(key=key, reverse=reverse)
        total = len(scoped)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        start = (page - 1) * page_size
        return {
            'items': scoped[start:start + page_size], 'page': page, 'page_size': page_size,
            'total': total, 'pages': pages,
            'tasknames': [{'name': name, 'count': count} for name, count in sorted(task_counts.items())],
            'kinds': [{'kind': name, 'count': count} for name, count in sorted(kind_counts.items())],
        }

    def _upload_media_paths(self, refresh=True):
        return {os.path.realpath(item['path']) for item in self.get_upload_library(refresh=refresh)}

    def get_replay_task_names(self):
        if not self.engine:
            return []
        return sorted(
            info.get('name', key.split('/', 1)[-1])
            for key, info in self.engine.task_dict.items()
            if info.get('task_type', 'replay') == 'replay'
        )

    def get_pipeline_states(self):
        pipeline_states = []
        if not self.engine:
            return pipeline_states

        type_labels = {
            'src_video': '原始视频',
            'src_video_pre': '转码前视频',
            'dm_video': '弹幕版视频',
            'subtitle': '语音字幕',
        }
        status_labels = {
            None: '未生成',
            'ready': '已就绪',
            'rendering': '渲染中',
            'uploading': '上传中',
            'uploaded': '已上传',
            'cleaned': '已清理',
        }

        try:
            tasks = list(self.engine.task_dict.items())
            for task_key, task_info in tasks:
                if task_info.get('task_type', 'replay') == 'highlight':
                    highlight_task = task_info.get('class')
                    for job_id, job in list(getattr(highlight_task, 'jobs', {}).items()):
                        if job.get('status') in ('completed', 'failed'):
                            continue
                        pipeline_states.append({
                            'taskname': task_info.get('name', task_key.split('/', 1)[-1]),
                            'task_type': 'highlight', 'group_id': job.get('group_id', job_id),
                            'recovery_id': job_id,
                            'ended': job.get('status') in ('completed', 'failed'),
                            'recovered': job_id in getattr(highlight_task, 'recovered_job_ids', set()),
                            'can_resume': job_id in getattr(highlight_task, 'recovered_job_ids', set()) and job.get('status') not in ('completed', 'failed'),
                            'active_count': (0 if job_id in getattr(highlight_task, 'recovered_job_ids', set())
                                             else 1 if job.get('status') in ('preparing', 'waiting_dependencies', 'analyzing', 'rendering', 'retry_wait', 'output_ready', 'uploading', 'cleaning') else 0),
                            'ready_count': len(job.get('outputs') or []),
                            'segment_count': len(job.get('segments') or []),
                            'can_delete': job_id in getattr(highlight_task, 'recovered_job_ids', set()),
                            'summary': (f'待恢复（原状态: {job.get("status")}）'
                                        if job_id in getattr(highlight_task, 'recovered_job_ids', set())
                                        else job.get('status')),
                            'stages': [{'type': '热点混剪', 'status': job.get('status'),
                                        'file': os.path.basename(job.get('manifest') or ''), 'waiting': 0}],
                        })
                    continue
                taskname = task_info.get('name', task_key.split('/', 1)[-1])
                replay_task = task_info.get('class')
                event_class = getattr(replay_task, 'event_class', None)
                if not event_class:
                    continue

                ended_groups = set(event_class.ended_dict)
                recovered_groups = set(getattr(event_class, 'recovered_group_ids', set()))
                for group_id, video_states in list(event_class.state_dict.items()):
                    stages = []
                    active_count = 0
                    ready_count = 0
                    for video_state in list(video_states):
                        for video_type, info in video_state.items():
                            status = info.get('status')
                            if status is None and info.get('file') is None:
                                continue
                            if status in ('rendering', 'uploading') or info.get('wait'):
                                active_count += 1
                            if status == 'ready':
                                ready_count += 1
                            video = info.get('file')
                            path = getattr(video, 'path', '') if video else ''
                            stages.append({
                                'type': type_labels.get(video_type, video_type),
                                'status': status_labels.get(status, status or '未知'),
                                'file': os.path.basename(path) if path else '',
                                'waiting': len(info.get('wait', [])),
                            })

                    ended = group_id in ended_groups
                    if active_count:
                        summary = '处理中'
                    elif ended and ready_count:
                        summary = '已加载，等待后续处理'
                    elif ended:
                        summary = '已结束'
                    else:
                        summary = '直播进行中'
                    pipeline_states.append({
                        'taskname': taskname,
                        'task_type': 'replay',
                        'group_id': group_id,
                        'recovery_id': group_id,
                        'segment_count': len(video_states),
                        'ended': ended,
                        'recovered': group_id in recovered_groups,
                        'can_resume': group_id in recovered_groups,
                        'can_delete': group_id in recovered_groups and active_count == 0,
                        'summary': summary,
                        'stages': stages,
                    })
        except RuntimeError:
            self.logger.debug('流水线状态正在更新，本次 WebUI 刷新跳过。')

        return pipeline_states

    def get_restart_status(self):
        if not self.runtime_controller:
            return {
                'available': False,
                'pending': False,
                'mode': 'idle',
                'force': False,
                'idle': False,
                'reason': '',
                'requested_at': None,
                'blocking': ['运行时控制器不可用'],
            }

        status = self.runtime_controller.get_restart_status()
        status['available'] = True
        return status

    def start_helper(self):
        self.webapp = self.create_app()
        self.webserver = make_server(self.host, self.port, self.webapp, threaded=True)
        self.webserver.serve_forever()

    def start(self):
        self.webapp_thread = threading.Thread(target=self.start_helper, daemon=True)
        self.webapp_thread.start()
        self.logger.info(f'DanmakuRender 5 started at http://{self.host}:{self.port}')

    def stop(self):
        self.stoped = True
        self.cover_executor.shutdown(wait=False, cancel_futures=True)
        if self.webserver:
            self.webserver.shutdown()
            self.webserver.server_close()
        if self.webapp_thread and self.webapp_thread.is_alive():
            self.webapp_thread.join(timeout=5)
