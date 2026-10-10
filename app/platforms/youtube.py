"""YouTube：yt-dlp 取元数据 / 字幕 / 音频（需要 Deno 作为 JS 运行时）。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
import yt_dlp

from app import transcript as tr
from app.models import MediaInfo, Transcript
from app.platforms.base import UA, LogFn, Platform, PlatformError

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MANUAL_FALLBACK = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK", "en", "en-US", "en-GB"]


def match(url: str) -> Optional[str]:
    try:
        u = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host.endswith("youtu.be"):
        vid = u.path.strip("/").split("/")[0]
        return vid if ID_RE.match(vid) else None
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [""])[0]
            return vid if ID_RE.match(vid) else None
        parts = u.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            return parts[1] if ID_RE.match(parts[1]) else None
    return None


def _ydl_opts(**kw) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "noprogress": True}
    opts.update(kw)
    return opts


def pick_subtitle(info: dict) -> Optional[tuple[str, str, list]]:
    """按方案 §4.2 选字幕，返回 (kind, lang, formats)。kind = manual / auto。"""
    lang = (info.get("language") or "").strip()
    manual = info.get("subtitles") or {}
    manual = {k: v for k, v in manual.items() if k != "live_chat"}

    def find(keys: dict, want: str) -> Optional[str]:
        if not want:
            return None
        if want in keys:
            return want
        for k in keys:
            if k.split("-")[0] == want.split("-")[0] and not k.endswith("-orig"):
                return k
        return None

    for want in [lang] + MANUAL_FALLBACK:
        k = find(manual, want)
        if k:
            return "manual", k, manual[k]

    auto = info.get("automatic_captions") or {}
    # 仅用视频原语言的自动字幕（-orig 为原语言 ASR），不用自动翻译
    for k in auto:
        if k.endswith("-orig"):
            return "auto", k, auto[k]
    if lang and lang in auto:
        return "auto", lang, auto[lang]
    return None


class YouTube(Platform):
    name = "youtube"
    label = "YouTube"

    def resolve(self, url: str, log: LogFn) -> MediaInfo:
        vid = match(url)
        canonical = f"https://www.youtube.com/watch?v={vid}"
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as ydl:
                info = ydl.extract_info(canonical, download=False)
        except yt_dlp.utils.DownloadError as e:
            raise PlatformError(f"YouTube 解析失败：{e}") from e
        d = info.get("upload_date") or ""
        return MediaInfo(
            platform="youtube",
            id=vid,
            url=canonical,
            title=info.get("title", ""),
            author=info.get("uploader") or info.get("channel") or "",
            duration=float(info.get("duration") or 0),
            publish_date=f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else "",
            extra={
                "language": info.get("language") or "",
                "subtitles": {k: v for k, v in (info.get("subtitles") or {}).items()},
                "automatic_captions": {
                    k: v for k, v in (info.get("automatic_captions") or {}).items() if k.endswith("-orig") or k == info.get("language")
                },
                "description": (info.get("description") or "")[:2000],
            },
        )

    def fetch_subtitle(self, info: MediaInfo, log: LogFn) -> Optional[Transcript]:
        picked = pick_subtitle(info.extra)
        if not picked:
            log("YouTube 没有可用的人工字幕或原语言自动字幕")
            return None
        kind, lang, formats = picked
        fmt = next((f for f in formats if f.get("ext") == "json3"), None) or next(
            (f for f in formats if f.get("ext") == "vtt"), None
        )
        if not fmt:
            log(f"字幕 {lang} 没有 json3/vtt 格式，跳过")
            return None
        log(f"下载 YouTube {'人工' if kind == 'manual' else '自动'}字幕：{lang}（{fmt['ext']}）")
        r = httpx.get(fmt["url"], headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        segs = tr.parse_json3(r.text) if fmt["ext"] == "json3" else tr.parse_vtt(r.text)
        return Transcript(
            source="platform_subtitle",
            language=lang.replace("-orig", ""),
            segments=segs,
            detail=f"YouTube {'人工' if kind == 'manual' else '自动'}字幕 {lang}",
        )

    def download_audio(self, info: MediaInfo, dest_dir: Path, log: LogFn) -> Path:
        return ytdlp_download_audio(info.url, dest_dir, log)

    def timestamp_url(self, info: MediaInfo, seconds: int) -> Optional[str]:
        return f"{info.url}&t={seconds}s"


def ytdlp_download_audio(url: str, dest_dir: Path, log: LogFn, http_headers: Optional[dict] = None) -> Path:
    last = {"pct": -10}

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                pct = int(d.get("downloaded_bytes", 0) * 100 / total)
                if pct - last["pct"] >= 10:
                    last["pct"] = pct
                    log(f"下载 {pct}%（{total / 1e6:.1f} MB）")

    opts = _ydl_opts(
        format="bestaudio[ext=m4a]/bestaudio/best[height<=480]",
        outtmpl=str(dest_dir / "source.%(ext)s"),
        progress_hooks=[hook],
        retries=3,
    )
    if http_headers:
        opts["http_headers"] = http_headers
    log("用 yt-dlp 下载音频…")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        raise PlatformError(f"音频下载失败：{e}") from e
    if not path.exists():
        cands = sorted(dest_dir.glob("source.*"))
        if not cands:
            raise PlatformError("音频下载失败：找不到下载文件")
        path = cands[0]
    return path
