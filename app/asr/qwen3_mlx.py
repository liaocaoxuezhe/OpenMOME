"""Qwen3-ASR（MLX）：在子进程里运行 app.asr.qwen3_worker，解析其 JSON 输出。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from app.asr.base import ASRError, ASRProvider, ProgressFn
from app.config import ROOT, redact, settings
from app.models import Segment, Transcript

# 正在运行的子进程（任务被删除/服务关闭时用于终止）
_current: dict[str, subprocess.Popen] = {}


def cancel_all() -> None:
    for p in list(_current.values()):
        if p.poll() is None:
            p.kill()


class Qwen3MLX(ASRProvider):
    name = "qwen3-mlx"

    def __init__(self, model: Optional[str] = None):
        self.model = model or settings.asr_model

    @property
    def label(self) -> str:
        return self.model.split("/")[-1]

    def transcribe(
        self,
        audio: Path,
        work_dir: Path,
        *,
        duration: float,
        diarize: bool,
        num_speakers: Optional[int],
        log: Callable[[str], None],
        progress: ProgressFn,
    ) -> Transcript:
        out = work_dir / "asr_raw.json"
        cmd = [
            sys.executable, "-m", "app.asr.qwen3_worker", "--audio", str(audio), "--out", str(out),
            "--model", settings.local_model(self.model), "--aligner", settings.aligner_path,
        ]
        env = dict(os.environ)
        env.setdefault("HF_HUB_DISABLE_XET", "1")  # Xet 通道走代理时容易卡住，用普通 HTTP
        env["TOKENIZERS_PARALLELISM"] = "false"
        if diarize:
            if settings.hf_token:
                env["HF_TOKEN"] = settings.hf_token
                cmd.append("--diarize")
                if num_speakers:
                    cmd += ["--num-speakers", str(num_speakers)]
            else:
                log("未配置 HF_TOKEN，跳过说话人分离（只转写）")
                diarize = False
        # M0 实测 1.7B + 说话人分离 RTF 最高约 0.5（后台有负载时），留一倍余量
        timeout = duration * 1.0 + 600 if duration > 0 else 4 * 3600
        log(f"启动转写子进程：{self.label}{' + 说话人分离' if diarize else ''}，超时 {timeout / 60:.0f} 分钟")

        t0 = time.time()
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1
        )
        key = str(work_dir)
        _current[key] = proc
        stderr_tail: deque[str] = deque(maxlen=40)
        threading.Thread(target=lambda: [stderr_tail.append(ln) for ln in proc.stderr], daemon=True).start()
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        diarized = diarize
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = ev.get("event")
                if kind == "progress":
                    p = float(ev["progress"])
                    el = time.time() - t0
                    eta = f"，预计还需 {el / p - el:.0f}s" if p > 0.02 else ""
                    progress(p, f"转写 {p * 100:.0f}%{eta}")
                elif kind == "stage":
                    progress(-1, ev.get("message", ""))
                    log(ev.get("message", ""))
                elif kind == "warning":
                    log("⚠️ " + ev.get("message", ""))
                    if "说话人分离失败" in ev.get("message", ""):
                        diarized = False
                elif kind == "done":
                    log(f"转写完成：{ev.get('segments')} 段，语言 {ev.get('language')}，耗时 {ev.get('elapsed')}s")
            code = proc.wait()
        finally:
            timer.cancel()
            _current.pop(key, None)
        if code != 0 or not out.exists():
            tail = redact("".join(stderr_tail))[-1500:]
            if time.time() - t0 >= timeout - 1:
                raise ASRError(f"转写超时（>{timeout / 60:.0f} 分钟）")
            if code < 0:  # 被信号终止：服务关闭或任务被删除
                raise ASRError("转写被中断（服务关闭或任务被删除），可点击重试，已下载的音频会复用")
            raise ASRError(f"转写子进程失败（exit {code}）：\n{tail}")

        data = json.loads(out.read_text(encoding="utf-8"))
        segs = [Segment(float(s["start"]), float(s["end"]), s["text"], s.get("speaker")) for s in data["segments"] if s["text"].strip()]
        detail = self.label + (" + pyannote 说话人分离" if data.get("diarized") and diarized else "")
        return Transcript(source="local_asr", language=data.get("language", ""), segments=segs, detail=detail)
