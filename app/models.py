"""统一数据结构：所有字幕/转写来源都先转成 Transcript，后续导出和 LLM 只认这一种。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional


@dataclass
class Segment:
    start: float  # 秒
    end: float
    text: str
    speaker: Optional[str] = None  # "S1"… 或 None


@dataclass
class Transcript:
    source: Literal["platform_subtitle", "local_asr"]
    language: str
    segments: list[Segment]
    detail: str = ""  # 例如 "YouTube 人工字幕 en" / "Qwen3-ASR-1.7B"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        return cls(
            source=d["source"],
            language=d.get("language", ""),
            segments=[Segment(**s) for s in d["segments"]],
            detail=d.get("detail", ""),
        )

    @property
    def char_count(self) -> int:
        return sum(len(s.text) for s in self.segments)


@dataclass
class MediaInfo:
    platform: Literal["youtube", "bilibili", "xiaoyuzhou"]
    id: str
    url: str  # 规范化后的链接
    title: str = ""
    author: str = ""
    duration: float = 0.0  # 秒
    publish_date: str = ""  # YYYY-MM-DD
    extra: dict = field(default_factory=dict)  # 平台私有字段（如 B站 cid、小宇宙音频直链）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MediaInfo":
        return cls(**d)
