import time

from DMR.Uploader.biliapi.biliapi import (
    add_episodes, add_section, delete_episode, get_season, get_seasons, get_section,
)

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


def find_video_season(cookie_file: str, bvid: str) -> dict | None:
    """查找稿件当前所在的合集、小节和单集，未加入合集时返回 None。"""
    page = 1
    season_ids = []
    while True:
        response = get_seasons(cookie_file, page=page, page_size=100)
        if response.get("code") != 0:
            raise RuntimeError(response.get("message") or response)
        data = response.get("data") or {}
        items = data.get("seasons") or []
        for item in items:
            season = item.get("season") if isinstance(item, dict) else None
            if isinstance(season, dict) and season.get("id") is not None:
                season_ids.append((int(season["id"]), str(season.get("title") or "")))
        try:
            total = int(data.get("total") or len(season_ids))
        except (TypeError, ValueError):
            total = len(season_ids)
        if not items or len(season_ids) >= total:
            break
        page += 1
        if page > 100:
            raise RuntimeError("合集数量异常，已停止查找稿件所属合集。")

    for season_id, known_title in season_ids:
        response = get_season(cookie_file, season_id)
        if response.get("code") != 0:
            continue
        data = response.get("data") or {}
        season = data.get("season") or {}
        sections_data = data.get("sections") or {}
        sections = sections_data.get("sections") if isinstance(sections_data, dict) else sections_data
        for section in sections or []:
            episodes = section.get("episodes") or section.get("ep_list") or []
            if not episodes and section.get("id") is not None:
                section_response = get_section(cookie_file, int(section["id"]))
                if section_response.get("code") == 0:
                    section_data = section_response.get("data") or {}
                    episodes = section_data.get("episodes") or \
                        (section_data.get("section") or {}).get("episodes") or []
            for episode in episodes:
                archive = episode.get("archive") if isinstance(episode, dict) else None
                episode_bvid = (episode.get("bvid") if isinstance(episode, dict) else None) or \
                    (archive.get("bvid") if isinstance(archive, dict) else None)
                if str(episode_bvid or "").casefold() != str(bvid).casefold():
                    continue
                return {
                    "season_id": season_id,
                    "season_title": str(season.get("title") or known_title),
                    "section_id": int(section.get("id")) if section.get("id") is not None else None,
                    "section_title": str(section.get("title") or ""),
                    "episode_id": int(episode.get("id")) if episode.get("id") is not None else None,
                    "episode_title": str(episode.get("title") or
                                         (archive.get("title") if isinstance(archive, dict) else "") or ""),
                }
    return None


def sync_video_season(cookie_file: str, bvid: str, season_id: int | None,
                      section_title: str = "", episode_title: str = "") -> dict:
    """把稿件合集归属同步为目标值；相同值不调用写接口。"""
    current = find_video_season(cookie_file, bvid)
    target_id = int(season_id) if season_id else None
    section_title = str(section_title or "")
    episode_title = str(episode_title or "")
    if current and current["season_id"] == target_id:
        section_same = not section_title or current["section_title"] == section_title
        title_same = not episode_title or current["episode_title"] == episode_title
        if section_same and title_same:
            return {"code": 0, "message": "合集信息未变更", "changed": False}
    if not current and target_id is None:
        return {"code": 0, "message": "稿件未加入合集", "changed": False}

    if current and current.get("episode_id") is None:
        raise RuntimeError("无法取得原合集单集ID，不能安全修改合集归属。")
    if current:
        removed = delete_episode(cookie_file, current["episode_id"])
        if removed.get("code") != 0:
            raise RuntimeError(f"从原合集移除稿件失败: {removed}")
    if target_id is None:
        return {"code": 0, "message": "已移出合集", "changed": True}

    target_title = episode_title or (current or {}).get("episode_title") or str(bvid)
    added = add_video_to_season(cookie_file, target_id, bvid, section_title, target_title)
    if added.get("code") == 0:
        return {"code": 0, "message": "合集信息已更新", "changed": True}

    if current:
        rollback = add_video_to_season(
            cookie_file, current["season_id"], bvid,
            current.get("section_title") or "", current.get("episode_title") or str(bvid),
        )
        if rollback.get("code") != 0:
            raise RuntimeError(f"更新合集失败且恢复原合集也失败: {added}; rollback={rollback}")
    raise RuntimeError(f"更新合集失败: {added}")



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
