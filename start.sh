#!/usr/bin/env bash
# OpenMOME 一键启动：检查依赖 → 启动服务（强制使用 .venv 里的 Python 3.11+，不用系统 3.9）
set -euo pipefail
cd "$(dirname "$0")"

# 确保 Homebrew 的 ffmpeg / deno 在 PATH 里（yt-dlp 需要 deno 作为 YouTube 的 JS 运行时）
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.deno/bin:$PATH"
export HF_HUB_DISABLE_XET=1

if [ ! -x .venv/bin/python ]; then
  echo "❌ 没有找到 .venv，请先执行：uv venv --python 3.11 .venv && uv sync"
  exit 1
fi
command -v ffmpeg >/dev/null || { echo "❌ 未安装 ffmpeg：brew install ffmpeg"; exit 1; }
command -v deno   >/dev/null || echo "⚠️  未安装 Deno，YouTube 可能下载失败：brew install deno"
[ -f .env ] || { echo "❌ 缺少 .env，请先 cp .env.example .env 并填写"; exit 1; }

HOST=$(grep -E '^HOST=' .env | cut -d= -f2 || true); HOST=${HOST:-127.0.0.1}
PORT=$(grep -E '^PORT=' .env | cut -d= -f2 || true); PORT=${PORT:-8765}

echo "▶ OpenMOME 启动中： http://$HOST:$PORT"
( sleep 2 && open "http://$HOST:$PORT" >/dev/null 2>&1 || true ) &
exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
