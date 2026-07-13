from datetime import datetime
import logging
import os
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
        self.logger = logging.getLogger(__name__)
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
        self.repeat_color = str(self.repeat_danmaku.get('color', 'ff9900')).lstrip('#').zfill(6)
        self.repeat_hot_line_spacing = max(
            float(self.repeat_danmaku.get('hot_line_spacing', 1.25)),
            1.0,
        )
        self.repeat_hot_max_width = min(
            max(float(self.repeat_danmaku.get('hot_max_width', 0.85)), 0.1),
            1.0,
        )
        self.repeat_hot_width_safety = max(
            float(self.repeat_danmaku.get('hot_width_safety', 1.12)),
            1.0,
        )
        self.repeat_pre_hot_max_width = min(
            max(float(self.repeat_danmaku.get('pre_hot_max_width', 0.75)), 0.0),
            1.0,
        )
        self.repeat_hot_min_font_scale = max(
            float(self.repeat_danmaku.get('hot_min_font_scale', 1.0)),
            0.1,
        )
        self.repeat_hot_max_visible = max(
            int(self.repeat_danmaku.get('hot_max_visible', 0)),
            0,
        )
        self.repeat_hot_ellipsis = bool(self.repeat_danmaku.get('hot_ellipsis', True))
        self.repeat_fonts_dir = os.path.abspath(
            os.path.expanduser(str(self.repeat_danmaku.get('fonts_dir', './fonts')))
        )
        self._font_path_cache = {}
        self._font_measure_cache = {}
        self.hot_levels = self._load_hot_levels()
        self.repeat_hot_threshold = self.hot_levels[0]['threshold']
        if self.repeat_hot_threshold < self.repeat_color_threshold:
            raise ValueError('the first hot_levels.threshold must be >= color_threshold')
        self.kwargs = kwargs

        self._lock = threading.Lock()
        self._super_chat_tails = []  # 初始化 _super_chat_tails 属性
        self._super_chat_state = 0
        self._latest_end_time = 0
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

    @staticmethod
    def _normalize_color(value, field):
        color = str(value).lstrip('#')
        if len(color) != 6:
            raise ValueError(f'{field} must be a 6-digit RGB hexadecimal color')
        try:
            int(color, 16)
        except ValueError as exc:
            raise ValueError(f'{field} must be a 6-digit RGB hexadecimal color') from exc
        return color.lower()

    def _font_family_from_file(self, font_file):
        path = os.path.expanduser(str(font_file))
        if not os.path.isabs(path):
            path = os.path.join(self.repeat_fonts_dir, path)
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            self.logger.warning('热点字体文件不存在，将使用配置字体: %s', path)
            return None
        try:
            from PIL import ImageFont
            return ImageFont.truetype(path, 16).getname()[0]
        except Exception as exc:
            self.logger.warning('无法读取热点字体 %s，将使用配置字体: %s', path, exc)
            return None

    def _resolve_font_path(self, family, font_file=None):
        cache_key = (str(family).lower(), str(font_file or '').lower())
        if cache_key in self._font_path_cache:
            return self._font_path_cache[cache_key]
        candidates = []
        if font_file:
            path = os.path.expanduser(str(font_file))
            candidates.append(path if os.path.isabs(path) else os.path.join(self.repeat_fonts_dir, path))
        if os.path.isdir(self.repeat_fonts_dir):
            candidates.extend(
                os.path.join(self.repeat_fonts_dir, name)
                for name in os.listdir(self.repeat_fonts_dir)
                if name.lower().endswith(('.ttf', '.otf', '.ttc'))
            )
        system_fonts = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts')
        if os.path.isdir(system_fonts):
            candidates.extend(
                os.path.join(system_fonts, name)
                for name in os.listdir(system_fonts)
                if name.lower().endswith(('.ttf', '.otf', '.ttc'))
            )
        try:
            from PIL import ImageFont
            wanted = str(family).casefold()
            fallback = None
            for path in candidates:
                if not os.path.isfile(path):
                    continue
                if font_file and os.path.basename(path).casefold() == os.path.basename(str(font_file)).casefold():
                    self._font_path_cache[cache_key] = path
                    return path
                try:
                    names = ImageFont.truetype(path, 16).getname()
                    if wanted in (str(value).casefold() for value in names):
                        fallback = path
                        break
                except Exception:
                    continue
            self._font_path_cache[cache_key] = fallback
            return fallback
        except Exception:
            self._font_path_cache[cache_key] = None
            return None

    def _load_hot_levels(self):
        legacy_effect = self.repeat_danmaku.get('hot_update_effect', {})
        if not isinstance(legacy_effect, dict):
            legacy_effect = {}
        legacy_pulse = {
            'enabled': bool(legacy_effect.get('enabled', True)),
            'scale': max(float(legacy_effect.get('pulse_scale', 1.25)), 1.0),
            'duration_ms': max(int(float(legacy_effect.get('duration', 0.25)) * 1000), 0),
            'glow_size': max(float(legacy_effect.get('glow_size', 4)), float(self.outlinesize)),
            'glow_blur': max(float(legacy_effect.get('glow_blur', 3)), 0),
        }
        base = {
            'font': self.font,
            'font_file': None,
            'color': self._normalize_color(
                self.repeat_danmaku.get('hot_color', 'ff0000'), 'hot_color'
            ),
            'font_scale': max(float(self.repeat_danmaku.get('hot_font_scale', 1.35)), 0.1),
            'count_font_scale': min(
                max(float(self.repeat_danmaku.get('hot_count_font_scale', 0.78)), 0.1), 1.0
            ),
            'outline_color': self._normalize_color(self.outlinecolor, 'outlinecolor'),
            'outline_size': max(float(self.outlinesize), 0),
            'suffix': str(self.repeat_danmaku.get('hot_suffix', '🔥')),
            'pulse': legacy_pulse,
        }
        configured = self.repeat_danmaku.get('hot_levels')
        if not configured:
            level = dict(base)
            level['pulse'] = dict(base['pulse'])
            level['threshold'] = max(int(self.repeat_danmaku.get('hot_threshold', 10)), 1)
            return [level]
        if not isinstance(configured, list):
            raise ValueError('repeat_danmaku.hot_levels must be a list')

        levels = []
        inherited = base
        previous_threshold = 0
        for index, item in enumerate(configured):
            if not isinstance(item, dict) or 'threshold' not in item:
                raise ValueError(f'hot_levels[{index}] must contain threshold')
            threshold = max(int(item['threshold']), 1)
            if threshold <= previous_threshold:
                raise ValueError('hot_levels thresholds must be strictly increasing')
            level = dict(inherited)
            level['pulse'] = dict(inherited['pulse'])
            level['threshold'] = threshold
            for key in ('font', 'suffix'):
                if key in item:
                    level[key] = str(item[key])
                    if key == 'font':
                        level['font_file'] = None
            if item.get('font_file'):
                level['font_file'] = str(item['font_file'])
                level['font'] = self._font_family_from_file(item['font_file']) or level['font']
            if 'color' in item:
                level['color'] = self._normalize_color(item['color'], f'hot_levels[{index}].color')
            if 'outline_color' in item:
                level['outline_color'] = self._normalize_color(
                    item['outline_color'], f'hot_levels[{index}].outline_color'
                )
            for key in ('font_scale', 'count_font_scale', 'outline_size'):
                if key in item:
                    level[key] = max(float(item[key]), 0.1 if key != 'outline_size' else 0)
            if 'pulse' in item:
                pulse = item['pulse'] if isinstance(item['pulse'], dict) else {}
                level['pulse'].update(pulse)
            level['pulse']['enabled'] = bool(level['pulse'].get('enabled', False))
            level['pulse']['scale'] = max(float(level['pulse'].get('scale', 1.08)), 1.0)
            level['pulse']['duration_ms'] = max(int(level['pulse'].get('duration_ms', 600)), 0)
            level['pulse']['glow_size'] = max(
                float(level['pulse'].get('glow_size', level['outline_size'])),
                level['outline_size'],
            )
            level['pulse']['glow_blur'] = max(float(level['pulse'].get('glow_blur', 0)), 0)
            levels.append(level)
            inherited = level
            previous_threshold = threshold
        return levels

    def _style_for_count(self, count):
        style = self.hot_levels[0]
        for level in self.hot_levels:
            if count < level['threshold']:
                break
            style = level
        return style

    @staticmethod
    def _text_units(text):
        units = 0.0
        for char in str(text):
            if unicodedata.combining(char):
                continue
            width = unicodedata.east_asian_width(char)
            if width in ('W', 'F') or unicodedata.category(char) in ('So', 'Sk'):
                units += 1.0
            elif ord(char) < 128:
                units += 0.55
            else:
                units += 0.8
        return units

    def _measure_text(self, text, size, style):
        path = self._resolve_font_path(style['font'], style.get('font_file'))
        if path:
            try:
                from PIL import ImageFont
                key = (path, int(size))
                font = self._font_measure_cache.get(key)
                if font is None:
                    font = ImageFont.truetype(path, int(size))
                    self._font_measure_cache[key] = font
                return float(font.getlength(str(text)))
            except Exception:
                pass
        return self._text_units(text) * size

    def _fit_pre_hot_content(self, text):
        if not self.repeat_pre_hot_max_width:
            return text
        max_width = self.width * self.repeat_pre_hot_max_width
        if self._text_units(text) * self.fontsize <= max_width:
            return text
        kept = []
        used = self._text_units('…') * self.fontsize
        for char in str(text):
            char_width = self._text_units(char) * self.fontsize
            if used + char_width > max_width:
                break
            kept.append(char)
            used += char_width
        return ''.join(kept).rstrip() + '…'

    def _fit_hot_content(self, text, count, style):
        text = str(text)
        suffix = f" ×{count} {style['suffix']}"
        base_size = max(int(round(self.fontsize * style['font_scale'])), 1)
        count_size = max(int(round(base_size * style['count_font_scale'])), 1)
        pulse_scale = style['pulse']['scale'] if style['pulse']['enabled'] else 1.0
        edge_padding = self.margin_w * 2 + max(
            style['outline_size'], style['pulse']['glow_size'] if style['pulse']['enabled'] else 0
        ) * 2
        max_width = max(
            (self.width * self.repeat_hot_max_width - edge_padding) / pulse_scale,
            self.width * 0.1,
        )

        def estimated_width(body, body_size, small_size):
            return (
                self._measure_text(body, body_size, style)
                + self._measure_text(suffix, small_size, style)
            ) * self.repeat_hot_width_safety

        width = estimated_width(text, base_size, count_size)
        min_size = max(int(round(self.fontsize * self.repeat_hot_min_font_scale)), 1)
        if width > max_width:
            ratio = max_width / width
            base_size = max(int(base_size * ratio), min_size)
            count_size = max(int(round(base_size * style['count_font_scale'])), 1)
            width = estimated_width(text, base_size, count_size)

        if width > max_width and self.repeat_hot_ellipsis:
            ellipsis = '…'
            available = max_width / self.repeat_hot_width_safety - self._measure_text(
                suffix, count_size, style
            )
            kept = []
            used = self._measure_text(ellipsis, base_size, style)
            for char in text:
                char_width = self._measure_text(char, base_size, style)
                if used + char_width > available:
                    break
                kept.append(char)
                used += char_width
            text = ''.join(kept).rstrip() + ellipsis
            width = estimated_width(text, base_size, count_size)
        return text, suffix, base_size, count_size, width

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
        display_content = danmu.text.replace('\n',' ').replace('\r',' ')
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
            display_content = self._fit_pre_hot_content(display_content)

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
        
        dm_length = self._get_length(display_content)
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
        content = display_content
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
                        'first_hot': events[0][0],
                        'key': session['key'],
                        'style': self._style_for_count(count),
                    })
        if not segments:
            return

        # 按所有热点的出现、更新和结束时间切片，使每个时间片中的热点组整体居中。
        boundaries = sorted({
            boundary
            for segment in segments
            for boundary in (segment['start'], segment['end'])
        })
        region_top = self.dst
        region_bottom = self.dst + (self.height - self.dst) * self.dmrate
        region_height = region_bottom - region_top
        center_y = self.dst + ((self.height - self.dst) * self.dmrate) / 2
        lines = []
        for start, end in zip(boundaries, boundaries[1:]):
            if end <= start:
                continue
            # 热度优先；同数量时优先最近更新，其次优先更早成为热点，最后按文本稳定排序。
            ranked = sorted(
                (segment for segment in segments if segment['start'] <= start < segment['end']),
                key=lambda segment: (
                    -segment['count'], -segment['start'], segment['first_hot'], segment['key']
                ),
            )
            prepared = []
            occupied_height = 0.0
            for segment in ranked:
                if self.repeat_hot_max_visible and len(prepared) >= self.repeat_hot_max_visible:
                    break
                style = segment['style']
                body, suffix, body_size, count_size, _ = self._fit_hot_content(
                    segment['text'], segment['count'], style
                )
                pulse_scale = style['pulse']['scale'] if style['pulse']['enabled'] else 1.0
                outline = max(
                    style['outline_size'],
                    style['pulse']['glow_size'] if style['pulse']['enabled'] else 0,
                )
                item_height = (
                    body_size * pulse_scale * self.repeat_hot_line_spacing
                    + outline * 2 + self.margin_h
                )
                if occupied_height + item_height > region_height:
                    continue
                item = dict(segment)
                item.update({
                    'body': body,
                    'suffix': suffix,
                    'body_size': body_size,
                    'count_size': count_size,
                    'height': item_height,
                })
                prepared.append(item)
                occupied_height += item_height

            # 排名第一放在中心，后续热点依次向上、向下展开。
            upper = list(reversed(prepared[1::2]))
            visual = upper + prepared[:1] + prepared[2::2]
            cursor_y = center_y - occupied_height / 2
            for segment in visual:
                y = cursor_y + segment['height'] / 2
                cursor_y += segment['height']
                style = segment['style']
                t0 = '%02d:%02d:%05.2f' % sec2hms(start)
                t1 = '%02d:%02d:%05.2f' % sec2hms(end)
                content = (
                    f"{segment['body']}"
                    f"{{\\fs{segment['count_size']}}}{segment['suffix']}"
                )
                effect = ''
                # 仅在该热点自身的计数更新时触发；其他热点造成的时间切片不重复触发。
                pulse = style['pulse']
                if pulse['enabled'] and start == segment['start']:
                    effect_ms = min(pulse['duration_ms'], int((end - start) * 1000))
                    if effect_ms > 0:
                        pulse_percent = pulse['scale'] * 100
                        effect = (
                            f'\\fscx{pulse_percent:g}\\fscy{pulse_percent:g}'
                            f"\\bord{pulse['glow_size']:g}"
                            f"\\blur{pulse['glow_blur']:g}"
                            f'\\t(0,{effect_ms},'
                            f'\\fscx100\\fscy100'
                            f"\\bord{style['outline_size']:g}\\blur0)"
                        )
                font = self._escape_ass_text(style['font'])
                lines.append(
                    f'Dialogue: 2,{t0},{t1},R2L,,0,0,0,,'
                    f'{{\\an5\\pos({self.width // 2},{int(round(y))})'
                    f"\\fn{font}\\fs{segment['body_size']}\\b1\\alpha&H{self.opacity}"
                    f"\\1c&H{RGB2BGR(style['color'])}&"
                    f"\\3c&H{RGB2BGR(style['outline_color'])}&"
                    f"\\bord{style['outline_size']:g}"
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
