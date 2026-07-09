import json
from datetime import datetime
import logging
import os
import threading
import queue
from concurrent.futures import ThreadPoolExecutor
from os.path import join, exists
from typing import Tuple

from DMR.utils import (
    VideoInfo, DateTimeDecoder, atomic_json_dump, video_info_from_dict,
    uuid, PipeMessage,
)


class Render():
    def __init__(self,
                 pipe:Tuple[queue.Queue, queue.Queue],
                 nrenders:int=1,
                 **kwargs,
                 ) -> None:
        
        self.nrenders = int(nrenders)
        self.send_queue, self.recv_queue = pipe
        self.logger = logging.getLogger(__name__)
        self.kwargs = kwargs
        self.stoped = True

        self._piperecvprocess = None
        self.render_tasks = {}
        self.failed_tasks = {}
        self.failed_tasks_file = '.temp/failed_renders.json'
        self.active_tasks_file = '.temp/active_renders.json'
        self.load_failed_tasks()
        self.load_interrupted_tasks()

        self._render_class = {}
        self.render_executors = ThreadPoolExecutor(max_workers=self.nrenders)
        self._lock = threading.Lock()
        self._group_task_order = {}  # group_id -> [task_uuid, ...] 按提交顺序
        self._completed_tasks = {}  # task_uuid -> (status, desc) 已完成待发送
        self.last_retry_error = None

    def load_failed_tasks(self):
        if exists(self.failed_tasks_file):
            try:
                with open(self.failed_tasks_file, 'r', encoding='utf-8') as f:
                    data = json.load(f, cls=DateTimeDecoder)
                    for uuid, task in data.items():
                        if task.get('video'):
                            task['video'] = video_info_from_dict(task['video'])
                        if isinstance(task.get('completion_result'), dict):
                            task['completion_result'] = video_info_from_dict(task['completion_result'])
                        if task.get('status') != 'completion_pending':
                            task['status'] = 'failed'
                        # Update config as well
                            # if 'config' in task and 'video' in task['config']:
                            #     task['config']['video'] = task['video']
                        self.failed_tasks[uuid] = task
                self.logger.info(f'Loaded {len(self.failed_tasks)} failed render tasks.')
            except Exception as e:
                self.logger.error(f'Failed to load failed tasks: {e}')

    def save_failed_tasks(self):
        try:
            atomic_json_dump(self.failed_tasks, self.failed_tasks_file)
        except Exception as e:
            self.logger.error(f'Failed to save failed tasks: {e}')

    def load_interrupted_tasks(self):
        if not exists(self.active_tasks_file):
            return
        try:
            with open(self.active_tasks_file, 'r', encoding='utf-8') as f:
                data = json.load(f, cls=DateTimeDecoder)
            for task_uuid, task in data.items():
                if task.get('video'):
                    task['video'] = video_info_from_dict(task['video'])
                if isinstance(task.get('completion_result'), dict):
                    task['completion_result'] = video_info_from_dict(task['completion_result'])
                if task.get('status') == 'completion_pending':
                    task['failure_reason'] = '渲染已完成，流水线尚未确认；恢复时不会重复渲染'
                else:
                    task['status'] = 'interrupted'
                    task['failure_reason'] = '程序在渲染任务完成前停止'
                self.failed_tasks[task_uuid] = task
            self.save_failed_tasks()
            atomic_json_dump({}, self.active_tasks_file)
            self.logger.warning(f'Recovered {len(data)} interrupted render tasks.')
        except Exception as e:
            self.logger.error(f'Failed to load interrupted render tasks: {e}')

    def save_active_tasks(self):
        try:
            atomic_json_dump(self.render_tasks, self.active_tasks_file)
        except Exception as e:
            self.logger.error(f'Failed to save active render tasks: {e}')

    def retry_task(self, uuid):
        with self._lock:
            self.last_retry_error = None
            if uuid in self.failed_tasks:
                task = self.failed_tasks[uuid]
                if task.get('status') == 'completion_pending':
                    task = self.failed_tasks.pop(uuid)
                    self.render_tasks[task['uuid']] = task
                    self.save_active_tasks()
                    self.save_failed_tasks()
                    self._send_task_result(task, 'info', task.get('completion_result'))
                    return True
                video = task.get('video')
                if not video or not exists(video.path):
                    self.last_retry_error = f'输入视频不存在: {getattr(video, "path", None)}'
                    return False
                task = self.failed_tasks.pop(uuid)

                # Re-submit
                task['status'] = 'waiting'
                task.pop('failure_reason', None)
                self.render_tasks[task['uuid']] = task
                group_id = task.get('group_id')
                if group_id is not None:
                    self._group_task_order.setdefault(group_id, []).append(task['uuid'])
                self.save_active_tasks()
                self.save_failed_tasks()
                self.render_executors.submit(self._render_subprocess, task)
                return True
            return False

    def delete_failed_task(self, uuid):
        with self._lock:
            if uuid in self.failed_tasks:
                self.failed_tasks.pop(uuid)
                self.save_failed_tasks()
                return True
            return False

    def _pipeSend(self, event, msg, target='engine', dtype=None, data=None, **kwargs):
        if self.send_queue:
            msg = PipeMessage(
                source='render',
                target=target,
                event=event,
                msg=msg,
                dtype=dtype,
                data=data,
                **kwargs,
            )
            self.send_queue.put(msg)

    def _pipeRecvMonitor(self):
        while self.stoped == False and self.recv_queue is not None:
            message:PipeMessage = self.recv_queue.get()
            try:
                if message.target == 'render':
                   if message.event == 'newtask':
                       self.add_task(message)
                   elif message.event == 'ack':
                       self._ack_task(message.request_id)
            except Exception as e:
                self.logger.error(f'Message:{message} raise an error.')
                self.logger.exception(e)
    
    def start(self):
        self.stoped = False
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()

    def add_task(self, msg:PipeMessage):
        with self._lock:
            for task in list(self.render_tasks.values()) + list(self.failed_tasks.values()):
                if msg.request_id and task.get('request_id') == msg.request_id:
                    self._pipeSend(
                        'accepted', '渲染任务已存在', target=msg.source,
                        request_id=msg.request_id,
                    )
                    return
            config = msg.data
            source = msg.source
            request_id = msg.request_id
            task = {
                'uuid': uuid(),
                'source': source,
                'request_id': request_id,
                'mode': config.get('mode', 'dmrender'),
                'args': config.get('args', {}),
                'video': config.get('video'),
                'output': config.get('output'),
                # 'config': config,
                'status': 'waiting',
            }
            self.render_tasks[task['uuid']] = task
            group_id = video.group_id if (video := task.get('video')) else None
            task['group_id'] = group_id
            if group_id is not None:
                self._group_task_order.setdefault(group_id, []).append(task['uuid'])
            self.save_active_tasks()
            self._pipeSend(
                'accepted', '渲染任务已持久化', target=msg.source,
                request_id=msg.request_id,
            )
            self.render_executors.submit(self._render_subprocess, task)

    def _send_task_result(self, task, status, desc=''):
        """实际发送单个任务的结果"""
        if status == 'error':
            self.failed_tasks[task['uuid']] = task
            self.save_failed_tasks()

            self._pipeSend(
                event='error',
                msg=f"渲染视频{task['output']}时出现错误: {desc}",
                target=task['source'],
                request_id=task['request_id'],
                dtype=str(type(desc)),
                data=desc,
            )
        else:
            task['status'] = 'completion_pending'
            task['completion_result'] = desc
            self.save_active_tasks()
            self._pipeSend(
                event='end',
                msg=f"视频{task['output']}渲染完成",
                target=task['source'],
                request_id=task['request_id'],
                dtype='dict',
                data={
                    'output': desc,
                },
            )

    def _gather(self, task, status, desc=''):
        with self._lock:
            if status == 'error':
                task['status'] = 'failed'
                task['failure_reason'] = str(desc)
                self.failed_tasks[task['uuid']] = task
                self.save_failed_tasks()
            if status == 'error':
                self.render_tasks.pop(task['uuid'], None)
                self.save_active_tasks()
            group_id = task.get('group_id')

            # 没有 group_id 的任务直接发送
            if group_id is None or group_id not in self._group_task_order:
                self._send_task_result(task, status, desc)
                return

            # 缓存完成结果，按组内顺序依次发送
            self._completed_tasks[task['uuid']] = (task, status, desc)
            order = self._group_task_order[group_id]
            while order and order[0] in self._completed_tasks:
                t_uuid = order.pop(0)
                t, s, d = self._completed_tasks.pop(t_uuid)
                self._send_task_result(t, s, d)

            # 组内所有任务都已发送完毕，清理
            if not order:
                self._group_task_order.pop(group_id, None)

    def _ack_task(self, request_id):
        with self._lock:
            for task_uuid, task in list(self.render_tasks.items()):
                if task.get('request_id') == request_id and task.get('status') == 'completion_pending':
                    self.render_tasks.pop(task_uuid)
                    self.save_active_tasks()
                    return

    def _render_subprocess(self, task):
        task['status'] = 'rendering'
        with self._lock:
            self.save_active_tasks()
        try:
            render_args = task['args']
            mode:str = task['mode']
            video:VideoInfo = task.get('video')
            output:str = task.get('output')

            if mode == 'dmrender':
                from .dmrender import DmRender as TargetRender
            elif mode == 'transcode':
                from .transcode import Transcoder as TargetRender
            elif mode == 'rawffmpeg':
                raise NotImplementedError
                from .ffmpeg import RawFFmpegRender as TargetRender
            
            target_render = TargetRender(**render_args)
            self.logger.info(f'正在渲染: {video.path}')
            os.makedirs(os.path.dirname(output), exist_ok=True)

            self._render_class[task['uuid']] = target_render
            status, info = target_render.render_one(video=video, output=output)
            self._render_class.pop(task['uuid'])

            if status:
                self._gather(task, 'info', desc=info)
            else:
                self._gather(task, 'error', desc=info)
        except KeyboardInterrupt:
            target_render.stop()
        except Exception as e:
            self.logger.exception(e)
            self._gather(task, 'error', desc=e)

    def stop(self):
        self.stoped = True
        for uuid, render in self._render_class.items():
            render.stop()
        self.render_executors.shutdown(wait=False)

        with self._lock:
            for uuid, task in self.render_tasks.items():
                if uuid not in self.failed_tasks:
                    task['status'] = 'interrupted'
                    task['failure_reason'] = '程序停止时任务尚未完成'
                    self.failed_tasks[uuid] = task
            self.save_failed_tasks()
            atomic_json_dump({}, self.active_tasks_file)

        self.logger.info('Render stopped.')
