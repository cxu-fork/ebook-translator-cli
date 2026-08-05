"""CLI 入口与翻译编排器。"""
import argparse
import asyncio
import concurrent.futures
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

from . import __version__
from .cache import TranslationCache, md5
from .config import (
    Config, MAX_CONCURRENCY, NATIVE_FORMATS, SUPPORTED_INPUT_FORMATS, load_config,
)
from .converter import convert, convert_to_epub, ConverterError
from .engines import get_engine
from .engines.base import TranslationEngine
from .epub import (
    extract_from_epub, build_cache_rows, write_translated_epub,
)

# ---------------------------------------------------------------------------
# ANSI 颜色
# ---------------------------------------------------------------------------
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"

def _c(text: str, *codes: str) -> str:
    if not sys.stderr.isatty():
        return text
    return "".join(codes) + text + _RESET


def _c_stdout(text: str, *codes: str) -> str:
    if not sys.stdout.isatty():
        return text
    return "".join(codes) + text + _RESET


# ---------------------------------------------------------------------------

def _err(msg, *pbars):
    """输出错误消息，兼容 tqdm 和非 tqdm 环境。"""
    line = _c(f"  ✗ {msg}", _RED)
    logging.error(msg)
    for p in pbars:
        if p is not None:
            try:
                p.write(line)
                return
            except Exception:
                pass
    print(line, file=sys.stderr)


# 错误分类：决定重试次数与退避策略
_ERR_PERMANENT = "permanent"
_ERR_RATE_LIMIT = "rate_limit"
_ERR_EMPTY = "empty"
_ERR_TRANSIENT = "transient"

# 空译文通常是模型/代理拒写，多打几次往往无用
_EMPTY_MAX_ATTEMPTS = 2


def _classify_translation_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return _ERR_RATE_LIMIT
        if 400 <= status < 500 and status not in {408, 409, 425}:
            return _ERR_PERMANENT
    text = str(exc).lower()
    if isinstance(exc, ValueError) and "不能覆盖保留字段" in text:
        return _ERR_PERMANENT
    permanent_needles = (
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "invalid_api_key",
        "incorrect api key",
        "api 密钥无效",
        "密钥无效",
        "已过期",
        "输出被截断",
        "内容过滤",
        "content_filter",
        "stop_reason=max_tokens",
        "stop_reason=refusal",
    )
    if any(n in text for n in permanent_needles):
        return _ERR_PERMANENT
    if "429" in text or "频率超限" in text or "too many" in text or "rate limit" in text:
        return _ERR_RATE_LIMIT
    if "空译文" in text or "空结果" in text or "empty" in text:
        return _ERR_EMPTY
    return _ERR_TRANSIENT


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    value = response.headers.get("Retry-After") if response is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

class _BatchStopped(Exception):
    pass


class _RateLimiter:
    """Thread-safe request-start limiter shared by all translation calls."""

    def __init__(self, interval: float):
        self.interval = max(0.0, float(interval or 0.0))
        self._lock = threading.Lock()
        self._next_time = 0.0

    def acquire(self, stop_event: threading.Event):
        while True:
            if stop_event.is_set():
                raise _BatchStopped()
            with self._lock:
                now = time.monotonic()
                wait = self._next_time - now
                if wait <= 0:
                    self._next_time = now + self.interval
                    return
            if stop_event.wait(wait):
                raise _BatchStopped()

    def defer(self, seconds: float):
        with self._lock:
            self._next_time = max(
                self._next_time, time.monotonic() + max(0.0, seconds))


