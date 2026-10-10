"""小宇宙：从单集页面 __NEXT_DATA__ 取音频直链（公开单集免登录）。

不取官方文字稿：需要登录态，自动化有账号风控风险。一律本地转写。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import httpx

from app.models import MediaInfo
from app.platforms.base import UA, LogFn, Platform, PlatformError, UnsupportedURL

EPISODE_RE = re.compile(r"xiaoyuzhoufm\.com/episode/([0-9a-f]{24})")
PODCAST_RE = re.compile(r"xiaoyuzhoufm\.com/podcast/[0-9a-f]{24}")
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def match(url: str) -> Optional[str]:
    m = EPISODE_RE.search(url)
    if m:
        return m.group(1)
    if PODCAST_RE.search(url):
        raise UnsupportedURL("这是小宇宙节目主页，请粘贴具体某一集的链接（xiaoyuzhoufm.com/episode/...）")
    return None


def parse_episode(html: str) -> dict:
    """从页面 HTML 解析出单集字段；页面结构变化时抛出明确错误。"""
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise PlatformError("小宇宙页面结构可能已变化：找不到 __NEXT_DATA__")
    try:
        ep = json.loads(m.group(1))["props"]["pageProps"]["episode"]
    except (KeyError, json.JSONDecodeError) as e:
        raise PlatformError(f"小宇宙页面结构可能已变化：{e}") from e
    if ep.get("isPrivateMedia") or (ep.get("payType") not in (None, "FREE")):
        raise PlatformError("这是小宇宙付费/私有单集，MVP 不支持")
    audio = (ep.get("enclosure") or {}).get("url") or ((ep.get("media") or {}).get("source") or {}).get("url")
    if not audio:
        raise PlatformError("小宇宙页面里没有找到音频地址")
    if "private-media" in audio:
        raise PlatformError("这是小宇宙付费单集，MVP 不支持")
    return {
        "title": ep.get("title", ""),
        "author": (ep.get("podcast") or {}).get("title", ""),
        "duration": float(ep.get("duration") or 0),
        "publish_date": (ep.get("pubDate") or "")[:10],
        "audio_url": audio,
        "shownotes": ep.get("description", "")[:2000],
    }


class Xiaoyuzhou(Platform):
    name = "xiaoyuzhou"
    label = "小宇宙"

    def resolve(self, url: str, log: LogFn) -> MediaInfo:
        eid = match(url)
        canonical = f"https://www.xiaoyuzhoufm.com/episode/{eid}"
        r = httpx.get(canonical, headers={"User-Agent": UA}, follow_redirects=True, timeout=20)
        if r.status_code == 404:
            raise PlatformError("小宇宙单集不存在或已下架")
        r.raise_for_status()
        ep = parse_episode(r.text)
        return MediaInfo(
            platform="xiaoyuzhou",
            id=eid,
            url=canonical,
            title=ep["title"],
            author=ep["author"],
            duration=ep["duration"],
            publish_date=ep["publish_date"],
            extra={"audio_url": ep["audio_url"], "description": ep["shownotes"]},
        )

    def download_audio(self, info: MediaInfo, dest_dir: Path, log: LogFn) -> Path:
        url = info.extra["audio_url"]
        ext = ".m4a" if ".m4a" in url else ".mp3"
        dest = dest_dir / f"source{ext}"
        log("从小宇宙下载音频…")
        with httpx.stream("GET", url, headers={"User-Agent": UA}, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done, last = 0, -10
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 16):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        if pct - last >= 10:
                            log(f"下载 {pct}%（{done / 1e6:.1f}/{total / 1e6:.1f} MB）")
                            last = pct
        return dest
