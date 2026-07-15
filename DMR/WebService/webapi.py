import logging
import threading
import queue
import secrets
import os
import yaml
import glob
from flask import Flask, request, render_template, redirect, url_for, flash, session
from functools import wraps
from datetime import datetime
from werkzeug.serving import make_server

from DMR.utils import *

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
                group_id = item.get('group_id')
                task_info = (self.engine.task_dict.get(f'replay/{taskname}') or
                             self.engine.task_dict.get(taskname)) if self.engine else None
                event_class = getattr(task_info.get('class'), 'event_class', None) if task_info else None
                if not event_class or not group_id:
                    skipped.append({'taskname': taskname, 'group_id': group_id, 'reason': '记录不存在。'})
                    continue
                success, reason = event_class.delete_recovered_pipeline_state(group_id)
                result = {'taskname': taskname, 'group_id': group_id}
                if success:
                    deleted.append(result)
                else:
                    result['reason'] = reason
                    skipped.append(result)

            return {'status': 'success', 'deleted': deleted, 'skipped': skipped}

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
                        pipeline_states.append({
                            'taskname': task_info.get('name', task_key.split('/', 1)[-1]),
                            'task_type': 'highlight', 'group_id': job.get('group_id', job_id),
                            'ended': job.get('status') in ('completed', 'failed'),
                            'recovered': True, 'active_count': 1 if job.get('status') in ('analyzing', 'rendering', 'uploading') else 0,
                            'ready_count': len(job.get('outputs') or []),
                            'segment_count': len(job.get('segments') or []),
                            'can_delete': False, 'summary': job.get('status'),
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
                        'group_id': group_id,
                        'segment_count': len(video_states),
                        'ended': ended,
                        'recovered': group_id in recovered_groups,
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
