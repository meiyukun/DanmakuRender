"""
bili_section.py 功能测试
使用前请确保 COOKIE_FILE 路径正确且 cookie 有效
"""
import json
from bili_section import add_video_to_bilibili_section, add_video_to_season
from biliapi import get_season, get_section, delete_section

# ========== 配置 ==========
COOKIE_FILE = ".login_info/bilibili.json"
SEASON_ID = 7076024          # 合集 ID
TEST_BVID = "BV18pDBBUEhH"  # 测试用的 bvid
# ==========================


def test_get_season():
    """测试获取合集信息"""
    print("=" * 50)
    print("测试: get_season")
    resp = get_season(COOKIE_FILE, SEASON_ID)
    print(f"code: {resp.get('code')}")
    season = resp.get("data", {}).get("season", {})
    print(f"合集标题: {season.get('title')}")
    sections = resp.get("data", {}).get("sections", {}).get("sections", [])
    print(f"小节数量: {len(sections)}")
    for s in sections:
        print(f"  - [{s['id']}] {s['title']} (order={s['order']}, epCount={s.get('epCount', '?')})")
    return resp


def test_get_section(section_id: int):
    """测试获取小节信息"""
    print("=" * 50)
    print(f"测试: get_section (id={section_id})")
    resp = get_section(COOKIE_FILE, section_id)
    print(f"code: {resp.get('code')}")
    section = resp.get("data", {}).get("section", {})
    print(f"小节标题: {section.get('title')}")
    episodes = resp.get("data", {}).get("episodes", [])
    print(f"剧集数量: {len(episodes)}")
    for ep in episodes[:5]:
        print(f"  - [{ep['id']}] {ep['title']} (bvid={ep.get('bvid', '?')})")
    if len(episodes) > 5:
        print(f"  ... 共 {len(episodes)} 条")
    return resp


def test_add_video_to_bilibili_section(section_id: int):
    """测试直接添加视频到指定 section"""
    print("=" * 50)
    print(f"测试: add_video_to_bilibili_section (section_id={section_id})")
    resp = add_video_to_bilibili_section(
        cookies=COOKIE_FILE,
        bvid=TEST_BVID,
        title="测试剧集标题",
        section_id=section_id,
    )
    print(f"结果: {json.dumps(resp, ensure_ascii=False)}")
    return resp


def test_add_video_to_season_existing_section(section_name: str):
    """测试添加视频到已有的 section"""
    print("=" * 50)
    print(f"测试: add_video_to_season (已有section: '{section_name}')")
    resp = add_video_to_season(
        cookie_file=COOKIE_FILE,
        season_id=SEASON_ID,
        bvid=TEST_BVID,
        section_name=section_name,
        episode_title="测试-已有小节",
    )
    print(f"结果: {json.dumps(resp, ensure_ascii=False)}")
    return resp


def test_add_video_to_season_new_section(section_name: str):
    """测试添加视频到新建的 section"""
    print("=" * 50)
    print(f"测试: add_video_to_season (新建section: '{section_name}')")
    resp = add_video_to_season(
        cookie_file=COOKIE_FILE,
        season_id=SEASON_ID,
        bvid=TEST_BVID,
        section_name=section_name,
        episode_title="测试-新建小节",
    )
    print(f"结果: {json.dumps(resp, ensure_ascii=False)}")
    return resp


def test_add_video_to_season_default_section():
    """测试 section_name 为空时使用最后一个 section"""
    print("=" * 50)
    print("测试: add_video_to_season (section_name 为空, 使用最后一个section)")
    resp = add_video_to_season(
        cookie_file=COOKIE_FILE,
        season_id=SEASON_ID,
        bvid=TEST_BVID,
        section_name="",
        episode_title="测试-默认小节",
    )
    print(f"结果: {json.dumps(resp, ensure_ascii=False)}")
    return resp


def test_delete_section_by_name(section_name: str):
    """测试按名称删除小节"""
    print("=" * 50)
    print(f"测试: delete_section_by_name (name='{section_name}')")
    season_resp = get_season(COOKIE_FILE, SEASON_ID)
    sections = season_resp.get("data", {}).get("sections", {}).get("sections", [])
    target = next((s for s in sections if s["title"] == section_name), None)
    if target is None:
        print(f"未找到名称为 '{section_name}' 的小节，可用小节: {[s['title'] for s in sections]}")
        return None
    print(f"找到小节: [{target['id']}] {target['title']}，执行删除...")
    resp = delete_section(COOKIE_FILE, target["id"])
    print(f"结果: {json.dumps(resp, ensure_ascii=False)}")
    return resp


if __name__ == "__main__":
    # 1. 查询测试（只读，安全）
    # season_resp = test_get_season()
    #
    # sections = season_resp.get("data", {}).get("sections", {}).get("sections", [])
    # if sections:
    #     test_get_section(sections[0]["id"])

    # 2. 写操作测试（会实际修改合集，按需取消注释）
    # 注意：以下测试会向合集中添加视频，请确认后再执行

    # if sections:
    #     test_add_video_to_bilibili_section(sections[0]["id"])

    # if sections:
    #     test_add_video_to_season_existing_section(sections[0]["title"])

    test_add_video_to_season_new_section("测试删除")

    # test_add_video_to_season_default_section()

    # test_delete_section_by_name("测试删除")
