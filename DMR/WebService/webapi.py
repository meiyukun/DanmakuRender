import logging
import threading
import queue
import secrets
import os
import yaml
import glob
import json
import re
from flask import Flask, request, render_template, redirect, url_for, flash, session, send_file
from functools import wraps
from copy import deepcopy
from datetime import datetime
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
                version_id = f"custom-v{max(used_numbers, default=1) + 1}"
                extension = os.path.splitext(paths[0])[1] or '.mp4'
                output = safe_filename(os.path.join(os.path.dirname(manifest_path), f'{version_id}{extension}'))
                highlight_info = next((info for info in self.engine.task_dict.values()
                                       if info.get('task_type') == 'highlight'), None)
                highlight_task = (highlight_info or {}).get('class')
                job = next((item for item in getattr(highlight_task, 'jobs', {}).values()
                            if os.path.realpath(item.get('manifest') or '') == os.path.realpath(manifest_path)), None)
                config = deepcopy((job or {}).get('config', {}).get('encoding') or
                                  getattr(highlight_task, 'config', {}).get('defaults', {}).get('encoding', {}))
                from DMR.Highlight.cutter import recompose_clips, write_manifest
                recompose_clips(paths, output, config, self.logger)
                record = {'id': version_id, 'path': output, 'clip_ids': clip_ids,
                          'duration': sum(float(clip_map[value].get('duration', 0)) for value in clip_ids),
                          'uploaded': False, 'created_at': datetime.now().isoformat()}
                versions.append(record)
                write_manifest(manifest_path, manifest)
                return {'status': 'success', 'version': record}
            except Exception as error:
                self.logger.exception('热点片段重新拼接失败')
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
                path = os.path.realpath(item.get('path', ''))
                if path not in self._highlight_media_paths(manifest_path):
                    raise ValueError('文件不属于该热点任务。')
                if os.path.exists(path):
                    os.remove(path)
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
        if manifest.get('version') not in (1, 2):
            raise ValueError('热点清单版本不受支持。')
        return manifest

    def _highlight_media_paths(self, manifest_path):
        try:
            manifest = self._read_highlight_manifest(manifest_path)
        except Exception:
            return set()
        paths = []
        paths.extend(item.get('path') for item in manifest.get('clips') or [])
        paths.extend(item.get('path') for item in manifest.get('outputs') or [])
        paths.extend(item.get('path') for item in manifest.get('versions') or [])
        return {os.path.realpath(path) for path in paths if path}

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
                                'versions': manifest.get('versions') or []})
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
        if self.webserver:
            self.webserver.shutdown()
            self.webserver.server_close()
        if self.webapp_thread and self.webapp_thread.is_alive():
            self.webapp_thread.join(timeout=5)
