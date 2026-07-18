import hashlib
import json
import logging
import os
import queue
import threading
import time
from copy import deepcopy

from ..Highlight import Highlight
from ..utils import DateTimeDecoder, FFprobe, PipeMessage, atomic_json_dump, merge_dict, uuid, video_info_from_dict


class HighlightTask:
    """Independent hotspot orchestration task subscribed to one or more ReplayTasks."""

    task_type = 'highlight'

    def __init__(self, taskname, config, pipe, ai_client=None):
        self.send_queue, self.recv_queue = pipe
        self.taskname = taskname
        self.config = config
        self.ai_client = ai_client
        self.logger = logging.getLogger(f'DMR.HighlightTask.{taskname}')
        self.stoped = True
        self.jobs = {}
        self.recovered_job_ids = set()
        state_id = hashlib.sha256(taskname.encode('utf-8')).hexdigest()
        self.state_file = os.path.join('.temp', 'highlight_states', f'{state_id}.json')
        self._worker_recv = queue.Queue()
        self._worker_send = queue.Queue()
        runtime = config.get('runtime') or {}
        self.worker = Highlight(
            (self._worker_send, self._worker_recv),
            nhighlights=runtime.get('max_workers', 1), state_name=state_id,
            ai_client=ai_client,
        )
        self._load_state()

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, 'r', encoding='utf-8') as file:
                self.jobs = json.load(file, cls=DateTimeDecoder)
            self.recovered_job_ids = set(self.jobs)
            for job in self.jobs.values():
                for segment in job.get('segments', []):
                    if isinstance(segment.get('video'), dict):
                        segment['video'] = video_info_from_dict(segment['video'])
                    if isinstance(segment.get('source_video'), dict):
                        segment['source_video'] = video_info_from_dict(segment['source_video'])
                job['outputs'] = [
                    video_info_from_dict(output) if isinstance(output, dict) else output
                    for output in job.get('outputs', [])
                ]
        except Exception as error:
            self.logger.error('恢复热点任务状态失败: %s', error)
            self.jobs = {}

    def _save(self):
        atomic_json_dump(self.jobs, self.state_file)

    def resume_recovered_job(self, job_id):
        if job_id not in self.recovered_job_ids:
            return False, '只能恢复本次启动加载的热点任务。'
        job = self.jobs.get(job_id)
        if not job:
            return False, '热点任务记录不存在。'
        if job.get('status') in ('completed', 'failed'):
            return False, '热点任务已经结束。'
        previous_request_id = job.get('request_id')
        if previous_request_id:
            self._worker_recv.put(PipeMessage(
                source=f'highlight/{self.taskname}', target='highlight', event='discard',
                request_id=previous_request_id,
            ))
        for dependency in job.get('dependencies', {}).values():
            if dependency.get('status') == 'waiting':
                dependency['status'] = 'interrupted'
        job['request_id'] = uuid()
        job['error'] = None
        job['retries'] = 0
        self.recovered_job_ids.discard(job_id)
        if job.get('status') in ('uploading', 'output_ready') and job.get('outputs'):
            job['uploads'] = {}
            self._start_uploads(job_id, job)
        elif job.get('status') == 'cleaning' and job.get('outputs'):
            job['cleanups'] = {}
            job['status'] = 'completed'
            self._start_clean(job)
        else:
            self._dispatch_if_ready(job_id, job)
        self._save()
        self.logger.info('已恢复热点任务 %s。', job_id)
        return True, ''

    def delete_recovered_job(self, job_id):
        if job_id not in self.recovered_job_ids:
            return False, '只能删除本次启动恢复的热点任务记录。'
        job = self.jobs.get(job_id)
        if not job:
            return False, '热点任务记录不存在。'
        request_id = job.get('request_id')
        if request_id:
            self._worker_recv.put(PipeMessage(
                source=f'highlight/{self.taskname}', target='highlight', event='discard',
                request_id=request_id,
            ))
        self.jobs.pop(job_id, None)
        self.recovered_job_ids.discard(job_id)
        self._save()
        self.logger.info('已取消并删除恢复热点任务记录 %s，未删除视频文件。', job_id)
        return True, ''

    def delete_completed_job_by_manifest(self, manifest_path):
        """Remove one terminal highlight result from persistent task history."""
        target = os.path.realpath(manifest_path or '')
        job_id, job = next(((key, value) for key, value in self.jobs.items()
                            if os.path.realpath(value.get('manifest') or '') == target), (None, None))
        if not job:
            return False, '热点场次记录不存在。'
        if job.get('status') not in ('completed', 'failed'):
            return False, f'热点场次仍处于 {job.get("status")} 状态，不能删除。'
        if getattr(self, 'send_queue', None) is not None:
            self._release(job.get('source_task'), job.get('group_id'),
                          retain_id=os.path.realpath(manifest_path), release_retained=True)
        self.jobs.pop(job_id, None)
        self.recovered_job_ids.discard(job_id)
        self._save()
        return True, ''

    @staticmethod
    def _processor_config(target):
        source = target.get('source') or {}
        analysis = target.get('analysis') or {}
        encoding = target.get('encoding') or {}
        result = deepcopy(encoding)
        result.update({
            'source': {'video': source.get('video', 'src_video')},
            'danmaku_source': source.get('danmaku', 'auto'),
            'danmaku_types': source.get('danmaku_types', ['danmaku', 'emoticon']),
            'detection': deepcopy(analysis.get('detection') or {}),
            'ai': deepcopy(analysis.get('ai') or {}),
            'categories': deepcopy(analysis.get('categories') or {}),
            'test_fallback_clip_seconds': analysis.get('test_fallback_clip_seconds', 0),
            'outputs': deepcopy(target.get('outputs') or []),
            'upload': {'enabled': bool((target.get('upload') or {}).get('enabled'))},
        })
        return result

    def _subscribe(self, event):
        for source_task, target in self.config.get('targets', {}).items():
            if target.get('enabled', True):
                self.send_queue.put(PipeMessage(
                    source=f'highlight/{self.taskname}', target=f'replay/{source_task}',
                    event=event, data={'highlight_task': self.taskname},
                ))

    def _accept_source(self, message):
        data = message.data or {}
        source_task = data.get('source_task')
        manual = bool(data.get('manual'))
        target = deepcopy(data.get('target_config')) if manual else self.config.get('targets', {}).get(source_task)
        if not target or (not manual and not target.get('enabled', True)):
            return
        group_id = data.get('group_id')
        run_id = data.get('run_id')
        job_id = f'{source_task}:{group_id}:manual:{run_id}' if manual else f'{source_task}:{group_id}'
        if job_id in self.jobs:
            return
        if manual and any(
            job.get('source_task') == source_task and job.get('group_id') == group_id and
            job.get('status') not in ('completed', 'failed')
            for job in self.jobs.values()
        ):
            self.logger.warning('%s: %s 已有正在运行的热点任务。', self.taskname, group_id)
            return
        if data.get('session_ended') is not True:
            self.logger.warning('%s: 忽略尚未确认整场结束的热点通知 %s。', self.taskname, job_id)
            return
        video_states = data.get('video_states') or []
        expected_segments = int(data.get('segment_count', len(video_states)))
        if expected_segments <= 0 or len(video_states) != expected_segments:
            return self._fail(
                job_id, source_task, group_id,
                f'整场直播分段快照不完整: 期望 {expected_segments} 段，实际 {len(video_states)} 段',
            )
        source = target.get('source') or {}
        video_type = source.get('video', 'src_video')
        subtitle_mode = source.get('subtitle', 'prefer')
        segments = []
        ignored_segments = []
        dependencies = {}
        deadline = time.time() + max(0, int(source.get('subtitle_timeout', 10800)))
        ordered_states = sorted(
            video_states,
            key=lambda state: getattr(
                ((state.get('src_video') or {}).get('file') or
                 (state.get('src_video_pre') or {}).get('file') or
                 (state.get(video_type) or {}).get('file')),
                'segment_id', 0,
            ),
        )
        for index, state in enumerate(ordered_states):
            selected = (state.get(video_type) or {}).get('file')
            original = ((state.get('src_video') or {}).get('file') or
                        (state.get('src_video_pre') or {}).get('file') or selected)
            if video_type == 'src_video' and not selected:
                # 直播结束时转码可能尚未完成；热点任务使用该分段的录制原件，避免漏段。
                selected = original
            if not original or not getattr(original, 'path', None):
                continue
            duration = float(getattr(original, 'duration', 0) or 0)
            if duration <= 0 and os.path.exists(original.path):
                duration = float(FFprobe.get_duration(original.path) or 0)
            min_duration = max(0, float(source.get('min_segment_duration', 30)))
            if duration < min_duration:
                ignored_segments.append({
                    'segment_id': getattr(original, 'segment_id', index),
                    'path': original.path, 'duration': duration,
                    'reason': f'duration<{min_duration}',
                })
                continue
            subtitle = (state.get('subtitle') or {}).get('file')
            if subtitle and not isinstance(subtitle, str):
                subtitle = getattr(subtitle, 'path', None)
            segment = {
                'segment_id': getattr(original, 'segment_id', index), 'video': selected,
                'dm_file': getattr(original, 'dm_file_id', None),
                'raw_dm_file': getattr(original, 'raw_dm_file_id', None),
                'subtitle': subtitle if subtitle_mode != 'disabled' else None,
                'source_video': original,
            }
            upload_candidates = [selected, original, (state.get('dm_video') or {}).get('file')]
            for uploaded in upload_candidates:
                if not uploaded:
                    continue
                upload_info = (getattr(uploaded, 'dm_video_upload', None) or
                               getattr(uploaded, 'src_video_upload', None))
                bvid = (getattr(uploaded, 'dm_video_id', None) or
                        getattr(uploaded, 'src_video_id', None))
                if bvid:
                    segment['bilibili_upload'] = deepcopy(upload_info) if upload_info else {
                        'bvid': bvid, 'part_title': os.path.splitext(os.path.basename(uploaded.path))[0],
                    }
                    break
            segments.append(segment)
        accounted_segments = len(segments) + len(ignored_segments)
        if accounted_segments != expected_segments:
            return self._fail(
                job_id, source_task, group_id,
                f'整场直播存在缺少源视频的分段: 期望 {expected_segments} 段，'
                f'可用 {len(segments)} 段，忽略 {len(ignored_segments)} 段',
            )
        if not segments:
            return self._fail(
                job_id, source_task, group_id,
                f'整场直播没有达到最短时长 {source.get("min_segment_duration", 30)} 秒的有效分段',
            )
        job = {
            'source_task': source_task, 'group_id': group_id, 'status': 'preparing',
            'manual': manual, 'run_id': run_id,
            'received_at': time.time(), 'source_segment_count': expected_segments,
            'segment_count': len(segments), 'ignored_segments': ignored_segments, 'segments': segments,
            'config': deepcopy(target), 'uploads': {}, 'dependencies': dependencies,
            'deadline': deadline, 'error': None,
        }
        self.jobs[job_id] = job
        self._save()
        preprocess = target.get('preprocess') or {}
        for segment in segments:
            if video_type == 'dm_video' and not segment.get('video'):
                args = deepcopy(preprocess.get('dmrender') or {})
                if not args:
                    return self._fail(job_id, source_task, group_id, '缺少弹幕版视频，且未配置 preprocess.dmrender')
                request_id = uuid()
                ext = args.get('format', 'mp4')
                output_dir = args.get('output_dir') or os.path.dirname(segment['source_video'].path) + '（弹幕版）'
                output = os.path.join(output_dir, os.path.splitext(os.path.basename(segment['source_video'].path))[0] + f'（热点任务弹幕版）.{ext}')
                dependencies[request_id] = {'kind': 'render', 'segment_id': segment['segment_id'], 'status': 'waiting'}
                self.send_queue.put(PipeMessage(
                    source=f'highlight/{self.taskname}', target='render', event='newtask', request_id=request_id,
                    data={'taskname': self.taskname, 'mode': 'dmrender', 'video': segment['source_video'],
                          'output': output, 'args': args},
                ))
            if subtitle_mode in ('prefer', 'required') and not segment.get('subtitle'):
                transcribe = deepcopy(preprocess.get('transcribe') or {})
                existing_upload = segment.get('bilibili_upload')
                if existing_upload:
                    bili_args = deepcopy(transcribe.get('bilibili') or {})
                    if existing_upload.get('account') and not bili_args.get('cookies'):
                        bili_args['account'] = existing_upload['account']
                    transcribe = {'engine': 'bilibili', 'bilibili': bili_args}
                if transcribe:
                    request_id = uuid()
                    engine = transcribe.get('engine', 'xm')
                    transcribe_args = deepcopy(transcribe.get(engine, transcribe.get('args', {})))
                    if engine == 'bilibili' and segment.get('bilibili_upload'):
                        transcribe_args['existing_upload'] = deepcopy(segment['bilibili_upload'])
                    dependencies[request_id] = {'kind': 'subtitle', 'segment_id': segment['segment_id'], 'status': 'waiting'}
                    self.send_queue.put(PipeMessage(
                        source=f'highlight/{self.taskname}', target='transcriber', event='newtask', request_id=request_id,
                        data={'taskname': self.taskname, 'video': segment['source_video'], 'engine': engine,
                              'args': transcribe_args},
                    ))
        request_id = uuid()
        job['request_id'] = request_id
        self._save()
        self._dispatch_if_ready(job_id, job)

    def _dispatch_if_ready(self, job_id, job):
        if any(item.get('status') == 'waiting' for item in job.get('dependencies', {}).values()):
            job['status'] = 'waiting_dependencies'
            self._save()
            return
        source = job['config'].get('source') or {}
        missing_video = [item['segment_id'] for item in job['segments'] if not item.get('video')]
        missing_subtitle = [item['segment_id'] for item in job['segments'] if not item.get('subtitle')]
        if missing_video:
            return self._fail(job_id, job['source_task'], job['group_id'], f'缺少可剪辑视频分段: {missing_video}')
        if source.get('subtitle', 'prefer') == 'required' and missing_subtitle:
            return self._fail(job_id, job['source_task'], job['group_id'], f'缺少必需字幕分段: {missing_subtitle}')
        job['status'] = 'analyzing'
        self._save()
        processor_config = self._processor_config(job['config'])
        if not processor_config.get('output_dir') and job['segments']:
            processor_config['output_dir'] = os.path.join(
                os.path.dirname(job['segments'][0]['video'].path), f'高能混剪-{self.taskname}'
            )
        run_suffix = f'manual-{job["run_id"]}' if job.get('manual') else 'automatic'
        processor_config['output_dir'] = os.path.join(
            processor_config['output_dir'], f'{job["group_id"]}-{run_suffix}'
        )
        self._worker_recv.put(PipeMessage(
            source=f'highlight/{self.taskname}', target='highlight', event='newtask', request_id=job['request_id'],
            data={'taskname': self.taskname, 'source_task': job['source_task'],
                  'group_id': job['group_id'], 'segments': job['segments'],
                  'source_segment_count': job.get('source_segment_count'),
                  'ignored_segments': job.get('ignored_segments', []),
                  'subtitle_status': {'mode': source.get('subtitle', 'prefer')},
                  'args': processor_config},
        ))

    def _on_dependency(self, message):
        for job_id, job in self.jobs.items():
            dependency = job.get('dependencies', {}).get(message.request_id)
            if not dependency:
                continue
            if dependency.get('status') != 'waiting':
                self.logger.info('忽略已结束依赖的迟到消息: %s (%s)', message.request_id,
                                 dependency.get('status'))
                return
            segment = next((item for item in job['segments'] if item['segment_id'] == dependency['segment_id']), None)
            if message.event == 'end' and dependency['kind'] == 'render':
                segment['video'] = (message.data or {}).get('output')
                dependency['status'] = 'ready'
                self.send_queue.put(PipeMessage(source=f'highlight/{self.taskname}', target='render',
                                                event='ack', request_id=message.request_id))
            elif message.event == 'end' and dependency['kind'] == 'subtitle':
                result = message.data if isinstance(message.data, dict) else {}
                segment['subtitle'] = result.get('subtitle') if result else message.data
                dependency['status'] = 'ready'
            else:
                dependency['status'] = 'failed'
                dependency['error'] = message.msg
            self._dispatch_if_ready(job_id, job)
            return

    def _release(self, source_task, group_id, retain_id=None, release_retained=False):
        self.send_queue.put(PipeMessage(
            source=f'highlight/{self.taskname}', target=f'replay/{source_task}', event='highlight/release',
            data={'highlight_task': self.taskname, 'group_id': group_id,
                  'retain_id': retain_id, 'release_retained': release_retained},
        ))

    def _fail(self, job_id, source_task, group_id, reason):
        self.jobs[job_id] = {'source_task': source_task, 'group_id': group_id,
                             'status': 'failed', 'error': str(reason), 'finished_at': time.time()}
        self._save()
        self._release(source_task, group_id)

    def _on_worker(self, message):
        job_id, job = next(((key, value) for key, value in self.jobs.items()
                            if value.get('request_id') == message.request_id), (None, None))
        if not job:
            return
        if message.event == 'end':
            result = message.data or {}
            job.update({'status': 'output_ready', 'manifest': result.get('manifest'),
                        'outputs': result.get('outputs') or [], 'finished_at': time.time()})
            self._save()
            virtual = str(((job.get('config') or {}).get('source') or {}).get('video')) == 'dm_video'
            self._release(job['source_task'], job['group_id'],
                          retain_id=os.path.realpath(job.get('manifest') or '') if virtual else None)
            self._start_uploads(job_id, job)
            self._worker_recv.put(PipeMessage(source=f'highlight/{self.taskname}', target='highlight',
                                              event='ack', request_id=message.request_id))
        elif message.event == 'error':
            runtime = self.config.get('runtime') or {}
            retries = int(job.get('retries', 0))
            if retries < int(runtime.get('retry_count', 1)):
                job['retries'] = retries + 1
                job['status'] = 'retry_wait'
                job['last_error'] = message.msg
                job['request_id'] = uuid()
                self._save()
                timer = threading.Timer(max(0, int(runtime.get('retry_interval', 60))),
                                        self._dispatch_if_ready, args=(job_id, job))
                timer.daemon = True
                timer.start()
            else:
                self._fail(job_id, job['source_task'], job['group_id'], message.msg)

    def _start_uploads(self, job_id, job):
        upload = job.get('config', {}).get('upload') or {}
        if not upload.get('enabled', False):
            job['status'] = 'completed'
            self._save()
            self._start_clean(job)
            return
        common = upload.get('common') or {}
        overrides = upload.get('outputs') or {}
        for output in job.get('outputs') or []:
            args = merge_dict(deepcopy(common), deepcopy(overrides.get(output.dtype) or {}))
            if not args.pop('enabled', True):
                continue
            args = self._replace_task_keywords(args, job)
            request_id = uuid()
            job['uploads'][request_id] = {'dtype': output.dtype, 'status': 'uploading'}
            self.send_queue.put(PipeMessage(
                source=f'highlight/{self.taskname}', target='uploader', event='newtask', request_id=request_id,
                data={'taskname': self.taskname, 'files': [output], 'engine': args['engine'],
                      'stateless': True, 'upload_group': f'{self.taskname}_{job["group_id"]}_{output.dtype}',
                      'args': args},
            ))
        job['status'] = 'uploading' if job['uploads'] else 'completed'
        self._save()

    def _replace_task_keywords(self, value, job):
        if isinstance(value, str):
            return value.replace('{HIGHLIGHT_TASK}', self.taskname).replace('{SOURCE_TASK}', job['source_task'])
        if isinstance(value, dict):
            return {key: self._replace_task_keywords(item, job) for key, item in value.items()}
        if isinstance(value, list):
            return [self._replace_task_keywords(item, job) for item in value]
        return value

    def _on_upload(self, message):
        for job in self.jobs.values():
            upload = job.get('uploads', {}).get(message.request_id)
            if not upload:
                continue
            upload['status'] = 'uploaded' if message.event == 'end' else 'failed'
            if message.event != 'end':
                upload['error'] = message.msg
            if all(item['status'] != 'uploading' for item in job['uploads'].values()):
                job['status'] = 'completed' if all(item['status'] == 'uploaded' for item in job['uploads'].values()) else 'failed'
            self._save()
            if job['status'] == 'completed':
                self._start_clean(job)
            return

    def _start_clean(self, job):
        clean = job.get('config', {}).get('clean') or {}
        if not clean.get('enabled', False):
            return
        job.setdefault('cleanups', {})
        configured = clean.get('outputs') or {}
        for output in job.get('outputs') or []:
            args = deepcopy(configured.get(output.dtype) or configured.get('all') or {})
            if not args or not args.get('method'):
                continue
            request_id = uuid()
            job['cleanups'][request_id] = {'dtype': output.dtype, 'status': 'cleaning'}
            self.send_queue.put(PipeMessage(
                source=f'highlight/{self.taskname}', target='cleaner', event='newtask', request_id=request_id,
                data={'taskname': self.taskname, 'files': [output], 'method': args['method'],
                      'delay': args.get('delay', 0), 'args': args},
            ))
        if job['cleanups']:
            job['status'] = 'cleaning'
            self._save()

    def _on_clean(self, message):
        for job in self.jobs.values():
            cleanup = job.get('cleanups', {}).get(message.request_id)
            if not cleanup:
                continue
            cleanup['status'] = 'cleaned' if message.event == 'end' else 'failed'
            if all(item['status'] != 'cleaning' for item in job['cleanups'].values()):
                job['status'] = 'completed' if all(item['status'] == 'cleaned' for item in job['cleanups'].values()) else 'failed'
            self._save()
            return

    def _monitor(self):
        while not self.stoped:
            message = self.recv_queue.get()
            if message.event == 'source_ready':
                self._accept_source(message)
            elif message.source == 'uploader' and message.event in ('end', 'error'):
                self._on_upload(message)
            elif message.source in ('render', 'transcriber') and message.event in ('end', 'error'):
                self._on_dependency(message)
            elif message.source == 'cleaner' and message.event in ('end', 'error'):
                self._on_clean(message)
            elif message.event == 'tick':
                for job_id, job in list(self.jobs.items()):
                    if job.get('status') == 'waiting_dependencies' and time.time() >= job.get('deadline', 0):
                        for dependency in job.get('dependencies', {}).values():
                            if dependency.get('status') == 'waiting':
                                dependency['status'] = 'timeout'
                        self._dispatch_if_ready(job_id, job)
            elif message.event == 'exit':
                self.stop()
                break

    def _worker_monitor(self):
        while not self.stoped:
            self._on_worker(self._worker_send.get())

    def start(self):
        self.stoped = False
        self.worker.start()
        threading.Thread(target=self._monitor, daemon=True).start()
        threading.Thread(target=self._worker_monitor, daemon=True).start()
        threading.Thread(target=self._tick_monitor, daemon=True).start()
        self._subscribe('highlight/subscribe')

    def _tick_monitor(self):
        while not self.stoped:
            threading.Event().wait(30)
            if not self.stoped:
                self.recv_queue.put(PipeMessage(source=f'highlight/{self.taskname}',
                                                target=f'highlight/{self.taskname}', event='tick'))

    def stop(self):
        self._subscribe('highlight/unsubscribe')
        self.stoped = True
        self.worker.stop()
        self._save()
