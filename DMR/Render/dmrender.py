import copy
import logging
import os
import platform
import tempfile
from .baserender import BaseRender
from .ffmpeg import RawFFmpegRender
from .danmaku_timeline import generate_timeline_png, merge_timeline_filter
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
                 danmaku_timeline:dict=None,
                 fonts_dir:str=None,
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
        self.danmaku_timeline = danmaku_timeline if isinstance(danmaku_timeline, dict) else {}
        self.fonts_dir = fonts_dir

        self.logger = logging.getLogger(__name__)
        self.raw_ffmpeg = RawFFmpegRender(debug=self.debug)

    def render_helper(self, video: VideoInfo, danmaku: str, output: str, to_stdout: bool = False, logfile=None):
        video_path=video.path
        ffmpeg_args = [self.ffmpeg if self.ffmpeg else 'ffmpeg', '-y']
        ffmpeg_args += self.hwaccel_args
        timeline_png = None
        timeline_progress_png = None
        # 渲染前后执行的Python脚本
        if self.before_cmd:
            self._execute_python_script(self.before_cmd, video, "before_render")

        try:
            extra_inputs = []

            for i, extra_input in enumerate(self.extra_inputs):
                extra_inputs.append(replace_keywords(extra_input, video))

            source_w, source_h = FFprobe.get_resolution(video_path)
            if not (source_h and source_w):
                self.logger.warning(f'获取视频 {video_path} 分辨率失败, 将使用默认分辨率 1920x1080.')
                source_w, source_h = 1920, 1080
            output_w, output_h = source_w, source_h

            if self.output_resize:
                if 'x' in str(self.output_resize):
                    output_w, output_h = [int(x) for x in str(self.output_resize).lower().split('x', 1)]
                    scale_args = ['-s', self.output_resize]
                else:
                    scale = float(self.output_resize)
                    output_w, output_h = int(source_w*scale), int(source_h*scale)
                    scale_args = ['-s', f'{output_w}x{output_h}']
            else:
                scale_args = ['-noautoscale']

            render_danmaku = danmaku
            if platform.system().lower() == 'windows':
                render_danmaku = render_danmaku.replace("\\", "/").replace(":/", "\\:/")

            video['danmaku'] = render_danmaku
            default_danmaku_filter = 'subtitles=filename=\'%s\'' % render_danmaku
            fonts_filter_option = ''
            if self.fonts_dir:
                render_fonts_dir = os.path.abspath(os.path.expanduser(str(self.fonts_dir)))
                if os.path.isdir(render_fonts_dir):
                    if platform.system().lower() == 'windows':
                        render_fonts_dir = render_fonts_dir.replace("\\", "/").replace(":/", "\\:/")
                    render_fonts_dir = render_fonts_dir.replace("'", "\\'")
                    fonts_filter_option = ":fontsdir='%s'" % render_fonts_dir
                    default_danmaku_filter += fonts_filter_option
                else:
                    self.logger.warning('ASS字幕字体目录不存在，将只使用系统字体: %s', render_fonts_dir)
            timeline_scale_filter = f'scale={output_w}:{output_h}' if self.output_resize else None
            filter_merged = False
            timeline_enabled = self.danmaku_timeline.get('enabled', False)

            # 自定义video filter
            if self.advanced_render_args.get('filter_complex'):
                filter_name = '-filter_complex'
                filter_str = self.advanced_render_args.get('filter_complex')
                filter_str = replace_keywords(filter_str, video).replace("\n", "").replace("\r", "")
                if fonts_filter_option and 'fontsdir=' not in filter_str:
                    subtitle_filter = "subtitles=filename='%s'" % render_danmaku
                    filter_str = filter_str.replace(
                        subtitle_filter,
                        subtitle_filter + fonts_filter_option,
                        1,
                    )
            else:
                filter_name = '-vf'
                filter_str = default_danmaku_filter

            if timeline_enabled:
                duration = video.duration
                try:
                    duration = float(duration)
                except (TypeError, ValueError):
                    duration = -1
                if duration <= 0:
                    duration = FFprobe.get_duration(video_path)
                if duration <= 0:
                    self.logger.warning(f'获取视频 {video_path} 时长失败，跳过弹幕密度时间轴.')
                else:
                    timeline_file = tempfile.NamedTemporaryFile(prefix='dmr_danmaku_timeline_', suffix='.png', delete=False)
                    timeline_png = timeline_file.name
                    timeline_file.close()
                    timeline_progress_file = tempfile.NamedTemporaryFile(prefix='dmr_danmaku_timeline_progress_', suffix='.png', delete=False)
                    timeline_progress_png = timeline_progress_file.name
                    timeline_progress_file.close()
                    generate_timeline_png(
                        danmaku,
                        timeline_png,
                        duration,
                        (output_w, output_h),
                        self.danmaku_timeline,
                        timeline_progress_png,
                    )
                    timeline_input_index = 1 + sum(1 for arg in extra_inputs if str(arg) == '-i')
                    timeline_progress_input_index = timeline_input_index + 1
                    merged_filter, filter_merged, warning = merge_timeline_filter(
                        filter_str if filter_name == '-filter_complex' else None,
                        timeline_input_index,
                        timeline_progress_input_index,
                        duration,
                        output_w,
                        output_h,
                        self.danmaku_timeline,
                        default_danmaku_filter,
                        timeline_scale_filter,
                    )
                    if warning:
                        self.logger.warning(warning)
                    if filter_merged:
                        extra_inputs += [
                            '-loop', '1', '-t', duration, '-i', timeline_png,
                            '-loop', '1', '-t', duration, '-i', timeline_progress_png,
                        ]
                        filter_name = '-filter_complex'
                        filter_str = merged_filter
                        if timeline_scale_filter:
                            scale_args = ['-noautoscale']
                    else:
                        os.remove(timeline_png)
                        timeline_png = None
                        os.remove(timeline_progress_png)
                        timeline_progress_png = None

            ffmpeg_args += [
                '-fflags', '+discardcorrupt+genpts',
                '-analyzeduration', '2147483647', '-probesize', '2147483647',
                '-i', video_path,
                *extra_inputs,
                filter_name, filter_str,
            ]
            if filter_merged:
                ffmpeg_args += ['-map', '[vout]', '-map', '0:a?']

            ffmpeg_args += [
                '-c:v', self.vencoder,
                *self.vencoder_args,
                '-c:a', self.aencoder,
                *self.aencoder_args,
                *scale_args,
                output,
            ]
            status, info = self.raw_ffmpeg.call_ffmpeg(ffmpeg_args)
        finally:
            if timeline_png and exists(timeline_png):
                try:
                    os.remove(timeline_png)
                except OSError as e:
                    self.logger.warning(f'清理弹幕密度时间轴临时文件失败: {e}')
            if timeline_progress_png and exists(timeline_progress_png):
                try:
                    os.remove(timeline_progress_png)
                except OSError as e:
                    self.logger.warning(f'清理弹幕密度时间轴临时文件失败: {e}')

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
