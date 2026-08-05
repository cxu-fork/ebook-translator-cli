"""Configuration loading and defaults."""
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_KNOWN_ENGINES = {"openai", "claude", "deepseek"}
MAX_CONCURRENCY = 256

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
    temperature: float | None = 0.3
    top_p: float | None = 1.0
    concurrency: int = 3
    request_interval: float = 1.0
    request_timeout: float = 60.0
    max_retries: int = 5
    retry_delay: float = 5.0
    stream: bool = False
    prompt: str | None = None
    sampling: str = "temperature"
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
    translation_style: str = ""
    translate_tags: str = ""
    exclude_translate_tags: str = "sup,code,pre"
    only_files: str = ""
    exclude_files: str = ""
    test_enabled: bool = False
    test_num: int = 10
    retranslate_file: str = ""
    retranslate_start: str = ""
    retranslate_end: str = ""
    glossary_path: str = ""
    glossary: dict[str, str] = field(default_factory=dict)
    ebook_convert_path: str = ""
    max_error_count: int = 10
    skip_failed: bool = False
    log_file: str = ""
    engines: dict[str, EngineConfig] = field(default_factory=dict)

    def get_engine(self, name: str | None = None) -> EngineConfig:
        name = name or self.engine
        return self.engines.get(name, EngineConfig())

    def effective_prompt(self) -> str:
        return self.get_engine().prompt or self.prompt


def _build_engine(raw: dict) -> EngineConfig:
    kw: dict[str, Any] = {}
    for k in (
        "api_key", "base_url", "model", "temperature", "top_p",
        "concurrency", "request_interval", "request_timeout",
        "max_retries", "retry_delay", "stream", "prompt", "sampling",
    ):
        if k in raw:
            kw[k] = raw[k]
    extra = dict(raw.get("extra", {}))
    if "max_tokens" in raw:
        if "max_tokens" in extra:
            raise ValueError("max_tokens 不能同时平铺并写入 extra")
        extra["max_tokens"] = raw["max_tokens"]
    unknown = set(raw) - set(kw) - {"extra", "max_tokens"}
    if unknown:
        raise ValueError(f"引擎配置包含未知字段: {sorted(unknown)}")
    return EngineConfig(**kw, extra=extra)


def _expect_type(raw: dict[str, Any], keys: tuple[str, ...], expected: type):
    for key in keys:
        if key in raw and type(raw[key]) is not expected:
            raise ValueError(f"配置项 {key} 必须是 {expected.__name__}")


def _validate_engine(name: str, raw: Any):
    if name not in _KNOWN_ENGINES:
        raise ValueError(f"未知引擎 '{name}'，可用: {sorted(_KNOWN_ENGINES)}")
    if not isinstance(raw, dict):
        raise ValueError(f"引擎配置 {name} 必须是对象")
    _expect_type(raw, ("api_key", "base_url", "model", "sampling"), str)
    if "prompt" in raw and raw["prompt"] is not None and not isinstance(raw["prompt"], str):
        raise ValueError(f"引擎配置 {name}.prompt 必须是字符串或 null")
    _expect_type(raw, ("concurrency", "max_retries"), int)
    _expect_type(raw, ("stream",), bool)
    for key in ("temperature", "top_p", "request_interval", "request_timeout", "retry_delay"):
        if key in raw and raw[key] is None and key not in {"temperature", "top_p"}:
            raise ValueError(f"引擎配置 {name}.{key} 不能为空")
        if (key in raw and raw[key] is not None
                and (isinstance(raw[key], bool)
                     or not isinstance(raw[key], (int, float)))):
            raise ValueError(f"引擎配置 {name}.{key} 必须是数字")
        if key in raw and raw[key] is not None and not math.isfinite(raw[key]):
            raise ValueError(f"引擎配置 {name}.{key} 必须是有限数字")
    if "extra" in raw and not isinstance(raw["extra"], dict):
        raise ValueError(f"引擎配置 {name}.extra 必须是对象")
    for key in ("concurrency", "max_retries"):
        if key in raw and raw[key] < 1:
            raise ValueError(f"引擎配置 {name}.{key} 必须大于 0")
    if raw.get("concurrency", 1) > MAX_CONCURRENCY:
        raise ValueError(
            f"引擎配置 {name}.concurrency 不能大于 {MAX_CONCURRENCY}")
    if "request_timeout" in raw and raw["request_timeout"] <= 0:
        raise ValueError(f"引擎配置 {name}.request_timeout 必须大于 0")
    for key in ("request_interval", "retry_delay"):
        if key in raw and raw[key] < 0:
            raise ValueError(f"引擎配置 {name}.{key} 不能小于 0")
    if raw.get("temperature") is not None and raw["temperature"] < 0:
        raise ValueError(f"引擎配置 {name}.temperature 不能小于 0")
    if (raw.get("temperature") is not None
            and raw["temperature"] > (1 if name == "claude" else 2)):
        raise ValueError(f"引擎配置 {name}.temperature 超出支持范围")
    if raw.get("top_p") is not None and not 0 <= raw["top_p"] <= 1:
        raise ValueError(f"引擎配置 {name}.top_p 必须在 0 到 1 之间")
    extra = raw.get("extra", {})
    max_tokens = raw.get("max_tokens", extra.get("max_tokens"))
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
        raise ValueError(f"引擎配置 {name}.max_tokens 必须是正整数")
    if raw.get("sampling", "temperature") not in {"temperature", "top_p"}:
        raise ValueError(f"引擎配置 {name}.sampling 必须是 temperature 或 top_p")


