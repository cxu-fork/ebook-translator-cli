"""OpenAI 兼容翻译引擎（适用于 ChatGPT、DeepSeek 等）。"""
import json
from typing import Generator

import httpx

from .base import TranslationEngine
from ..config import EngineConfig

_DEFAULT_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIEngine(TranslationEngine):

    def __init__(self, config: EngineConfig, source_lang: str, target_lang: str):
        super().__init__(config, source_lang, target_lang)
        self.base_url = (config.base_url or _DEFAULT_BASE).rstrip("/")
        self.model = config.model or _DEFAULT_MODEL
        self.api_key = config.api_key
        if not self.api_key:
            raise ValueError("OpenAI 引擎需要设置 api_key")
        self.temperature = config.temperature
        self.stream = config.stream

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _body(self, text: str, prompt: str, stream: bool) -> dict:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            "temperature": self.temperature,
        }
        if self.config.top_p is not None:
            body["top_p"] = self.config.top_p
        if stream:
            body["stream"] = True
        return body

    def translate(self, text: str, prompt: str = "") -> str:
        prompt = self.build_prompt(prompt)
        body = self._body(text, prompt, stream=False)
        timeout = httpx.Timeout(self.config.request_timeout, connect=10)
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            )
            if resp.status_code == 401:
                raise RuntimeError("API 密钥无效或已过期")
            if resp.status_code == 429:
                raise RuntimeError("API 请求频率超限，请稍后重试")
            resp.raise_for_status()
            data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"API 返回空结果: {json.dumps(data)[:500]}")
        msg = choices[0].get("message", {})
        content = msg.get("content", "")
        if not content:
            content = choices[0].get("text", "")
        return content.strip()

    def translate_stream(self, text: str, prompt: str = "") -> Generator[str, None, None]:
        prompt = self.build_prompt(prompt)
        body = self._body(text, prompt, stream=True)
        timeout = httpx.Timeout(self.config.request_timeout, connect=10)
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[len("data:"):].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                        delta = obj["choices"][0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
