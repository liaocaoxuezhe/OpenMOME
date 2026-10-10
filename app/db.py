"""SQLite：只存任务状态与元数据；逐字稿等大文本放在任务目录文件里。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from app.config import settings

ACTIVE = ("resolving", "fetching_subtitle", "downloading", "transcribing", "summarizing")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    platform      TEXT NOT NULL,
    media_id      TEXT,
    title         TEXT DEFAULT '',
    author        TEXT DEFAULT '',
    duration      REAL DEFAULT 0,
    status        TEXT NOT NULL,
    stage_detail  TEXT DEFAULT '',
    progress      REAL DEFAULT 0,
    text_source   TEXT DEFAULT '',
    options       TEXT DEFAULT '{}',
    warnings      TEXT DEFAULT '[]',
    error_stage   TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_media ON tasks(platform, media_id);
"""

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
    if r is None:
        return None
    d = dict(r)
    d["options"] = json.loads(d.get("options") or "{}")
    d["warnings"] = json.loads(d.get("warnings") or "[]")
    return d


def create(url: str, platform: str, options: dict) -> dict:
    now = time.time()
    tid = uuid.uuid4().hex[:12]
    with _lock:
        conn().execute(
            "INSERT INTO tasks (id, url, platform, status, options, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (tid, url, platform, "queued", json.dumps(options, ensure_ascii=False), now, now),
        )
    return get(tid)


def get(tid: str) -> Optional[dict]:
    return _row(conn().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())


def list_all(limit: int = 200) -> list[dict]:
    rows = conn().execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def find_done(platform: str, media_id: str) -> Optional[dict]:
    r = conn().execute(
        "SELECT * FROM tasks WHERE platform=? AND media_id=? AND status='done' ORDER BY created_at DESC LIMIT 1",
        (platform, media_id),
    ).fetchone()
    return _row(r)


def update(tid: str, **fields: Any) -> None:
    if not fields:
        return
    for k in ("options", "warnings"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        conn().execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), tid))


def add_warning(tid: str, msg: str) -> None:
    t = get(tid)
    if t and msg not in t["warnings"]:
        update(tid, warnings=t["warnings"] + [msg])


def delete(tid: str) -> None:
    with _lock:
        conn().execute("DELETE FROM tasks WHERE id=?", (tid,))


def mark_interrupted() -> list[str]:
    """服务重启时：运行中的任务标记失败（可重试）；返回仍在排队的任务 id 以便重新入队。"""
    with _lock:
        c = conn()
        q = ",".join("?" * len(ACTIVE))
        c.execute(
            f"UPDATE tasks SET error_stage=status, status='failed', error_message='服务重启，任务被中断，可点击重试', "
            f"updated_at=? WHERE status IN ({q})",
            (time.time(), *ACTIVE),
        )
        rows = c.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at").fetchall()
    return [r["id"] for r in rows]
