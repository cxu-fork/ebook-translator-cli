"""翻译引擎基类。"""
from abc import ABC, abstractmethod
from typing import Generator
from urllib.parse import urlsplit, urlunsplit

from ..config import EngineConfig


def endpoint_url(base_url: str, suffix: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"API base_url 无效: {base_url}")
    path = parts.path.rstrip("/")
    if not path.endswith(suffix):
        if suffix.startswith("/v1/") and path.endswith("/v1"):
            path += suffix[3:]
        else:
            path += suffix
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


class TranslationEngine(ABC):

    def __init__(self, config: EngineConfig, source_lang: str, target_lang: str):
        self.config = config
        self.source_lang = source_lang
        self.target_lang = target_lang

    @abstractmethod
    def translate(self, text: str, prompt: str = "") -> str:
        """翻译文本，返回完整译文。"""

    @abstractmethod
    def translate_stream(self, text: str, prompt: str = "") -> Generator[str, None, None]:
        """流式翻译，逐块返回。"""

    def build_prompt(self, prompt_template: str) -> str:
        prompt = prompt_template.replace("<tlang>", self.target_lang)
        prompt = prompt.replace("<slang>", self.source_lang)
        return prompt

    def close(self) -> None:
        """Release underlying resources (HTTP clients, etc.)."""
