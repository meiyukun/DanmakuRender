import time

from DMR.Uploader.cover.cover_generate import create_cover
from DMR.utils import VideoInfo, replace_keywords


def fix_cover(config, video_info: VideoInfo):
    if config.get("cover") != '':
        return
    cover_info = config.get('cover_auto')
    for k, v in cover_info.items():
        if type(v) == str:
            cover_info[k] = replace_keywords(v, video_info)

    cover_filename = f'.temp/biliuprs_cover_{int(time.time()) + 86400}.png'
    create_cover(video_info.path, cover_filename, cover_info['title'], cover_info['desc'], cover_info['bottom'])
    config['cover'] = cover_filename
