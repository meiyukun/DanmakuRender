import queue
import logging
import threading

from .Cleaner import Cleaner
from .Downloader import Downloader
from .Render import Render
from .Transcriber import Transcriber
from .Uploader import Uploader
from .Task import HighlightTask, ReplayTask
from .WebService import WebService
from .utils import *


class DMREngine():
    def __init__(self, ai_client=None):
        self.logger = logging.getLogger(__name__)
        self.task_dict = {}
        self.plugin_dict = {}
        self.recv_queue = None
        self.stoped = True
        self.ai_client = ai_client
        
    def pipeSend(self, message:PipeMessage):
        target = message.target
        self.logger.debug(message)
        if target == 'engine':
            self.recv_queue.put(message)
        elif target.startswith(('replay/', 'highlight/')):
            task_key = target
            if task_key not in self.task_dict:
                if self.stoped:
                    self.logger.debug(f'Ignore message for stopped task {task_key}.')
                else:
                    self.logger.error(f'Task {task_key} not exists.')
                return
            self.task_dict[task_key]['send_queue'].put(message)
        elif target == 'render':
            self.plugin_dict['render']['send_queue'].put(message)
        elif target == 'uploader':
            self.plugin_dict['uploader']['send_queue'].put(message)
        elif target == 'transcriber':
            if 'transcriber' not in self.plugin_dict:
                self.logger.error('Transcriber plugin is not enabled.')
                return
            self.plugin_dict['transcriber']['send_queue'].put(message)
        elif target == 'cleaner':
            self.plugin_dict['cleaner']['send_queue'].put(message)
        elif target == 'downloader':
            self.plugin_dict['downloader']['send_queue'].put(message)
        else:
            # raise Exception(f'Unknown target {target}.')
            self.logger.error(f'Unknown target {target}.')

    def _pipeRecvMonitor(self):
        while not self.stoped:
            message:PipeMessage = self.recv_queue.get()
            try:
                if message.target == 'engine':
                    if message.event == 'info':
                        self.logger.info(message.msg)
                    elif message.event == 'addtask':
                        self.add_task(message.data['taskname'], message.data['config'])
                    elif message.event == 'deltask':
                        self.del_task(message.data)
                else:
                    self.pipeSend(message)
            except Exception as e:
                self.logger.error(f'Message:{message} raise an error.')
                self.logger.exception(e)
    
    def start(self):
        self.stoped = False
        self.recv_queue = queue.Queue()
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()
        self.logger.debug('DMR engine started.')

        for name, plugin in self.plugin_dict.items():
            if plugin['status'] == 0:
                plugin['class'].start()
                self.plugin_dict[name]['status'] = 1
                self.logger.debug(f'Plugin {name} started.')
        
        for task_key, task in self.task_dict.items():
            if task['status'] == 0:
                task['class'].start()
                self.task_dict[task_key]['status'] = 1
                if task['task_type'] == 'replay':
                    self.pipeSend(PipeMessage('engine', task_key, 'ready'))
                self.logger.debug(f'Task {task_key} started.')

    def add_plugin(self, name, config):
        send_queue = queue.Queue()
        if name == 'render':
            plugin = Render((self.recv_queue, send_queue), **config)
        elif name == 'uploader':
            plugin = Uploader((self.recv_queue, send_queue), ai_client=self.ai_client, **config)
        elif name == 'transcriber':
            plugin = Transcriber((self.recv_queue, send_queue), **config)
        elif name == 'cleaner':
            plugin = Cleaner((self.recv_queue, send_queue), **config)
        elif name == 'downloader':
            plugin = Downloader((self.recv_queue, send_queue), **config)
        elif name == 'webservice':
            plugin = WebService((self.recv_queue, send_queue), engine=self, ai_client=self.ai_client, **config)
        else:
            self.logger.error(f'Unknown plugin {name}.')
            # raise Exception(f'Unknown plugin {name}.')
        if self.stoped == False:
            plugin.start()
            self.logger.debug(f'Plugin {name} started.')
        else:
            self.logger.debug(f'Plugin {name} created.')
        self.plugin_dict[name] = {
            'class': plugin,
            'config': config,
            'send_queue': send_queue,
            'status': 0 if self.stoped else 1,
        }

    def add_task(self, taskname, config, task_type='replay'):
        send_queue = queue.Queue()
        task_cls = HighlightTask if task_type == 'highlight' else ReplayTask
        task = task_cls(taskname, config, (self.recv_queue, send_queue), ai_client=self.ai_client)
        task_key = f'{task_type}/{taskname}'
        self.task_dict[task_key] = {
            'class': task,
            'config': config,
            'send_queue': send_queue,
            'task_type': task_type,
            'name': taskname,
            'status': 0 if self.stoped else 1,
        }
        if self.stoped == False:
            task.start()
            if task_type == 'replay':
                self.pipeSend(PipeMessage('engine', f'replay/{taskname}', 'ready'))
                for other in self.task_dict.values():
                    if other.get('task_type') == 'highlight' and taskname in other['config'].get('targets', {}):
                        highlight_name = other['name']
                        self.pipeSend(PipeMessage(
                            f'highlight/{highlight_name}', f'replay/{taskname}', 'highlight/subscribe',
                            data={'highlight_task': highlight_name},
                        ))
            self.logger.debug(f'Task {taskname} started.')
        else:
            self.logger.debug(f'Task {taskname} created.')

    def del_task(self, taskname, task_type='replay'):
        task_key = f'{task_type}/{taskname}'
        if task_key in self.task_dict:
            self.pipeSend(PipeMessage('engine', task_key, 'exit'))
            # self.task_dict[taskname]['class'].stop()
            del self.task_dict[task_key]
            self.logger.debug(f'Task {taskname} deleted.')
        else:
            self.logger.debug(f'Task {taskname} not exists.')

    def stop(self):
        self.stoped = True
        task_keys = sorted(self.task_dict.keys(), key=lambda key: 0 if key.startswith('highlight/') else 1)
        for task_key in task_keys:
            task_type, taskname = task_key.split('/', 1)
            self.del_task(taskname, task_type)
        for name in self.plugin_dict.keys():
            try:
                self.plugin_dict[name]['class'].stop()
            except Exception as e:
                self.logger.exception(e)
        self.task_dict.clear()
        self.plugin_dict.clear()
        self.recv_queue.put(PipeMessage('engine', 'engine', 'exit'))
        self.logger.info('DMR engine stoped.')