# 术语表
# ---------------------------------------------------------------------------
class Glossary:
    def __init__(self, path: str = "", inline: dict[str, str] | None = None):
        self.pairs: list[tuple[str, str]] = []
        if path:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"术语表文件不存在: {Path(path).resolve()}")
            self._load(path)
        values = dict(self.pairs)
        values.update({
            source.strip(): target.strip()
            for source, target in (inline or {}).items()
            if source.strip()
        })
        self.pairs = sorted(values.items(), key=lambda pair: len(pair[0]), reverse=True)
        self._source_ids: dict[str, int] = {}
        for i, (src, _tgt) in enumerate(self.pairs):
            self._source_ids.setdefault(src, i)
        digest = hashlib.sha256(json.dumps(
            self.pairs, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()[:12]
        self._tokens = [
            f"{{{{etg_{digest}_{i:06d}}}}}" for i in range(len(self.pairs))
        ]
        self._source_pattern = (
            re.compile("|".join(re.escape(src) for src in self._source_ids))
            if self._source_ids else None
        )

    def _load(self, path: str):
        content = Path(path).read_text(encoding="utf-8-sig").strip()
        groups = re.split(r"\n{2,}", content)
        for group in groups:
            group = group.strip()
            if not group:
                continue
            lines = group.split("\n")
            src = lines[0].strip()
            tgt = lines[1].strip() if len(lines) >= 2 else src
            if src:
                self.pairs.append((src, tgt))

    def apply(self, text: str) -> str:
        if self._source_pattern is None:
            return text
        return self._source_pattern.sub(
            lambda match: self._tokens[self._source_ids[match.group(0)]],
            text,
        )

    def restore(self, text: str) -> str:
        for i, (_src, tgt) in enumerate(self.pairs):
            token = self._tokens[i][2:-2]
            pattern = r"\{\{\s*" + re.escape(token) + r"\s*\}\}"
            text = re.sub(pattern, lambda _m, value=tgt: value, text)
        return text


def _file_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_cache_key(input_path: str, elements: list, config: Config,
                     glossary: Glossary) -> str:
    engine_cfg = config.get_engine()
    element_sig = md5(json.dumps(
        [(el.uid, el.page_href, el.original) for el in elements],
        ensure_ascii=False,
        separators=(",", ":"),
    ))
    # merge_enabled / merge_length only affect request batching, not translation
    # identity. Keep them out so batching changes reuse per-paragraph progress.
    payload = {
        "cache_version": 5,
        "source_content_md5": _file_md5(input_path),
        "element_signature": element_sig,
        "engine": config.engine,
        "source_lang": config.source_lang,
        "target_lang": config.target_lang,
        "prompt": config.effective_prompt(),
        "model": engine_cfg.model,
        "base_url": engine_cfg.base_url,
        "sampling": engine_cfg.sampling,
        "temperature": engine_cfg.temperature,
        "top_p": engine_cfg.top_p,
        "extra": engine_cfg.extra,
        "translate_tags": config.translate_tags,
        "exclude_translate_tags": config.exclude_translate_tags,
        "glossary": glossary.pairs,
    }
    return md5(json.dumps(payload, ensure_ascii=False, sort_keys=True))


# ---------------------------------------------------------------------------
# 翻译工作器
# ---------------------------------------------------------------------------
class TranslationWorker:
    def __init__(
        self,
        engine: TranslationEngine,
        cache: TranslationCache,
        config: Config,
        glossary: Glossary,
        progress_callback=None,
    ):
        self.engine = engine
        self.cache = cache
        self.config = config
        self.glossary = glossary
        self.abort_count = 0
        self.progress_callback = progress_callback
        self._abort_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._rate_limiter = _RateLimiter(0)

    def _max_attempts_for(self, kind: str, configured: int) -> int:
        configured = max(1, int(configured or 1))
        if kind == _ERR_PERMANENT:
            return 1
        if kind == _ERR_EMPTY:
            return min(configured, _EMPTY_MAX_ATTEMPTS)
        return configured

    def _translate_one(self, text: str, prompt: str | None = None) -> str:
        import random
        engine_cfg = self.config.get_engine()
        max_retries = max(1, int(engine_cfg.max_retries or 1))
        retry_delay = engine_cfg.retry_delay
        prompt = prompt if prompt is not None else self.config.effective_prompt()

        for attempt in range(1, max_retries + 1):
            self._rate_limiter.acquire(self._stop_event)
            try:
                result = self.engine.translate(text, prompt=prompt)
                if not result or not result.strip():
                    raise RuntimeError("API 返回空译文")
                with self._abort_lock:
                    self.abort_count = 0
                return result
            except Exception as e:
                kind = _classify_translation_error(e)
                if kind == _ERR_PERMANENT:
                    raise
                allowed = self._max_attempts_for(kind, max_retries)
                if attempt < allowed:
                    server_wait = None
                    if kind == _ERR_RATE_LIMIT:
                        retry_after = _retry_after_seconds(e)
                        if retry_after is not None:
                            self._rate_limiter.defer(retry_after)
                            server_wait = retry_after
                            base = 0
                        else:
                            base = retry_delay * 2 * attempt
                        tag = "限流"
                    elif kind == _ERR_EMPTY:
                        # 空译文短退避，避免白等
                        base = min(retry_delay, 2.0) * attempt
                        tag = "空译文"
                    else:
                        base = retry_delay * attempt
                        tag = "重试"
                    jitter = random.uniform(0.5, 1.5)
                    wait = server_wait if server_wait is not None else base * jitter
                    if self.progress_callback:
                        self.progress_callback(
                            "desc",
                            _c(f"{tag} {attempt}/{allowed} ({wait:.0f}s)", _YELLOW),
                        )
                    if self._stop_event.wait(wait):
                        raise _BatchStopped()
                else:
                    raise

    def _merge_groups(self, paragraphs: list) -> list[list]:
        if not self.config.merge_enabled or self.config.merge_length <= 0:
            return [[p] for p in paragraphs]

        groups: list[list] = []
        current: list = []
        current_len = 0
        for para in paragraphs:
            text_len = len(para.original)
            if current and current_len + text_len > self.config.merge_length:
                groups.append(current)
                current = []
                current_len = 0
            current.append(para)
            current_len += text_len
        if current:
            groups.append(current)
        return groups

    def _merge_prompt(self) -> str:
        return (
            self.config.effective_prompt()
            + "\n\nYou will receive a JSON array of segments. Translate each "
              "segment text independently and preserve all segment ids. Return "
              "only valid JSON in this exact shape: "
              '[{"id":"0","text":"translated text"}]. Do not wrap it in '
              "Markdown and do not add explanations."
        )

    def _parse_merged_result(self, response: str, expected_ids: set[str]) -> dict[str, str]:
        text = response.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text).strip()
        candidates = [i for i in (text.find("["), text.find("{")) if i >= 0]
        start = min(candidates) if candidates else -1
        if start > 0:
            text = text[start:]

        data = json.loads(text)
        result: dict[str, str] = {}
        if isinstance(data, dict):
            if isinstance(data.get("segments"), list):
                data = data["segments"]
            elif isinstance(data.get("translations"), list):
                data = data["translations"]
            else:
                for key, value in data.items():
                    if not isinstance(value, str):
                        raise ValueError("合并翻译返回的译文必须是字符串")
                    result[str(key)] = value
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    raise ValueError("合并翻译返回的数组元素不是对象")
                sid = str(item.get("id", ""))
                value = item.get("text", item.get("translation", ""))
                if not isinstance(value, str):
                    raise ValueError("合并翻译返回的译文必须是字符串")
                result[sid] = value

        if set(result) != expected_ids:
            raise ValueError("合并翻译返回的 segment id 不完整")
        empty = [sid for sid, value in result.items() if not value.strip()]
        if empty:
            raise ValueError("合并翻译返回空译文")
        return result

    def _translate_group(self, group: list) -> dict[str, str]:
        if len(group) == 1:
            para = group[0]
            text = self.glossary.apply(para.original)
            result = self._translate_one(text)
            return {para.id: self.glossary.restore(result).strip()}

        segments = [
            {"id": str(i), "text": self.glossary.apply(para.original)}
            for i, para in enumerate(group)
        ]
        payload = json.dumps(segments, ensure_ascii=False)
        expected_ids = {str(i) for i in range(len(group))}

        try:
            response = self._translate_one(payload, prompt=self._merge_prompt())
            merged = self._parse_merged_result(response, expected_ids)
        except (json.JSONDecodeError, ValueError) as e:
            logging.warning("合并翻译解析失败，回退逐段: %s", e)
        except RuntimeError as e:
            if not any(token in str(e).lower() for token in (
                "输出被截断", "内容过滤", "content_filter", "max_tokens", "refusal",
            )):
                raise
            logging.warning("合并翻译输出受限，回退逐段: %s", e)
        else:
            return {
                para.id: self.glossary.restore(merged[str(i)]).strip()
                for i, para in enumerate(group)
            }
        fallback: dict[str, str] = {}
        for para in group:
            text = self.glossary.apply(para.original)
            result = self._translate_one(text)
            fallback[para.id] = self.glossary.restore(result).strip()
        return fallback

    async def translate_batch(self, paragraphs: list, concurrency: int = 3,
                              interval: float = 1.0):
        concurrency = max(1, min(int(concurrency or 1), MAX_CONCURRENCY))
        sem = asyncio.Semaphore(concurrency)
        done_count = 0
        failed_count = 0
        self._stop_event = threading.Event()
        self._rate_limiter = _RateLimiter(interval)
        stop_event = self._stop_event
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
        groups = self._merge_groups(paragraphs)

        async def translate_group(group):
            nonlocal done_count, failed_count
            if stop_event.is_set():
                failed_count += len(group)
                if self.progress_callback:
                    self.progress_callback("update", len(group))
                return

            async with sem:
                if stop_event.is_set():
                    failed_count += len(group)
                    if self.progress_callback:
                        self.progress_callback("update", len(group))
                    return
                loop = asyncio.get_running_loop()
                try:
                    result_map = await loop.run_in_executor(
                        executor, self._translate_group, group
                    )
                    updates = []
                    for para in group:
                        result = result_map.get(para.id, "").strip()
                        if not result:
                            raise RuntimeError("API 返回空译文")
                        updates.append((
                            para.id, result,
                            self.config.engine, self.config.target_lang,
                        ))
                    self.cache.update_translations(updates)
                    done_count += len(group)
                    with self._abort_lock:
                        self.abort_count = 0
                except _BatchStopped:
                    failed_count += len(group)
                except Exception as e:
                    if self.config.skip_failed:
                        failed_count += len(group)
                        if self.progress_callback:
                            sample = group[0].original if group else ""
                            self.progress_callback(
                                "write",
                                _c(f"  ⚠ 跳过: {sample[:60]}... -> {str(e)[:80]}", _YELLOW),
                            )
                    else:
                        failed_count += len(group)
                        with self._abort_lock:
                            self.abort_count += 1
                            abort_count = self.abort_count
                        if self.progress_callback:
                            sample = group[0].original if group else ""
                            self.progress_callback(
                                "write",
                                _c(f"  ✗ 翻译失败: {sample[:60]}... -> {str(e)[:80]}", _RED),
                            )
                        if (self.config.max_error_count > 0
                                and abort_count >= self.config.max_error_count):
                            stop_event.set()
                finally:
                    if self.progress_callback:
                        self.progress_callback("update", len(group))

        try:
            tasks = [translate_group(group) for group in groups]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logging.error(
                        "翻译任务异常: %s",
                        result,
                        exc_info=(type(result), result, result.__traceback__),
                    )
            return done_count, failed_count
        finally:
            stop_event.set()
            executor.shutdown(wait=True)

# ---------------------------------------------------------------------------
# 单本书翻译流程
# ---------------------------------------------------------------------------
def _do_retranslate(cache, config):
    """Clear translations for paragraphs matching retranslate range."""
    target_file = config.retranslate_file
    start_text = config.retranslate_start
    end_text = config.retranslate_end or ""

    # Find paragraph IDs that match the file and text range
    all_paras = cache.get_all_with_ignored()
    matching_ids = []
    in_range = False

    for para in all_paras:
        if target_file:
            if not para.page:
                continue
            if target_file not in {para.page, Path(para.page).name}:
                continue
        if start_text and start_text in para.original:
            in_range = True
        if in_range:
            matching_ids.append(para.id)
        if end_text and end_text in para.original and in_range:
            in_range = False
            break

    if matching_ids:
        cleared = cache.clear_translations(matching_ids)
        logging.info("重翻译: 已清除 %d 段缓存 (匹配 %s)", cleared, target_file)
        if sys.stderr.isatty():
            print(
                _c(f"  重翻译: 已清除 {cleared} 段缓存", _YELLOW),
                file=sys.stderr,
            )
    else:
        logging.warning("重翻译: 未找到匹配的段落 (file=%s, start=%s)", target_file, start_text)


def translate_book(
    input_path: str,
    output_path: str,
    output_format: str,
    config: Config,
    glossary: Glossary,
    book_pbar=None,
    overall_pbar=None,
):
    resources: dict[str, object] = {}
    try:
        return _translate_book_impl(
            input_path, output_path, output_format, config, glossary,
            book_pbar, overall_pbar, resources,
        )
    finally:
        cache = resources.get("cache")
        if cache is not None:
            try:
                cache.close()
            except Exception:
                pass
        translated = resources.get("translated_epub")
        if isinstance(translated, str) and os.path.exists(translated):
            try:
                os.remove(translated)
            except OSError:
                pass
        tmp_epub = resources.get("tmp_epub")
        if isinstance(tmp_epub, str):
            _cleanup(tmp_epub)


def _translate_book_impl(
    input_path: str,
    output_path: str,
    output_format: str,
    config: Config,
    glossary: Glossary,
    book_pbar=None,
    overall_pbar=None,
    _resources: dict[str, object] | None = None,
):
    """完整流程：提取 -> 翻译 -> 注入 -> 转换。"""
    from tqdm import tqdm

    input_ext = Path(input_path).suffix.lstrip(".").lower()
    if input_ext not in SUPPORTED_INPUT_FORMATS:
        if book_pbar:
            book_pbar.write(_c(f"  ✗ 不支持的格式: .{input_ext}", _RED))
        return False

    book_name = Path(input_path).stem
    char_count = 0
    logging.info("开始处理: %s", input_path)

    def _status(msg):
        if book_pbar:
            book_pbar.write(msg)
        elif overall_pbar:
            overall_pbar.write(msg)

    # --- 步骤 1: 获取 EPUB ---
    tmp_epub = None
    working_epub = input_path

    if input_ext not in NATIVE_FORMATS:
        _status(_c(f"转换格式 .{input_ext} -> EPUB", _CYAN))
        try:
            tmp_epub = convert_to_epub(input_path, config.ebook_convert_path)
            working_epub = tmp_epub
            if _resources is not None:
                _resources["tmp_epub"] = tmp_epub
        except ConverterError as e:
            _err(f"格式转换失败: {e}", book_pbar, overall_pbar)
            return False

    # --- 步骤 2: 提取可翻译内容 ---
    _status(_c("提取文本内容", _CYAN))
    try:
        elements, meta = extract_from_epub(
            working_epub,
            only_files=config.only_files,
            exclude_files=config.exclude_files,
            translate_tags=config.translate_tags,
            exclude_translate_tags=config.exclude_translate_tags,
        )
    except Exception as e:
        _err(f"EPUB 解析失败: {e}", book_pbar, overall_pbar)
        _cleanup(tmp_epub)
        return False

    if not elements:
        _status(_c("  ✗ 未找到可翻译内容", _YELLOW))
        logging.warning("未找到可翻译内容: %s", input_path)
        _cleanup(tmp_epub)
        return False

    char_count = sum(len(el.original) for el in elements)
    title = meta.get("title", book_name) or book_name

    # --- 步骤 3: 设置缓存 ---
    _status(_c("检查缓存", _CYAN))
    cache_key = _build_cache_key(input_path, elements, config, glossary)
    if config.cache_enabled:
        cache_dir = os.path.join(config.cache_dir, "books")
        cache = TranslationCache(os.path.join(cache_dir, f"{cache_key}.db"))
        logging.info("缓存文件: %s", cache.db_path)
    else:
        cache = TranslationCache(":memory:", persistence=False)
        logging.info("缓存已禁用: 使用内存缓存")
    if _resources is not None:
        _resources["cache"] = cache
    cache.set_info("title", title)
    cache.set_info("engine", config.engine)
    cache.set_info("target_lang", config.target_lang)
    cache.set_info("source", str(Path(input_path).resolve()))

    rows = build_cache_rows(elements)
    cache.save_paragraphs(rows)

    # --- Retranslate mode: clear specific paragraph translations ---
    if config.retranslate_start:
        _do_retranslate(cache, config)

    untranslated = cache.get_untranslated()
    total = cache.total_count()
    already = cache.translated_count()

    # --- Test mode: limit to first N paragraphs ---
    if config.test_enabled and config.test_num > 0:
        untranslated = untranslated[:config.test_num]
        logging.info("测试模式: 仅翻译前 %d 段", config.test_num)

    # --- 步骤 4: 翻译 ---
    if untranslated:
        try:
            engine_cls = get_engine(config.engine)
            engine_cfg = config.get_engine()
            engine = engine_cls(engine_cfg, config.source_lang, config.target_lang)
        except ValueError as e:
            _err(f"引擎初始化失败: {e}", book_pbar, overall_pbar)
            cache.close()
            _cleanup(tmp_epub)
            return False

        concurrency = engine_cfg.concurrency
        interval = engine_cfg.request_interval

        # tqdm 子进度条：翻译进度
        trans_desc = _c(f"翻译 {title[:30]}", _BOLD)
        if already > 0:
            trans_desc += _c(f" (缓存 {already}/{total})", _DIM)

        trans_pbar = tqdm(
            total=len(untranslated),
            desc=trans_desc,
            unit="段",
            leave=False,
            position=1 if (book_pbar or overall_pbar) else 0,
            ncols=80,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        )

        def _progress(event, value):
            if event == "update":
                trans_pbar.update(value)
            elif event == "write":
                trans_pbar.write(value)
            elif event == "desc":
                trans_pbar.set_description(value)

        worker = TranslationWorker(engine, cache, config, glossary,
                                    progress_callback=_progress)

        start = time.time()
        try:
            done, failed = asyncio.run(
                worker.translate_batch(untranslated, concurrency, interval)
            )
        except (RuntimeError, KeyboardInterrupt):
            trans_pbar.close()
            cache.close()
            _cleanup(tmp_epub)
            raise
        finally:
            try:
                engine.close()
            except Exception:
                pass

        elapsed = time.time() - start
        trans_pbar.close()

        if book_pbar:
            msg = f"  ✓ {title}: {total} 段, {char_count} 字符"
            if already > 0:
                msg += f", 缓存命中 {already}"
            if failed > 0:
                msg += _c(f", 失败 {failed}", _RED)
            msg += _c(f" [{elapsed:.1f}s]", _DIM)
            book_pbar.write(msg)
        logging.info(
            "翻译完成: %s, total=%s, done=%s, failed=%s, cached=%s, elapsed=%.1fs",
            title, total, done, failed, already, elapsed,
        )
        if failed > 0:
            if config.skip_failed:
                if book_pbar:
                    book_pbar.write(
                        _c(f"  ⚠ {title}: {failed} 段翻译失败，已跳过保留原文", _YELLOW))
                logging.warning("%s: %d 段翻译失败，已跳过", title, failed)
            else:
                _err(f"{title}: {failed} 段翻译失败，已保留进度，未生成输出",
                     book_pbar, overall_pbar)
                cache.close()
                _cleanup(tmp_epub)
                return False
    else:
        if book_pbar:
            book_pbar.write(
                f"  ✓ {title}: {total} 段全部已缓存 "
                + _c(f"({char_count} 字符)", _DIM)
            )
        logging.info("全部命中缓存: %s, total=%s", title, total)

    # --- 步骤 5: 注入翻译 ---
    _status(_c("写入译文", _CYAN))
    all_paras = cache.get_all()
    missing = [p for p in all_paras if not p.translation]
    if missing:
        if config.skip_failed:
            logging.warning("%s: %d 段未翻译，本次输出保留原文", title, len(missing))
        else:
            _err(f"{title}: 仍有 {len(missing)} 段未翻译，未生成输出",
                 book_pbar, overall_pbar)
            cache.close()
            _cleanup(tmp_epub)
            return False
    trans_map = {p.id: p.translation for p in all_paras if p.translation}

    output_dir = os.path.dirname(output_path) or "."
    fd, translated_epub = tempfile.mkstemp(
        prefix=f".{book_name}_", suffix=".epub", dir=output_dir)
    os.close(fd)
    os.remove(translated_epub)
    if _resources is not None:
        _resources["translated_epub"] = translated_epub
    try:
        injected = write_translated_epub(
            working_epub, translated_epub, trans_map,
            position=config.translation_position,
            expected_count=len(trans_map),
            style=config.translation_style,
            translate_tags=config.translate_tags,
            exclude_translate_tags=config.exclude_translate_tags,
        )
        logging.info("写入译文: %s, injected=%s", title, injected)
    except Exception as e:
        _err(f"写入译文失败: {e}", book_pbar, overall_pbar)
        if os.path.exists(translated_epub):
            os.remove(translated_epub)
        cache.close()
        _cleanup(tmp_epub)
        return False

    # --- 步骤 6: 格式转换 ---
    final_path = output_path
    if output_format != "epub":
        _status(_c(f"转换为 .{output_format}", _CYAN))
        try:
            convert(translated_epub, final_path, output_format,
                    config.ebook_convert_path)
        except ConverterError as e:
            base = output_path.rsplit(".", 1)[0]
            fallback = base + ".translated.epub"
            suffix = 2
            while (os.path.exists(fallback)
                   or os.path.abspath(fallback) == os.path.abspath(input_path)):
                fallback = f"{base}.translated-{suffix}.epub"
                suffix += 1
            os.replace(translated_epub, fallback)
            _err(f"输出转换失败({e})，已回退保存为 EPUB: {fallback}",
                 book_pbar, overall_pbar)
            cache.close()
            _cleanup(tmp_epub)
            return False
        if os.path.exists(translated_epub):
            os.remove(translated_epub)
    else:
        os.replace(translated_epub, final_path)

    cache.close()
    _cleanup(tmp_epub)
    logging.info("处理完成: %s -> %s", input_path, final_path)
    return True


def _cleanup(tmp_epub):
    if tmp_epub:
        tmp_dir = os.path.dirname(tmp_epub)
        if os.path.basename(tmp_dir).startswith("et_epub_"):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            try:
                os.remove(tmp_epub)
            except FileNotFoundError:
                pass

# ---------------------------------------------------------------------------
# 收集待翻译书籍
# ---------------------------------------------------------------------------
def collect_books(input_path: str) -> list[str]:
    p = Path(input_path)
    if p.is_file():
        if p.suffix.lstrip(".").lower() in SUPPORTED_INPUT_FORMATS:
            return [str(p)]
        return []
    if p.is_dir():
        books: list[str] = []
        for f in sorted(p.iterdir()):
            if (f.is_file() and not f.name.startswith(".")
                    and f.suffix.lstrip(".").lower() in SUPPORTED_INPUT_FORMATS):
                books.append(str(f))
        return books
    return []


# ---------------------------------------------------------------------------
# 格式化文件大小
# ---------------------------------------------------------------------------
def _human_size(path: str) -> str:
    try:
        size = os.path.getsize(path)
    except OSError:
        return "?"
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# CLI 主函数
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None):
    # Windows 终端编码兜底：避免中文/Unicode 符号输出时 UnicodeEncodeError
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None:
            try:
                _stream.reconfigure(errors="replace")
            except Exception:
                pass

    from tqdm import tqdm

    args = _parse_args(argv)
    try:
        config = _apply_overrides(args)
    except (OSError, ValueError, TypeError) as e:
        print(_c("配置错误: ", _RED) + str(e), file=sys.stderr)
        raise SystemExit(1) from e

    # 设置日志
    if config.log_file:
        os.makedirs(os.path.dirname(config.log_file) or ".", exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.FileHandler(config.log_file, encoding="utf-8")],
            force=True,
        )
        logging.info("启动 ebook-translator v%s", __version__)
    else:
        logging.basicConfig(handlers=[logging.NullHandler()], force=True)

    # 打印 banner
    if sys.stderr.isatty():
        print(_c(f"\n  ebook-translator v{__version__}", _BOLD), file=sys.stderr)
        print(
            _c(f"  引擎: ", _DIM) + _c(config.engine, _CYAN) +
            _c(f"  | ", _DIM) +
            _c(config.source_lang, _GREEN) + _c(" -> ", _DIM) +
            _c(config.target_lang, _GREEN),
            file=sys.stderr,
        )
        print(file=sys.stderr)

    # 收集书籍
    books = collect_books(args.input)
    if not books:
        logging.error("未找到支持的电子书文件: %s", args.input)
        print(_c("错误: ", _RED) + f"在 {args.input} 中未找到支持的电子书文件", file=sys.stderr)
        print(_c(f"支持的格式: {', '.join(sorted(SUPPORTED_INPUT_FORMATS))}", _DIM), file=sys.stderr)
        sys.exit(1)
    logging.info("找到 %s 本书", len(books))

    output_format = args.output_format.lower()
    # 预览模式
    if args.dry_run:
        print(_c_stdout(f"\n找到 {len(books)} 本书:\n", _BOLD))
        for b in books:
            print(f"  {Path(b).name}  {_c_stdout(f'({_human_size(b)})', _DIM)}")
            logging.info("dry-run: %s", b)
        print()
        return

    output_paths: dict[str, str] = {}
    for book in books:
        out_path = os.path.realpath(
            os.path.join(args.output, f"{Path(book).stem}.{output_format}")
        )
        if out_path in output_paths:
            print(
                _c("错误: ", _RED)
                + f"输入文件输出名冲突: {output_paths[out_path]} 和 {book}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if os.path.realpath(book) == out_path:
            print(
                _c("错误: ", _RED) + f"拒绝覆盖输入文件: {book}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        output_paths[out_path] = book

    # 检查输出目录
    try:
        os.makedirs(args.output, exist_ok=True)
    except OSError as e:
        print(
            _c("错误: ", _RED) + f"无法创建输出目录 {args.output}: {e}",
            file=sys.stderr,
        )
        raise SystemExit(1) from e
    if not os.access(args.output, os.W_OK):
        logging.error("输出目录无写入权限: %s", args.output)
        print(_c("错误: ", _RED) + f"输出目录无写入权限: {args.output}", file=sys.stderr)
        sys.exit(1)

    # 加载术语表
    try:
        glossary = Glossary(config.glossary_path, config.glossary)
    except OSError as e:
        print(_c("配置错误: ", _RED) + str(e), file=sys.stderr)
        raise SystemExit(1) from e
    if glossary.pairs:
        if sys.stderr.isatty():
            print(_c(f"  术语表: {len(glossary.pairs)} 条", _DIM), file=sys.stderr)

    # 检查 ebook-convert (仅非 EPUB 输出时)
    if output_format != "epub":
        from .converter import find_ebook_convert
        try:
            find_ebook_convert(config.ebook_convert_path)
        except ConverterError:
            logging.error("未找到 ebook-convert，无法输出 %s", output_format)
            print(
                _c("错误: ", _RED) +
                f"输出格式为 .{output_format} 但未找到 ebook-convert\n" +
                _c("  提示: ", _DIM) + "安装 calibre 或使用 -o epub 直接输出 EPUB",
                file=sys.stderr,
            )
            sys.exit(1)

    # 处理书籍
    results: list[dict] = []  # 每本书的结果
    interrupted = False

    # 信号处理：Ctrl+C 优雅退出
    def _sigint_handler(sig, frame):
        nonlocal interrupted
        if interrupted:
            print(_c("\n\n  强制退出", _RED), file=sys.stderr)
            os._exit(130)
        interrupted = True
        print(
            _c("\n\n  ⚠ 收到中断信号，正在保存进度...", _YELLOW),
            _c("\n  再按一次 Ctrl+C 强制退出\n", _DIM),
            file=sys.stderr,
        )
        raise KeyboardInterrupt

    original_handler = signal.signal(signal.SIGINT, _sigint_handler)

    # 总体进度条
    overall_pbar = tqdm(
        total=len(books),
        desc=_c("总进度", _BOLD),
        unit="本",
        position=0,
        ncols=80,
        bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
    )

    batch_start = time.time()

    try:
        for book_path in books:
            if interrupted:
                break

            stem = Path(book_path).stem
            out_path = os.path.join(args.output, f"{stem}.{output_format}")
            book_result = {
                "name": stem,
                "input": book_path,
                "output": out_path,
                "success": False,
                "skipped": False,
            }

            if os.path.exists(out_path) and not args.force:
                logging.info("跳过已有输出: %s", out_path)
                overall_pbar.write(
                    f"  ⏭ 跳过: {stem}"
                    + _c(" (输出文件已存在，使用 --force 覆盖)", _DIM)
                )
                book_result["skipped"] = True
            else:
                try:
                    ok = translate_book(
                        book_path, out_path, output_format, config, glossary,
                        book_pbar=None,
                        overall_pbar=overall_pbar,
                    )
                    book_result["success"] = ok
                    logging.info("书籍结果: %s success=%s", book_path, ok)
                except Exception as e:
                    _err(f"处理失败 {stem}: {e}", overall_pbar)
                    logging.exception("处理失败: %s", book_path)

            results.append(book_result)
            overall_pbar.update(1)
    except KeyboardInterrupt:
        interrupted = True
        logging.warning("批处理被中断")
    finally:
        overall_pbar.close()
        signal.signal(signal.SIGINT, original_handler)

    # 打印摘要
    batch_elapsed = time.time() - batch_start
    _print_summary(results, batch_elapsed, interrupted)
    failed = sum(1 for r in results if not r["success"] and not r["skipped"])
    if interrupted:
        sys.exit(130)
    if failed > 0:
        sys.exit(1)


def _print_summary(results: list[dict], elapsed: float, interrupted: bool):
    succeeded = sum(1 for r in results if r["success"])
    failed = sum(1 for r in results if not r["success"] and not r["skipped"])
    skipped = sum(1 for r in results if r["skipped"])

    print(file=sys.stderr)
    if interrupted:
        print(_c("  ⚠ 翻译被中断，进度已保存", _YELLOW), file=sys.stderr)
    else:
        print(_c("  ─── 翻译完成 ───", _BOLD), file=sys.stderr)

    print(file=sys.stderr)
    print(f"  成功: {_c(str(succeeded), _GREEN)}", file=sys.stderr)
    if failed > 0:
        print(f"  失败: {_c(str(failed), _RED)}", file=sys.stderr)
    if skipped > 0:
        print(f"  跳过: {_c(str(skipped), _DIM)}", file=sys.stderr)
    print(f"  用时: {elapsed / 60:.1f} 分钟", file=sys.stderr)
    print(file=sys.stderr)

    if interrupted:
        print(
            _c("  提示: ", _DIM) +
            "重新运行同一命令即可从断点继续翻译",
            file=sys.stderr,
        )
        print(file=sys.stderr)

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ebook-translator",
        description="无头命令行批量电子书翻译工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_c("示例:\n", _DIM) +
              f"  ebook-translator /books /output -o mobi -c config.json\n"
              f"  ebook-translator /book.epub /output -o epub\n"
              f"  ebook-translator /books /output --dry-run\n",
    )
    p.add_argument("input", help="输入目录或单个电子书文件路径")
    p.add_argument("output", help="输出目录")
    p.add_argument(
        "--output-format", "-o", default="epub",
        choices=("epub", "mobi", "azw3"),
        help="输出格式 (epub, mobi, azw3)，默认: epub",
    )
    p.add_argument(
        "--config", "-c", default="",
        help="配置文件路径 (config.json)",
    )
    p.add_argument(
        "--engine", "-e", default="",
        choices=("openai", "claude", "deepseek"),
        help="翻译引擎 (openai, claude, deepseek)",
    )
    p.add_argument(
        "--source-lang", "-s", default="",
        help="源语言",
    )
    p.add_argument(
        "--target-lang", "-t", default="",
        help="目标语言",
    )
    p.add_argument(
        "--concurrency", type=int, default=0,
        help="并发翻译数",
    )
    p.add_argument(
        "--force", "-f", action="store_true",
        help="覆盖已存在的输出文件",
    )
    p.add_argument(
        "--no-cache", action="store_true",
        help="禁用翻译缓存 (不支持断点续翻)",
    )
    p.add_argument(
        "--skip-failed", action="store_true",
        help="跳过翻译失败的段落，保留原文继续生成输出",
    )
    p.add_argument(
        "--log-file", default="",
        help="日志输出到文件",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="预览模式：仅列出待翻译的书籍",
    )
    p.add_argument(
        "--test", action="store_true",
        help="测试模式：仅翻译前几段",
    )
    p.add_argument(
        "--test-num", type=int, default=0,
        help="测试模式翻译段落数 (默认: 10)",
    )
    p.add_argument(
        "--retranslate-file", default="",
        help="重翻译的页面文件名",
    )
    p.add_argument(
        "--retranslate-start", default="",
        help="重翻译起始文本",
    )
    p.add_argument(
        "--retranslate-end", default="",
        help="重翻译结束文本",
    )
    p.add_argument(
        "--version", "-V", action="version",
        version=f"ebook-translator {__version__}",
    )
    return p.parse_args(argv)


def _apply_overrides(args: argparse.Namespace) -> Config:
    config = load_config(args.config)

    if args.engine:
        config.engine = args.engine
    if args.source_lang:
        config.source_lang = args.source_lang
    if args.target_lang:
        config.target_lang = args.target_lang
    if args.concurrency > 0:
        if args.concurrency > MAX_CONCURRENCY:
            raise ValueError(f"concurrency 不能大于 {MAX_CONCURRENCY}")
        ecfg = config.get_engine()
        ecfg.concurrency = args.concurrency
        config.engines[config.engine] = ecfg
    if args.no_cache:
        config.cache_enabled = False
    if args.skip_failed:
        config.skip_failed = True
    if args.log_file:
        config.log_file = args.log_file
    if args.test:
        config.test_enabled = True
    if config.test_enabled:
        config.skip_failed = True
    if args.test_num > 0:
        config.test_num = args.test_num
    if args.retranslate_file:
        config.retranslate_file = args.retranslate_file
    if args.retranslate_start:
        config.retranslate_start = args.retranslate_start
    if args.retranslate_end:
        config.retranslate_end = args.retranslate_end

    values = (
        config.retranslate_file.strip(),
        config.retranslate_start.strip(),
        config.retranslate_end.strip(),
    )
    if any(values) and not values[1]:
        raise ValueError("重翻译必须设置 retranslate_start")

    return config
