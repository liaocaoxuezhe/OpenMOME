"""FastAPI 入口：静态页面、REST API、健康检查。启动：./start.sh"""
from __future__ import annotations

import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import db, pipeline, worker
from app.api.tasks import router
from app.asr.qwen3_mlx import cancel_all
from app.config import ROOT, settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.conn()
    worker.start()
    yield
    cancel_all()


app = FastAPI(title="OpenMOME", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache"})


def _version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        return out.splitlines()[0] if out else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _model_ready(path: str) -> bool:
    if Path(path).exists():
        return any(Path(path).glob("*.safetensors"))
    hub = Path(os.getenv("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    snap = hub / f"models--{path.replace('/', '--')}" / "snapshots"
    return snap.exists() and any(snap.rglob("*.safetensors"))


@app.get("/api/health")
def health():
    import yt_dlp

    pyannote = Path.home() / ".cache" / "huggingface" / "hub" / "models--pyannote--speaker-diarization-community-1"
    checks = {
        "ffmpeg": _version(["ffmpeg", "-version"]).split(" Copyright")[0],
        "deno": _version(["deno", "--version"]),
        "yt_dlp": yt_dlp.version.__version__,
        "asr_model": settings.asr_model,
        "asr_model_ready": _model_ready(settings.asr_model_path),
        "aligner_ready": _model_ready(settings.aligner_path),
        "pyannote_ready": pyannote.exists(),
        "hf_token": bool(settings.hf_token),
        "llm": f"{settings.llm_model} @ {settings.llm_base_url}",
        "llm_key": bool(settings.llm_api_key),
        "bilibili_cookie": bool(settings.bilibili_cookie),
        "disk_free_gb": round(pipeline.free_gb(), 1),
        "min_free_gb": settings.min_free_gb,
        "keep_audio": settings.keep_audio,
    }
    problems = []
    if not checks["ffmpeg"]:
        problems.append("未找到 ffmpeg（brew install ffmpeg）")
    if not checks["deno"]:
        problems.append("未找到 Deno，YouTube 可能下载失败（brew install deno）")
    if not checks["asr_model_ready"] or not checks["aligner_ready"]:
        problems.append("ASR 模型未下载（.venv/bin/python scripts/download_models.py），首次转写时会自动从 Hugging Face 下载，较慢")
    if not checks["hf_token"]:
        problems.append("未配置 HF_TOKEN：只转写，不区分说话人")
    if not checks["llm_key"]:
        problems.append("未配置 DEEPSEEK_API_KEY：无法生成笔记")
    if not checks["bilibili_cookie"]:
        problems.append("未配置 B站 Cookie：B站视频一律本地转写")
    if checks["disk_free_gb"] < settings.min_free_gb:
        problems.append(f"磁盘剩余不足 {settings.min_free_gb}GB，不接受新任务")
    return {"ok": not any("无法" in p or "不接受" in p or "ffmpeg" in p for p in problems), "checks": checks, "problems": problems}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
