"""下载 ASR 模型到项目 models/ 目录。

Qwen 模型优先从 ModelScope 下载（国内网络比 Hugging Face 快 10 倍以上），支持断点续传；
pyannote 说话人分离模型体积小，仍走 Hugging Face（需要 HF_TOKEN 并接受条款）。

用法：.venv/bin/python scripts/download_models.py [--skip-pyannote]
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

MODELS = ["Qwen/Qwen3-ASR-0.6B", "Qwen/Qwen3-ForcedAligner-0.6B"]  # 需要 1.7B 时加 --models Qwen/Qwen3-ASR-1.7B
PATTERNS = ["*.json", "*.safetensors", "*.txt", "*.model"]
MS_API = "https://modelscope.cn/api/v1/models/{repo}/repo/files?Recursive=true"
MS_FILE = "https://modelscope.cn/models/{repo}/resolve/master/{path}"


def download_file(client: httpx.Client, url: str, dest: Path, size: int) -> None:
    if dest.exists() and dest.stat().st_size == size:
        return
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    t0, last = time.time(), 0.0
    with client.stream("GET", url, headers=headers, follow_redirects=True) as r:
        if r.status_code == 200 and have:  # 服务器不支持续传，从头下
            have = 0
        r.raise_for_status()
        with open(part, "ab" if have else "wb") as f:
            done = have
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last > 2 or done == size:
                    last = now
                    speed = (done - have) / max(now - t0, 1e-6) / 1e6
                    print(f"\r  {dest.name}: {done / 1e9:.2f}/{size / 1e9:.2f} GB  {speed:.1f} MB/s", end="", flush=True)
    print()
    if part.stat().st_size != size:
        raise RuntimeError(f"{dest.name} 大小不符：{part.stat().st_size} != {size}，请重新运行续传")
    part.rename(dest)


def download_modelscope(repo: str) -> Path:
    dest_dir = settings.models_dir / repo.split("/")[-1]
    dest_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=httpx.Timeout(60, read=120), trust_env=False) as client:
        files = client.get(MS_API.format(repo=repo)).json()["Data"]["Files"]
        files = [f for f in files if f["Type"] == "blob" and any(fnmatch.fnmatch(f["Path"], p) for p in PATTERNS)]
        print(f"[{repo}] {len(files)} 个文件，共 {sum(f['Size'] for f in files) / 1e9:.2f} GB → {dest_dir}")
        for f in files:
            target = dest_dir / f["Path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            download_file(client, MS_FILE.format(repo=repo, path=f["Path"]), target, int(f["Size"]))
    return dest_dir


def download_pyannote() -> None:
    if not settings.hf_token:
        print("[pyannote] 未配置 HF_TOKEN，跳过（将只转写、不区分说话人）")
        return
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from huggingface_hub import snapshot_download

    repo = "pyannote/speaker-diarization-community-1"
    print(f"[{repo}] 从 Hugging Face 下载…")
    try:
        snapshot_download(repo, token=settings.hf_token)
        print(f"[{repo}] OK")
    except Exception as e:  # noqa: BLE001
        print(f"[{repo}] 失败：{type(e).__name__}: {str(e)[:300]}")
        print("  请确认已在 https://huggingface.co/pyannote/speaker-diarization-community-1 页面接受使用条款")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-pyannote", action="store_true")
    ap.add_argument("--models", nargs="*", default=None, help=f"要下载的 ModelScope 模型，默认 {MODELS}")
    args = ap.parse_args()
    for repo in args.models or MODELS:
        download_modelscope(repo)
    if not args.skip_pyannote:
        download_pyannote()
    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
