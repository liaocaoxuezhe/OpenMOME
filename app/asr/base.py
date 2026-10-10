"""ASRProvider 抽象：为后续可选模型（0.6B、Whisper、FunASR…）预留。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from app.models import Transcript

ProgressFn = Callable[[float, str], None]  # (0~1, 说明文字)


class ASRError(RuntimeError):
    pass


class ASRProvider:
    name: str = ""

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
        raise NotImplementedError
