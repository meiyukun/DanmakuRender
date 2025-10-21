import json
import requests


def add_video_to_bilibili_section(
        cookies: str,
        bvid: str,
        title: str,
        section_id: int,
) -> dict:
    cookie, csrf = read_bilibili_cookies(cookies)
    return _add_video_to_bilibili_season(csrf, title, bvid, section_id, cookie)


def _add_video_to_bilibili_season(
        csrf_token: str,
        title: str,
        bvid: str,
        section_id: int,
        cookie: str
) -> dict:
    """向哔哩哔哩剧集分区添加视频"""
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://member.bilibili.com",
        "priority": "u=1, i",
        "referer": f"https://member.bilibili.com/platform/upload/video/frame?type=edit&bvid={bvid}",
        "sec-ch-ua": "\"Google Chrome\";v=\"137\", \"Chromium\";v=\"137\", \"Not/A)Brand\";v=\"24\"",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "\"Windows\"",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Cookie": cookie
    }

    payload = {
        "sectionId": section_id,
        "episodes": [{"title": title, "bvid": bvid}],
        "csrf": csrf_token
    }

    url = f"https://member.bilibili.com/x2/creative/web/season/section/episodes/add?csrf={csrf_token}"

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
        return {"error": str(e), "status_code": response.status_code}


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


# 使用示例
if __name__ == "__main__":
    # 文件路径（当前目录下的bilibili.json）
    COOKIE_FILE = ".login_info/bilibili.json"

    # 读取 Cookie 和 CSRF
    cookie_str, _csrf_token = read_bilibili_cookies(COOKIE_FILE)

    if not cookie_str or not _csrf_token:
        print("无法获取有效的 Cookie 或 CSRF 令牌")
        exit(1)

    # 请求参数
    video_title = "117有妖气-带弹幕回放2025年06月04日晚上"
    video_bvid = "BV1DcTHz2Eav"
    target_section_id = 6190172  # 目标剧集分区ID

    # 执行请求
    result = _add_video_to_bilibili_season(
        csrf_token=_csrf_token,
        title=video_title,
        bvid=video_bvid,
        section_id=target_section_id,
        cookie=cookie_str
    )

    print("请求结果:", result)
