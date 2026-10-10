"""Platform 抽象：识别链接 / 取元数据 / 取字幕 / 下载音频。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from app.models import MediaInfo, Transcript

LogFn = Callable[[str], None]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class UnsupportedURL(ValueError):
    """链接无法识别或不支持，message 直接展示给用户。"""


class PlatformError(RuntimeError):
    """平台侧错误（付费内容、页面结构变化、下载失败等），message 展示给用户。"""


class Platform:
    name: str = ""
    label: str = ""

    def resolve(self, url: str, log: LogFn) -> MediaInfo:
        raise NotImplementedError

    def fetch_subtitle(self, info: MediaInfo, log: LogFn) -> Optional[Transcript]:
        """返回平台字幕；没有可用字幕时返回 None（流水线会改走本地转写）。"""
        return None

    def download_audio(self, info: MediaInfo, dest_dir: Path, log: LogFn) -> Path:
        raise NotImplementedError

    def timestamp_url(self, info: MediaInfo, seconds: int) -> Optional[str]:
        """笔记里时间戳跳转链接；不支持定位的平台返回 None。"""
        return None
