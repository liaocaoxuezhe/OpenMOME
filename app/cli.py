"""命令行：.venv/bin/python -m app.cli <url> [--force-asr] [--diarize/--no-diarize] [--num-speakers N]

同步执行一次完整流水线（不经过 Web 队列），结果写入 data/tasks/<id>/，与 Web 共用历史记录。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

from app import db, pipeline, platforms


def main() -> int:
    ap = argparse.ArgumentParser(description="OpenMOME 命令行")
    ap.add_argument("url")
    ap.add_argument("--force-asr", action="store_true", help="忽略平台字幕，强制本地转写")
    ap.add_argument("--diarize", dest="diarize", action="store_true", default=None, help="开启说话人分离")
    ap.add_argument("--no-diarize", dest="diarize", action="store_false", help="关闭说话人分离")
    ap.add_argument("--num-speakers", type=int, default=None)
    args = ap.parse_args()

    try:
        platform = platforms.detect(args.url)
    except platforms.UnsupportedURL as e:
        print(f"❌ {e}")
        return 2
    t = db.create(args.url, platform, {"force_asr": args.force_asr, "diarize": args.diarize, "num_speakers": args.num_speakers})
    tid = t["id"]
    print(f"任务 {tid}（{platform}）→ {pipeline.task_dir(tid)}")

    log = pipeline.task_dir(tid) / "log.txt"
    stop = threading.Event()

    def tail():
        pos = 0
        while not stop.is_set():
            if log.exists():
                with open(log, encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                if chunk:
                    print(chunk, end="", flush=True)
            time.sleep(0.5)

    th = threading.Thread(target=tail, daemon=True)
    th.start()
    pipeline.execute(tid)
    time.sleep(0.8)
    stop.set()
    th.join()
    t = db.get(tid)
    if t["status"] == "done":
        d = pipeline.task_dir(tid)
        print(f"\n✅ 完成：\n  逐字稿 {d / 'transcript.md'}\n  笔记   {d / 'note.md'}")
        return 0
    print(f"\n❌ 失败（{t['error_stage']}）：{t['error_message']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
