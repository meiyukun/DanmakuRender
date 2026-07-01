import time

from DMR.Uploader.biliapi.biliapi import add_episodes, get_season, add_section

SEASON_ADD_RETRY_TIMES = 3
SEASON_ADD_RETRY_INTERVAL = 10


def add_video_to_season(
        cookie_file: str,
        season_id: int,
        bvid: str,
        section_name: str,
        episode_title: str,
) -> dict:
    last_error = None

    for retry_index in range(SEASON_ADD_RETRY_TIMES):
        try:
            return _add_video_to_season_once(
                cookie_file=cookie_file,
                season_id=season_id,
                bvid=bvid,
                section_name=section_name,
                episode_title=episode_title,
            )
        except Exception as e:
            last_error = e
            if retry_index >= SEASON_ADD_RETRY_TIMES - 1:
                break
            time.sleep(SEASON_ADD_RETRY_INTERVAL * (retry_index + 1))

    return {
        "code": -1,
        "message": f"添加视频到合集失败，已重试 {SEASON_ADD_RETRY_TIMES} 次",
        "error": str(last_error),
    }


def _add_video_to_season_once(
        cookie_file: str,
        season_id: int,
        bvid: str,
        section_name: str,
        episode_title: str,
) -> dict:
    resp = get_season(cookie_file, season_id)
    sections = resp.get("data", {}).get("sections", {}).get("sections", [])
    if not sections:
        raise ValueError(f"合集 {season_id} 中没有任何小节")

    if not section_name:
        # 使用最后一个 section（按 order 排序取最大）
        target = max(sections, key=lambda s: s.get("order", 0))
    else:
        # 查找 title 匹配的 section
        target = next((s for s in sections if s["title"] == section_name), None)
        if target is None:
            # 新建 section
            ret = add_section(cookie_file, season_id, section_name)
            if ret.get("code") != 0:
                raise RuntimeError(f"创建合集小节失败: {ret}")
            new_section_id = ret["data"]
            ret = add_video_to_bilibili_section(
                cookies=cookie_file, bvid=bvid, title=episode_title, section_id=new_section_id,
            )
            if ret.get("code") != 0:
                raise RuntimeError(f"添加视频到合集小节失败: {ret}")
            return ret

    ret = add_video_to_bilibili_section(
        cookies=cookie_file, bvid=bvid, title=episode_title, section_id=target["id"],
    )
    if ret.get("code") != 0:
        raise RuntimeError(f"添加视频到合集小节失败: {ret}")
    return ret


def add_video_to_bilibili_section(
        cookies: str,
        bvid: str,
        title: str,
        section_id: int,
) -> dict:
    episodes = [{"title": title, "bvid": bvid}]
    return add_episodes(cookies, section_id, episodes)



# 使用示例
if __name__ == "__main__":
    COOKIE_FILE = ".login_info/bilibili.json"
    video_title = "2026年04月07日晚上"
    video_bvid = "BV18pDBBUEhH"
    target_section_id = 7802270

    result = add_video_to_bilibili_section(
        cookies=COOKIE_FILE,
        bvid=video_bvid,
        title=video_title,
        section_id=target_section_id,
    )
    print("请求结果:", result)
