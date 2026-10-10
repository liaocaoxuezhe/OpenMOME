"""读取 .env 配置。密钥只从环境变量读取，不写进代码、不入库、不打日志。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _bilibili_cookie() -> str:
    """兼容两种写法：完整 Cookie 串（含 `SESSDATA=...;`）或只填 SESSDATA 的值。"""
    raw = (os.getenv("BILIBILI_COOKIE") or os.getenv("BILIBILI_SESSDATA") or "").strip().strip("\"'")
    if not raw:
        return ""
    return raw if "=" in raw else f"SESSDATA={raw}"


@dataclass(frozen=True)
class Settings:
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    llm_api_key: str = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
    hf_token: str = os.getenv("HF_TOKEN", "")
    bilibili_cookie: str = _bilibili_cookie()
    asr_model: str = os.getenv("ASR_MODEL", "Qwen/Qwen3-ASR-0.6B")
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8765"))
    keep_audio: bool = _bool("KEEP_AUDIO", False)
    min_free_gb: float = float(os.getenv("MIN_FREE_GB", "5"))
    data_dir: Path = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    models_dir: Path = Path(os.getenv("MODELS_DIR", str(ROOT / "models")))

    def local_model(self, repo: str) -> str:
        """models/<名字> 存在（scripts/download_models.py 下载的）就用本地路径，否则交给 HF Hub。"""
        p = self.models_dir / repo.split("/")[-1]
        return str(p) if (p / "config.json").exists() else repo

    @property
    def asr_model_path(self) -> str:
        return self.local_model(self.asr_model)

    @property
    def aligner_path(self) -> str:
        return self.local_model("Qwen/Qwen3-ForcedAligner-0.6B")

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "openmome.db"

    def secrets(self) -> list[str]:
        """日志脱敏用：所有需要遮盖的敏感字符串。"""
        out = [self.llm_api_key, self.hf_token]
        for part in self.bilibili_cookie.split(";"):
            if "=" in part:
                out.append(part.split("=", 1)[1].strip())
        return [s for s in out if s and len(s) >= 6]


settings = Settings()


def redact(text: str) -> str:
    for s in settings.secrets():
        text = text.replace(s, "***")
    return text
