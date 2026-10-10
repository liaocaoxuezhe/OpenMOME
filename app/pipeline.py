"""流水线编排与状态机（方案 §4.7）。

queued → resolving → fetching_subtitle ─┬─(有字幕)──────────────────────► summarizing → done
                                         └─(无字幕) downloading → transcribing ─┘
每一步产物落盘到 data/tasks/<id>/，重试时已有产物的步骤直接跳过。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import traceback
from pathlib import Path
from typing import Optional

from app import db, llm, platforms
from app import transcript as tr
from app.asr import ASRError, get_provider as get_asr
from app.config import redact, settings
from app.llm import LLMError
from app.models import MediaInfo, Transcript
from app.platforms import PlatformError, UnsupportedURL

AUDIO_EXTS = (".m4a", ".mp3", ".webm", ".opus", ".aac", ".ogg", ".mp4", ".flac", ".wav")


class Cancelled(Exception):
    pass


_cancelled: set[str] = set()


def cancel(tid: str) -> None:
    _cancelled.add(tid)


def task_dir(tid: str) -> Path:
    return settings.tasks_dir / tid


def free_gb() -> float:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(settings.data_dir).free / 1e9


class Run:
    """一次任务执行的上下文：日志、进度、状态更新。"""

    def __init__(self, tid: str):
        self.tid = tid
        self.dir = task_dir(tid)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.task = db.get(tid)
        self.opts = self.task["options"]
        self.stage = "queued"

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {redact(msg)}"
        with open(self.dir / "log.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.check_cancel()

    def check_cancel(self) -> None:
        if self.tid in _cancelled:
            raise Cancelled()

    def set_stage(self, stage: str, detail: str = "") -> None:
        self.check_cancel()
        self.stage = stage
        db.update(self.tid, status=stage, stage_detail=detail, progress=0)
        if detail:
            self.log(detail)

    def progress(self, p: float, detail: str) -> None:
        fields = {"stage_detail": detail}
        if p >= 0:
            fields["progress"] = round(p, 4)
        db.update(self.tid, **fields)
        self.check_cancel()

    def warn(self, msg: str) -> None:
        self.log("⚠️ " + msg)
        db.add_warning(self.tid, msg)

    # ---------------------------------------------------------------- 产物读写

    def write(self, name: str, content: str) -> None:
        (self.dir / name).write_text(content, encoding="utf-8")

    def load_info(self) -> Optional[MediaInfo]:
        p = self.dir / "info.json"
        return MediaInfo.from_dict(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None

    def save_info(self, info: MediaInfo) -> None:
        d = info.to_dict()
        # 字幕下载地址是带签名的临时链接，不落盘
        d["extra"] = {k: v for k, v in d["extra"].items() if k not in ("subtitles", "automatic_captions")}
        self.write("info.json", json.dumps(d, ensure_ascii=False, indent=1))

    def load_transcript(self) -> Optional[Transcript]:
        p = self.dir / "transcript.json"
        return Transcript.from_dict(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else None

    def save_transcript(self, t: Transcript, info: MediaInfo) -> None:
        self.write("transcript.json", tr.to_json(t))
        self.write("transcript.md", tr.to_markdown(t, info.title))
        self.write("transcript.srt", tr.to_srt(t))
        db.update(self.tid, text_source=t.source)


# ---------------------------------------------------------------- 各步骤


def step_resolve(run: Run) -> MediaInfo:
    run.set_stage("resolving", "解析链接、获取元数据…")
    p = platforms.get(run.task["platform"])
    info = p.resolve(run.task["url"], run.log)
    run.save_info(info)
    db.update(run.tid, media_id=info.id, title=info.title, author=info.author, duration=info.duration)
    run.log(f"《{info.title}》 {info.author} · 时长 {info.duration / 60:.1f} 分钟")
    return info


def step_subtitle(run: Run, info: MediaInfo) -> Optional[Transcript]:
    if run.opts.get("force_asr"):
        run.log("已勾选「强制本地转写」，跳过平台字幕")
        return None
    if info.platform == "xiaoyuzhou":
        run.log("小宇宙一律本地转写（官方文字稿需登录，有风控风险）")
        return None
    run.set_stage("fetching_subtitle", "查找平台字幕…")
    p = platforms.get(info.platform)
    try:
        t = p.fetch_subtitle(info, run.log)
    except Exception as e:  # 字幕失败不致命，降级为本地转写
        run.log(f"获取字幕出错，改走本地转写：{type(e).__name__}: {e}")
        return None
    if info.platform == "bilibili" and t is None and not settings.bilibili_cookie:
        run.warn("未配置 B站 Cookie，B站字幕不可用，已自动改为本地转写")
    if t is None:
        return None
    reason = tr.sanity_check(t, info.duration)
    if reason:
        run.warn(f"平台字幕不可用（{reason}），改为本地转写")
        return None
    run.log(f"使用平台字幕：{t.detail}，{len(t.segments)} 条，{t.char_count} 字（跳过下载和转写）")
    return t


def step_download(run: Run, info: MediaInfo) -> Path:
    existing = [p for p in run.dir.glob("source.*") if p.suffix in AUDIO_EXTS and p.stat().st_size > 0]
    if existing:
        run.log(f"复用已下载的音频 {existing[0].name}")
        return existing[0]
    if free_gb() < settings.min_free_gb:
        raise PlatformError(f"磁盘剩余 {free_gb():.1f}GB，低于 {settings.min_free_gb}GB，不下载")
    run.set_stage("downloading", "下载音频…")
    p = platforms.get(info.platform)
    path = p.download_audio(info, run.dir, lambda m: (run.log(m), run.progress(-1, m)))
    run.log(f"音频已下载：{path.name}（{path.stat().st_size / 1e6:.1f} MB）")
    return path


def to_wav(src: Path, dst: Path) -> None:
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(dst)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or not dst.exists():
        raise ASRError(f"ffmpeg 转换失败：{r.stderr[-500:]}")


def probe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True,
        text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def step_transcribe(run: Run, info: MediaInfo, audio: Path) -> Transcript:
    run.set_stage("transcribing", "转换音频格式…")
    wav = run.dir / "audio.wav"
    to_wav(audio, wav)
    duration = info.duration or probe_duration(wav)
    if not info.duration and duration:
        info.duration = duration
        run.save_info(info)
        db.update(run.tid, duration=duration)
    diarize = run.opts.get("diarize")
    if diarize is None:  # 默认：小宇宙开，YouTube / B站关
        diarize = info.platform == "xiaoyuzhou"
    if diarize and not settings.hf_token:
        run.warn("未配置 HF_TOKEN，只转写、不区分说话人")
    asr = get_asr()
    try:
        t = asr.transcribe(
            wav,
            run.dir,
            duration=duration,
            diarize=bool(diarize),
            num_speakers=run.opts.get("num_speakers") or None,
            log=run.log,
            progress=run.progress,
        )
    finally:
        wav.unlink(missing_ok=True)
        (run.dir / "asr_raw.json").unlink(missing_ok=True)
    if diarize and settings.hf_token and "说话人分离" not in t.detail:
        run.warn("说话人分离未成功（可能未在 Hugging Face 接受 pyannote 条款），只输出了逐字稿")
    if not t.segments:
        raise ASRError("转写结果为空（音频可能没有人声）")
    return t


def step_summarize(run: Run, info: MediaInfo, t: Transcript) -> None:
    run.set_stage("summarizing", "生成分析笔记…")
    t0 = time.time()
    body = llm.generate_note(t, info, log=run.log, progress=lambda d: run.progress(-1, d))
    run.write("note_raw.md", body)
    p = platforms.get(info.platform)
    run.write("note.md", llm.render_note(body, t, info, lambda s: p.timestamp_url(info, s)))
    run.log(f"笔记生成完成，用时 {time.time() - t0:.0f}s")


def cleanup_audio(run: Run) -> None:
    if settings.keep_audio:
        return
    for p in run.dir.glob("source.*"):
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- 入口


def execute(tid: str, mode: str = "full") -> None:
    """mode = full（含重试，已有产物的步骤跳过）/ summarize（只重跑笔记）。"""
    if not db.get(tid):
        return
    run = Run(tid)
    t_start = time.time()
    db.update(tid, error_stage="", error_message="")
    run.log(f"===== 开始任务（{mode}） =====")
    try:
        info = run.load_info() or step_resolve(run)
        t = run.load_transcript()
        if t is None:
            t = step_subtitle(run, info)
            if t is None:
                audio = step_download(run, info)
                t = step_transcribe(run, info, audio)
            run.save_transcript(t, info)
            cleanup_audio(run)
        else:
            run.log("复用已有逐字稿")
        step_summarize(run, info, t)
        db.update(tid, status="done", stage_detail=f"完成，总耗时 {time.time() - t_start:.0f}s", progress=1)
        run.log(f"===== 完成，总耗时 {time.time() - t_start:.0f}s =====")
    except Cancelled:
        _cancelled.discard(tid)
        return
    except Exception as e:  # noqa: BLE001
        if isinstance(e, (UnsupportedURL, PlatformError, ASRError, LLMError)):
            msg = str(e)
        else:
            msg = f"{type(e).__name__}: {e}"
        msg = redact(msg)
        try:
            with open(run.dir / "log.txt", "a", encoding="utf-8") as f:
                f.write(redact(traceback.format_exc()) + "\n")
        except OSError:
            pass
        if db.get(tid):
            db.update(tid, status="failed", error_stage=run.stage, error_message=msg[:2000])
        if run.stage != "summarizing":
            # 转写阶段失败时保留已下载的原始音频，方便重试；其余中间文件清掉
            (run.dir / "audio.wav").unlink(missing_ok=True)
