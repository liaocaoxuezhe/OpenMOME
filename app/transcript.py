"""字幕/转写结果解析成 Segment，以及导出 md / srt / json、健全性检查。"""
from __future__ import annotations

import html
import json
import re
from typing import Optional

from app.models import Segment, Transcript

# ---------------------------------------------------------------- 时间格式


def fmt_clock(sec: float) -> str:
    """[mm:ss]，超过 1 小时为 [h:mm:ss]。"""
    s = int(max(sec, 0))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_srt_time(sec: float) -> str:
    ms = int(round(max(sec, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_clock(text: str) -> Optional[int]:
    """'12:34' / '1:02:03' → 秒。"""
    parts = text.strip().split(":")
    if not all(p.isdigit() for p in parts) or not 2 <= len(parts) <= 3:
        return None
    n = [int(p) for p in parts]
    return n[0] * 60 + n[1] if len(n) == 2 else n[0] * 3600 + n[1] * 60 + n[2]


_TS_RE = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})|(\d+):(\d{2})[.,](\d{1,3})")


def _vtt_time(t: str) -> float:
    m = _TS_RE.search(t)
    if not m:
        raise ValueError(t)
    if m.group(1) is not None:
        h, mi, s, ms = m.group(1, 2, 3, 4)
    else:
        h, (mi, s, ms) = "0", m.group(5, 6, 7)
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


# ---------------------------------------------------------------- 解析器

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    text = html.unescape(_TAG_RE.sub("", text))
    return re.sub(r"\s+", " ", text).strip()


def parse_vtt(text: str) -> list[Segment]:
    """WebVTT / SRT 通用解析；自动去掉 YouTube 滚动字幕产生的重复行。"""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    segs: list[Segment] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->")[:2]
        try:
            start, end = _vtt_time(a), _vtt_time(b.split()[0] if b.split() else b)
        except ValueError:
            continue
        body = _clean(" ".join(lines[idx + 1 :]))
        if body:
            segs.append(Segment(start, end, body))
    return dedupe_rolling(segs)


def dedupe_rolling(segs: list[Segment]) -> list[Segment]:
    """YouTube 自动字幕的滚动格式：后一条包含前一条的末尾，去重只保留新增部分。"""
    out: list[Segment] = []
    prev_raw = ""  # 上一条的原始全文（out 里存的可能是裁剪后的增量）
    for s in segs:
        raw = s.text
        if out and prev_raw:
            if raw == prev_raw:
                out[-1].end = max(out[-1].end, s.end)
                continue
            if raw.startswith(prev_raw):
                s = Segment(s.start, s.end, raw[len(prev_raw) :].strip(), s.speaker)
                if not s.text:
                    prev_raw = raw
                    continue
        out.append(s)
        prev_raw = raw
    return out


def parse_json3(text: str) -> list[Segment]:
    """YouTube json3 字幕格式。"""
    data = json.loads(text)
    segs: list[Segment] = []
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        body = _clean("".join(s.get("utf8", "") for s in ev["segs"]))
        if not body:
            continue
        start = ev.get("tStartMs", 0) / 1000
        end = start + ev.get("dDurationMs", 0) / 1000
        segs.append(Segment(start, end, body))
    return dedupe_rolling(segs)


def parse_bilibili_json(data: dict) -> list[Segment]:
    """B站字幕 JSON：{"body": [{"from", "to", "content"}]}。"""
    return [
        Segment(float(it["from"]), float(it["to"]), _clean(it.get("content", "")))
        for it in data.get("body", [])
        if _clean(it.get("content", ""))
    ]


# ---------------------------------------------------------------- 合并与检查

_END_PUNCT = "。！？!?…."


def merge_segments(segs: list[Segment], max_chars: int = 120, max_gap: float = 1.5) -> list[Segment]:
    """把零碎字幕合并成可读的句段（同一说话人、间隔短、未到句末）。导出 md 和喂给 LLM 时使用。"""
    out: list[Segment] = []
    for s in segs:
        if out:
            p = out[-1]
            same = p.speaker == s.speaker
            close = s.start - p.end <= max_gap
            short = len(p.text) + len(s.text) <= max_chars
            if same and close and short and not p.text.endswith(tuple(_END_PUNCT)):
                sep = " " if _is_latin(p.text[-1:]) and _is_latin(s.text[:1]) else ""
                out[-1] = Segment(p.start, max(p.end, s.end), p.text + sep + s.text, p.speaker)
                continue
        out.append(Segment(s.start, s.end, s.text, s.speaker))
    return out


def _is_latin(ch: str) -> bool:
    return bool(ch) and ch.isascii() and not ch.isspace()


def sanity_check(t: Transcript, duration: float) -> Optional[str]:
    """字幕不可用时返回原因；可用返回 None。方案 §4.2。"""
    if not t.segments:
        return "字幕为空"
    if duration > 0:
        covered = sum(max(0.0, s.end - s.start) for s in t.segments)
        span = t.segments[-1].end
        if span < duration * 0.5 and covered < duration * 0.5:
            return f"字幕只覆盖了 {span:.0f}s / {duration:.0f}s（不足 50%）"
        # 英文按词数算字数（每词约 2 个汉字的信息量）
        text = " ".join(s.text for s in t.segments)
        cjk = len(re.findall(r"[一-鿿]", text))
        words = len(re.findall(r"[A-Za-z]+", text))
        units = cjk + words * 2
        need = duration / 60 * 30
        if units < need:
            return f"字幕字数过少（{units} < {need:.0f}）"
    return None


# ---------------------------------------------------------------- 导出


def to_markdown(t: Transcript, title: str = "") -> str:
    lines = [f"# {title}" if title else "# 逐字稿", ""]
    lines.append(f"> 文字来源：{'平台字幕' if t.source == 'platform_subtitle' else '本地转写'}（{t.detail}）")
    lines.append("")
    for s in merge_segments(t.segments):
        who = f"**{s.speaker}**：" if s.speaker else ""
        lines.append(f"`[{fmt_clock(s.start)}]` {who}{s.text}")
        lines.append("")
    return "\n".join(lines)


def to_srt(t: Transcript) -> str:
    out = []
    for i, s in enumerate(t.segments, 1):
        who = f"[{s.speaker}] " if s.speaker else ""
        out.append(f"{i}\n{fmt_srt_time(s.start)} --> {fmt_srt_time(s.end)}\n{who}{s.text}\n")
    return "\n".join(out)


def to_json(t: Transcript) -> str:
    return json.dumps(t.to_dict(), ensure_ascii=False, indent=1)


def to_llm_text(t: Transcript) -> str:
    """喂给 LLM 的格式：[mm:ss] S1: 文本。"""
    lines = []
    for s in merge_segments(t.segments, max_chars=200, max_gap=2.5):
        who = f"{s.speaker}: " if s.speaker else ""
        lines.append(f"[{fmt_clock(s.start)}] {who}{s.text}")
    return "\n".join(lines)
