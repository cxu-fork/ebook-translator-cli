"""翻译引擎注册表。"""
from .base import TranslationEngine
from .openai_engine import OpenAIEngine
from .anthropic_engine import AnthropicEngine

ENGINE_REGISTRY: dict[str, type[TranslationEngine]] = {
    "openai": OpenAIEngine,
    "claude": AnthropicEngine,
    "deepseek": OpenAIEngine,  # DeepSeek 使用 OpenAI 兼容 API
}


def get_engine(name: str) -> type[TranslationEngine]:
    cls = ENGINE_REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"未知引擎 '{name}'，可用: {list(ENGINE_REGISTRY)}"
        )
    return cls
