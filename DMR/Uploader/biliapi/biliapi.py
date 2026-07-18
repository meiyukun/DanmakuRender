import json
import threading
import time

import requests

BASE_URL = "https://member.bilibili.com/x2/creative/web"
ARCHIVE_BASE_URL = "https://member.bilibili.com/x/web"
WRITE_API_INTERVAL = 6

_write_api_lock = threading.Lock()
_last_write_api_time = 0.0


def _make_headers(cookie: str, referer: str = "https://member.bilibili.com/platform/home") -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://member.bilibili.com",
        "priority": "u=1, i",
        "referer": referer,
        "sec-ch-ua": "\"Google Chrome\";v=\"137\", \"Chromium\";v=\"137\", \"Not/A)Brand\";v=\"24\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Cookie": cookie,
    }


def _make_form_headers(cookie: str, referer: str = "https://member.bilibili.com/platform/home") -> dict:
    headers = _make_headers(cookie, referer)
    headers["content-type"] = "application/x-www-form-urlencoded"
    return headers


def _get(cookie: str, path: str, params: dict) -> dict:
    headers = _make_headers(cookie)
    resp = requests.get(f"{BASE_URL}{path}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _wait_for_write_api() -> None:
    global _last_write_api_time

    with _write_api_lock:
        now = time.monotonic()
        wait_time = WRITE_API_INTERVAL - (now - _last_write_api_time)
        if wait_time > 0:
            time.sleep(wait_time)
        _last_write_api_time = time.monotonic()


def _post_json(cookie: str, csrf: str, path: str, payload: dict) -> dict:
    headers = _make_headers(cookie)
    url = f"{BASE_URL}{path}?csrf={csrf}"
    _wait_for_write_api()
    resp = requests.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    return resp.json()


def _post_form(cookie: str, path: str, data: dict) -> dict:
    headers = _make_form_headers(cookie)
    _wait_for_write_api()
    resp = requests.post(f"{BASE_URL}{path}", headers=headers, data=data)
    resp.raise_for_status()
    return resp.json()


# 1. 获取合集信息
def get_season(cookie_file: str, season_id: int) -> dict:
    cookie, _ = read_bilibili_cookies(cookie_file)
    return _get(cookie, "/season", {"id": season_id})


def get_seasons(cookie_file: str, page: int = 1, page_size: int = 100) -> dict:
    """获取当前账号创建的合集列表。"""
    cookie, _ = read_bilibili_cookies(cookie_file)
    if not cookie:
        raise ValueError(f"账号文件中没有可用的B站Cookie: {cookie_file}")
    return _get(cookie, "/seasons", {"pn": int(page), "ps": int(page_size)})


def get_archives(cookie_file: str, page: int = 1, page_size: int = 30,
                 status: str = "pubed") -> dict:
    """获取当前账号最近的投稿；默认只返回已发布稿件。"""
    cookie, _ = read_bilibili_cookies(cookie_file)
    if not cookie:
        raise ValueError(f"账号文件中没有可用的B站Cookie: {cookie_file}")
    response = requests.get(
        f"{ARCHIVE_BASE_URL}/archives", headers=_make_headers(cookie),
        params={"status": status, "pn": int(page), "ps": int(page_size),
                "interactive": 1, "coop": 1}, timeout=15,
    )
    response.raise_for_status()
    return response.json()


def get_archive_view(cookie_file: str, bvid: str) -> dict:
    """读取一个已有稿件的完整编辑信息。"""
    cookie, _ = read_bilibili_cookies(cookie_file)
    if not cookie:
        raise ValueError(f"账号文件中没有可用的B站Cookie: {cookie_file}")
    response = requests.get(
        f"https://member.bilibili.com/x/vupre/web/archive/view",
        headers=_make_headers(cookie), params={"bvid": str(bvid)}, timeout=15,
    )
    response.raise_for_status()
    return response.json()


# 2. 编辑合集
def edit_season(cookie_file: str, season: dict, sorts: list) -> dict:
    cookie, csrf = read_bilibili_cookies(cookie_file)
    payload = {"season": season, "sorts": sorts}
    return _post_json(cookie, csrf, "/season/edit", payload)


# 3. 创建小节
def add_section(cookie_file: str, season_id: int, title: str, section_type: int = 0) -> dict:
    cookie, csrf = read_bilibili_cookies(cookie_file)
    # 若合集未开启小节功能（no_section=1），先切换为启用
    season_resp = _get(cookie, "/season", {"id": season_id})
    no_section = season_resp.get("data", {}).get("season", {}).get("no_section", 0)
    if no_section == 1:
        switch_resp = switch_section(cookie_file, season_id, enable=True)
        if switch_resp.get("code") != 0:
            raise RuntimeError(f"启用合集小节功能失败: {switch_resp}")
    payload = {
        "type": section_type,
        "seasonId": season_id,
        "title": title,
        "captcha_token": "",
    }
    return _post_json(cookie, csrf, "/season/section/add", payload)


# 4. 编辑小节
def edit_section(cookie_file: str, section: dict, sorts: list) -> dict:
    cookie, csrf = read_bilibili_cookies(cookie_file)
    payload = {
        "section": section,
        "sorts": sorts,
        "captcha_token": "",
    }
    return _post_json(cookie, csrf, "/season/section/edit", payload)


# 5. 删除小节
def delete_section(cookie_file: str, section_id: int) -> dict:
    cookie, csrf = read_bilibili_cookies(cookie_file)
    data = {
        "id": section_id,
        "csrf": csrf,
    }
    return _post_form(cookie, "/season/section/del", data)


# 6. 获取小节信息
def get_section(cookie_file: str, section_id: int) -> dict:
    cookie, _ = read_bilibili_cookies(cookie_file)
    return _get(cookie, "/season/section", {"id": section_id})


# 7. 在Section中添加Episode
def add_episodes(cookie_file: str, section_id: int, episodes: list) -> dict:
    cookie, csrf = read_bilibili_cookies(cookie_file)
    payload = {
        "sectionId": section_id,
        "episodes": episodes,
        "csrf": csrf,
    }
    return _post_json(cookie, csrf, "/season/section/episodes/add", payload)


# 8. 移动Episode
def move_episode(cookie_file: str, section_id: int, ep_id: int) -> dict:
    cookie, csrf = read_bilibili_cookies(cookie_file)
    data = {
        "sectionId": section_id,
        "epId": ep_id,
        "csrf": csrf,
    }
    return _post_form(cookie, "/season/section/episode/move", data)


# 9. 删除Episode
def delete_episode(cookie_file: str, ep_id: int) -> dict:
    cookie, csrf = read_bilibili_cookies(cookie_file)
    data = {
        "id": ep_id,
        "csrf": csrf,
    }
    return _post_form(cookie, "/season/section/episode/del", data)


# 10. 切换合集 Section 功能开关
def switch_section(cookie_file: str, season_id: int, enable: bool) -> dict:
    """切换合集的小节功能开关。enable=True 表示启用小节（no_section=0），False 表示禁用（no_section=1）。"""
    cookie, csrf = read_bilibili_cookies(cookie_file)
    data = {
        "no_section": 0 if enable else 1,
        "season_id": season_id,
        "csrf": csrf,
    }
    return _post_form(cookie, "/season/section/switch", data)

def read_bilibili_cookies(file_path: str = ".login_info/bilibili.json") -> tuple[str, str]:
    """
    从 JSON 文件读取 B站 Cookie 和 CSRF 令牌

    Args:
        file_path (str): JSON 文件路径

    Returns:
        tuple[str, str]: (完整Cookie字符串, CSRF令牌)
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 提取关键 Cookie 值
        cookies = data.get('cookie_info', {}).get('cookies', [])
        cookie_dict = {c['name']: c['value'] for c in cookies}

        # 构建完整 Cookie 字符串
        cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])

        # 提取 CSRF 令牌 (bili_jct)
        csrf_token = cookie_dict.get('bili_jct', '')

        return cookie_str, csrf_token

    except Exception as e:
        print(f"读取 Cookie 文件失败: {e}")
        return "", ""

