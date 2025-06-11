import time

from DMR.Uploader.cover.cover_generate import create_cover
from DMR.utils import VideoInfo, replace_keywords
from DMR.utils.utils import replace_keywords_all


def fix_cover(config, video_info: VideoInfo):
    if config.get("cover") != '':
        return
    cover_info = config.get('cover_auto').copy()
    replace_keywords_all(cover_info,video_info)


    cover_filename = f'.temp/biliuprs_cover_{int(time.time()) + 86400}.png'
    create_cover(video_info.path, cover_filename, cover_info['title'], cover_info['desc'], cover_info['bottom'])
    config['cover'] = cover_filename

if __name__ == '__main__':
    from datetime import datetime
    n=datetime.now()
    f=datetime.timestamp(n)
    d=int(datetime.timestamp(n))
    tmp=n.timestamp()

    print(f)
    print(d)
    print(tmp)