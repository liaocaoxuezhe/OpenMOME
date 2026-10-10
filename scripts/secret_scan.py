"""E4 验收：在代码、日志、数据库里搜索 .env 中的明文密钥。只打印命中的文件名，不打印密钥本身。

用法：.venv/bin/python scripts/secret_scan.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

SKIP_DIRS = {".venv", "models", ".git", "__pycache__", "node_modules"}
secrets = settings.secrets()
print(f"检查 {len(secrets)} 个敏感值（DeepSeek Key / HF Token / B站 Cookie 各字段）")

hits = []
for p in ROOT.rglob("*"):
    if not p.is_file() or p.name == ".env" or any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
        continue
    if p.stat().st_size > 50_000_000:
        continue
    data = p.read_bytes()
    for s in secrets:
        if s.encode() in data:
            hits.append(str(p.relative_to(ROOT)))
            break

if hits:
    print("❌ 发现明文密钥：")
    for h in hits:
        print("  ", h)
    sys.exit(1)
print("✅ 代码、日志、数据库中未发现明文密钥")
