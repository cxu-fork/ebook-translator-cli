"""OpenAI 兼容翻译引擎（适用于 ChatGPT、DeepSeek 等）。"""
import json
import threading
from typing import Generator
from urllib.parse import urlsplit

import httpx

from .base import TranslationEngine, endpoint_url
from ..config import EngineConfig, MAX_CONCURRENCY

_DEFAULT_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIEngine(TranslationEngine):
    default_base = _DEFAULT_BASE
    default_model = _DEFAULT_MODEL
    _reserved_extra = {"messages", "model", "temperature", "top_p", "stream"}

    def __init__(self, config: EngineConfig, source_lang: str, target_lang: str):
        super().__init__(config, source_lang, target_lang)
        self.base_url = (config.base_url or self.default_base).rstrip("/")
        self.chat_url = endpoint_url(self.base_url, "/chat/completions")
        self.model = config.model or (
            self.default_model
            if urlsplit(self.base_url).hostname == urlsplit(self.default_base).hostname
            else ""
        )
        self.api_key = config.api_key
        if not self.api_key:
            raise ValueError("OpenAI 引擎需要设置 api_key")
        self.temperature = config.temperature
        self.stream = config.stream
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def _body(self, text: str, prompt: str, stream: bool) -> dict:
        body: dict = {
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
        }
        if self.config.sampling == "temperature" and self.temperature is not None:
            body["temperature"] = self.temperature
        if self.model:
            body["model"] = self.model
        if self.config.sampling == "top_p" and self.config.top_p is not None:
            body["top_p"] = self.config.top_p
        if stream:
            body["stream"] = True
        # Merge extra body params (e.g. response_format, seed, etc.)
        if self.config.extra:
            conflicts = self._reserved_extra.intersection(self.config.extra)
            if conflicts:
                raise ValueError(
                    f"OpenAI extra 不能覆盖保留字段: {sorted(conflicts)}"
                )
            body.update(self.config.extra)
        return body

    def _get_client(self) -> httpx.Client:
        client = self._client
        if client is not None:
            return client
        with self._client_lock:
            if self._client is None:
                timeout = httpx.Timeout(self.config.request_timeout, connect=10)
                concurrency = min(MAX_CONCURRENCY, max(1, self.config.concurrency))
                limits = httpx.Limits(
                    max_connections=max(8, concurrency * 2),
                    max_keepalive_connections=max(4, concurrency),
                )
                self._client = httpx.Client(timeout=timeout, limits=limits)
            return self._client

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def translate(self, text: str, prompt: str = "") -> str:
        if self.stream:
            return "".join(self.translate_stream(text, prompt)).strip()
        prompt = self.build_prompt(prompt)
        body = self._body(text, prompt, stream=False)
        client = self._get_client()
        resp = client.post(
            self.chat_url,
            headers=self._headers(),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"API 返回空结果: {json.dumps(data)[:500]}")
        choice = choices[0]
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise RuntimeError("API 输出被截断 (finish_reason=length)")
        if finish_reason == "content_filter":
            raise RuntimeError("API 输出被内容过滤 (finish_reason=content_filter)")
        if finish_reason == "insufficient_system_resource":
            raise RuntimeError("API 资源暂时不足 (insufficient_system_resource)")
        msg = choice.get("message", {})
        if msg.get("refusal"):
            raise RuntimeError("API 输出被内容过滤 (refusal)")
        content = msg.get("content", "")
        if not content:
            content = choice.get("text", "")
        if not isinstance(content, str):
            raise RuntimeError("API 返回的译文不是字符串")
        if not content:
            raise RuntimeError(f"API 返回空译文: {json.dumps(data)[:500]}")
        return content.strip()

    def translate_stream(self, text: str, prompt: str = "") -> Generator[str, None, None]:
        prompt = self.build_prompt(prompt)
        body = self._body(text, prompt, stream=True)
        client = self._get_client()
        with client.stream(
            "POST",
            self.chat_url,
            headers=self._headers(),
            json=body,
        ) as resp:
            resp.raise_for_status()
            completed = False
            for line in resp.iter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if chunk == "[DONE]":
                    completed = True
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError as e:
                    raise RuntimeError("API 流式响应 JSON 无效") from e
                if not isinstance(obj, dict):
                    raise RuntimeError("API 流式响应 JSON 结构无效")
                if obj.get("error"):
                    raise RuntimeError(f"API 流式错误: {obj['error']}")
                choices = obj.get("choices")
                if not choices:
                    if obj.get("usage") is not None or obj.get("type") == "ping":
                        continue
                    raise RuntimeError("API 流式响应缺少 choices")
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise RuntimeError("API 流式响应 choice 格式无效")
                finish_reason = choice.get("finish_reason")
                if finish_reason == "length":
                    raise RuntimeError("API 输出被截断 (finish_reason=length)")
                if finish_reason == "content_filter":
                    raise RuntimeError(
                        "API 输出被内容过滤 (finish_reason=content_filter)"
                    )
                if finish_reason == "insufficient_system_resource":
                    raise RuntimeError(
                        "API 资源暂时不足 (insufficient_system_resource)"
                    )
                if finish_reason is not None:
                    completed = True
                delta = choice.get("delta", {})
                if not isinstance(delta, dict):
                    raise RuntimeError("API 流式响应 delta 格式无效")
                content = delta.get("content")
                if delta.get("refusal"):
                    raise RuntimeError("API 输出被内容过滤 (refusal)")
                if content is not None:
                    if not isinstance(content, str):
                        raise RuntimeError("API 流式译文不是字符串")
                    if content:
                        yield content
            if not completed:
                raise RuntimeError("API 流式响应未完整结束")


class DeepSeekEngine(OpenAIEngine):
    default_base = "https://api.deepseek.com/v1"
    default_model = "deepseek-chat"
