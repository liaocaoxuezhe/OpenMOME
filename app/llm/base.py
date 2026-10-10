"""LLMProvider 抽象：后续接 Qwen / Kimi / 本地 Ollama 都走 OpenAI 兼容实现，只改配置。"""
from __future__ import annotations

from typing import Callable, Optional


class LLMError(RuntimeError):
    pass


class LLMProvider:
    name: str = ""
    model: str = ""

    def chat(self, system: str, user: str, *, on_delta: Optional[Callable[[int], None]] = None, max_tokens: int = 8000) -> str:
        raise NotImplementedError
