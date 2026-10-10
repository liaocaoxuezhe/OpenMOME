"""说话人分离子进程：python -m app.asr.diarize_worker --audio X --out Y [--device cpu]

与转写进程同时运行：转写用 GPU（MLX），这里默认用 CPU 跑 pyannote，两者不抢资源。
输出 {"turns": [{"speaker", "start", "end"}, ...]}；stdout 每行一个 JSON 事件。
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def emit(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--device", default="cpu", choices=["cpu", "mps", "auto"])
    ap.add_argument("--num-speakers", type=int, default=None)
    ap.add_argument("--threads", type=int, default=0, help="torch CPU 线程数，0 为默认")
    args = ap.parse_args()

    t0 = time.time()
    from app.asr.qwen3_worker import watch_parent

    watch_parent()
    import torch

    if args.threads > 0:
        torch.set_num_threads(args.threads)
    from mlx_qwen3_asr.audio import load_audio_np
    from mlx_qwen3_asr.diarization import infer_speaker_turns, validate_diarization_config

    audio = load_audio_np(args.audio)
    config = validate_diarization_config(
        num_speakers=args.num_speakers, min_speakers=1, max_speakers=8, device=args.device
    )
    emit(event="stage", message=f"说话人分离开始（{args.device}）")
    turns = infer_speaker_turns(audio, sr=16000, config=config)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"turns": turns}, f, ensure_ascii=False)
    emit(event="done", elapsed=round(time.time() - t0, 1), turns=len(turns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
