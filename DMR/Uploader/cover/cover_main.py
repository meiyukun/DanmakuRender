import time

from DMR.Uploader.cover.cover_generate import create_cover
from DMR.utils import VideoInfo, replace_keywords


def fix_cover(config, video_info: VideoInfo):
    if config.get("cover") != '':
        return
    cover_info = config.get('cover_auto')
    cover_title = replace_keywords(cover_info['title'], video_info)
    cover_desc = replace_keywords(cover_info['desc'], video_info)
    cover_bottom = replace_keywords(cover_info['bottom'], video_info)
    if cover_bottom == '' and cover_title == '' and cover_desc == '':
        return
    cover_filename = f'.temp/biliuprs_cover_{int(time.time()) + 86400}.png'
    create_cover(video_info.path, cover_filename, cover_title, cover_desc, cover_bottom)
    config['cover'] = cover_filename
