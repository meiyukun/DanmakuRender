from datetime import datetime
import threading
import unicodedata
from DMR.utils import *

__all__ = ['AssWriter']

class AssWriter():
    """
    ASS弹幕写入器，定义了ASS弹幕格式和信息，用于流式处理弹幕
    """
    def __init__(self,
                 description:str,
                 width:int,
                 height:int,
                 dst:int,
                 dmrate:float,
                 font:str,
                 fontsize:int,
                 margin_h:int,
                 margin_w:int,
                 dmduration:float,
                 opacity:float,
                 auto_fontsize:bool,
                 outlinecolor:str,
                 outlinesize:int,
                 dm_template:dict=None,
                 repeat_danmaku:dict=None,
                 **kwargs) -> None:
        self.description = description
        self.height = height
        self.width = width
        self.dmrate = dmrate
        if auto_fontsize:
            self.fontsize = int(height / 1080 * fontsize)
        else:
            self.fontsize = int(fontsize)
        self.font = font

        self.margin_h = margin_h if margin_h > 1 else margin_h * self.height
        self.margin_w = margin_w if margin_w > 1 else margin_w * self.width
        self.dst = dst
        self.dmduration = dmduration
        self.opacity = hex(255-int(opacity*255))[2:].zfill(2)
        self.outlinecolor = str(outlinecolor).zfill(6)
        self.outlinesize = outlinesize
        self.ass_text_template = dm_template.get('ass_text') if dm_template else None
        self.repeat_danmaku = repeat_danmaku if isinstance(repeat_danmaku, dict) else {}
        self.repeat_enabled = bool(self.repeat_danmaku.get('enabled', False))
        self.repeat_timeout = max(float(self.repeat_danmaku.get('session_timeout', 8)), 0.1)
        self.repeat_color_threshold = max(int(self.repeat_danmaku.get('color_threshold', 6)), 1)
        self.repeat_hot_threshold = max(int(self.repeat_danmaku.get('hot_threshold', 10)), 1)
        if self.repeat_hot_threshold < self.repeat_color_threshold:
            raise ValueError('repeat_danmaku.hot_threshold must be >= color_threshold')
        self.repeat_color = str(self.repeat_danmaku.get('color', 'ff9900')).lstrip('#').zfill(6)
        self.repeat_hot_color = str(
            self.repeat_danmaku.get('hot_color', 'ff0000')
        ).lstrip('#').zfill(6)
        self.repeat_hot_font_scale = max(
            float(self.repeat_danmaku.get('hot_font_scale', 1.35)),
            1.0,
        )
        self.repeat_hot_count_font_scale = min(
            max(float(self.repeat_danmaku.get('hot_count_font_scale', 0.78)), 0.1),
            1.0,
        )
        self.repeat_hot_line_spacing = max(
            float(self.repeat_danmaku.get('hot_line_spacing', 1.25)),
            1.0,
        )
        self.repeat_hot_suffix = str(self.repeat_danmaku.get('hot_suffix', '🔥'))
        hot_update_effect = self.repeat_danmaku.get('hot_update_effect', {})
        if not isinstance(hot_update_effect, dict):
            hot_update_effect = {}
        self.hot_effect_enabled = bool(hot_update_effect.get('enabled', True))
        self.hot_effect_duration = max(float(hot_update_effect.get('duration', 0.25)), 0)
        self.hot_effect_pulse_scale = max(
            float(hot_update_effect.get('pulse_scale', 1.25)),
            1.0,
        )
        self.hot_effect_glow_size = max(
            float(hot_update_effect.get('glow_size', 4)),
            float(self.outlinesize),
        )
        self.hot_effect_glow_blur = max(
            float(hot_update_effect.get('glow_blur', 3)),
            0,
        )
        self.kwargs = kwargs

        self._lock = threading.Lock()
        self._super_chat_tails = []  # 初始化 _super_chat_tails 属性
        self._super_chat_state = 0
        self._latest_end_time = 0
        self._hot_fontsize = max(int(self.fontsize * self.repeat_hot_font_scale), self.fontsize)
        self._hot_count_fontsize = max(int(round(self._hot_fontsize * self.repeat_hot_count_font_scale)), 1)
        self._ntracks = max(
            int(((self.height - self.dst) * self.dmrate) / (self.fontsize + self.margin_h)),
            1,
        )
        self._repeat_sessions = {}
        self._completed_hot_sessions = []

        self.meta_info = [
            '[Script Info]',
            f'Title: {self.description}',
            'ScriptType: v4.00+',
            'Collisions: Normal',
            f'PlayResX: {self.width}',
            f'PlayResY: {self.height}',
            'Timer: 100.0000',
            '',
            '[V4+ Styles]',
            'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
            f'Style: R2L,{self.font},{self.fontsize},&H{self.opacity}FFFFFF,&H{self.opacity}000000,&H{self.opacity}{self.outlinecolor},&H4F0000FF,-1,0,0,0,100,100,0,0,1,{self.outlinesize},0,1,0,0,0,0',
            f'Style: message_box,Microsoft YaHei,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H1E6A5149,1,0,0,0,100.00,100.00,0.00,0.00,1,1,0,7,0,0,0,1',
            '',
            '[Events]',
            'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text',
        ]
    
    def _get_length(self, string:str):
        length = 0
        for s in string:
            if len(s.encode('utf-8')) == 1:
                length += 0.5*self.fontsize
            else:
                length += self.fontsize
        return int(length)

    def open(self, filename):
        with self._lock:
            if hasattr(self, '_filename'):
                self._flush_repeat_sessions()
            self._filename = filename
            self._track_tails = [None for _ in range(self._ntracks)]
            self._repeat_sessions = {}
            self._completed_hot_sessions = []
            with open(filename,'w',encoding='utf-8') as f:
                for info in self.meta_info:
                    f.write(info+'\n')

    def add(self, danmu, **kwargs):
        if isinstance(danmu, SuperChatDanmaku):
            return self.add_super_chat(danmu)
        elif isinstance(danmu, SimpleDanmaku):
            return self.add_simple(danmu, **kwargs)
        return False

    def add_simple(self, danmu:SimpleDanmaku, calc_collision=True):
        """
        添加弹幕到ASS文件 
        danmu: 待添加弹幕
        calc_collision: 是否计算冲突，冲突的弹幕将会被自动忽略
        """
        display_color = danmu.color
        if self.repeat_enabled:
            self._expire_repeat_sessions(danmu.time)
            session = self._get_repeat_session(danmu)
            session['count'] += 1
            session['last_time'] = danmu.time
            count = session['count']
            if count >= self.repeat_hot_threshold:
                session['hot_events'].append((danmu.time, count))
                return True
            if count >= self.repeat_color_threshold:
                display_color = self.repeat_color

        tid, max_dist = 0, -1e5
        
        # 计算给出弹幕到指定弹幕的距离
        def tail_dist(tail_dm:SimpleDanmaku, tic:float):
            if not tail_dm:
                return 1e5
            dm_length = self._get_length(tail_dm.text)
            dist = (tic - tail_dm.time) * (dm_length + self.width) / self.dmduration - dm_length 
            return dist
        
        for i, tail_dm in enumerate(self._track_tails):
            dist = tail_dist(tail_dm, danmu.time)
            if dist > 0.2 * self.width and dist > self.margin_w:
                tid = i
                max_dist = dist
                break
            if dist > max_dist:
                max_dist = dist
                tid = i
        
        if calc_collision and max_dist < self.margin_w:
            return False
        
        dm_length = self._get_length(danmu.text)
        x0 = self.width
        x1 = -dm_length
        y = self.fontsize + (self.fontsize + self.margin_h) * tid

        t0 = danmu.time
        t1 = t0 + self.dmduration

        t0 = '%02d:%02d:%05.2f'%sec2hms(t0)
        t1 = '%02d:%02d:%05.2f'%sec2hms(t1)
        
        # set ass Dialogue
        dm_info = f'Dialogue: 0,{t0},{t1},R2L,,0,0,0,,'
        dm_info += '{\\move(%d,%d,%d,%d)}'%(x0, y + self.dst, x1, y + self.dst)
        dm_info += '{\\alpha&H%s\\1c&H%s&}'%(self.opacity, RGB2BGR(display_color))
        content = danmu.text.replace('\n',' ').replace('\r',' ')
        if not self.ass_text_template:
            dm_info += content
        else:
            dm_info += replace_keywords(self.ass_text_template, danmu)

        with self._lock, open(self._filename, 'a', encoding='utf-8') as f:
            f.write(dm_info + '\n')
        
        self._track_tails[tid] = danmu
        return True

    @staticmethod
    def _normalize_repeat_text(text):
        text = unicodedata.normalize('NFKC', str(text))
        text = ''.join(text.split())
        if not text:
            return ''

        # 连续重复的字符只保留一个，使“？？？”、“😂😂😂”、“哈哈哈”和
        # 它们的单字符形式能够进入同一计数会话。
        collapsed = ''.join(
            char for index, char in enumerate(text)
            if index == 0 or char != text[index - 1]
        )
        text_chars = [
            char for char in collapsed
            if unicodedata.category(char)[0] in ('L', 'N')
        ]
        emoji_chars = [
            char for char in collapsed
            if unicodedata.category(char) in ('So', 'Sk')
        ]
        punctuation_chars = [
            char for char in collapsed
            if unicodedata.category(char)[0] in ('P', 'S')
            and char not in emoji_chars
        ]

        # 对“😂！？、啊？、？？？”这类短反应提取符号核心。最多允许夹带
        # 一个文字或数字，避免把“为什么？”等正常句子归入问号热点。
        if len(text_chars) <= 1:
            if emoji_chars:
                return 'reaction:' + ''.join(emoji_chars)
            if punctuation_chars:
                return 'reaction:' + ''.join(punctuation_chars)

        return 'text:' + collapsed

    @staticmethod
    def _escape_ass_text(text):
        return str(text).replace('\\', '＼').replace('{', '｛').replace('}', '｝')

    def _get_repeat_session(self, danmu):
        key = self._normalize_repeat_text(danmu.text)
        session = self._repeat_sessions.get(key)
        if session is None:
            session = {
                'key': key,
                'text': danmu.text,
                'count': 0,
                'last_time': danmu.time,
                'hot_events': [],
            }
            self._repeat_sessions[key] = session
        elif key.startswith('reaction:') and len(danmu.text) > len(session['text']):
            # 热点使用本轮中信息更完整的变体，例如优先显示“？？？”而不是“？”。
            session['text'] = danmu.text
        return session

    def _expire_repeat_sessions(self, current_time):
        expired = [
            key for key, session in self._repeat_sessions.items()
            if current_time - session['last_time'] >= self.repeat_timeout
        ]
        for key in expired:
            session = self._repeat_sessions.pop(key)
            if session['hot_events']:
                self._completed_hot_sessions.append(session)

    def _flush_repeat_sessions(self):
        self._completed_hot_sessions.extend(
            session for session in self._repeat_sessions.values()
            if session['hot_events']
        )
        self._repeat_sessions.clear()
        self._write_hot_sessions()
        self._completed_hot_sessions.clear()

    def _write_hot_sessions(self):
        segments = []
        for session in self._completed_hot_sessions:
            events = session['hot_events']
            safe_text = self._escape_ass_text(session['text'])
            for index, (start, count) in enumerate(events):
                end = (
                    events[index + 1][0]
                    if index + 1 < len(events)
                    else session['last_time'] + self.repeat_timeout
                )
                if end > start:
                    segments.append({
                        'start': start,
                        'end': end,
                        'count': count,
                        'text': safe_text,
                        'order': (events[0][0], session['key']),
                    })
        if not segments:
            return

        # 按所有热点的出现、更新和结束时间切片，使每个时间片中的热点组整体居中。
        boundaries = sorted({
            boundary
            for segment in segments
            for boundary in (segment['start'], segment['end'])
        })
        center_y = self.dst + ((self.height - self.dst) * self.dmrate) / 2
        # ASS 字形高度可能超过字号本身，尤其是描边和 emoji 回退字体，
        # 因此热点使用独立的行距倍率，避免视觉边界相互覆盖。
        line_height = self._hot_fontsize * self.repeat_hot_line_spacing + self.margin_h
        lines = []
        for start, end in zip(boundaries, boundaries[1:]):
            if end <= start:
                continue
            active = sorted(
                (
                    segment for segment in segments
                    if segment['start'] <= start < segment['end']
                ),
                key=lambda segment: segment['order'],
            )
            for index, segment in enumerate(active):
                y = center_y + (index - (len(active) - 1) / 2) * line_height
                t0 = '%02d:%02d:%05.2f' % sec2hms(start)
                t1 = '%02d:%02d:%05.2f' % sec2hms(end)
                content = (
                    f"{segment['text']}"
                    f"{{\\fs{self._hot_count_fontsize}}} ×{segment['count']}"
                    f" {self.repeat_hot_suffix}"
                )
                effect = ''
                # 仅在该热点自身的计数更新时触发；其他热点造成的时间切片不重复触发。
                if self.hot_effect_enabled and start == segment['start']:
                    effect_ms = int(min(self.hot_effect_duration, end - start) * 1000)
                    if effect_ms > 0:
                        pulse_percent = self.hot_effect_pulse_scale * 100
                        effect = (
                            f'\\fscx{pulse_percent:g}\\fscy{pulse_percent:g}'
                            f'\\bord{self.hot_effect_glow_size:g}'
                            f'\\blur{self.hot_effect_glow_blur:g}'
                            f'\\t(0,{effect_ms},'
                            f'\\fscx100\\fscy100'
                            f'\\bord{float(self.outlinesize):g}\\blur0)'
                        )
                lines.append(
                    f'Dialogue: 2,{t0},{t1},R2L,,0,0,0,,'
                    f'{{\\an5\\pos({self.width // 2},{int(round(y))})'
                    f'\\fs{self._hot_fontsize}\\b1\\alpha&H{self.opacity}'
                    f'\\1c&H{RGB2BGR(self.repeat_hot_color)}&'
                    f'{effect}}}{content}\n'
                )

        with open(self._filename, 'a', encoding='utf-8') as f:
            f.writelines(lines)

    def add_super_chat(self, super_chat: SuperChatDanmaku):
        with self._lock:
            if not self._filename:
                raise RuntimeError("ASS file is not open.")

            # 格式化超级弹幕内容
            content_lines = []
            for i in range(0, len(super_chat.content), 15):
                content_lines.append(super_chat.content[i:i + 15])
            formatted_content = '\\N'.join(content_lines)

            # 计算当前超级弹幕数量和更新最晚结束时间
            current_time = super_chat.time
            if current_time > self._latest_end_time:
                self._super_chat_state = 0  # 重置状态
            self._super_chat_state += 1
            self._latest_end_time = current_time + 20  # 每个超级弹幕持续20秒

            # 根据当前状态计算 y 坐标
            base_y = 100
            y_offset = 120
            y = base_y + (self._super_chat_state - 1) * y_offset

            t0 = current_time
            t1 = t0 + 20  # Super Chat 持续时间固定为20秒

            t0_display = '%02d:%02d:%05.2f' %sec2hms(t0)
            t1_display = '%02d:%02d:%05.2f' %sec2hms(t1)

            # 构建 ASS 格式的弹幕信息
            dm_info = (
                f'Dialogue: 0,{t0_display},{t1_display},message_box,,0000,0000,0000,,'
                f'{{\\pos(0,{y})\\c&HFF6600\\shad0\\p1}}m 0 0 l 250 0 l 250 81 l 0 81\n'
                f'Dialogue: 0,{t0_display},{t1_display},message_box,,0000,0000,0000,,'
                f'{{\\pos(0,{y + 40})\\shad0\\p1\\c&HCC0000}}m 0 0 l 250 0 l 250 80 l 0 80\n'
                f'Dialogue: 1,{t0_display},{t1_display},message_box,,0000,0000,0000,,'
                f'{{\\pos(6,{y + 5})\\c&HFFFFFF\\fs15\\b1\\q2}}{super_chat.uname}\n'
                f'Dialogue: 1,{t0_display},{t1_display},message_box,,0000,0000,0000,,'
                f'{{\\pos(6,{y + 20})\\c&HFFFFFF\\fs15\\q2}}SuperChat CNY {super_chat.price}\n'
                f'Dialogue: 1,{t0_display},{t1_display},message_box,,0000,0000,0000,,'
                f'{{\\pos(6,{y + 40})\\c&HFFFFFF\\q2}}{formatted_content}\n'
            )

            with open(self._filename, 'a', encoding='utf-8') as f:
                f.write(dm_info)

            self._super_chat_tails.append(super_chat)

    def close(self):
        with self._lock:
            self._flush_repeat_sessions()
            del self._filename
            del self._track_tails
