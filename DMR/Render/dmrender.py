import copy
import logging
import os
import platform
from .baserender import BaseRender
from .ffmpeg import RawFFmpegRender
from os.path import exists
from DMR.utils import *

class DmRender(BaseRender):
    def __init__(self,
                 hwaccel_args: list,
                 vencoder: str,
                 vencoder_args: list,
                 aencoder: str,
                 aencoder_args: list,
                 output_resize: str,
                 advanced_render_args: dict=None,
                 ffmpeg: str = None,
                 debug=False,
                 before_cmd:str=None,
                 after_cmd:str=None,
                 extra_inputs:list=None,
                 **kwargs
                 ):
        self.hwaccel_args = hwaccel_args if hwaccel_args is not None else []
        self.vencoder = vencoder
        self.vencoder_args = vencoder_args
        self.aencoder = aencoder
        self.aencoder_args = aencoder_args
        self.output_resize = output_resize
        self.advanced_render_args = advanced_render_args if isinstance(advanced_render_args, dict) else {}
        self.ffmpeg = ffmpeg if ffmpeg else ToolsList.get('ffmpeg')
        self.debug = debug
        self.before_cmd = before_cmd
        self.after_cmd = after_cmd
        self.extra_inputs = extra_inputs if extra_inputs is not None else []

        self.logger = logging.getLogger(__name__)
        self.raw_ffmpeg = RawFFmpegRender(debug=self.debug)

    def render_helper(self, video: VideoInfo, danmaku: str, output: str, to_stdout: bool = False, logfile=None):
        video_path=video.path
        ffmpeg_args = [self.ffmpeg if self.ffmpeg else 'ffmpeg', '-y']
        ffmpeg_args += self.hwaccel_args
        # 渲染前后执行的Python脚本
        if self.before_cmd:
            self._execute_python_script(self.before_cmd, video, "before_render")

        extra_inputs = []

        for i, extra_input in enumerate(self.extra_inputs):
            extra_inputs.append(replace_keywords(extra_input, video))

        if self.output_resize:
            if 'x' in str(self.output_resize):
                scale_args = ['-s', self.output_resize]
            else:
                w, h = FFprobe.get_resolution(video_path)
                if not (h and w):
                    self.logger.warning(f'获取视频 {video_path} 分辨率失败, 将使用默认分辨率 1920x1080.')
                    w, h = 1920, 1080
                scale = float(self.output_resize)
                w, h = int(w*scale), int(h*scale)
                scale_args = ['-s', f'{w}x{h}']
        else:
            scale_args = ['-noautoscale']

        if platform.system().lower() == 'windows':
            danmaku = danmaku.replace("\\", "/").replace(":/", "\\:/")

        video['danmaku'] = danmaku
        # 自定义video filter
        if self.advanced_render_args.get('filter_complex'):
            filter_name = '-filter_complex'
            filter_str = self.advanced_render_args.get('filter_complex')
            filter_str = replace_keywords(filter_str, video).replace("\n", "").replace("\r", "")
        else:
            filter_name = '-vf'
            filter_str = 'subtitles=filename=\'%s\'' % danmaku
        
        ffmpeg_args += [
            '-fflags', '+discardcorrupt+genpts',
            '-analyzeduration', '2147483647', '-probesize', '2147483647',
            '-i', video_path,
            *extra_inputs,
            filter_name, filter_str,

            '-c:v', self.vencoder,
            *self.vencoder_args,
            '-c:a', self.aencoder,
            *self.aencoder_args,
            *scale_args,
            output,
        ]
        status, info = self.raw_ffmpeg.call_ffmpeg(ffmpeg_args)

        if self.after_cmd:
            self._execute_python_script(self.after_cmd, video, "after_render")

        return status, info

    def render_one(self, video: VideoInfo, output: str, **kwargs):
        if not exists(video.path):
            raise RuntimeError(f'不存在视频文件 {video.path}，跳过渲染.')
        danmaku = kwargs.get('danmaku') or video.dm_file_id
        if not danmaku or not exists(danmaku):
            raise RuntimeError(f'不存在弹幕文件 {danmaku}，跳过渲染.')

        valid_output = safe_filename(output)
        if valid_output != output:
            self.logger.warning(f'输出文件名 {output} 不合法或已存在，已更改为 {valid_output}.')
            output = valid_output   

        start_time = video.ctime
        status, info = self.render_helper(video, danmaku, output, **kwargs)
        if status:
            output_info:VideoInfo = copy.deepcopy(video)
            output_info.dtype = 'dm_video'
            output_info.path = output
            output_info.file_id = uuid()
            output_info.size = os.path.getsize(output)
            output_info.ctime = start_time
            output_info.dm_file_id = danmaku
            output_info.src_video_id = video.file_id
            return status, output_info
        else:
            return status, info

    def _execute_python_script(self, script: str, video: VideoInfo, script_name: str):
        """执行Python脚本"""
        try:
            # 替换脚本中的关键词
            processed_script = replace_keywords(script, video)
            
            # 准备执行环境
            exec_globals = {
                'video': video,
                'os': os,
                'platform': platform,
                'logger': self.logger,
            }
            exec_locals = {}
            
            # 执行脚本
            self.logger.info(f"执行 {script_name} Python脚本")
            exec(processed_script, exec_globals, exec_locals)
            
        except Exception as e:
            self.logger.error(f"执行 {script_name} Python脚本时发生错误: {str(e)}")
            raise

    def stop(self):
        self.logger.debug('ffmpeg render stop.')
        self.raw_ffmpeg.stop()
