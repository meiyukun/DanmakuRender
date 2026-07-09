import queue
import logging
import threading
import os
import json
import hashlib

from typing import Tuple
from .liveevents import LiveEvents
from ..utils import *


class ReplayTask():
    def __init__(self, taskname, config:dict, pipe:Tuple[queue.Queue, queue.Queue]):
        self.send_queue, self.recv_queue = pipe
        self.taskname = taskname
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.event_class = LiveEvents(self.taskname, self.config)
        self._event_dict = {}
        self.stoped = True
        outbox_id = hashlib.sha256(taskname.encode('utf-8')).hexdigest()
        self.outbox_file = os.path.join('.temp', 'replay_outbox', f'{outbox_id}.json')
        self._outbox = self._load_outbox()

    @staticmethod
    def _restore_message_data(data):
        if not isinstance(data, dict):
            return data
        restored = dict(data)
        if isinstance(restored.get('video'), dict):
            restored['video'] = video_info_from_dict(restored['video'])
        if isinstance(restored.get('files'), list):
            restored['files'] = [
                video_info_from_dict(file_info)
                if isinstance(file_info, dict) else file_info
                for file_info in restored['files']
            ]
        return restored

    def _load_outbox(self):
        if not os.path.exists(self.outbox_file):
            return {}
        try:
            with open(self.outbox_file, 'r', encoding='utf-8') as f:
                data = json.load(f, cls=DateTimeDecoder)
            for item in data.values():
                item['data'] = self._restore_message_data(item.get('data'))
            return data
        except Exception as e:
            self.logger.error(f'{self.taskname}: 恢复任务发件箱失败: {e}')
            return {}

    def _save_outbox(self):
        atomic_json_dump(self._outbox, self.outbox_file)

    def _accept_outbox_message(self, request_id):
        if request_id and self._outbox.pop(request_id, None) is not None:
            self._save_outbox()

    def add_event(self, event, trigger):
        if self._event_dict.get(event) != None:
            self._event_dict[event].append(trigger)
        self._event_dict[event] = [trigger]

    def _pipeSend(self, msg:PipeMessage):
        if not msg.source.startswith('replay/'):
            msg.source = 'replay/' + msg.source
        if msg.event == 'newtask' and msg.request_id:
            self._outbox[msg.request_id] = dict(msg)
            self._save_outbox()
        self.send_queue.put(msg)

    def _pipeRecvMonitor(self):
        while self.stoped == False:
            msg:PipeMessage = self.recv_queue.get()
            try:
                if msg.target.startswith('replay/') and msg.target.split('/')[1] == self.taskname:
                    if msg.event == 'accepted':
                        self._accept_outbox_message(msg.request_id)
                        continue
                    event = msg.source +'/'+ msg.event
                    funcs = self._event_dict.get(event) or self._event_dict.get(msg.event)
                    if isinstance(funcs, list):
                        for func in funcs:
                            ret_msgs = func(msg)
                            if not ret_msgs:
                                continue
                            elif isinstance(ret_msgs, list):
                                for return_msg in ret_msgs:
                                    self._pipeSend(return_msg)
                            elif isinstance(ret_msgs, PipeMessage):
                                self._pipeSend(ret_msgs)
                            else:
                                self.logger.error(f'Event:{event} return an unknown type of message.')
                    else:
                        if self._event_dict.get('default') is None:
                            self.logger.info(f'Event:{event} is not registered at task:{self.taskname}.')
                        else:
                            for func in self._event_dict['default']:
                                ret_msgs = func(msg)
                                if not ret_msgs:
                                    continue
                                elif isinstance(ret_msgs, list):
                                    for return_msg in ret_msgs:
                                        self._pipeSend(return_msg)
                                elif isinstance(ret_msgs, PipeMessage):
                                    self._pipeSend(ret_msgs)
                                else:
                                    self.logger.error(f'Event:{event} return an unknown type of message.')

                    if msg.source in ('render', 'uploader') and msg.event == 'end':
                        self._pipeSend(PipeMessage(
                            source=self.taskname,
                            target=msg.source,
                            event='ack',
                            request_id=msg.request_id,
                        ))
                    
                    if msg.event == 'exit':
                        self.logger.debug(f'Task:{self.taskname} recieved exit message.')
                        self.stop()
                        break
            except Exception as e:
                self.logger.error(f'Message:{msg} raise an error: {e}')
                self.logger.exception(e)

    def start(self):
        self.stoped = False
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()

        for event, trigger in self.event_class.event_dict.items():
            if isinstance(trigger, (list, tuple, set)):
                for func in trigger:
                    self.add_event(event, func)
            self.add_event(event, trigger)

        for persisted in list(self._outbox.values()):
            self._pipeSend(PipeMessage(**persisted))

    def stop(self):
        self.stoped = True
        self._event_dict.clear()
