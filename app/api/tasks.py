"""REST 路由（方案 §4.8）。"""
from __future__ import annotations

import json
import shutil
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import db, pipeline, platforms, worker
from app.asr.qwen3_mlx import _current as asr_procs
from app.config import settings
from app.platforms import UnsupportedURL

router = APIRouter(prefix="/api")


class CreateReq(BaseModel):
    url: str
    force_asr: bool = False
    diarize: Optional[bool] = None  # None = 按平台默认（小宇宙开，其他关）
    num_speakers: Optional[int] = Field(default=None, ge=1, le=10)
    force_new: bool = False  # 已有结果时仍新建任务


def _public(t: dict) -> dict:
    t = dict(t)
    t["queue_position"] = worker.queue_position(t["id"]) if t["status"] == "queued" else -1
    d = pipeline.task_dir(t["id"])
    t["files"] = {
        "transcript": (d / "transcript.json").exists(),
        "note": (d / "note.md").exists(),
    }
    return t


def _get_or_404(tid: str) -> dict:
    t = db.get(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@router.post("/tasks")
def create_task(req: CreateReq):
    try:
        platform = platforms.detect(req.url)
    except UnsupportedURL as e:
        raise HTTPException(400, str(e))
    free = pipeline.free_gb()
    if free < settings.min_free_gb:
        raise HTTPException(507, f"磁盘剩余 {free:.1f}GB，低于 {settings.min_free_gb}GB，请先清理空间")
    if not req.force_new:
        # 能离线拿到 id 的平台先查重（B站 b23 短链要联网解析，跳过）
        media_id = _offline_media_id(platform, req.url)
        if media_id:
            done = db.find_done(platform, media_id)
            if done:
                return {"exists": True, "task_id": done["id"], "title": done["title"]}
    opts = {"force_asr": req.force_asr, "diarize": req.diarize, "num_speakers": req.num_speakers}
    t = db.create(req.url.strip(), platform, opts)
    worker.submit(t["id"])
    return {"exists": False, "task_id": t["id"]}


def _offline_media_id(platform: str, url: str) -> Optional[str]:
    from app.platforms import bilibili, xiaoyuzhou, youtube

    if platform == "xiaoyuzhou":
        return xiaoyuzhou.match(url)
    if platform == "youtube":
        return youtube.match(url)
    m = bilibili.match(url)
    if m and m[0].startswith("BV"):
        return f"{m[0]}_p{m[1]}"
    return None


@router.get("/tasks")
def list_tasks():
    return [_public(t) for t in db.list_all()]


@router.get("/tasks/{tid}")
def get_task(tid: str):
    t = _public(_get_or_404(tid))
    p = pipeline.task_dir(tid) / "info.json"
    t["info"] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    t["running"] = worker.current["id"] == tid
    return t


@router.get("/tasks/{tid}/transcript")
def get_transcript(tid: str, format: str = "md", download: bool = False):
    t = _get_or_404(tid)
    ext = {"md": "md", "srt": "srt", "json": "json"}.get(format)
    if not ext:
        raise HTTPException(400, "format 只能是 md / srt / json")
    p = pipeline.task_dir(tid) / f"transcript.{ext}"
    if not p.exists():
        raise HTTPException(404, "逐字稿尚未生成")
    media = {"md": "text/markdown", "srt": "application/x-subrip", "json": "application/json"}[ext]
    if download:
        return FileResponse(p, media_type=f"{media}; charset=utf-8", filename=f"{_safe_name(t)}.{ext}")
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type=f"{media}; charset=utf-8")


@router.get("/tasks/{tid}/note")
def get_note(tid: str, download: bool = False):
    t = _get_or_404(tid)
    p = pipeline.task_dir(tid) / "note.md"
    if not p.exists():
        raise HTTPException(404, "笔记尚未生成")
    if download:
        return FileResponse(p, media_type="text/markdown; charset=utf-8", filename=f"{_safe_name(t)}-笔记.md")
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@router.get("/tasks/{tid}/log")
def get_log(tid: str):
    _get_or_404(tid)
    p = pipeline.task_dir(tid) / "log.txt"
    return PlainTextResponse(p.read_text(encoding="utf-8")[-20000:] if p.exists() else "", media_type="text/plain; charset=utf-8")


def _busy(t: dict) -> bool:
    return t["status"] == "queued" or t["status"] in db.ACTIVE


@router.post("/tasks/{tid}/regenerate")
def regenerate(tid: str):
    t = _get_or_404(tid)
    if _busy(t):
        raise HTTPException(409, "任务正在进行中")
    if not (pipeline.task_dir(tid) / "transcript.json").exists():
        raise HTTPException(400, "还没有逐字稿，无法只重新生成笔记，请用重试")
    db.update(tid, status="queued", stage_detail="等待重新生成笔记", error_message="", error_stage="")
    worker.submit(tid, "summarize")
    return {"ok": True}


@router.post("/tasks/{tid}/retry")
def retry(tid: str):
    t = _get_or_404(tid)
    if t["status"] != "failed":
        raise HTTPException(409, "只有失败的任务可以重试")
    db.update(tid, status="queued", stage_detail="等待重试", error_message="", error_stage="")
    worker.submit(tid, "full")
    return {"ok": True}


@router.delete("/tasks/{tid}")
def delete_task(tid: str):
    _get_or_404(tid)
    d = pipeline.task_dir(tid)
    if worker.current["id"] == tid:
        pipeline.cancel(tid)
        p = asr_procs.get(str(d))
        if p and p.poll() is None:
            p.kill()
    db.delete(tid)
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


def _safe_name(t: dict) -> str:
    name = (t.get("title") or t["id"]).strip()
    for ch in '\\/:*?"<>|\n\r\t':
        name = name.replace(ch, " ")
    return name[:80].strip() or t["id"]
