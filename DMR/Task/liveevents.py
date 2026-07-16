import logging
import os
import json
import hashlib
from copy import deepcopy
from .baseevents import BaseEvents
from ..utils import *

class LiveEvents(BaseEvents):
    def __init__(self, name, config):
        super().__init__(name, config)
        self.state_dict = {}
        self.ended_dict = {}
        self.highlight_subscribers = set()
        self.highlight_holds = {}
        self.recovered_group_ids = set()
        self.logger = logging.getLogger(__name__)
        state_id = hashlib.sha256(name.encode('utf-8')).hexdigest()
        self.state_file = os.path.join('.temp', 'replay_states', f'{state_id}.json')
        self.recover_pipeline = config.get('common_event_args', {}).get('recover_pipeline', True)
        if self.recover_pipeline:
            self._load_state()
        elif os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
                self.logger.info(f'{self.name}: 已按配置丢弃未完成流水线状态，未删除视频文件.')
            except OSError as e:
                self.logger.warning(f'{self.name}: 丢弃未完成流水线状态失败: {e}')

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                state = json.load(f, cls=DateTimeDecoder)
            self.state_dict = state.get('state_dict', {})
            self.ended_dict = state.get('ended_dict', {})
            self.highlight_holds = {key: set(value) for key, value in state.get('highlight_holds', {}).items()}
            self.recovered_group_ids = set(self.state_dict)
            for video_states in self.state_dict.values():
                for video_state in video_states:
                    for info in video_state.values():
                        if isinstance(info.get('file'), dict):
                            info['file'] = video_info_from_dict(info['file'])
                        if isinstance(info.get('pending_render'), dict):
                            pending = dict(info['pending_render'])
                            data = pending.get('data') or {}
                            if isinstance(data.get('video'), dict):
                                data['video'] = video_info_from_dict(data['video'])
                            pending['data'] = data
                            info['pending_render'] = PipeMessage(**pending)
            loaded_count = len(self.state_dict)
            self._free_state_memory()
            removed_count = loaded_count - len(self.state_dict)
            if removed_count:
                self._save_state()
                self.logger.info(f'{self.name}: 自动清理 {removed_count} 个已无后续动作的流水线状态.')
            if self.state_dict:
                self.logger.warning(
                    f'{self.name}: 已加载 {len(self.state_dict)} 个未完成视频组的流水线状态.'
                )
        except Exception as e:
            self.logger.error(f'{self.name}: 恢复流水线状态失败: {e}')
            self.logger.exception(e)
            self.state_dict = {}
            self.ended_dict = {}
            self.recovered_group_ids = set()

    def _save_state(self):
        try:
            if not self.state_dict:
                if os.path.exists(self.state_file):
                    os.remove(self.state_file)
                return
            atomic_json_dump({
                'taskname': self.name,
                'state_dict': self.state_dict,
                'ended_dict': self.ended_dict,
                'highlight_holds': {key: sorted(value) for key, value in self.highlight_holds.items()},
            }, self.state_file)
        except Exception as e:
            self.logger.error(f'{self.name}: 保存流水线状态失败: {e}')

    def delete_recovered_pipeline_state(self, group_id):
        if group_id not in self.recovered_group_ids:
            return False, '只能删除本次启动恢复的流水线记录。'

        video_states = self.state_dict.get(group_id)
        if video_states is None:
            return False, '流水线记录不存在。'
        for video_state in video_states:
            for info in video_state.values():
                if info.get('wait') or info.get('status') in ('rendering', 'uploading'):
                    return False, '流水线仍有正在处理或等待响应的任务。'

        self.state_dict.pop(group_id, None)
        self.ended_dict.pop(group_id, None)
        self.recovered_group_ids.discard(group_id)
        self._save_state()
        self.logger.info(f'{self.name}: 已删除恢复流水线记录 {group_id}，未删除视频文件.')
        return True, ''

    def resume_recovered_pipeline_state(self, group_id):
        """Reconcile stale request ids and rebuild pending work after a restart."""
        if group_id not in self.recovered_group_ids:
            return False, '只能恢复本次启动加载的流水线记录。', []
        video_states = self.state_dict.get(group_id)
        if not video_states:
            return False, '流水线记录不存在。', []

        messages = []
        for index, video_state in enumerate(video_states):
            for video_type, info in video_state.items():
                info['wait'] = []
                if video_type == 'subtitle':
                    info.pop('pending_render', None)
                if info.get('status') not in ('rendering', 'waiting_subtitle', 'transcribing', 'uploading'):
                    continue
                file_info = info.get('file')
                path = file_info if isinstance(file_info, str) else getattr(file_info, 'path', None)
                info['status'] = 'ready' if path and os.path.exists(path) else None

            source = video_state.get('src_video', {})
            source_file = source.get('file')
            if not source_file or not getattr(source_file, 'path', None) or not os.path.exists(source_file.path):
                source_pre = video_state.get('src_video_pre', {}).get('file')
                if source_pre and getattr(source_pre, 'path', None) and os.path.exists(source_pre.path):
                    source.update({'status': 'ready', 'file': source_pre, 'wait': []})
                    source_file = source_pre
            if not source_file or not os.path.exists(source_file.path):
                continue

            subtitle = video_state.get('subtitle', {})
            if self.config['common_event_args'].get('auto_transcribe') and subtitle.get('status') is None:
                request_id = uuid()
                args = self.config.get('transcribe_args', {})
                engine = args.get('engine', 'xm')
                messages.append(PipeMessage(
                    source=self.name, target='transcriber', event='newtask', request_id=request_id,
                    data={'taskname': self.name, 'video': source_file, 'engine': engine,
                          'args': args.get(engine, {})},
                ))
                subtitle.update({'status': 'transcribing', 'wait': [request_id]})

            dm_video = video_state.get('dm_video', {})
            if self.config['common_event_args'].get('auto_render') and dm_video.get('status') is None:
                args = self.config.get('render_args', {}).get('dmrender', {})
                if args.get('output_name'):
                    filename = replace_keywords(args['output_name'], source_file, replace_invalid=True) + f".{args.get('format', 'mp4')}"
                else:
                    filename = os.path.splitext(os.path.basename(source_file.path))[0] + f"（弹幕版）.{args.get('format', 'mp4')}"
                output_dir = args.get('output_dir') or os.path.dirname(source_file.path) + '（弹幕版）'
                request_id = uuid()
                messages.append(PipeMessage(
                    source=self.name, target='render', event='newtask', request_id=request_id,
                    data={'taskname': self.name, 'mode': 'dmrender', 'video': source_file,
                          'output': os.path.join(output_dir, filename), 'args': args},
                ))
                dm_video.update({'status': 'rendering', 'wait': [request_id]})

        if group_id in self.ended_dict and self.config['common_event_args'].get('auto_upload'):
            messages.extend(self._check_for_upload(group_id))
        self.recovered_group_ids.discard(group_id)
        self._save_state()
        self.logger.info('%s: 已恢复视频组流水线 %s，重新投递 %d 个任务。', self.name, group_id, len(messages))
        return True, '', messages

    @property
    def event_dict(self):
        return {
            'ready': self.onReady,
            'exit': self.onExit,
            'downloader/livestart': self.defaultEvent, 
            'downloader/livesegment': self.onLiveSegment,
            'downloader/liveend': self.onLiveEnd,
            'downloader/livestop': self.onLiveEnd,
            'render/end': self.onRenderEnd,
            'render/error': self.defaultEvent,
            'transcriber/end': self.onTranscribeEnd,
            'transcriber/error': self.onTranscribeError,
            'highlight/subscribe': self.onHighlightSubscribe,
            'highlight/unsubscribe': self.onHighlightUnsubscribe,
            'highlight/release': self.onHighlightRelease,
            'uploader/end': self.onUploadEnd,
            'uploader/error': self.defaultEvent,
            'cleaner/end': self.defaultEvent,
            'cleaner/error': self.defaultEvent,
            'tick': self.onTick,
            'default': self.defaultEvent,
        }
    
    def defaultEvent(self, message:PipeMessage):
        if message.msg:
            self.logger.info(f'{self.name}: {message.msg}')
        else:
            self.logger.debug('%s: 收到无文本事件 %s/%s。', self.name, message.source, message.event)

    def onTick(self, *args, **kwargs):
        """Internal pipeline heartbeat; intentionally produces no user-visible log."""
        return None

    def onReady(self, *args, **kwargs):
        return PipeMessage(
            source=self.name,
            target='downloader',
            event='newtask',
            data={
                'dltype': self.config['download_args']['dltype'],
                'taskname': self.name,
                'config': self.config['download_args'],
            }
        )
    
    def onLiveSegment(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}')
        video:VideoInfo = message.data
        video_state = {
            # 'video_id': uuid(8),
            'src_video': {'status': None, 'file': None, 'wait': []},
            'src_video_pre': {'status': None, 'file': None, 'wait': []},
            'dm_video': {'status': None, 'file': None, 'wait': []},
            'subtitle': {'status': None, 'file': None, 'wait': [], 'pending_render': None},
        }
        if self.state_dict.get(video.group_id):
            self.state_dict[video.group_id].append(video_state)
        else:
            self.state_dict[video.group_id] = [video_state]

        ret_msgs = []
        if self.config['common_event_args'].get('auto_transcribe'):
            if video.path and os.path.exists(video.path):
                transcribe_msg = PipeMessage(
                    source=self.name,
                    target='transcriber',
                    event='newtask',
                    request_id=uuid(),
                    data={
                        'taskname': self.name,
                        'video': video,
                        'engine': self.config['transcribe_args'].get('engine', 'xm'),
                        'args': self.config['transcribe_args'].get(
                            self.config['transcribe_args'].get('engine', 'xm'), {}
                        ),
                    }
                )
                self.state_dict[video.group_id][-1]['subtitle']['status'] = 'transcribing'
                self.state_dict[video.group_id][-1]['subtitle']['wait'].append(transcribe_msg.request_id)
                ret_msgs.append(transcribe_msg)
            else:
                self.logger.info(f'{self.name}: 视频 {video.path} 不是本地文件，跳过语音转录.')

        if self.config['common_event_args'].get('auto_transcode'):
            transcode_args = self.config['render_args']['transcode']
            if transcode_args.get('output_name'):
                filename = replace_keywords(transcode_args['output_name'], video, replace_invalid=True) + \
                        f".{transcode_args.get('format','mp4')}"
            else:
                filename = os.path.splitext(os.path.basename(video.path))[0] + \
                        f"（转码后）.{transcode_args.get('format','mp4')}"
            if transcode_args.get('output_dir'):
                output_dir = transcode_args.get('output_dir')
            else:
                output_dir = os.path.dirname(video.path) + '（转码后）'
            output = os.path.join(output_dir, filename)
            transcode_msg = PipeMessage(
                source=self.name,
                target='render',
                event='newtask',
                request_id=uuid(),
                data={
                    'taskname': self.name,
                    'mode': 'transcode',
                    'video': video,
                    'output': output,
                    'args': transcode_args,
                }
            )
            self.state_dict[video.group_id][-1]['src_video_pre'].update({'status': 'ready', 'file': video})
            self.state_dict[video.group_id][-1]['src_video']['status'] = 'rendering'
            self.state_dict[video.group_id][-1]['src_video']['wait'].append(transcode_msg.request_id)
            ret_msgs.append(transcode_msg)
        else:
            self.state_dict[video.group_id][-1]['src_video'].update({'status': 'ready', 'file': video})
        
        if self.config['common_event_args'].get('auto_render'):
            render_args = self.config['render_args']['dmrender']
            if render_args.get('output_name'):
                filename = replace_keywords(render_args['output_name'], video, replace_invalid=True) + \
                        f".{render_args.get('format','mp4')}"
            else:
                filename = os.path.splitext(os.path.basename(video.path))[0] + \
                        f"（弹幕版）.{render_args.get('format','mp4')}"
            if render_args.get('output_dir'):
                output_dir = render_args.get('output_dir')
            else:
                output_dir = os.path.dirname(video.path) + '（弹幕版）'
            output = os.path.join(output_dir, filename)
            render_msg = PipeMessage(
                source=self.name,
                target='render',
                event='newtask',
                request_id=uuid(),
                data={
                    'taskname': self.name,
                    'mode': 'dmrender',
                    'video': video,
                    'output': output,
                    'args': render_args,
                }
            )
            self.state_dict[video.group_id][-1]['dm_video']['status'] = 'rendering'
            self.state_dict[video.group_id][-1]['dm_video']['wait'].append(render_msg.request_id)
            wait_subtitle = bool(render_args.get('burn_subtitle')) and bool(
                self.config['common_event_args'].get('auto_transcribe')
            )
            if wait_subtitle:
                self.state_dict[video.group_id][-1]['dm_video']['status'] = 'waiting_subtitle'
                self.state_dict[video.group_id][-1]['dm_video']['wait'].remove(render_msg.request_id)
                self.state_dict[video.group_id][-1]['subtitle']['pending_render'] = render_msg
            else:
                ret_msgs.append(render_msg)

        if self.config['common_event_args'].get('auto_upload'):
            ret_msgs += self._check_for_upload(video.group_id, len(self.state_dict[video.group_id])-1)
        self._save_state()
        return ret_msgs

    def _finish_transcribe(self, message, succeeded):
        self.logger.info(f'{self.name}: {message.msg}')
        ret_msgs = []
        for video_states in self.state_dict.values():
            for video_state in video_states:
                info = video_state.get('subtitle')
                if not info or message.request_id not in info.get('wait', []):
                    continue
                info['wait'].remove(message.request_id)
                result = message.data if isinstance(message.data, dict) else {}
                info['file'] = result.get('subtitle')
                info['status'] = 'ready' if succeeded else 'failed'
                pending = info.pop('pending_render', None)
                if pending:
                    policy = self.config['render_args']['dmrender'].get(
                        'subtitle_failure_policy', 'continue'
                    )
                    if succeeded or policy == 'continue':
                        video_state['dm_video']['status'] = 'rendering'
                        video_state['dm_video']['wait'].append(pending.request_id)
                        ret_msgs.append(pending)
                    elif policy == 'skip':
                        video_state['dm_video']['status'] = None
                    else:
                        video_state['dm_video']['status'] = 'failed'
                break
        self._free_state_memory()
        self._save_state()
        return ret_msgs

    def onTranscribeEnd(self, message):
        return self._finish_transcribe(message, True)

    def onTranscribeError(self, message):
        return self._finish_transcribe(message, False)
    
    def onLiveEnd(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}.')
        group_id = message.data
        if group_id is None:
            return
        
        if group_id in self.state_dict:
            self.ended_dict[group_id] = time.time()
        else:
            self.logger.debug(f'No such group:{group_id}.')
        
        ret_msgs = []
        if self.config['common_event_args'].get('auto_upload'):
            upload_msgs = self._check_for_upload(group_id)
            ret_msgs += upload_msgs
        for subscriber in sorted(self.highlight_subscribers):
            self.highlight_holds.setdefault(group_id, set()).add(subscriber)
            video_states = deepcopy(self.state_dict.get(group_id, []))
            ret_msgs.append(PipeMessage(
                source=f'replay/{self.name}', target=f'highlight/{subscriber}', event='source_ready',
                data={'source_task': self.name, 'group_id': group_id,
                      'session_ended': True, 'segment_count': len(video_states),
                      'video_states': video_states},
            ))

        self._free_state_memory()
        self._save_state()
        return ret_msgs

    def onHighlightSubscribe(self, message):
        task = (message.data or {}).get('highlight_task')
        ret_msgs = []
        if task:
            was_subscribed = task in self.highlight_subscribers
            self.highlight_subscribers.add(task)
            self.logger.info('%s: 热点任务 %s 已订阅直播结束事件。', self.name, task)
            if not was_subscribed:
                for group_id in self.ended_dict:
                    if group_id not in self.state_dict:
                        continue
                    self.highlight_holds.setdefault(group_id, set()).add(task)
                    video_states = deepcopy(self.state_dict[group_id])
                    ret_msgs.append(PipeMessage(
                        source=f'replay/{self.name}', target=f'highlight/{task}', event='source_ready',
                        data={'source_task': self.name, 'group_id': group_id,
                              'session_ended': True, 'segment_count': len(video_states),
                              'video_states': video_states},
                    ))
                self._save_state()
        return ret_msgs

    def onHighlightUnsubscribe(self, message):
        task = (message.data or {}).get('highlight_task')
        self.highlight_subscribers.discard(task)
        for holds in self.highlight_holds.values():
            holds.discard(task)
        self._free_state_memory()
        self._save_state()

    def onHighlightRelease(self, message):
        data = message.data or {}
        holds = self.highlight_holds.get(data.get('group_id'))
        if holds is not None:
            holds.discard(data.get('highlight_task'))
            if not holds:
                self.highlight_holds.pop(data.get('group_id'), None)
        self._free_state_memory()
        self._save_state()
    
    def _check_for_upload(self, group_id:str, _idx:int=None):
        ret_msgs = []
        if not self.state_dict.get(group_id):
            return ret_msgs
        
        upload_args = self.config['upload_args']
        for idx, video_state in enumerate(self.state_dict[group_id]):
            if _idx is not None and idx != _idx:
                continue
            for vtype, info in video_state.items():
                if info['status'] != 'ready':
                    continue
                for upload_file_types, upload_arg in upload_args.items():
                    # 判断当前视频是否需要上传
                    if vtype in upload_file_types.split('+'):
                        for upid, arg in enumerate(upload_arg):
                            # 实时上传
                            if not arg.get('realtime'):
                                continue
                            if info['file'].duration < arg.get('min_length', 0):
                                self.logger.info(f'视频{info["file"].path}时长为{info["file"].duration}s，设置{arg.get("min_length", 0)}s，跳过上传.')
                                continue
                            upload_group_id = info['file'].upload_group_id if hasattr(info['file'], 'upload_group_id') else group_id
                            upload_msg = PipeMessage(
                                source=self.name,
                                target='uploader',
                                event='newtask',
                                request_id=uuid(),
                                data={
                                    'taskname': self.name,
                                    'files': [info['file']],
                                    'engine': arg['engine'],
                                    'stateless': False,
                                    'upload_group': upload_group_id+'_'+upload_file_types+'_'+str(upid),
                                    'args': arg,
                                }
                            )
                            self.state_dict[group_id][idx][vtype]['status'] = 'uploading'
                            self.state_dict[group_id][idx][vtype]['wait'].append(upload_msg.request_id)
                            ret_msgs.append(upload_msg)
        
        # 如果当前视频组已经被标记结束，检查是否有视频组完全准备好上传（用于非实时上传）
        if group_id in self.ended_dict:
            # 遍历所有视频类型
            video_types = list(self.state_dict[group_id][-1].keys())
            for vtype in video_types:
                # 检查是否全部准备上传
                videos = []
                for idx, video_state in enumerate(self.state_dict[group_id]):
                    if video_state[vtype]['status'] == 'ready':
                        videos.append(video_state[vtype]['file'])
                    else:
                        videos = []
                        break
                if not videos:
                    continue

                # 判断当前视频是否需要上传
                for upload_file_types, upload_arg in upload_args.items():
                    if vtype in upload_file_types.split('+'):
                        for upid, arg in enumerate(upload_arg):
                            # 此处只做非实时上传
                            if arg.get('realtime'):
                                continue
                            up_videos = [video for video in videos if video.duration >= arg.get('min_length', 0)]
                            upload_group_id = up_videos[0].upload_group_id if hasattr(up_videos[0], 'upload_group_id') else group_id
                            upload_msg = PipeMessage(
                                source=self.name,
                                target='uploader',
                                event='newtask',
                                request_id=uuid(),
                                data={
                                    'taskname': self.name,
                                    'files': up_videos,
                                    'engine': arg['engine'],
                                    'stateless': True,
                                    'upload_group': upload_group_id+'_'+upload_file_types+'_'+str(upid),
                                    'args': arg,
                                }
                            )
                            # 标记状态信息
                            for idx, _ in enumerate(self.state_dict[group_id]):
                                self.state_dict[group_id][idx][vtype]['status'] = 'uploading'
                                self.state_dict[group_id][idx][vtype]['wait'].append(upload_msg.request_id)
                            ret_msgs.append(upload_msg)

        return ret_msgs
    
    def onRenderEnd(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}.')
        request_id = message.request_id
        video:VideoInfo = message.data.get('output')
        if not video or video.group_id not in self.state_dict:
            self.logger.error(
                f'{self.name}: 渲染任务已完成，但找不到视频组流水线状态，无法继续自动上传.'
            )
            return
        video_states = self.state_dict[video.group_id]
        # 将状态信息中request_id对应的等待移除
        for idx, video_state in enumerate(video_states):
            for vtype, info in video_state.items():
                if request_id in info['wait']:
                    self.state_dict[video.group_id][idx][vtype]['wait'].remove(request_id)
                    if len(self.state_dict[video.group_id][idx][vtype]['wait']) == 0:
                        self.state_dict[video.group_id][idx][vtype]['status'] = 'ready'
                        self.state_dict[video.group_id][idx][vtype]['file'] = video
        
        ret_msgs = []
        if self.config['common_event_args'].get('auto_upload'):
            upload_msgs = self._check_for_upload(video.group_id)
            ret_msgs += upload_msgs
        self._save_state()
        return ret_msgs
    
    def _check_for_clean(self, group_id=None):
        ret_msgs = []
        clean_args = self.config['clean_args']
        requested_group = group_id
        for group_id, video_states in self.state_dict.items():
            if requested_group is not None and requested_group != group_id:
                continue
            for idx, video_state in enumerate(video_states):
                for vtype, info in video_state.items():
                    if self.highlight_holds.get(group_id) and vtype in ('src_video', 'src_video_pre', 'dm_video', 'subtitle'):
                        continue
                    if info['status'] != 'uploaded':
                        continue
                    for clean_file_types, clean_arg in clean_args.items():
                        # 判断当前视频是否需要清理
                        if vtype in clean_file_types.split('+') or clean_file_types == 'all':
                            for arg in clean_arg:
                                files = [info['file']]
                                # 判断是否需要清理源文件
                                if vtype == 'dm_video' and arg.get('w_srcfile', False) == True and video_state['src_video']['file'] is not None:
                                    files.append(video_state['src_video']['file'])
                                    self.state_dict[group_id][idx]['src_video']['status'] = 'cleaned'
                                # 判断是否需要清理源文件（转码前）
                                if vtype == 'src_video' and arg.get('w_srcpre', True) == True and video_state['src_video_pre']['file'] is not None:
                                    files.append(video_state['src_video_pre']['file'])
                                    self.state_dict[group_id][idx]['src_video_pre']['status'] = 'cleaned'
                                
                                clean_msg = PipeMessage(
                                    source=self.name,
                                    target='cleaner',
                                    event='newtask',
                                    request_id=uuid(),
                                    data={
                                        'taskname': self.name,
                                        'files': files,
                                        'method': arg['method'],
                                        'delay': arg['delay'],
                                        'args': arg,
                                    }
                                )
                                ret_msgs.append(clean_msg)
                    self.state_dict[group_id][idx][vtype]['status'] = 'cleaned'

        return ret_msgs
    
    @staticmethod
    def _file_type_is_configured(args, video_type):
        return any(
            configured_type == 'all' or video_type in configured_type.split('+')
            for configured_type in args
        )

    def _stage_has_follow_up(self, video_type, info):
        status = info.get('status')
        if info.get('wait') or status in ('rendering', 'uploading'):
            return True
        if video_type == 'subtitle':
            return status in ('transcribing',) or bool(info.get('wait')) or bool(info.get('pending_render'))
        if status == 'ready':
            return (
                self.config['common_event_args'].get('auto_upload')
                and self._file_type_is_configured(self.config.get('upload_args', {}), video_type)
            )
        if status == 'uploaded':
            return (
                self.config['common_event_args'].get('auto_clean')
                and self._file_type_is_configured(self.config.get('clean_args', {}), video_type)
            )
        return status not in (None, 'cleaned')

    def _group_has_follow_up(self, video_states):
        return any(
            self._stage_has_follow_up(video_type, info)
            for video_state in video_states
            for video_type, info in video_state.items()
        )

    def _free_state_memory(self):
        for group_id in list(self.ended_dict.keys()):
            video_states = self.state_dict.get(group_id)
            if video_states is None:
                self.ended_dict.pop(group_id)
                self.recovered_group_ids.discard(group_id)
                continue
            if not self._group_has_follow_up(video_states) and not self.highlight_holds.get(group_id):
                self.logger.debug(f'视频组{group_id}处理完成，视频信息已被释放.')
                self.ended_dict.pop(group_id)
                self.state_dict.pop(group_id)
                self.highlight_holds.pop(group_id, None)
                self.recovered_group_ids.discard(group_id)

        for group_id in list(self.ended_dict.keys()):
            if time.time() - self.ended_dict[group_id] > 72*3600:
                self.logger.debug(f'视频组{group_id}处理超时，视频信息将被释放.')
                self.ended_dict.pop(group_id)
                self.state_dict.pop(group_id)
                self.recovered_group_ids.discard(group_id)

    def onUploadEnd(self, message:PipeMessage):
        self.logger.info(f'{self.name}: {message.msg}.')
        request_id = message.request_id
        # 将状态信息中request_id对应的等待移除
        for group_id, video_states in self.state_dict.items():
            for idx, video_state in enumerate(video_states):
                for vtype, info in video_state.items():
                    if request_id in info['wait']:
                        self.state_dict[group_id][idx][vtype]['wait'].remove(request_id)
                        if len(self.state_dict[group_id][idx][vtype]['wait']) == 0:
                            self.state_dict[group_id][idx][vtype]['status'] = 'uploaded'
        
        ret_msgs = []
        if self.config['common_event_args'].get('auto_clean'):
            clean_msgs = self._check_for_clean()
            ret_msgs += clean_msgs
        self._free_state_memory()
        self._save_state()
        return ret_msgs

    def onExit(self, *args, **kwargs) -> None:
        self.logger.info(f'{self.name}: 任务结束.')
        # Keep unfinished state across both graceful and unexpected restarts.
        self._save_state()
        return PipeMessage(
            source=self.name,
            target='downloader',
            event='stoptask',
            data=self.name,
        )
