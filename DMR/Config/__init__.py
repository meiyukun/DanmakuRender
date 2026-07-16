import os
import glob
import yaml
import logging
import hashlib
from copy import deepcopy
from typing import List
from DMR.utils import filename_to_taskname, merge_dict, ToolsList

# __all__ = ['Config', 'new_config']

class Config():
    _base_config_path = 'DMR/Config/default.yml'

    def __init__(self, global_config_path:str) -> None:
        self.global_config_path = global_config_path
        self.replay_config_path_raw = []
        self.replay_config_paths:List[str] = []
        self.highlight_config_paths:List[str] = []
        self.file_hashes = {}
        self.logger = logging.getLogger(__name__)
        
        self._init_config()
        self.check_update()

    def _get_file_hash(self, path):
        try:
            with open(path, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception:
            return None

    def _init_config(self):
        with open(self._base_config_path, 'r', encoding='utf-8') as f:
            self._base_config = yaml.safe_load(f)

        self.global_config = deepcopy(self._base_config)
        self.replay_config = {}
        self.highlight_config = {}

        with open(self.global_config_path, 'r', encoding='utf-8') as f:
            _global_config = yaml.safe_load(f)
        self.file_hashes[self.global_config_path] = self._get_file_hash(self.global_config_path)
        
        self.global_config = merge_dict(self.global_config, _global_config)

        for toolname, path in self.global_config.get('executable_tools_path',{}).items():
            if not path:
                ToolsList.get(toolname, auto_install=True)
            else:
                ToolsList.set(toolname, path)

        dmr_engine_args = self.global_config.get('dmr_engine_args', {})
        self.replay_config_path_raw = dmr_engine_args.get('config_path', ['./configs'])
        if isinstance(self.replay_config_path_raw, str):
            self.replay_config_path_raw = [self.replay_config_path_raw]

    def add_task_config(self, config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                _replay_config = yaml.safe_load(f)
            _replay_config = _replay_config or {}
            if any(key in _replay_config for key in ('highlight_args', 'highlight_upload_args')) or \
                    (_replay_config.get('common_event_args') or {}).get('auto_highlight'):
                raise ValueError('热点剪辑已迁移到独立 DMH-*.yml；请删除直播任务中的 auto_highlight、highlight_args 和 highlight_upload_args。')
            
            current_hash = self._get_file_hash(config_path)
            self.file_hashes[config_path] = current_hash
            
            taskname = filename_to_taskname(config_path)
            replay_config = {}
            
            common_args = _replay_config.get('common_event_args')
            if not common_args: return None
            replay_config['common_event_args'] = deepcopy(common_args)
            # 默认保持原有的恢复行为；全局设置可由单个录制任务覆盖。
            replay_config['common_event_args'].setdefault(
                'recover_pipeline',
                self.global_config.get('dmr_engine_args', {}).get('recover_pipeline', True),
            )
            
            global_download_args = self.global_config['download_args']
            dltype = _replay_config.get('download_args', {}).get('dltype', 'live')
            replay_config['download_args'] = deepcopy(global_download_args.get(dltype, {}))
            if _replay_config.get('download_args'):
                replay_config['download_args'] = merge_dict(replay_config['download_args'], _replay_config['download_args'])

            if common_args.get('auto_render') or common_args.get('auto_transcode'):
                replay_config['render_args'] = deepcopy(self.global_config['render_args'])
                if _replay_config.get('render_args'):
                    replay_config['render_args'] = merge_dict(replay_config['render_args'], _replay_config['render_args'])

            if common_args.get('auto_transcribe'):
                replay_config['transcribe_args'] = deepcopy(self.global_config['transcribe_args'])
                if _replay_config.get('transcribe_args'):
                    replay_config['transcribe_args'] = merge_dict(replay_config['transcribe_args'], _replay_config['transcribe_args'])

            if common_args.get('auto_upload'):
                global_upload_args = self.global_config['upload_args']
                replay_config['upload_args'] = {}
                if _replay_config.get('upload_args'):
                    for upload_file_types, upload_args in _replay_config['upload_args'].items():
                        if isinstance(upload_args, dict):
                            upload_args = [upload_args]
                        replay_config['upload_args'][upload_file_types] = []
                        for upload_arg in upload_args:
                            target = upload_arg.get('target', 'bilibili')
                            if not global_upload_args.get(target):
                                raise ValueError(f'不存在可用的上传目标 {target}.')
                            upload_config = deepcopy(global_upload_args[target])
                            upload_config = merge_dict(upload_config, upload_arg)
                            replay_config['upload_args'][upload_file_types].append(upload_config)

            if common_args.get('auto_clean'):
                global_clean_args = self.global_config['clean_args']
                replay_config['clean_args'] = {}
                if _replay_config.get('clean_args'):
                    for clean_file_types, clean_args in _replay_config['clean_args'].items():
                        if isinstance(clean_args, dict):
                            clean_args = [clean_args]
                        replay_config['clean_args'][clean_file_types] = []
                        for clean_arg in clean_args:
                            method = clean_arg.get('method')
                            if not method:
                                continue
                            if not global_clean_args.get(method):
                                raise ValueError(f'不存在可用的清理方法 {method}.')
                            clean_config = deepcopy(global_clean_args[method])
                            clean_config = merge_dict(clean_config, clean_arg)
                            replay_config['clean_args'][clean_file_types].append(clean_config)

            self.replay_config[taskname] = deepcopy(replay_config)
            return taskname
        except Exception as e:
            self.logger.error(f"Error loading config {config_path}:")
            self.logger.exception(e)
            return None

    def add_highlight_config(self, config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                raw = yaml.safe_load(f) or {}
            if raw.get('task_type') != 'highlight':
                raise ValueError('DMH 配置必须声明 task_type: highlight。')
            defaults = deepcopy(raw.get('defaults') or {})
            defaults.setdefault('source', {}).setdefault('min_segment_duration', 30)
            defaults.setdefault('source', {}).setdefault('subtitle', 'available')
            defaults.setdefault('upload', {}).setdefault('common', {}).setdefault('engine', 'biliwebapi')
            encoding = defaults.setdefault('encoding', {})
            encoding.setdefault('mode', 'copy')
            encoding.setdefault('copy_fallback', 'reencode')
            encoding.setdefault('keyframe_alignment', 'outward')
            encoding.setdefault('keep_clips', True)
            detection = defaults.setdefault('analysis', {}).setdefault('detection', {})
            for key, value in {
                'boundary_quiet_seconds': 4,
                'pre_roll_seconds': 10,
                'boundary_start_quantile': 0.05,
                'boundary_end_quantile': 0.92,
                'peak_merge_max_gap_seconds': 45,
                'peak_merge_valley_ratio': 0.25,
                'peak_merge_quiet_seconds': 6,
                'max_clip_peak_position_ratio': 0.65,
            }.items():
                detection.setdefault(key, value)
            targets = raw.get('targets') or {}
            if not isinstance(targets, dict) or not targets:
                raise ValueError('热点任务至少需要配置一个 targets 直播任务。')
            resolved = {}
            for source_task, override in targets.items():
                override = override or {}
                merged = merge_dict(deepcopy(defaults), override)
                source = merged.get('source') or {}
                if source.get('video', 'src_video') not in ('src_video', 'dm_video'):
                    raise ValueError(f'{source_task}: source.video 仅支持 src_video 或 dm_video。')
                if source.get('subtitle', 'prefer') not in ('disabled', 'available', 'prefer', 'required'):
                    raise ValueError(f'{source_task}: source.subtitle 策略无效。')
                resolved[source_task] = merged
            taskname = filename_to_taskname(config_path)
            if self.highlight_config and taskname not in self.highlight_config:
                configured = ', '.join(self.highlight_config)
                raise ValueError(
                    f'全局只能加载一份 DMH-*.yml 热点剪辑配置；当前已加载 {configured}，不能再加载 {taskname}。'
                )
            self.highlight_config[taskname] = {
                'task_type': 'highlight',
                'runtime': deepcopy(raw.get('runtime') or {}),
                'defaults': defaults,
                'targets': resolved,
            }
            self.file_hashes[config_path] = self._get_file_hash(config_path)
            return taskname
        except Exception as e:
            self.logger.error(f'Error loading highlight config {config_path}:')
            self.logger.exception(e)
            return None

    def check_update(self):
        updated_tasks = []
        new_tasks = []
        deleted_tasks = []
        
        # Check global config
        try:
            current_hash = self._get_file_hash(self.global_config_path)
            if current_hash != self.file_hashes.get(self.global_config_path):
                self.file_hashes[self.global_config_path] = current_hash
                return 'global', None
        except Exception:
            pass

        # get replay configs
        current_files = set()
        current_highlight_files = set()
        for raw_path in self.replay_config_path_raw:
            if os.path.isfile(raw_path):
                if os.path.basename(raw_path).startswith('DMH-'):
                    current_highlight_files.add(raw_path)
                else:
                    current_files.add(raw_path)
            elif os.path.isdir(raw_path):
                config_files = glob.glob(os.path.join(raw_path, 'DMR-**.yml'))
                for f in config_files:
                    current_files.add(f)
                for f in glob.glob(os.path.join(raw_path, 'DMH-**.yml')):
                    current_highlight_files.add(f)

        old_files = set(self.replay_config_paths)
        old_highlight_files = set(self.highlight_config_paths)
        
        for f in current_files - old_files:
            if self.add_task_config(f):
                new_tasks.append(f)
        
        for f in old_files - current_files:
            deleted_tasks.append(f)
            t = filename_to_taskname(f)
            self.replay_config.pop(t, None)
            self.file_hashes.pop(f, None)
            
        for f in current_files & old_files:
            try:
                current_hash = self._get_file_hash(f)
                if current_hash != self.file_hashes.get(f) and self.add_task_config(f):
                    updated_tasks.append(f)
            except Exception:
                pass

        for f in current_highlight_files - old_highlight_files:
            if self.add_highlight_config(f):
                new_tasks.append(f)
        for f in old_highlight_files - current_highlight_files:
            deleted_tasks.append(f)
            self.highlight_config.pop(filename_to_taskname(f), None)
            self.file_hashes.pop(f, None)
        for f in current_highlight_files & old_highlight_files:
            if self._get_file_hash(f) != self.file_hashes.get(f) and self.add_highlight_config(f):
                updated_tasks.append(f)
        
        if new_tasks or deleted_tasks or updated_tasks:
            self.replay_config_paths = list(current_files)
            self.highlight_config_paths = list(current_highlight_files)
            return 'tasks', {
                'new': list(new_tasks),
                'deleted': list(deleted_tasks),
                'updated': list(updated_tasks)
            }
        
        return None, None

    def get_config(self, name):
        return self.global_config.get(name)
    
    def get_replay_config(self, taskname):
        return self.replay_config.get(taskname)
    
    def get_replaytasks(self):
        return list(self.replay_config.keys())

    def get_highlight_config(self, taskname):
        return self.highlight_config.get(taskname)

    def get_highlighttasks(self):
        return list(self.highlight_config.keys())
