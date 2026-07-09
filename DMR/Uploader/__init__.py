import logging
import threading
import queue
import time
import json
from concurrent.futures import ThreadPoolExecutor
from os.path import join, exists
from datetime import datetime
from typing import Tuple

from DMR.utils import (
    VideoInfo, PipeMessage, DateTimeDecoder, atomic_json_dump,
    video_info_from_dict, uuid,
)


class Uploader():
    def __init__(self,
                 pipe:Tuple[queue.Queue, queue.Queue],
                 nuploaders:int=1,
                 **kwargs,
                 ) -> None:
        
        self.nuploaders = int(nuploaders)
        self.send_queue, self.recv_queue = pipe
        self.logger = logging.getLogger(__name__)
        self.kwargs = kwargs

        self.stoped = True
        self._piperecvprocess = None
        self._uploader_pool = {}
        self.upload_tasks = {}
        self.failed_tasks = {}
        self.failed_tasks_file = '.temp/failed_uploads.json'
        self.active_tasks_file = '.temp/active_uploads.json'
        self.load_failed_tasks()
        self.load_interrupted_tasks()
        
        self.upload_executors = ThreadPoolExecutor(max_workers=self.nuploaders)
        self._lock = threading.Lock()
        self.last_retry_error = None

    def load_failed_tasks(self):
        if exists(self.failed_tasks_file):
            try:
                with open(self.failed_tasks_file, 'r', encoding='utf-8') as f:
                    data = json.load(f, cls=DateTimeDecoder)
                    # Restore datetime objects and VideoInfo objects
                    for uuid, task in data.items():
                        if 'files' in task:
                            restored_files = []
                            # Wrap in VideoInfo
                            for f in task['files']:
                                restored_files.append(video_info_from_dict(f))
                            task['files'] = restored_files
                        if task.get('status') != 'completion_pending':
                            task['status'] = 'failed'
                            # Also update files in config
                            # if 'config' in task and 'files' in task['config']:
                            #     task['config']['files'] = restored_files
                        self.failed_tasks[uuid] = task
                self.logger.info(f'Loaded {len(self.failed_tasks)} failed upload tasks.')
            except Exception as e:
                self.logger.error(f'Failed to load failed tasks: {e}')

    def save_failed_tasks(self):
        try:
            atomic_json_dump(self.failed_tasks, self.failed_tasks_file)
        except Exception as e:
            self.logger.error(f'Failed to save failed tasks: {e}')

    @staticmethod
    def _persistable_tasks(tasks):
        result = {}
        for task_uuid, task in tasks.items():
            persisted = dict(task)
            persisted['stream_queue'] = None
            result[task_uuid] = persisted
        return result

    def load_interrupted_tasks(self):
        if not exists(self.active_tasks_file):
            return
        try:
            with open(self.active_tasks_file, 'r', encoding='utf-8') as f:
                data = json.load(f, cls=DateTimeDecoder)
            for task_uuid, task in data.items():
                task['files'] = [
                    video_info_from_dict(file_info)
                    for file_info in task.get('files', [])
                ]
                task['stream_queue'] = None
                if task.get('status') == 'completion_pending':
                    task['failure_reason'] = '上传已完成，流水线尚未确认；恢复时不会重复上传'
                else:
                    task['status'] = 'interrupted'
                    task['failure_reason'] = '程序在上传任务完成前停止；重试可能造成重复投稿'
                self.failed_tasks[task_uuid] = task
            self.save_failed_tasks()
            atomic_json_dump({}, self.active_tasks_file)
            self.logger.warning(f'Recovered {len(data)} interrupted upload tasks.')
        except Exception as e:
            self.logger.error(f'Failed to load interrupted upload tasks: {e}')

    def save_active_tasks(self):
        try:
            atomic_json_dump(
                self._persistable_tasks(self.upload_tasks),
                self.active_tasks_file,
            )
        except Exception as e:
            self.logger.error(f'Failed to save active upload tasks: {e}')

    def retry_task(self, uuid):
        with self._lock:
            self.last_retry_error = None
            if uuid in self.failed_tasks:
                task = self.failed_tasks[uuid]
                if task.get('status') == 'completion_pending':
                    task = self.failed_tasks.pop(uuid)
                    self.upload_tasks[task['uuid']] = task
                    self.save_active_tasks()
                    self.save_failed_tasks()
                    self._send_completed_result(task)
                    return True
                missing = [file.path for file in task.get('files', []) if not exists(file.path)]
                if missing:
                    self.last_retry_error = f'待上传文件不存在: {missing}'
                    return False
                task = self.failed_tasks.pop(uuid)
                
                # Re-submit
                task['status'] = 'waiting'
                task.pop('failure_reason', None)
                self.upload_tasks[task['uuid']] = task
                self.save_active_tasks()
                self.save_failed_tasks()
                if task.get('stream_queue'):
                    threading.Thread(target=self._upload_subprocess, args=(task,), daemon=True).start()
                else:
                    self.upload_executors.submit(self._upload_subprocess, task)
                return True
            return False

    def delete_failed_task(self, uuid):
        with self._lock:
            if uuid in self.failed_tasks:
                self.failed_tasks.pop(uuid)
                self.save_failed_tasks()
                return True
            return False

    def _pipeSend(self, event, msg, target='engine', request_id=None, dtype=None, data=None, **kwargs):
        if self.send_queue:
            msg = PipeMessage(
                source='uploader',
                target=target,
                event=event,
                request_id=request_id,
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
                if message.target == 'uploader':
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

    def _free_uploader_pool(self):
        with self._lock:
            for upload_group in list(self._uploader_pool.keys()):
                expire = self._uploader_pool[upload_group]['expire']
                if expire > 0 and time.time() - self._uploader_pool[upload_group]['ctime'] > expire:
                    self._uploader_pool[upload_group]['class'].stop()
                    self._uploader_pool.pop(upload_group)
                    self.logger.debug(f'Uploader {upload_group} expired and removed.')

    def add_task(self, msg:PipeMessage):
        with self._lock:
            for task in list(self.upload_tasks.values()) + list(self.failed_tasks.values()):
                if msg.request_id and task.get('request_id') == msg.request_id:
                    self._pipeSend(
                        'accepted', '上传任务已存在', target=msg.source,
                        request_id=msg.request_id,
                    )
                    return
            config = msg.data
            file = config.get('files')[0]
            stream_queue = None
            if hasattr(file, 'stream_queue') and file.stream_queue is not None:
                stream_queue = file.stream_queue
            task = {
                'uuid': uuid(),
                'source': msg.source,
                'request_id': msg.request_id,
                'upload_group': config.get('upload_group'),
                'engine': config.get('engine', 'biliuprs'),
                'args': config.get('args', {}),
                'files': config.get('files'),
                'stream_queue': stream_queue,
                # 'config': config,
                'status': 'waiting',
            }
            self.upload_tasks[task['uuid']] = task
            self.save_active_tasks()
            self._pipeSend(
                'accepted', '上传任务已持久化', target=msg.source,
                request_id=msg.request_id,
            )
            if stream_queue:
                threading.Thread(target=self._upload_subprocess, args=(task,), daemon=True).start()
            else:
                self.upload_executors.submit(self._upload_subprocess, task)

    def _gather(self, task, status, desc=''):
        with self._lock:
            if status == 'error':
                task['stream_queue'] = None
                task['status'] = 'failed'
                task['failure_reason'] = str(desc)
                self.failed_tasks[task['uuid']] = task
                self.save_failed_tasks()
                self.upload_tasks.pop(task['uuid'], None)
                self.save_active_tasks()
            if status == 'error':
                self._pipeSend(
                    event='error',
                    msg=f"上传视频 {[f.path for f in task['files']]} 时出现错误:{desc}",
                    target=task['source'],
                    request_id=task['request_id'],
                    dtype=str(type(desc)),
                    data=desc,
                )
            else:
                task['status'] = 'completion_pending'
                task['completion_result'] = desc
                self.save_active_tasks()
                self._send_completed_result(task)

    def _send_completed_result(self, task):
        self._pipeSend(
            event='end',
            msg=f"视频 {[f.path for f in task['files']]} 上传完成: {task.get('completion_result')}",
            target=task['source'],
            request_id=task['request_id'],
            dtype='dict',
            data={},
        )

    def _ack_task(self, request_id):
        with self._lock:
            for task_uuid, task in list(self.upload_tasks.items()):
                if task.get('request_id') == request_id and task.get('status') == 'completion_pending':
                    self.upload_tasks.pop(task_uuid)
                    self.save_active_tasks()
                    return

    def _upload_subprocess(self, task):
        task['status'] = 'uploading'
        with self._lock:
            self.save_active_tasks()
        try:
            upload_args = task['args']
            upload_group:str = task['upload_group']

            with self._lock:
                if upload_group in self._uploader_pool:
                    target_uploader = self._uploader_pool[upload_group]['class']
                    self._uploader_pool[upload_group]['ctime'] = time.time()
                else:
                    engine:str = task['engine']
                    if engine == 'biliuprs':
                        from .biliuprs import biliuprs as TargetUploader
                    elif engine == 'subprocess':
                        from .subprocess_uploader import SubprocessUploader as TargetUploader
                    elif engine == 'youtubev3':
                        from .youtubev3 import youtubev3 as TargetUploader
                    elif engine == 'biliwebapi':
                        from .biliwebapi import BiliWebApi as TargetUploader
                    else:
                        raise ValueError(f'Unknown engine: {engine}')
                    
                    target_uploader = TargetUploader(**upload_args)
                    self._uploader_pool[upload_group] = {
                        'class': target_uploader,
                        'ctime': time.time(),
                        'expire': task.get('expire', 7*24*3600)
                    }

            files = task['files']
            stream_queue = task.get('stream_queue', None)
            retry = upload_args.get('retry', 0)
            if stream_queue:
                retry = 0       # 流式上传无法重试
            status = info = None
            
            while retry >= 0:
                try:
                    if stream_queue:
                        self.logger.info(f"正在同步上传 {files[0].title} 至 {upload_args.get('account')}")
                    else:
                        self.logger.info(f"正在上传 {[f.path for f in files]} 至 {upload_args.get('account')}")
                    # logging.debug(task)
                    res = target_uploader.upload(files=files, stream_queue=stream_queue, **upload_args)
                    
                    if len(res) == 3:
                        status, info, _ = res
                    else:
                        status, info = res

                except KeyboardInterrupt:
                    target_uploader.stop()
                    self.stop()
                    return
                except Exception as e:
                    status, info = False, e
                    self.logger.exception(e)
                
                retry -= 1
                if status or self.stoped:
                    break
                elif retry < 0:
                    self.logger.warning(f'上传 {[f.path for f in files]} 时出现错误，跳过上传.')
                    break
                else:
                    self.logger.warning(f'上传 {[f.path for f in files]} 时出现错误，即将重传.')
                    self.logger.debug(info)
                    time.sleep(60)
            
            if status:
                self._gather(task, 'info', desc=info)
            else:
                self._gather(task, 'error', desc=info)

            self._free_uploader_pool()
        
        except Exception as e:
            self.logger.exception(e)
            self._gather(task, 'error', desc=e)

    def stop(self):
        self.stoped = True
        self.upload_executors.shutdown(wait=False)
        for upload_group in self._uploader_pool:
            self._uploader_pool[upload_group]['class'].stop()
        
        with self._lock:
            for uuid, task in self.upload_tasks.items():
                if uuid not in self.failed_tasks:
                    task['stream_queue'] = None
                    task['status'] = 'interrupted'
                    task['failure_reason'] = '程序停止时任务尚未完成；重试可能造成重复投稿'
                    self.failed_tasks[uuid] = task
            self.save_failed_tasks()
            atomic_json_dump({}, self.active_tasks_file)

        self.logger.info('Uploader stopped.')
