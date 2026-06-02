"""Anthropic Claude 翻译引擎。"""
import json
from typing import Generator

import httpx

from .base import TranslationEngine
from ..config import EngineConfig

_DEFAULT_MODEL = "claude-sonnet-4-20250514"


class AnthropicEngine(TranslationEngine):

    def __init__(self, config: EngineConfig, source_lang: str, target_lang: str):
        super().__init__(config, source_lang, target_lang)
        self.api_key = config.api_key
        if not self.api_key:
            raise ValueError("Anthropic 引擎需要设置 api_key")
        self.model = config.model or _DEFAULT_MODEL
        self.temperature = config.temperature
        self.base_url = (config.base_url or "https://api.anthropic.com").rstrip("/")
        self.messages_url = self._endpoint("/v1/messages")
        self.stream = config.stream

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

    def _endpoint(self, suffix: str) -> str:
        if self.base_url.endswith(suffix):
            return self.base_url
        return f"{self.base_url}{suffix}"

    def _body(self, text: str, prompt: str, stream: bool) -> dict:
        body: dict = {
            "model": self.model,
            "max_tokens": 4096,
            "system": prompt,
            "messages": [{"role": "user", "content": text}],
            "temperature": self.temperature,
        }
        if self.config.top_p is not None:
            body["top_p"] = self.config.top_p
        if stream:
            body["stream"] = True
        return body

    def translate(self, text: str, prompt: str = "") -> str:
        if self.stream:
            return "".join(self.translate_stream(text, prompt)).strip()
        prompt = self.build_prompt(prompt)
        body = self._body(text, prompt, stream=False)
        timeout = httpx.Timeout(self.config.request_timeout, connect=10)
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                self.messages_url,
                headers=self._headers(),
                json=body,
            )
            if resp.status_code == 401:
                raise RuntimeError("API 密钥无效或已过期")
            if resp.status_code == 429:
                raise RuntimeError("API 请求频率超限，请稍后重试")
            resp.raise_for_status()
            data = resp.json()
        content_blocks = data.get("content", [])
        if not content_blocks:
            raise RuntimeError(f"API 返回空结果: {json.dumps(data)[:500]}")
        return content_blocks[0].get("text", "").strip()

    def translate_stream(self, text: str, prompt: str = "") -> Generator[str, None, None]:
        prompt = self.build_prompt(prompt)
        body = self._body(text, prompt, stream=True)
        timeout = httpx.Timeout(self.config.request_timeout, connect=10)
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST",
                self.messages_url,
                headers=self._headers(),
                json=body,
            ) as resp:
                if resp.status_code == 401:
                    raise RuntimeError("API 密钥无效或已过期")
                if resp.status_code == 429:
                    raise RuntimeError("API 请求频率超限，请稍后重试")
                resp.raise_for_status()
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[len("data:"):].strip()
                    try:
                        obj = json.loads(chunk)
                        event_type = obj.get("type", "")
                        if event_type == "message_stop":
                            break
                        if event_type == "content_block_delta":
                            delta = obj.get("delta", {})
                            text_chunk = delta.get("text")
                            if text_chunk:
                                yield text_chunk
                    except (json.JSONDecodeError, KeyError):
                        continue
