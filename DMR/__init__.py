import logging
import os
import shutil
import threading
import json
import time
import logging

from .engine import DMREngine
from .Config import Config
from .utils import filename_to_taskname


class DanmakuRender():
    def __init__(self, config:Config, **kwargs) -> None:
        self.logger = logging.getLogger('DMR')
        self.config = config
        self.kwargs = kwargs
        self.stoped = True
        self.engine_args = self.config.get_config('dmr_engine_args')
        self.engine = DMREngine()
        self._restart_lock = threading.Lock()
        self._restart_requested = False
        self._restart_mode = 'idle'
        self._restart_reason = ''
        self._restart_requested_at = None

    def start(self):
        self.stoped = False
        os.makedirs('.temp', exist_ok=True)
        
        self.logger.debug(f'Global Config:\n{json.dumps(self._redact_config(self.config.global_config), indent=4, ensure_ascii=False)}')
        self.logger.debug(f'Replay Config:\n{json.dumps(self._redact_config(self.config.replay_config), indent=4, ensure_ascii=False)}')
        self.engine.start()
        plugin_enabled = self.config.get_config('dmr_engine_args')['enabled_plugins']
        for plugin_name in plugin_enabled:
            plugin_config = dict(self.config.get_config(plugin_name+'_kernel_args') or {})
            if plugin_name == 'webservice':
                plugin_config['runtime_controller'] = self
            self.engine.add_plugin(plugin_name, plugin_config)

        for taskname in self.config.get_replaytasks():
            replay_config = self.config.get_replay_config(taskname)
            if replay_config.get('common_event_args', {}).get('auto_transcribe') and 'transcriber' not in plugin_enabled:
                self.logger.error(f'任务 {taskname} 已启用 auto_transcribe，但 dmr_engine_args.enabled_plugins 未启用 transcriber 插件。')
            self.engine.add_task(taskname, replay_config)
        for taskname in self.config.get_highlighttasks():
            self.engine.add_task(taskname, self.config.get_highlight_config(taskname), 'highlight')

        threading.Thread(target=self._monintor, daemon=True).start()

    @classmethod
    def _redact_config(cls, value, key=''):
        sensitive = {'api_key', 'cookies', 'cookie', 'password', 'token', 'access_token', 'refresh_token'}
        if key.lower() in sensitive and value not in (None, ''):
            return '***REDACTED***'
        if isinstance(value, dict):
            return {item_key: cls._redact_config(item, item_key) for item_key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact_config(item, key) for item in value]
        return value

    def check_config_update(self):
        try:
            update_type, update_info = self.config.check_update()
            
            if update_type == 'global':
                self.logger.info('检测到全局配置更新，请重启程序以生效。')
            
            elif update_type == 'tasks':
                for config_path in sorted(update_info['new'], key=lambda path: os.path.basename(path).startswith('DMH-')):
                    taskname = filename_to_taskname(config_path)
                    self.logger.info(f'检测到新任务配置文件: {taskname}，正在添加任务...')
                    task_type = 'highlight' if os.path.basename(config_path).startswith('DMH-') else 'replay'
                    getter = self.config.get_highlight_config if task_type == 'highlight' else self.config.get_replay_config
                    self.engine.add_task(taskname, getter(taskname), task_type)
                
                for config_path in update_info['deleted']:
                    taskname = filename_to_taskname(config_path)
                    self.logger.info(f'检测到任务配置文件删除: {taskname}，正在停止任务...')
                    task_type = 'highlight' if os.path.basename(config_path).startswith('DMH-') else 'replay'
                    self.engine.del_task(taskname, task_type)

                for config_path in update_info['updated']:
                    taskname = filename_to_taskname(config_path)
                    self.logger.info(f'检测到任务配置文件更新: {taskname}，正在重启任务...')
                    
                    task_type = 'highlight' if os.path.basename(config_path).startswith('DMH-') else 'replay'
                    self.engine.del_task(taskname, task_type)
                    time.sleep(5)
                    new_taskname = filename_to_taskname(config_path)
                    getter = self.config.get_highlight_config if task_type == 'highlight' else self.config.get_replay_config
                    self.engine.add_task(new_taskname, getter(new_taskname), task_type)

        except Exception as e:
            self.logger.error(f'动态载入配置文件错误:')
            self.logger.exception(e)

    def _monintor(self):
        REFRESH_INTERVAL = 60
        time.sleep(REFRESH_INTERVAL)
        while not self.stoped:
            self.check_config_update()
            # clean temp file
            files = os.listdir('.temp')
            for file in files:
                try:
                    basename = os.path.splitext(os.path.basename(file))[0]
                    expired_time = basename.split('_')[-1]
                    if expired_time.isdigit():
                        expired_time = int(expired_time)
                    else:
                        expired_time = 0
                    # 只清理2024.01.01之后的过期文件，过早的文件认为不是程序创建的不清理
                    if expired_time > 1704038400 and expired_time < int(time.time()):
                        file = os.path.join('.temp', file)
                        if os.path.isfile(file):
                            os.remove(file)
                            self.logger.debug(f'已清理临时文件: {file}')
                        elif os.path.isdir(file):
                            shutil.rmtree(file)
                            self.logger.debug(f'已清理临时文件夹: {file}')
                except Exception as e:
                    self.logger.debug(f'清理临时文件{file}失败: {e}')
            
            time.sleep(REFRESH_INTERVAL)

    def stop(self):
        self.stoped = True
        self.engine.stop()

    def request_restart(self, reason='webui', mode='idle'):
        if mode not in ('idle', 'force'):
            mode = 'idle'
        with self._restart_lock:
            self._restart_requested = True
            self._restart_mode = mode
            self._restart_reason = reason or 'webui'
            self._restart_requested_at = time.time()
        if mode == 'force':
            self.logger.warning(f'已请求强制重启: {self._restart_reason}')
        else:
            self.logger.info(f'已请求空闲后重启: {self._restart_reason}')

    def cancel_restart(self):
        with self._restart_lock:
            was_requested = self._restart_requested
            self._restart_requested = False
            self._restart_mode = 'idle'
            self._restart_reason = ''
            self._restart_requested_at = None
        if was_requested:
            self.logger.info('已取消空闲后重启请求。')
        return was_requested

    def is_restart_requested(self):
        with self._restart_lock:
            return self._restart_requested

    def should_restart_now(self):
        status = self.get_restart_status()
        return status['pending'] and (status['mode'] == 'force' or status['idle'])

    def get_restart_status(self):
        with self._restart_lock:
            pending = self._restart_requested
            mode = self._restart_mode
            reason = self._restart_reason
            requested_at = self._restart_requested_at

        idle, blocking = self._get_idle_status()
        return {
            'pending': pending,
            'mode': mode,
            'force': mode == 'force',
            'idle': idle,
            'reason': reason,
            'requested_at': requested_at,
            'blocking': blocking,
        }

    def _get_idle_status(self):
        blocking = []

        downloader_info = self.engine.plugin_dict.get('downloader')
        if downloader_info:
            downloader = downloader_info.get('class')
            for taskname, task in getattr(downloader, 'download_tasks', {}).items():
                if self._is_download_task_active(task):
                    blocking.append(f'录制任务 {taskname}')

        render_info = self.engine.plugin_dict.get('render')
        if render_info:
            render_tasks = getattr(render_info.get('class'), 'render_tasks', {})
            if render_tasks:
                blocking.append(f'渲染任务 {len(render_tasks)} 个')

        transcriber_info = self.engine.plugin_dict.get('transcriber')
        if transcriber_info:
            transcribe_tasks = getattr(transcriber_info.get('class'), 'transcribe_tasks', {})
            if transcribe_tasks:
                blocking.append(f'转录任务 {len(transcribe_tasks)} 个')

        uploader_info = self.engine.plugin_dict.get('uploader')
        if uploader_info:
            upload_tasks = getattr(uploader_info.get('class'), 'upload_tasks', {})
            if upload_tasks:
                blocking.append(f'上传任务 {len(upload_tasks)} 个')

        cleaner_info = self.engine.plugin_dict.get('cleaner')
        if cleaner_info:
            clean_tasks = getattr(cleaner_info.get('class'), 'clean_tasks', {})
            active_clean_tasks = [
                task for task in clean_tasks.values()
                if task.get('status') == 'cleaning'
            ]
            if active_clean_tasks:
                blocking.append(f'清理任务 {len(active_clean_tasks)} 个')

        active_highlight_statuses = {
            'preparing', 'waiting_dependencies', 'analyzing', 'rendering',
            'retry_wait', 'output_ready', 'uploading', 'cleaning',
        }
        for task_info in self.engine.task_dict.values():
            if task_info.get('task_type') != 'highlight':
                continue
            jobs = getattr(task_info.get('class'), 'jobs', {})
            active_jobs = [job for job in jobs.values() if job.get('status') in active_highlight_statuses]
            if active_jobs:
                blocking.append(f'热点剪辑任务 {len(active_jobs)} 个')

        return len(blocking) == 0, blocking

    @staticmethod
    def _is_download_task_active(task):
        if getattr(task, 'onair', False):
            return True

        if hasattr(task, 'downloader') and task.downloader is not None:
            downloader = task.downloader
            for proc_name in ('ffmpeg_proc', 'streamlink_proc', 'streamgears_proc', 'ytdl_proc', 'yutto_proc'):
                proc = getattr(downloader, proc_name, None)
                if proc is not None and getattr(proc, 'poll', lambda: None)() is None:
                    return True

        if task.__class__.__name__ in ('StreamDownloadTask', 'SyncStreamDownloadTask'):
            return not getattr(task, 'stoped', True)

        return False
