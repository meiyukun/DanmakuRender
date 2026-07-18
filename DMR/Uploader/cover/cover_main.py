import logging
import os
import time

from DMR.Uploader.cover.ai_cover import generate_ai_cover
from DMR.Uploader.cover.cover_generate import create_cover
from DMR.utils.utils import replace_keywords_all


logger = logging.getLogger(__name__)


def fix_cover(config, video_info, ai_client=None):
    if config.get("cover") != '':
        return
    cover_auto = config.get('cover_auto')
    if not cover_auto:
        return
    if cover_auto.get('enabled', True) is False:
        return

    files = video_info if isinstance(video_info, list) else [video_info]
    if not files:
        return
    first_video = files[0]

    if cover_auto.get('ai', {}).get('enabled', False):
        try:
            ai_cover = generate_ai_cover(files, first_video, cover_auto, ai_client=ai_client)
            if ai_cover:
                config['cover'] = ai_cover
                logger.info("已生成AI封面: %s", ai_cover)
                return
        except Exception as e:
            logger.warning("AI封面生成失败，将回退到本地封面生成: %s", e)

    cover_info = cover_auto.copy()
    replace_keywords_all(cover_info, first_video)

    os.makedirs('.temp', exist_ok=True)
    cover_filename = f'.temp/biliuprs_cover_{int(time.time()) + 86400}.png'
    try:
        create_cover(first_video.path, cover_filename, cover_info['title'], cover_info['desc'], cover_info['bottom'])
        config['cover'] = cover_filename
    except Exception as e:
        logger.warning("本地封面生成失败，跳过自动封面: %s", e)


if __name__ == '__main__':
    from datetime import datetime
    n=datetime.now()
    f=datetime.timestamp(n)
    d=int(datetime.timestamp(n))
    tmp=n.timestamp()

    print(f)
    print(d)
    print(tmp)
