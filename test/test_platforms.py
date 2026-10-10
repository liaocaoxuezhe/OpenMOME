"""链接识别、WBI 签名、小宇宙页面解析、YouTube 字幕选择（离线）。"""
from pathlib import Path

import pytest

from app import platforms
from app.platforms import bilibili, xiaoyuzhou, youtube
from app.platforms.base import PlatformError, UnsupportedURL

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.xiaoyuzhoufm.com/episode/619896e8138b51cbd78f3912", "xiaoyuzhou"),
        ("xiaoyuzhoufm.com/episode/619896e8138b51cbd78f3912?s=abc", "xiaoyuzhou"),
        ("https://www.youtube.com/watch?v=iG9CE55wbtY&list=PLx&t=30", "youtube"),
        ("https://youtu.be/iG9CE55wbtY?si=xx", "youtube"),
        ("https://www.youtube.com/shorts/iG9CE55wbtY", "youtube"),
        ("https://m.youtube.com/live/iG9CE55wbtY", "youtube"),
        ("https://www.bilibili.com/video/BV1gTHd6aE5e/?spm_id_from=333", "bilibili"),
        ("https://m.bilibili.com/video/av170001", "bilibili"),
        ("https://b23.tv/abcd123", "bilibili"),
    ],
)
def test_detect(url, expected):
    assert platforms.detect(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyin.com/video/123",
        "https://www.youtube.com/playlist?list=PLx",
        "https://www.bilibili.com/",
        "not a url",
        "",
    ],
)
def test_detect_unsupported(url):
    with pytest.raises(UnsupportedURL):
        platforms.detect(url)


def test_xiaoyuzhou_podcast_page_hint():
    with pytest.raises(UnsupportedURL, match="节目主页"):
        platforms.detect("https://www.xiaoyuzhoufm.com/podcast/5e280fab418a84a0461fbd2f")


def test_youtube_ids():
    assert youtube.match("https://www.youtube.com/watch?v=iG9CE55wbtY&list=PLx") == "iG9CE55wbtY"
    assert youtube.match("https://youtu.be/iG9CE55wbtY?t=30") == "iG9CE55wbtY"
    assert youtube.match("https://www.youtube.com/watch?v=short") is None


def test_bilibili_page():
    assert bilibili.match("https://www.bilibili.com/video/BV1gTHd6aE5e?p=2") == ("BV1gTHd6aE5e", 2)
    assert bilibili.match("https://www.bilibili.com/video/BV1gTHd6aE5e") == ("BV1gTHd6aE5e", 1)
    assert bilibili.match("https://www.bilibili.com/video/av170001?p=0") == ("av170001", 1)


def test_wbi_sign_known_vector():
    # bilibili-API-collect 文档中的示例
    img_key, sub_key = "7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"
    assert bilibili.get_mixin_key(img_key + sub_key) == "ea1db124af3c7062474693fa704f4ff8"
    signed = bilibili.wbi_sign({"foo": "114", "bar": "514", "zab": 1919810}, img_key, sub_key, wts=1702204169)
    assert signed["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"


def test_xiaoyuzhou_parse_fixture():
    ep = xiaoyuzhou.parse_episode((FIX / "xiaoyuzhou_episode.html").read_text(encoding="utf-8"))
    assert ep["title"].startswith("S01E03")
    assert ep["author"] == "开源面对面"
    assert ep["duration"] == 2929
    assert ep["audio_url"].endswith(".mp3")
    assert ep["publish_date"] == "2021-11-20"


def test_xiaoyuzhou_paid_rejected():
    html = (FIX / "xiaoyuzhou_episode.html").read_text(encoding="utf-8").replace('"FREE"', '"PAY_EPISODE"')
    with pytest.raises(PlatformError, match="付费"):
        xiaoyuzhou.parse_episode(html)


def test_xiaoyuzhou_broken_page():
    with pytest.raises(PlatformError, match="结构"):
        xiaoyuzhou.parse_episode("<html>nothing</html>")


def _fmt(ext="json3"):
    return [{"ext": ext, "url": "u"}]


def test_youtube_pick_manual_original_language():
    info = {"language": "en", "subtitles": {"zh-CN": _fmt(), "en": _fmt()}, "automatic_captions": {"en-orig": _fmt()}}
    assert youtube.pick_subtitle(info)[:2] == ("manual", "en")


def test_youtube_pick_manual_zh_fallback():
    info = {"language": "ja", "subtitles": {"zh-Hant": _fmt(), "zh-Hans": _fmt()}, "automatic_captions": {}}
    assert youtube.pick_subtitle(info)[:2] == ("manual", "zh-Hans")


def test_youtube_pick_auto_orig_only():
    info = {"language": "zh", "subtitles": {"live_chat": _fmt()}, "automatic_captions": {"zh-orig": _fmt(), "en": _fmt()}}
    assert youtube.pick_subtitle(info)[:2] == ("auto", "zh-orig")


def test_youtube_pick_none():
    assert youtube.pick_subtitle({"language": "zh", "subtitles": {}, "automatic_captions": {"en": _fmt()}}) is None
