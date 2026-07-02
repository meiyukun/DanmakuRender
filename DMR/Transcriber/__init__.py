import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple

from DMR.utils import PipeMessage, VideoInfo, uuid


class Transcriber:
    def __init__(
        self,
        pipe: Tuple[queue.Queue, queue.Queue],
        ntranscribers: int = 1,
        **kwargs,
    ) -> None:
        self.ntranscribers = int(ntranscribers)
        self.send_queue, self.recv_queue = pipe
        self.logger = logging.getLogger(__name__)
        self.kwargs = kwargs
        self.stoped = True

        self._piperecvprocess = None
        self.transcribe_tasks = {}
        self.transcribe_executors = ThreadPoolExecutor(max_workers=self.ntranscribers)
        self._lock = threading.Lock()

    def _pipeSend(self, event, msg, target='engine', request_id=None, dtype=None, data=None, **kwargs):
        if self.send_queue:
            msg = PipeMessage(
                source='transcriber',
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
            message: PipeMessage = self.recv_queue.get()
            try:
                if message.target == 'transcriber':
                    if message.event == 'newtask':
                        self.add_task(message)
                    elif message.event == 'exit':
                        break
            except Exception as e:
                self.logger.error(f'Message:{message} raise an error.')
                self.logger.exception(e)

    def start(self):
        self.stoped = False
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()

    def add_task(self, msg: PipeMessage):
        with self._lock:
            config = msg.data
            task = {
                'uuid': uuid(),
                'source': msg.source,
                'request_id': msg.request_id,
                'video': config.get('video'),
                'args': config.get('args') or {},
                'status': 'waiting',
            }
            self.transcribe_tasks[task['uuid']] = task
            self.transcribe_executors.submit(self._transcribe_subprocess, task)

    def _gather(self, task, status, desc=''):
        with self._lock:
            self.transcribe_tasks.pop(task['uuid'], None)

        video = task.get('video')
        video_path = video.path if isinstance(video, VideoInfo) else video
        if status == 'error':
            self._pipeSend(
                event='error',
                msg=f'转录视频 {video_path} 时出现错误: {desc}',
                target=task['source'],
                request_id=task['request_id'],
                dtype=str(type(desc)),
                data=desc,
            )
        else:
            self._pipeSend(
                event='end',
                msg=f'视频 {video_path} 转录完成: {desc}',
                target=task['source'],
                request_id=task['request_id'],
                dtype='str',
                data=desc,
            )

    def _transcribe_subprocess(self, task):
        task['status'] = 'transcribing'
        try:
            args = task['args']
            auth_file = args.get('auth_file')
            if not auth_file:
                raise ValueError('未配置 transcribe_args.xm.auth_file')
            video = task.get('video')
            if not video or not getattr(video, 'path', None):
                raise ValueError('转录任务缺少本地视频路径')

            from .xm import XMTranscriber
            target_transcriber = XMTranscriber(auth_file=auth_file)
            self.logger.info(f'正在转录: {video.path}')
            status, info = target_transcriber.transcribe(video)
            if status:
                self._gather(task, 'info', desc=info)
            else:
                self._gather(task, 'error', desc=info)
        except Exception as e:
            self.logger.exception(e)
            self._gather(task, 'error', desc=e)

    def stop(self):
        self.stoped = True
        if self.recv_queue is not None:
            self.recv_queue.put(PipeMessage(source='transcriber', target='transcriber', event='exit'))
        self.transcribe_executors.shutdown(wait=False)
        self.logger.info('Transcriber stopped.')
