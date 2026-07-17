import argparse
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from DMR.Downloader.Danmaku.asswriter import AssWriter
from DMR.utils import SimpleDanmaku


def danmaku(timestamp, text, index):
    return SimpleDanmaku(
        time=float(timestamp), dtype='danmaku', uname=f'压力测试用户{index}',
        content=text, text=text, color='ffffff',
    )


def repeated(events, text, start, end, rate, index_base):
    count = int((end - start) * rate) + 1
    for index in range(count):
        timestamp = start + index / rate
        if timestamp <= end:
            events.append((timestamp, index_base + index, text))


def build_events(duration):
    events = []

    long_hot = '这是一条故意写得非常非常长用来验证热点不会横向超出画面并且计数始终可见的八级测试弹幕'

    # 先占满所有轨道，使两个专用热点在发射前分别累积到50/100级。
    for lane in range(5):
        events.append((0.0, -100 + lane, f'预占轨道的超长普通弹幕 {lane} ' + '占位内容' * 20))
    repeated(events, '脉冲验证：五十级热点', 0.05, 0.099, 1000.0, 1_000)
    repeated(events, '脉冲验证：一百级热点', 0.12, 0.219, 1000.0, 2_000)
    repeated(events, '脉冲验证：五十级热点', 3.00, min(duration - 0.2, 9.0), 4.0, 3_000)
    repeated(events, '脉冲验证：一百级热点', 3.05, min(duration - 0.2, 9.0), 5.0, 4_000)

    # 阈值前的普通形态会占轨；间距足够后在8秒附近发射8级和20级。
    repeated(events, long_hot, 0.05, 0.121, 100.0, 10_000)
    repeated(events, '第一阶段：飞行中计数更新', 0.25, 0.321, 100.0, 20_000)
    for index in range(12):
        events.append((5.50 + index * 0.40, 21_000 + index, '第一阶段：飞行中计数更新'))
    events.append((8.30, 50_000, '调度触发普通消息 #0830'))

    # 第一阶段飞行期间累计出50级和100级，16秒时按热度排序发射。
    repeated(events, '第二阶段：五十级热点', 8.50, 15.40, 7.2, 30_000)
    repeated(events, '第二阶段：一百级热点', 8.55, 15.30, 15.0, 40_000)
    events.append((16.50, 51_000, '第二阶段调度触发 #1650'))

    # 发射后继续高频追加相同内容，专门验证50/100级每次计数更新都会重启脉冲。
    repeated(events, '第二阶段：五十级热点', 17.00, min(duration - 0.2, 22.4), 4.0, 52_000)
    repeated(events, '第二阶段：一百级热点', 17.05, min(duration - 0.2, 22.4), 5.0, 53_000)

    # 普通弹幕持续高密度进入，验证热点之后仍可按安全间距进入共享轨道。
    ordinary_count = int(duration * 18)
    ordinary_texts = [
        '普通弹幕测试', '画面内容讨论', '这里弹幕很多', '共享轨道压力',
        '不会穿过热点轨道', '热点离开后恢复', '测试普通滚动区域',
    ]
    for index in range(ordinary_count):
        timestamp = 0.35 + index / 18
        text = f'{ordinary_texts[index % len(ordinary_texts)]} #{index:04d}'
        events.append((timestamp, index, text))

    # 额外热点保持较低热度，用于验证等待队列中的热度排序。
    repeated(events, '另一个并发热点', 10.0, 15.0, 5.5, 60_000)
    repeated(events, '？？？', 11.0, 15.0, 6.0, 70_000)
    repeated(events, '😂😂😂', 12.0, 15.0, 7.0, 80_000)

    return sorted(events, key=lambda item: (item[0], item[1]))


def make_ass(ass_path, duration):
    config = yaml.safe_load((ROOT / 'configs' / 'global.yml').read_text(encoding='utf-8'))
    args = config['download_args']['live']
    repeat_config = dict(args['repeat_danmaku'])
    repeat_config['fonts_dir'] = str((ROOT / 'fonts').resolve())

    writer = AssWriter(
        description='热点弹幕压力测试', width=1920, height=1440,
        dst=args['dst'], dmrate=args['dmrate'], font=args['font'],
        fontsize=args['fontsize'], margin_h=args['margin_h'],
        margin_w=args['margin_w'], dmduration=args['dmduration'],
        opacity=args['opacity'], auto_fontsize=args['auto_fontsize'],
        outlinecolor=args['outlinecolor'], outlinesize=args['outlinesize'],
        repeat_danmaku=repeat_config,
    )
    writer.open(str(ass_path))
    events = build_events(duration)
    accepted = 0
    for timestamp, index, text in events:
        accepted += bool(writer.add(danmaku(timestamp, text, index)))
    flights = len(writer._hot_flights)
    updates = sum(len(flight['updates']) for flight in writer._hot_flights)
    writer.close()
    ass_text = ass_path.read_text(encoding='utf-8')
    pulse_lines = [
        line for line in ass_text.splitlines()
        if line.startswith('Dialogue: 2,') and r'\t(0,' in line
    ]
    pulse_updates = [
        line for line in pulse_lines
        if '脉冲验证：五十级热点' in line or '脉冲验证：一百级热点' in line
    ]
    return len(events), accepted, flights, updates, len(pulse_lines), len(pulse_updates)


def render(input_path, ass_path, output_path, duration):
    ffmpeg = ROOT / 'tools' / 'ffmpeg.exe'
    ass_filter_path = ass_path.relative_to(ROOT).as_posix().replace("'", r"\'")
    command = [
        str(ffmpeg), '-y', '-hide_banner', '-loglevel', 'warning',
        '-t', str(duration), '-i', str(input_path),
        '-vf', f"subtitles=filename='{ass_filter_path}':fontsdir='fonts',scale=960:720,fps=30",
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
        '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
        str(output_path),
    ]
    subprocess.run(command, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description='生成并渲染热点弹幕压力测试片段。')
    parser.add_argument('input', type=Path)
    parser.add_argument('--duration', type=float, default=23.0)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()

    input_path = options.input.resolve()
    output_path = (options.output or input_path.with_name(input_path.stem + '-热点压力测试.mp4')).resolve()
    ass_path = output_path.with_suffix('.ass')
    total, accepted, flights, updates, pulse_lines, pulse_updates = make_ass(
        ass_path, options.duration
    )
    print(f'ASS: {ass_path}')
    print(f'事件={total}, 接受={accepted}, 热点飞行={flights}, 计数片段={updates}')
    print(f'脉冲片段={pulse_lines}, 50/100级计数脉冲片段={pulse_updates}')
    render(input_path, ass_path, output_path, options.duration)
    print(f'视频: {output_path}')


if __name__ == '__main__':
    main()
