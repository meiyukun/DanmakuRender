from DMR.Downloader.Danmaku.AssHandle.ass_repeat_remove import filter_ass_danmaku, filter_ass_at
from DMR.Downloader.Danmaku.AssHandle.ass_turn_emoji import replace_emoji_in_ass


def ass_handle_default(input):
    filter_ass_danmaku(input, input)
    replace_emoji_in_ass(input)
    filter_ass_at(input,input)
