"""翻译引擎基类。"""
from abc import ABC, abstractmethod
from typing import Generator

from ..config import EngineConfig


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
