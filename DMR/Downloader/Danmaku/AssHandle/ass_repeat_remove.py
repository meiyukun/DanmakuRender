import re
from collections import defaultdict


def parse_ass_time(time_str):
    """将ASS格式的时间字符串转换为秒数"""
    hours, minutes, seconds, centiseconds = map(int, re.findall(r'\d+', time_str))
    return hours * 3600 + minutes * 60 + seconds + centiseconds / 100


def remove_effects(text):
    """去除弹幕文本中的特效部分"""
    return re.sub(r'{.*?}', '', text).strip()


def filter_ass_danmaku(input_file, output_file, max_repeats=200, interval=300):
    danmaku_dict = defaultdict(list)
    filtered_lines = []

    with open(input_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    for line in lines:
        if line.startswith('Dialogue:'):
            parts = line.strip().split(',', 9)
            start_time = parse_ass_time(parts[1])
            text = parts[9]
            clean_text = remove_effects(text)
            danmaku_dict[clean_text].append((start_time, line))
        else:
            filtered_lines.append(line)

    for text, entries in danmaku_dict.items():
        if len(entries) <= max_repeats:
            for _, line in entries:
                filtered_lines.append(line)
            continue

        # 按时间排序
        entries.sort(key=lambda x: x[0])
        i = 0
        while i < len(entries):
            count = 1
            j = i + 1
            while j < len(entries) and entries[j][0] - entries[i][0] <= interval:
                count += 1
                j += 1

            if count > max_repeats:
                i = j
            else:
                filtered_lines.append(entries[i][1])
                i += 1

    with open(output_file, 'w', encoding='utf-8') as f:
        f.writelines(filtered_lines)
