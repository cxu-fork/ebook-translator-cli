"""Anthropic Claude 翻译引擎。"""
import json
import threading
from typing import Generator

import httpx

from .base import TranslationEngine, endpoint_url
from ..config import EngineConfig, MAX_CONCURRENCY

_DEFAULT_MODEL = "claude-sonnet-4-20250514"


class AnthropicEngine(TranslationEngine):
    _reserved_extra = {"model", "system", "messages", "temperature", "top_p", "stream"}

    def __init__(self, config: EngineConfig, source_lang: str, target_lang: str):
        super().__init__(config, source_lang, target_lang)
        self.api_key = config.api_key
        if not self.api_key:
            raise ValueError("Anthropic 引擎需要设置 api_key")
        self.model = config.model or _DEFAULT_MODEL
        self.temperature = config.temperature
        self.base_url = (config.base_url or "https://api.anthropic.com").rstrip("/")
        self.messages_url = endpoint_url(self.base_url, "/v1/messages")
        self.stream = config.stream
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

    def _body(self, text: str, prompt: str, stream: bool) -> dict:
        extra = dict(self.config.extra)
        max_tokens = extra.pop("max_tokens", 64_000)
        conflicts = self._reserved_extra.intersection(extra)
        if conflicts:
            raise ValueError(
                f"Anthropic extra 不能覆盖保留字段: {sorted(conflicts)}"
            )
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": prompt,
            "messages": [{"role": "user", "content": text}],
        }
        if self.config.sampling == "temperature" and self.temperature is not None:
            body["temperature"] = self.temperature
        if self.config.sampling == "top_p" and self.config.top_p is not None:
            body["top_p"] = self.config.top_p
        if stream:
            body["stream"] = True
        body.update(extra)
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
            self.messages_url,
            headers=self._headers(),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        stop_reason = data.get("stop_reason")
        if stop_reason == "max_tokens":
            raise RuntimeError("API 输出被截断 (stop_reason=max_tokens)")
        if stop_reason == "refusal":
            raise RuntimeError("API 输出被内容过滤 (stop_reason=refusal)")
        content_blocks = data.get("content", [])
        if not content_blocks:
            raise RuntimeError(f"API 返回空结果: {json.dumps(data)[:500]}")
        texts = [block.get("text") for block in content_blocks
                 if isinstance(block, dict) and isinstance(block.get("text"), str)]
        text = "".join(texts).strip()
        if not text:
            raise RuntimeError(f"API 返回空译文: {json.dumps(data)[:500]}")
        return text

    def translate_stream(self, text: str, prompt: str = "") -> Generator[str, None, None]:
        prompt = self.build_prompt(prompt)
        body = self._body(text, prompt, stream=True)
        client = self._get_client()
        with client.stream(
            "POST",
            self.messages_url,
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
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError as e:
                    raise RuntimeError("API 流式响应 JSON 无效") from e
                if not isinstance(obj, dict):
                    raise RuntimeError("API 流式响应 JSON 结构无效")
                event_type = obj.get("type", "")
                if event_type == "error":
                    raise RuntimeError(f"API 流式错误: {obj.get('error', obj)}")
                if event_type == "message_delta":
                    delta = obj.get("delta", {})
                    if not isinstance(delta, dict):
                        raise RuntimeError("API 流式响应 delta 格式无效")
                    stop_reason = delta.get("stop_reason")
                    if stop_reason == "max_tokens":
                        raise RuntimeError(
                            "API 输出被截断 (stop_reason=max_tokens)"
                        )
                    if stop_reason == "refusal":
                        raise RuntimeError(
                            "API 输出被内容过滤 (stop_reason=refusal)"
                        )
                if event_type == "message_stop":
                    completed = True
                    break
                if event_type == "content_block_delta":
                    delta = obj.get("delta", {})
                    if not isinstance(delta, dict):
                        raise RuntimeError("API 流式响应 delta 格式无效")
                    text_chunk = delta.get("text")
                    if text_chunk is not None:
                        if not isinstance(text_chunk, str):
                            raise RuntimeError("API 流式译文不是字符串")
                        if text_chunk:
                            yield text_chunk
            if not completed:
                raise RuntimeError("API 流式响应未完整结束")
