from app.asr.base import ASRError, ASRProvider
from app.asr.qwen3_mlx import Qwen3MLX


def get_provider() -> ASRProvider:
    """MVP 固定 Qwen3-ASR（MLX）；后续在这里按配置返回不同 Provider。"""
    return Qwen3MLX()


__all__ = ["ASRError", "ASRProvider", "get_provider"]
