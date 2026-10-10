"""B站：view 接口取元数据，WBI 签名的 player/wbi/v2 取字幕（需 Cookie），yt-dlp 下载音频。

Cookie 只从 .env 读取，不入库、不打日志。
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

from app import transcript as tr
from app.config import settings
from app.models import MediaInfo, Transcript
from app.platforms.base import UA, LogFn, Platform, PlatformError, UnsupportedURL
from app.platforms.youtube import ytdlp_download_audio

BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"/video/av(\d+)", re.I)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def wbi_sign(params: dict, img_key: str, sub_key: str, wts: Optional[int] = None) -> dict:
    """按 bilibili-API-collect 文档计算 w_rid。"""
    mixin = get_mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time()) if wts is None else wts
    params = dict(sorted(params.items()))
    params = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params


def _headers(with_cookie: bool = True) -> dict:
    h = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    if with_cookie and settings.bilibili_cookie:
        h["Cookie"] = settings.bilibili_cookie
    return h


def match(url: str) -> Optional[tuple[str, int]]:
    """返回 (bvid 或 'av123', 分P)；b23.tv 短链返回 ('b23', 0) 由 resolve 跟随跳转。"""
    try:
        u = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host in ("b23.tv", "www.b23.tv", "bili2233.cn"):
        return ("b23", 0)
    if not (host.endswith("bilibili.com")):
        return None
    p = (parse_qs(u.query).get("p") or ["1"])[0]
    page = int(p) if p.isdigit() and int(p) >= 1 else 1
    m = BV_RE.search(u.path)
    if m:
        return (m.group(1), page)
    m = AV_RE.search(u.path)
    if m:
        return (f"av{m.group(1)}", page)
    return None


def expand_short(url: str) -> str:
    r = httpx.get(url if "://" in url else "https://" + url, headers=_headers(False), follow_redirects=True, timeout=15)
    return str(r.url)


@lru_cache(maxsize=1)
def _wbi_keys_cached(hour: int) -> tuple[str, str]:
    r = httpx.get("https://api.bilibili.com/x/web-interface/nav", headers=_headers(), timeout=15)
    data = r.json()["data"]["wbi_img"]
    stem = lambda u: u.rsplit("/", 1)[1].split(".")[0]  # noqa: E731
    return stem(data["img_url"]), stem(data["sub_url"])


def wbi_keys() -> tuple[str, str]:
    return _wbi_keys_cached(int(time.time() // 3600))


class Bilibili(Platform):
    name = "bilibili"
    label = "B站"

    def resolve(self, url: str, log: LogFn) -> MediaInfo:
        m = match(url)
        if m and m[0] == "b23":
            url = expand_short(url)
            log(f"短链跳转到：{url.split('?')[0]}")
            m = match(url)
        if not m or m[0] == "b23":
            raise UnsupportedURL("无法识别的 B站链接，请粘贴视频页链接（bilibili.com/video/BV...）")
        vid, page = m
        q = {"aid": vid[2:]} if vid.startswith("av") else {"bvid": vid}
        r = httpx.get("https://api.bilibili.com/x/web-interface/view", params=q, headers=_headers(), timeout=15)
        j = r.json()
        if j.get("code") != 0:
            raise PlatformError(f"B站视频信息获取失败：{j.get('message')}（code {j.get('code')}）")
        d = j["data"]
        pages = d.get("pages") or []
        if page > len(pages):
            raise PlatformError(f"该视频只有 {len(pages)} 个分P，没有 P{page}")
        pg = pages[page - 1] if pages else {"cid": d["cid"], "duration": d["duration"], "part": ""}
        bvid = d["bvid"]
        title = d["title"] + (f" - P{page} {pg.get('part', '')}" if len(pages) > 1 else "")
        canonical = f"https://www.bilibili.com/video/{bvid}" + (f"?p={page}" if page > 1 else "")
        return MediaInfo(
            platform="bilibili",
            id=f"{bvid}_p{page}",
            url=canonical,
            title=title,
            author=(d.get("owner") or {}).get("name", ""),
            duration=float(pg.get("duration") or d.get("duration") or 0),
            publish_date=time.strftime("%Y-%m-%d", time.localtime(d.get("pubdate") or 0)) if d.get("pubdate") else "",
            extra={"bvid": bvid, "aid": d.get("aid"), "cid": pg["cid"], "page": page, "description": (d.get("desc") or "")[:2000]},
        )

    def fetch_subtitle(self, info: MediaInfo, log: LogFn) -> Optional[Transcript]:
        if not settings.bilibili_cookie:
            log("未配置 B站 Cookie（BILIBILI_COOKIE / BILIBILI_SESSDATA），B站字幕接口拿不到字幕，改走本地转写")
            return None
        img_key, sub_key = wbi_keys()
        params = wbi_sign({"bvid": info.extra["bvid"], "cid": info.extra["cid"]}, img_key, sub_key)
        r = httpx.get("https://api.bilibili.com/x/player/wbi/v2", params=params, headers=_headers(), timeout=15)
        j = r.json()
        if j.get("code") != 0:
            log(f"B站字幕接口返回错误：{j.get('message')}（code {j.get('code')}）")
            return None
        data = j.get("data") or {}
        if not data.get("login_mid"):
            log("B站 Cookie 可能已过期（接口显示未登录），改走本地转写")
            return None
        subs = (data.get("subtitle") or {}).get("subtitles") or []
        if not subs:
            log("该视频没有 CC 字幕或 AI 字幕")
            return None
        # ① UP 主上传的 CC 字幕（中文优先）② AI 字幕 ai-zh
        def rank(s: dict) -> tuple:
            lan = s.get("lan", "")
            is_ai = lan.startswith("ai-")
            zh = "zh" in lan
            return (is_ai, not zh, lan)

        subs.sort(key=rank)
        best = subs[0]
        sub_url = best.get("subtitle_url") or ""
        if not sub_url:
            log("字幕地址为空，跳过")
            return None
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        log(f"下载 B站字幕：{best.get('lan_doc') or best.get('lan')}")
        body = httpx.get(sub_url, headers=_headers(False), timeout=15).json()
        segs = tr.parse_bilibili_json(body)
        lan = best.get("lan", "")
        return Transcript(
            source="platform_subtitle",
            language=lan.replace("ai-", ""),
            segments=segs,
            detail=f"B站{'AI' if lan.startswith('ai-') else 'CC'}字幕 {lan}",
        )

    def download_audio(self, info: MediaInfo, dest_dir: Path, log: LogFn) -> Path:
        return ytdlp_download_audio(info.url, dest_dir, log, http_headers={"Referer": "https://www.bilibili.com/"})

    def timestamp_url(self, info: MediaInfo, seconds: int) -> Optional[str]:
        sep = "&" if "?" in info.url else "?"
        return f"{info.url}{sep}t={seconds}"
