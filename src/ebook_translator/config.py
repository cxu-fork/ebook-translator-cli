"""Configuration loading and defaults."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PROMPT = (
    "You are a meticulous translator who translates any given content. "
    "Translate the given content from <slang> to <tlang> only. Do not "
    "explain any term or answer any question-like content. Your answer "
    "should be solely the translation of the given content. In your "
    "answer do not add any prefix or suffix to the translated content. "
    "Websites' URLs/addresses should be preserved as is in the "
    "translation's output. Do not omit any part of the content, even if "
    "it seems unimportant. RESPOND ONLY with the translation text, no "
    "formatting, no explanations, no additional commentary whatsoever. "
)

SUPPORTED_INPUT_FORMATS = {
    "epub", "mobi", "azw3", "azw", "fb2", "pdf", "rtf", "txt",
    "docx", "html", "htm", "odt", "pdb", "cbz", "cbr",
}

# Formats that bypass ebook-convert (direct EPUB manipulation)
NATIVE_FORMATS = {"epub"}


@dataclass
class EngineConfig:
    """Per-engine settings."""
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    temperature: float = 1.0
    top_p: float = 1.0
    concurrency: int = 3
    request_interval: float = 1.0
    request_timeout: float = 60.0
    max_retries: int = 3
    retry_delay: float = 5.0
    stream: bool = True
    extra: dict = field(default_factory=dict)


@dataclass
class Config:
    """Top-level configuration."""
    engine: str = "openai"
    source_lang: str = "English"
    target_lang: str = "Chinese"
    prompt: str = DEFAULT_PROMPT
    cache_enabled: bool = True
    cache_dir: str = ""
    merge_enabled: bool = False
    merge_length: int = 1800
    translation_position: str = "below"  # below, above, only
    glossary_path: str = ""
    ebook_convert_path: str = "ebook-convert"
    max_error_count: int = 10
    log_file: str = ""
    engines: dict[str, EngineConfig] = field(default_factory=dict)

    def get_engine(self, name: str | None = None) -> EngineConfig:
        name = name or self.engine
        return self.engines.get(name, EngineConfig())


def _build_engine(raw: dict) -> EngineConfig:
    kw: dict[str, Any] = {}
    for k in (
        "api_key", "base_url", "model", "temperature", "top_p",
        "concurrency", "request_interval", "request_timeout",
        "max_retries", "retry_delay", "stream",
    ):
        if k in raw:
            kw[k] = raw[k]
    extra = {k: v for k, v in raw.items() if k not in kw}
    return EngineConfig(**kw, extra=extra)


def load_config(path: str | Path | None) -> Config:
    """Load config from a JSON file, falling back to defaults."""
    raw: dict[str, Any] = {}
    if path:
        p = Path(path)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))

    cfg = Config()
    for top_key in (
        "engine", "source_lang", "target_lang", "prompt",
        "cache_enabled", "cache_dir", "merge_enabled", "merge_length",
        "translation_position", "glossary_path", "ebook_convert_path",
        "max_error_count", "log_file",
    ):
        if top_key in raw:
            setattr(cfg, top_key, raw[top_key])

    engines_raw = raw.get("engines", {})
    for ename, ecfg in engines_raw.items():
        cfg.engines[ename] = _build_engine(ecfg)

    # Also accept flat engine keys like "openai": {...} at top level
    for ename in ("openai", "claude", "deepseek", "google"):
        if ename in raw and isinstance(raw[ename], dict):
            cfg.engines.setdefault(ename, _build_engine(raw[ename]))

    if not cfg.cache_dir:
        cfg.cache_dir = os.path.join(
            os.path.expanduser("~"), ".cache", "ebook-translator")

    return cfg