def _validate_root(raw: Any):
    if not isinstance(raw, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    _expect_type(raw, (
        "engine", "source_lang", "target_lang", "prompt", "cache_dir",
        "translation_position", "translation_style", "translate_tags",
        "exclude_translate_tags", "only_files", "exclude_files",
        "retranslate_file", "retranslate_start", "retranslate_end",
        "glossary_path", "ebook_convert_path", "log_file",
    ), str)
    _expect_type(raw, (
        "cache_enabled", "merge_enabled", "test_enabled", "skip_failed",
    ), bool)
    _expect_type(raw, ("merge_length", "test_num", "max_error_count"), int)
    for key in ("merge_length", "test_num", "max_error_count"):
        if key in raw and raw[key] < 0:
            raise ValueError(f"配置项 {key} 不能小于 0")
    engine = raw.get("engine", "openai")
    if engine not in _KNOWN_ENGINES:
        raise ValueError(f"未知引擎 '{engine}'，可用: {sorted(_KNOWN_ENGINES)}")
    engines = raw.get("engines", {})
    if not isinstance(engines, dict):
        raise ValueError("配置项 engines 必须是对象")
    for name, engine_raw in engines.items():
        _validate_engine(name, engine_raw)
    for name in _KNOWN_ENGINES:
        if name in raw:
            _validate_engine(name, raw[name])
    if "glossary" in raw and not isinstance(raw["glossary"], dict):
        raise ValueError("配置项 glossary 必须是对象")


def load_config(path: str | Path | None) -> Config:
    """Load config from a JSON file, falling back to defaults.

    If *path* is not given (empty/None), automatically looks for
    ``config.json`` next to the running executable (frozen) or in the
    current working directory (development).
    """
    raw: dict[str, Any] = {}
    if path:
        p = Path(path)
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ValueError(f"配置文件 JSON 无效: {e}") from e
        else:
            raise FileNotFoundError(
                f"配置文件不存在: {p.resolve()}"
                f" 请确认路径正确，或从包含 config.json 的目录运行"
            )
    else:
        # Auto-discover config.json
        import sys
        candidates = [Path("config.json")]
        if getattr(sys, "frozen", False):
            candidates.insert(0, Path(sys.executable).parent / "config.json")
        for candidate in candidates:
            if candidate.is_file():
                try:
                    raw = json.loads(candidate.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    raise ValueError(f"配置文件 JSON 无效: {e}") from e
                break

    _validate_root(raw)

    cfg = Config()
    for top_key in (
        "engine", "source_lang", "target_lang", "prompt",
        "cache_enabled", "cache_dir", "merge_enabled", "merge_length",
        "translation_position", "translation_style", "translate_tags",
        "exclude_translate_tags", "only_files", "exclude_files",
        "test_enabled", "test_num", "retranslate_file", "retranslate_start",
        "retranslate_end", "glossary_path", "ebook_convert_path",
        "max_error_count", "skip_failed", "log_file", "glossary",
    ):
        if top_key in raw:
            setattr(cfg, top_key, raw[top_key])

    engines_raw = raw.get("engines", {})
    for ename, ecfg in engines_raw.items():
        cfg.engines[ename] = _build_engine(ecfg)

    # Also accept flat engine keys like "openai": {...} at top level
    for ename in _KNOWN_ENGINES:
        if ename in raw and isinstance(raw[ename], dict):
            cfg.engines.setdefault(ename, _build_engine(raw[ename]))

    if not cfg.cache_dir:
        cfg.cache_dir = os.path.join(
            os.path.expanduser("~"), ".cache", "ebook-translator")
    else:
        cfg.cache_dir = os.path.expanduser(cfg.cache_dir)

    if cfg.glossary_path:
        cfg.glossary_path = os.path.expanduser(cfg.glossary_path)
    if cfg.ebook_convert_path:
        cfg.ebook_convert_path = os.path.expanduser(cfg.ebook_convert_path)
    if cfg.log_file:
        cfg.log_file = os.path.expanduser(cfg.log_file)

    if cfg.translation_position not in {"below", "above", "left", "right", "only"}:
        raise ValueError(
            "translation_position 必须是 below、above、left、right 或 only"
        )

    return cfg
