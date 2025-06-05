import time

from DMR.Uploader.cover.cover_generate import create_cover
from DMR.utils import VideoInfo, replace_keywords


def fix_cover(config, video_info: VideoInfo):
    if config.get("cover") != '':
        return
    cover_info = config.get('cover_auto')
    cover_title = cover_info['title']
    cover_desc = cover_info['desc']
    cover_bottom = cover_info['bottom']
    if cover_bottom == '' and cover_title == '' and cover_desc == '':
        return
    cover_filename = f'.temp/biliuprs_cover_{int(time.time()) + 86400}.png'
    create_cover(video_info.path, cover_filename, cover_title, cover_desc, cover_bottom)
    config['cover'] = cover_filename
