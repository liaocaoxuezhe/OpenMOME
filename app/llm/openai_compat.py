"""OpenAI 兼容接口（DeepSeek / 通义 / Kimi / Ollama 都可以）。"""
from __future__ import annotations

from typing import Callable, Optional

from openai import APIConnectionError, APIStatusError, AuthenticationError, OpenAI

from app.config import redact, settings
from app.llm.base import LLMError, LLMProvider


class OpenAICompat(LLMProvider):
    name = "openai-compat"

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = base_url or settings.llm_base_url
        self.model = model or settings.llm_model
        key = api_key if api_key is not None else settings.llm_api_key
        if not key and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            raise LLMError("未配置 LLM API Key（.env 里的 DEEPSEEK_API_KEY）")
        self.client = OpenAI(base_url=self.base_url, api_key=key or "ollama", timeout=600, max_retries=2)

    def chat(self, system: str, user: str, *, on_delta: Optional[Callable[[int], None]] = None, max_tokens: int = 8000) -> str:
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.3,
                max_tokens=max_tokens,
                stream=True,
            )
            parts: list[str] = []
            n = 0
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    parts.append(delta)
                    n += len(delta)
                    if on_delta:
                        on_delta(n)
            text = "".join(parts).strip()
        except AuthenticationError as e:
            raise LLMError("LLM API Key 无效或已过期，请检查 .env") from e
        except APIConnectionError as e:
            raise LLMError(f"连接 LLM 服务失败（{self.base_url}），请检查网络") from e
        except APIStatusError as e:
            raise LLMError(redact(f"LLM 接口报错 {e.status_code}：{e.message}")) from e
        if not text:
            raise LLMError("LLM 返回了空内容")
        return text
