"""ASR 子进程入口：python -m app.asr.qwen3_worker --audio X --out Y [...]

在独立进程里加载 MLX / pyannote，跑完即退出，内存全部归还；崩溃也不影响 Web 服务。
stdout 每行一个 JSON 事件：{"event": "progress", "progress": 0.42} / {"event": "done"}。
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import subprocess
import sys
import threading
import time
from typing import Optional

END_PUNCT = set("。！？!?…")
CLAUSE_PUNCT = set("，,；;：:")


def emit(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


OPENING = set("“‘「『（([《")
PUNCT = END_PUNCT | CLAUSE_PUNCT | set("、.”’」』）)]》\"'—-")


def _match_at(text: str, start: int, chars: list[str]) -> int:
    """从 text[start] 开始按顺序匹配 chars（允许中间夹空格），成功返回结束位置，失败返回 -1。"""
    k = start
    for c in chars:
        while k < len(text) and text[k].isspace():
            k += 1
        if k >= len(text) or text[k].casefold() != c.casefold():
            return -1
        k += 1
    return k


def restore_punct(words: list[dict], text: str, window: int = 40) -> list[dict]:
    """把转写全文里的标点挂回对齐器的词/字上。

    库自带的 restore_punctuation 要求逐字完全一致，有一处不一致就整体放弃；
    这里遇到不一致时在后面 window 个字符内重新同步，只影响局部。
    """
    i, n, out = 0, len(text), []
    for w in words:
        chars = [c for c in str(w.get("text", "")) if not c.isspace()]
        if not chars:
            out.append(w)
            continue
        end = -1
        for start in range(i, min(n, i + window)):
            if text[start].isspace() or text[start] in PUNCT:
                continue
            end = _match_at(text, start, chars)
            if end != -1:
                break
        if end == -1:
            out.append(w)
            continue
        trailing = []
        while end < n and (text[end] in PUNCT and text[end] not in OPENING or text[end].isspace()):
            if not text[end].isspace():
                trailing.append(text[end])
            end += 1
        out.append({**w, "text": str(w["text"]).strip() + "".join(trailing)})
        i = end
    return out


def group_words(words: list[dict], no_space: bool, max_chars: int = 60, max_gap: float = 1.0, max_dur: float = 20.0) -> list[dict]:
    """把对齐器的词/字级时间戳合并成句段：句末标点、说话人变化、停顿、过长时断开。"""
    out: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        joiner = "" if no_space else " "
        text = joiner.join(w["text"].strip() for w in cur).strip()
        if text:
            out.append({"start": cur[0]["start"], "end": cur[-1]["end"], "text": text, "speaker": cur[0].get("speaker")})
        cur.clear()

    for w in words:
        t = str(w.get("text", "")).strip()
        if not t:
            continue
        w = {**w, "text": t, "start": float(w.get("start", 0)), "end": float(w.get("end", 0))}
        if cur:
            last = cur[-1]
            length = sum(len(x["text"]) for x in cur)
            if (
                w.get("speaker") != last.get("speaker")
                or w["start"] - last["end"] >= max_gap
                or w["end"] - cur[0]["start"] > max_dur
                or last["text"][-1] in END_PUNCT
                or (last["text"][-1] in CLAUSE_PUNCT and length >= max_chars * 0.6)
                or length >= max_chars
            ):
                flush()
        cur.append(w)
    flush()
    return out


def assign_speakers(words: list[dict], turns: list[dict]) -> list[dict]:
    """按时间重叠给每个词/字挂上说话人。

    库自带的 diarize_word_segments 是「每个字 × 每段说话」的双重循环，3 小时音频约 4 万字 × 3 千段，
    要跑上亿次；这里按开始时间二分查找，只检查附近的几段。
    """
    turns = sorted(turns, key=lambda t: float(t["start"]))
    starts = [float(t["start"]) for t in turns]
    # 往回看的范围：最长一段说话的时长（说话段之间可能重叠）
    max_len = max((float(t["end"]) - float(t["start"]) for t in turns), default=0.0)
    out = []
    for w in words:
        s = float(w.get("start", 0.0))
        e = max(float(w.get("end", s)), s)
        hi = bisect.bisect_right(starts, e)
        lo = bisect.bisect_left(starts, s - max_len)
        best, best_ov = None, 0.0
        for t in turns[lo:hi]:
            ov = min(e, float(t["end"])) - max(s, float(t["start"]))
            if ov > best_ov:
                best, best_ov = t, ov
        if best is None:  # 落在说话段之间的空隙：取中点最近的一段
            mid = (s + e) / 2
            cands = turns[max(0, hi - 2) : hi + 1]
            best = min(cands, key=lambda t: abs((float(t["start"]) + float(t["end"])) / 2 - mid)) if cands else None
        out.append({**w, "speaker": best["speaker"] if best else None})
    return out


def start_diarization(args) -> Optional[tuple]:
    out = args.out + ".diar.json"
    cmd = [sys.executable, "-m", "app.asr.diarize_worker", "--audio", args.audio, "--out", out, "--device", args.diarize_device]
    if args.num_speakers:
        cmd += ["--num-speakers", str(args.num_speakers)]
    err = open(args.out + ".diar.log", "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
    except OSError as e:
        emit(event="warning", message=f"说话人分离进程启动失败，只转写：{e}")
        return None
    emit(event="stage", message="转写与说话人分离同时进行…")
    return proc, out, err, time.time()


def finish_diarization(diar: Optional[tuple]) -> Optional[list[dict]]:
    if not diar:
        return None
    proc, out, err, t0 = diar
    if proc.poll() is None:
        emit(event="stage", message="转写完成，等待说话人分离结束…")
    code = proc.wait()
    err.close()
    try:
        if code == 0 and os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                turns = json.load(f)["turns"]
            emit(event="stage", message=f"说话人分离完成（{time.time() - t0:.0f}s，{len(turns)} 段）")
            return turns
        with open(err.name, encoding="utf-8", errors="replace") as f:
            tail = f.read()[-600:]
        emit(event="warning", message=f"说话人分离失败，降级为只转写（exit {code}）：{tail}")
        return None
    finally:
        for p in (out, err.name):
            if os.path.exists(p):
                os.remove(p)


def watch_parent() -> None:
    """父进程消失（被强杀）时自行退出，避免留下占 GPU 的孤儿进程。"""
    ppid = os.getppid()

    def loop():
        while True:
            time.sleep(2)
            if os.getppid() != ppid:
                os._exit(1)

    threading.Thread(target=loop, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B")
    ap.add_argument("--aligner", default="Qwen/Qwen3-ForcedAligner-0.6B")
    ap.add_argument("--diarize", action="store_true")
    ap.add_argument("--diarize-device", default="mps", choices=["cpu", "mps", "auto"],
                    help="M0 实测 20 分钟音频：mps 92s，cpu 617s")
    ap.add_argument("--num-speakers", type=int, default=None)
    ap.add_argument("--context", default="")
    ap.add_argument("--cache-limit-mb", type=int, default=1024, help="MLX 缓冲池上限，-1 为不限制")
    args = ap.parse_args()

    t0 = time.time()
    watch_parent()
    emit(event="stage", message="加载模型…")
    import mlx.core as mx

    # MLX 默认会把释放的显存留在缓冲池里复用，长音频下峰值可涨到 12GB；16GB 机器上限制缓冲池大小
    if args.cache_limit_mb >= 0:
        mx.set_cache_limit(args.cache_limit_mb * 1024 * 1024)
    from mlx_qwen3_asr import transcribe
    from mlx_qwen3_asr.tokenizer import is_no_space_language

    # 说话人分离在独立子进程里与转写同时进行（M0：20 分钟音频串行 208s → 并行 176s）
    diar = start_diarization(args) if args.diarize else None

    last = {"p": -1.0}

    def on_progress(payload: dict) -> None:
        if payload.get("event") == "chunk_completed":
            p = float(payload.get("progress") or 0.0)
            if p - last["p"] >= 0.01 or p >= 1.0:
                last["p"] = p
                emit(event="progress", progress=round(p, 4), elapsed=round(time.time() - t0, 1))

    try:
        result = transcribe(
            args.audio,
            model=args.model,
            forced_aligner=args.aligner,
            return_timestamps=True,
            context=args.context,
            on_progress=on_progress,
        )
    except BaseException:
        if diar:
            diar[0].kill()
        raise

    turns = finish_diarization(diar) if diar else None
    args.diarize = bool(turns)

    emit(event="stage", message="整理分段…")
    lang = result.language or ""
    no_space = is_no_space_language(lang)
    words = result.segments or []
    if words:
        words = restore_punct(words, result.text)
        if turns:
            words = assign_speakers(words, turns)
        segs = group_words(words, no_space)
    elif result.chunks:
        segs = [{"start": c.get("start", 0), "end": c.get("end", 0), "text": c.get("text", ""), "speaker": None} for c in result.chunks]
        if turns:
            segs = assign_speakers(segs, turns)
    else:
        segs = [{"start": 0.0, "end": 0.0, "text": result.text, "speaker": None}]

    # SPEAKER_00 → S1，按出场顺序编号
    if args.diarize:
        mapping: dict[str, str] = {}
        for s in segs:
            sp = s.get("speaker")
            if sp is not None:
                mapping.setdefault(sp, f"S{len(mapping) + 1}")
                s["speaker"] = mapping[sp]
        if len(mapping) <= 1:  # 只有一个说话人时不显示标签
            for s in segs:
                s["speaker"] = None
    else:
        for s in segs:
            s["speaker"] = None

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(
            {"language": lang, "diarized": args.diarize, "segments": segs, "text": result.text, "words": words},
            f,
            ensure_ascii=False,
        )
    emit(event="done", elapsed=round(time.time() - t0, 1), segments=len(segs), language=lang)
    return 0


if __name__ == "__main__":
    sys.exit(main())
