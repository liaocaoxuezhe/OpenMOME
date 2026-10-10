"""分析笔记生成：长度 ≤ 阈值单次生成；超长按时间切段 Map → Reduce。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from app import transcript as tr
from app.llm.base import LLMError, LLMProvider
from app.llm.openai_compat import OpenAICompat
from app.models import MediaInfo, Transcript

PROMPT_DIR = Path(__file__).parent / "prompts"
SINGLE_PASS_MAX_CHARS = 40_000
MAP_CHUNK_SECONDS = 20 * 60
PLATFORM_LABEL = {"youtube": "YouTube", "bilibili": "B站", "xiaoyuzhou": "小宇宙"}


def get_provider() -> LLMProvider:
    return OpenAICompat()


def load_prompt(name: str) -> tuple[str, str]:
    raw = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    _, system, user = re.split(r"<!--\s*(?:system|user)\s*-->", raw)
    return system.strip(), user.strip()


def fill(template: str, **kw) -> str:
    for k, v in kw.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def fmt_duration(sec: float) -> str:
    m_total = int(round(sec / 60))
    h, m = m_total // 60, m_total % 60
    return f"{h} 小时 {m} 分钟" if h else f"{m} 分钟"


def split_by_time(lines: list[tuple[float, str]], seconds: int) -> list[list[tuple[float, str]]]:
    chunks: list[list[tuple[float, str]]] = []
    for start, line in lines:
        if not chunks or start >= (len(chunks)) * seconds:
            chunks.append([])
        chunks[-1].append((start, line))
    return [c for c in chunks if c]


def generate_note(
    t: Transcript,
    info: MediaInfo,
    *,
    log: Callable[[str], None],
    progress: Callable[[str], None],
    provider: Optional[LLMProvider] = None,
) -> str:
    """返回 LLM 生成的笔记正文（不含头部元信息；时间戳仍为 [mm:ss] 文本）。"""
    llm = provider or get_provider()
    meta = dict(
        title=info.title,
        author=info.author,
        platform=PLATFORM_LABEL.get(info.platform, info.platform),
        duration=fmt_duration(info.duration or (t.segments[-1].end if t.segments else 0)),
        description=(info.extra.get("description") or "（无）")[:1500],
        speaker_note=(
            "逐字稿带有说话人标签（S1、S2…），引用和归纳观点时请标注说话人。"
            if any(s.speaker for s in t.segments)
            else "逐字稿没有说话人标签：不要写 S1/S2 等任何说话人标记，金句摘录只写原话和时间戳。"
        ),
    )
    text = tr.to_llm_text(t)
    sys_note, user_note = load_prompt("note")

    if len(text) <= SINGLE_PASS_MAX_CHARS:
        log(f"逐字稿 {len(text)} 字，单次生成笔记（{llm.model}）")
        return llm.chat(sys_note, fill(user_note, transcript=text, **meta), on_delta=lambda n: progress(f"生成笔记中…已输出 {n} 字"))

    # Map：按 20 分钟切段
    merged = tr.merge_segments(t.segments, max_chars=200, max_gap=2.5)
    lines = [(s.start, f"[{tr.fmt_clock(s.start)}] {(s.speaker + ': ') if s.speaker else ''}{s.text}") for s in merged]
    chunks = split_by_time(lines, MAP_CHUNK_SECONDS)
    log(f"逐字稿 {len(text)} 字，超过 {SINGLE_PASS_MAX_CHARS} 字，分 {len(chunks)} 段先摘要再汇总")
    sys_map, user_map = load_prompt("note_map")
    partials = []
    for i, ch in enumerate(chunks, 1):
        rng = f"{tr.fmt_clock(ch[0][0])} ~ {tr.fmt_clock(ch[-1][0])}"
        progress(f"分段摘要 {i}/{len(chunks)}（{rng}）")
        body = "\n".join(ln for _, ln in ch)
        out = llm.chat(sys_map, fill(user_map, part=i, total=len(chunks), range=rng, transcript=body, **meta), max_tokens=4000)
        partials.append(f"## 第 {i} 部分（{rng}）\n\n{out}")
    # Reduce
    digest = "（逐字稿过长，以下是按时间分段整理的详细要点摘要，时间戳均来自原逐字稿）\n\n" + "\n\n".join(partials)
    progress("汇总生成最终笔记…")
    return llm.chat(sys_note, fill(user_note, transcript=digest, **meta), on_delta=lambda n: progress(f"汇总笔记中…已输出 {n} 字"))


TS_RE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\](?!\()")


def render_note(body: str, t: Transcript, info: MediaInfo, ts_url: Callable[[int], Optional[str]]) -> str:
    """加上头部元信息，时间戳校验：超出总时长的去掉方括号；可定位的平台转为链接。"""
    body = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", body.strip())
    total = info.duration or (t.segments[-1].end if t.segments else 0)

    def repl(m: re.Match) -> str:
        sec = tr.parse_clock(m.group(1))
        if sec is None or (total and sec > total + 5):
            return m.group(1) + "(?)"
        url = ts_url(sec)
        return f"[{m.group(1)}]({url})" if url else f"`{m.group(1)}`"

    body = TS_RE.sub(repl, body)
    body = re.sub(r"(\]\([^)]*\)|`|\(\?\))(?=\[\d|`\d)", r"\1 ", body)  # 相邻时间戳之间加空格
    src = "平台字幕" if t.source == "platform_subtitle" else "本地转写"
    head = [
        f"# {info.title}",
        "",
        f"> 来源：{PLATFORM_LABEL.get(info.platform, info.platform)} · {info.author or '未知作者'} · {fmt_duration(total)}"
        f"{' · ' + info.publish_date if info.publish_date else ''} · [原链接]({info.url})  ",
        f"> 文字来源：{src}（{t.detail}）",
        "",
    ]
    return "\n".join(head) + "\n" + body + "\n"


__all__ = ["LLMError", "LLMProvider", "generate_note", "render_note", "get_provider"]
