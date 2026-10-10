"""单线程任务队列：同一时刻只跑 1 个任务（16GB 内存不允许并发转写）。"""
from __future__ import annotations

import queue
import threading
from typing import Optional

from app import db, pipeline

_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
_thread: Optional[threading.Thread] = None
current: dict[str, Optional[str]] = {"id": None}


def _loop() -> None:
    while True:
        tid, mode = _q.get()
        current["id"] = tid
        try:
            pipeline.execute(tid, mode)
        finally:
            current["id"] = None
            _q.task_done()


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    for tid in db.mark_interrupted():
        _q.put((tid, "full"))
    _thread = threading.Thread(target=_loop, name="openmome-worker", daemon=True)
    _thread.start()


def submit(tid: str, mode: str = "full") -> None:
    _q.put((tid, mode))


def queue_position(tid: str) -> int:
    """排在前面的任务数（不含正在运行的）；不在队列里返回 -1。"""
    items = [t for t, _ in list(_q.queue)]
    return items.index(tid) if tid in items else -1
