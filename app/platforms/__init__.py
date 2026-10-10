"""链接识别：只接受小宇宙 / YouTube / B站，防止被当作任意 URL 下载器。"""
from __future__ import annotations

from app.platforms import bilibili, xiaoyuzhou, youtube
from app.platforms.base import Platform, PlatformError, UnsupportedURL

PLATFORMS: dict[str, Platform] = {
    "xiaoyuzhou": xiaoyuzhou.Xiaoyuzhou(),
    "youtube": youtube.YouTube(),
    "bilibili": bilibili.Bilibili(),
}


def detect(url: str) -> str:
    """离线识别平台名；不支持时抛 UnsupportedURL（message 给用户看）。"""
    url = (url or "").strip()
    if not url:
        raise UnsupportedURL("请粘贴链接")
    if xiaoyuzhou.match(url):
        return "xiaoyuzhou"
    if youtube.match(url):
        return "youtube"
    if bilibili.match(url):
        return "bilibili"
    raise UnsupportedURL("暂只支持小宇宙单集、YouTube 视频、B站视频链接")


def get(name: str) -> Platform:
    return PLATFORMS[name]


__all__ = ["detect", "get", "PLATFORMS", "Platform", "PlatformError", "UnsupportedURL"]
