"""在线探测（需联网）：.venv/bin/python scripts/live_probe.py <url> [...]，打印元数据与字幕获取情况。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import platforms, transcript as tr  # noqa: E402

for url in sys.argv[1:]:
    print("=" * 60, "\n", url)
    name = platforms.detect(url)
    p = platforms.get(name)
    info = p.resolve(url, print)
    print(f"[{name}] {info.title} | {info.author} | {info.duration:.0f}s | {info.publish_date} | {info.url}")
    t = p.fetch_subtitle(info, print)
    if t:
        print(t.detail, "segments", len(t.segments), "chars", t.char_count, "check:", tr.sanity_check(t, info.duration))
        print(tr.to_llm_text(t)[:400])
    else:
        print("无平台字幕 → 本地转写")
