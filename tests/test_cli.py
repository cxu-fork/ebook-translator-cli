import json
import shutil
import tempfile
import time
import unittest
import zipfile
import asyncio
import httpx
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from ebook_translator import cli
from ebook_translator.cache import TranslationCache
from ebook_translator.config import Config, EngineConfig


def make_epub(path: Path, paragraphs: list[str]):
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            "<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
            "<rootfiles><rootfile full-path='content.opf' "
            "media-type='application/oebps-package+xml'/></rootfiles></container>",
        )
        zf.writestr(
            "content.opf",
            "<package xmlns='http://www.idpf.org/2007/opf'>"
            "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/'>"
            "<dc:title>T</dc:title></metadata>"
            "<manifest><item id='c' href='c.xhtml' "
            "media-type='application/xhtml+xml'/></manifest>"
            "<spine><itemref idref='c'/></spine></package>",
        )
        zf.writestr(
            "c.xhtml",
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            f"{body}</body></html>",
        )


class FakeEngine:
    calls = 0
    fail_on = ""

    def __init__(self, _config, _source_lang, _target_lang):
        pass

    def translate(self, text: str, prompt: str = "") -> str:
        type(self).calls += 1
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("simulated failure")
        if text.strip().startswith("["):
            segments = json.loads(text)
            return json.dumps([
                {"id": item["id"], "text": f"合并译文 {item['text']}"}
                for item in segments
            ], ensure_ascii=False)
        return f"译文 {text}"


class AuthFailEngine:
    calls = 0

    def translate(self, text: str, prompt: str = "") -> str:
        type(self).calls += 1
        raise RuntimeError("401 Unauthorized: API 密钥无效或已过期")


class AlwaysFailEngine:
    calls = 0

    def translate(self, text: str, prompt: str = "") -> str:
        type(self).calls += 1
        raise RuntimeError("temporary failure")


@dataclass
class Para:
    id: str
    original: str


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="et_cli_test_"))
        self.config = Config(
            engine="openai",
            cache_dir=str(self.tmp / "cache"),
            max_error_count=10,
        )
        self.config.engines["openai"] = EngineConfig(
            api_key="x", max_retries=1, concurrency=4, request_interval=0,
            stream=False,
        )
        FakeEngine.calls = 0
        FakeEngine.fail_on = ""

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_partial_translation_failure_does_not_write_output(self):
        epub = self.tmp / "in.epub"
        out = self.tmp / "out.epub"
        make_epub(epub, ["good", "bad"])
        FakeEngine.fail_on = "bad"

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine):
            ok = cli.translate_book(
                str(epub), str(out), "epub", self.config, cli.Glossary(""))

        self.assertFalse(ok)
        self.assertFalse(out.exists())

    def test_no_cache_retranslates_on_next_run(self):
        epub = self.tmp / "in.epub"
        make_epub(epub, ["hello"])
        self.config.cache_enabled = False

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine):
            ok1 = cli.translate_book(
                str(epub), str(self.tmp / "out1.epub"), "epub",
                self.config, cli.Glossary(""))
            ok2 = cli.translate_book(
                str(epub), str(self.tmp / "out2.epub"), "epub",
                self.config, cli.Glossary(""))

        self.assertTrue(ok1)
        self.assertTrue(ok2)
        self.assertEqual(2, FakeEngine.calls)
        self.assertFalse((self.tmp / "cache" / "books").exists())

    def test_merge_enabled_translates_multiple_paragraphs_in_one_request(self):
        epub = self.tmp / "in.epub"
        out = self.tmp / "out.epub"
        make_epub(epub, ["one", "two", "three"])
        self.config.merge_enabled = True
        self.config.merge_length = 100

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine):
            ok = cli.translate_book(
                str(epub), str(out), "epub", self.config, cli.Glossary(""))

        self.assertTrue(ok)
        self.assertEqual(1, FakeEngine.calls)
        with zipfile.ZipFile(out) as zf:
            data = zf.read("c.xhtml").decode("utf-8")
        self.assertEqual(3, data.count("et-translation"))
        self.assertIn("合并译文 one", data)
        self.assertIn("合并译文 two", data)
        self.assertIn("合并译文 three", data)

    def test_main_exits_nonzero_when_a_book_fails(self):
        bad = self.tmp / "bad.epub"
        bad.write_text("not a zip", encoding="utf-8")
        out_dir = self.tmp / "out"

        with self.assertRaises(SystemExit) as ctx:
            cli.main([str(bad), str(out_dir), "-o", "epub"])

        self.assertEqual(1, ctx.exception.code)

    def test_main_rejects_two_inputs_with_the_same_output_name(self):
        books = self.tmp / "books"
        books.mkdir()
        make_epub(books / "same.epub", ["one"])
        (books / "same.mobi").write_bytes(b"mobi")

        with self.assertRaises(SystemExit) as ctx:
            cli.main([str(books), str(self.tmp / "out")])

        self.assertEqual(1, ctx.exception.code)

    def test_main_rejects_output_directory_symlink_to_input_directory(self):
        books = self.tmp / "books-link-source"
        books.mkdir()
        book = books / "same.epub"
        make_epub(book, ["hello"])
        output_link = self.tmp / "output-link"
        try:
            output_link.symlink_to(books, target_is_directory=True)
        except OSError as e:
            self.skipTest(f"symlink unavailable: {e}")

        with self.assertRaises(SystemExit) as ctx:
            cli.main([str(book), str(output_link), "-o", "epub"])

        self.assertEqual(1, ctx.exception.code)

    def test_glossary_uses_longest_terms_first_and_restores_spaced_tokens(self):
        path = self.tmp / "glossary.txt"
        path.write_text(
            "AI\n人工智能\n\nAI model\nAI模型\n",
            encoding="utf-8",
        )
        glossary = cli.Glossary(str(path))

        protected = glossary.apply("AI model beats AI")
        self.assertIn(glossary._tokens[0], protected)
        self.assertIn(glossary._tokens[1], protected)
        self.assertNotIn(glossary._tokens[1] + " model", protected)
        spaced = (
            "{{ " + glossary._tokens[0][2:-2] + " }} beats "
            "{{ " + glossary._tokens[1][2:-2] + " }}"
        )
        self.assertEqual(
            "AI模型 beats 人工智能",
            glossary.restore(spaced),
        )

    def test_glossary_single_line_protects_and_inline_overrides(self):
        path = self.tmp / "glossary-single.txt"
        path.write_text("OpenAI\n\nAI\nold\n", encoding="utf-8")
        glossary = cli.Glossary(str(path), {"AI": "人工智能", "": "bad"})
        self.assertEqual(
            "OpenAI 人工智能",
            glossary.restore(glossary.apply("OpenAI AI")),
        )

    def test_merge_truncation_falls_back_to_individual_requests(self):
        calls: list[str] = []

        class TruncatedMergeEngine:
            def translate(self, text: str, prompt: str = "") -> str:
                calls.append(text)
                if text.strip().startswith("["):
                    raise RuntimeError("API 输出被截断 (finish_reason=length)")
                return f"译文 {text}"

        cache = TranslationCache(str(self.tmp / "merge-truncated.db"))
        paras = [Para("0", "one"), Para("1", "two")]
        cache.save_paragraphs([
            (p.id, p.id, "", p.original, False, None, None) for p in paras
        ])
        cfg = Config(engine="openai", merge_enabled=True, merge_length=100)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=1, request_interval=0,
        )
        worker = cli.TranslationWorker(
            TruncatedMergeEngine(), cache, cfg, cli.Glossary(""))

        self.assertEqual(
            (2, 0), asyncio.run(worker.translate_batch(paras, concurrency=1, interval=0))
        )
        self.assertEqual(3, len(calls))
        cache.close()

    def test_glossary_does_not_replace_inside_its_own_placeholder(self):
        path = self.tmp / "glossary_tokens.txt"
        path.write_text("AI model\nAI模型\n\nid\n标识\n", encoding="utf-8")
        glossary = cli.Glossary(str(path))

        self.assertEqual("AI模型", glossary.restore(glossary.apply("AI model")))

    def test_glossary_does_not_replace_literal_legacy_placeholder(self):
        path = self.tmp / "glossary_literal.txt"
        path.write_text("AI\n人工智能\n", encoding="utf-8")
        glossary = cli.Glossary(str(path))

        original = "Keep {{id_000000}} and translate AI"

        self.assertEqual(
            "Keep {{id_000000}} and translate 人工智能",
            glossary.restore(glossary.apply(original)),
        )

    def test_missing_glossary_is_a_configuration_error(self):
        with self.assertRaises(FileNotFoundError):
            cli.Glossary(str(self.tmp / "missing.txt"))

    def test_permanent_auth_error_is_not_retried(self):
        cache = TranslationCache(str(self.tmp / "auth.db"))
        cache.save_paragraphs([("p0", "m0", "", "hello", False, None, None)])
        cfg = Config(engine="openai", max_error_count=10)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=3, retry_delay=0, request_interval=0,
        )
        AuthFailEngine.calls = 0
        worker = cli.TranslationWorker(
            AuthFailEngine(), cache, cfg, cli.Glossary(""))

        done, failed = asyncio.run(worker.translate_batch(
            [Para("p0", "hello")], concurrency=1, interval=0))

        cache.close()
        self.assertEqual((0, 1), (done, failed))
        self.assertEqual(1, AuthFailEngine.calls)

    def test_max_error_count_counts_unstarted_groups_as_failed(self):
        cache = TranslationCache(str(self.tmp / "fail.db"))
        paras = [Para(str(i), f"text {i}") for i in range(3)]
        cache.save_paragraphs([
            (p.id, p.id, "", p.original, False, None, None) for p in paras
        ])
        cfg = Config(engine="openai", max_error_count=1, merge_enabled=False)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=1, request_interval=0,
        )
        AlwaysFailEngine.calls = 0
        worker = cli.TranslationWorker(
            AlwaysFailEngine(), cache, cfg, cli.Glossary(""))

        done, failed = asyncio.run(worker.translate_batch(
            paras, concurrency=1, interval=0))

        cache.close()
        self.assertEqual((0, 3), (done, failed))
        self.assertEqual(1, AlwaysFailEngine.calls)

    def test_max_error_count_cancels_rate_limited_groups_without_waiting(self):
        cache = TranslationCache(str(self.tmp / "fast_stop.db"))
        paras = [Para(str(i), f"text {i}") for i in range(8)]
        cache.save_paragraphs([
            (p.id, p.id, "", p.original, False, None, None) for p in paras
        ])
        cfg = Config(engine="openai", max_error_count=1, merge_enabled=False)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=1, request_interval=0,
        )
        AlwaysFailEngine.calls = 0
        worker = cli.TranslationWorker(
            AlwaysFailEngine(), cache, cfg, cli.Glossary(""))

        started = time.monotonic()
        result = asyncio.run(worker.translate_batch(
            paras, concurrency=1, interval=0.05))
        elapsed = time.monotonic() - started

        cache.close()
        self.assertEqual((0, 8), result)
        self.assertEqual(1, AlwaysFailEngine.calls)
        self.assertLess(elapsed, 0.2)


    def test_test_mode_limits_paragraphs(self):
        epub = self.tmp / "test_mode.epub"
        out = self.tmp / "out.epub"
        make_epub(epub, ["one", "two", "three", "four", "five"])
        self.config.test_enabled = True
        self.config.test_num = 2
        self.config.skip_failed = True

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine):
            ok = cli.translate_book(
                str(epub), str(out), "epub", self.config, cli.Glossary(""))

        self.assertTrue(ok)
        with zipfile.ZipFile(out) as zf:
            data = zf.read("c.xhtml").decode("utf-8")
        # All 5 paragraphs should exist in output (injection includes all)
        # but only first 2 got translated, rest keep originals
        self.assertIn("译文 one", data)
        self.assertIn("译文 two", data)
        # 3, 4, 5 were not translated, should keep original
        self.assertIn("three", data)
        self.assertIn("four", data)
        self.assertIn("five", data)

    def test_skip_failed_preserves_original_for_failed_paragraphs(self):
        epub = self.tmp / "skip.epub"
        out = self.tmp / "out.epub"
        make_epub(epub, ["good", "bad", "ok"])
        self.config.skip_failed = True
        # 逐段失败隔离：合并模式下整组会一起回退/跳过
        self.config.merge_enabled = False
        FakeEngine.fail_on = "bad"

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine):
            ok = cli.translate_book(
                str(epub), str(out), "epub", self.config, cli.Glossary(""))

        self.assertTrue(ok)
        with zipfile.ZipFile(out) as zf:
            data = zf.read("c.xhtml").decode("utf-8")
        self.assertIn("译文 good", data)
        self.assertIn("译文 ok", data)
        # "bad" paragraph should keep original since skip_failed=True
        self.assertIn("bad", data)

    def test_skip_failed_does_not_poison_resume_cache(self):
        epub = self.tmp / "retry.epub"
        make_epub(epub, ["good", "bad"])
        self.config.skip_failed = True
        self.config.merge_enabled = False
        FakeEngine.fail_on = "bad"

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine):
            self.assertTrue(cli.translate_book(
                str(epub), str(self.tmp / "first.epub"), "epub",
                self.config, cli.Glossary("")))
            first_calls = FakeEngine.calls
            FakeEngine.fail_on = ""
            self.config.skip_failed = False
            self.assertTrue(cli.translate_book(
                str(epub), str(self.tmp / "second.epub"), "epub",
                self.config, cli.Glossary("")))

        self.assertGreater(FakeEngine.calls, first_calls)
        with zipfile.ZipFile(self.tmp / "second.epub") as zf:
            data = zf.read("c.xhtml").decode("utf-8")
        self.assertIn("译文 bad", data)

    def test_translation_style_passed_to_output(self):
        epub = self.tmp / "style.epub"
        out = self.tmp / "out.epub"
        make_epub(epub, ["hello"])
        self.config.translation_style = "color: red; font-weight: bold"

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine):
            ok = cli.translate_book(
                str(epub), str(out), "epub", self.config, cli.Glossary(""))

        self.assertTrue(ok)
        with zipfile.ZipFile(out) as zf:
            data = zf.read("c.xhtml").decode("utf-8")
        self.assertIn("color: red", data)
        self.assertIn("font-weight: bold", data)

    def test_retranslate_target_file_skips_null_pages(self):
        cache = TranslationCache(str(self.tmp / "retrans_null.db"))
        cache.save_paragraphs([
            ("p0", "m0", "", "para 0", False, None, None),  # null page
            ("p1", "m1", "", "para 1", False, None, "chapter1.html"),
        ])
        cache.update_translation("p0", "trans 0", "engine", "zh")
        cache.update_translation("p1", "trans 1", "engine", "zh")

        cfg = Config(retranslate_file="chapter1.html", retranslate_start="para")
        cli._do_retranslate(cache, cfg)

        paras = cache.get_all()
        p0 = next(p for p in paras if p.id == "p0")
        p1 = next(p for p in paras if p.id == "p1")
        self.assertEqual("trans 0", p0.translation)
        self.assertIsNone(p1.translation)
        cache.close()

    def test_retranslate_file_match_is_not_a_substring(self):
        cache = TranslationCache(str(self.tmp / "retrans_exact.db"))
        cache.save_paragraphs([
            ("p1", "m1", "", "para", False, None, "Text/chapter1.html"),
            ("p11", "m11", "", "para", False, None, "Text/chapter11.html"),
        ])
        cache.update_translation("p1", "one", "engine", "zh")
        cache.update_translation("p11", "eleven", "engine", "zh")

        cli._do_retranslate(
            cache,
            Config(retranslate_file="chapter1.html", retranslate_start="para"),
        )

        translations = {p.id: p.translation for p in cache.get_all()}
        self.assertIsNone(translations["p1"])
        self.assertEqual("eleven", translations["p11"])
        cache.close()

    def test_output_conversion_failure_does_not_overwrite_input_epub(self):
        epub = self.tmp / "book.epub"
        make_epub(epub, ["hello"])
        original = epub.read_bytes()
        self.config.merge_enabled = False

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine), \
                mock.patch.object(
                    cli, "convert", side_effect=cli.ConverterError("broken")
                ):
            ok = cli.translate_book(
                str(epub), str(self.tmp / "book.mobi"), "mobi",
                self.config, cli.Glossary(""),
            )

        self.assertFalse(ok)
        self.assertEqual(original, epub.read_bytes())
        self.assertTrue((self.tmp / "book.translated.epub").exists())

    def test_cleanup_removes_owned_conversion_directory_with_sidecars(self):
        tmp_dir = self.tmp / "et_epub_sidecars"
        tmp_dir.mkdir()
        epub = tmp_dir / "book.epub"
        epub.write_bytes(b"epub")
        (tmp_dir / "metadata.opf").write_text("sidecar", encoding="utf-8")

        cli._cleanup(str(epub))

        self.assertFalse(tmp_dir.exists())

    def test_test_mode_auto_skip_failed(self):
        epub = self.tmp / "test_auto.epub"
        out = self.tmp / "out.epub"
        make_epub(epub, ["one", "two", "three"])

        class Args:
            config = ""
            engine = ""
            source_lang = ""
            target_lang = ""
            concurrency = 0
            no_cache = False
            skip_failed = False
            log_file = ""
            test = True
            test_num = 1
            retranslate_file = ""
            retranslate_start = ""
            retranslate_end = ""

        base = Config(cache_dir=str(self.tmp / "auto-cache"))
        with mock.patch.object(cli, "load_config", return_value=base):
            cfg = cli._apply_overrides(Args())
        self.assertTrue(cfg.test_enabled)
        self.assertTrue(cfg.skip_failed)

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine):
            ok = cli.translate_book(
                str(epub), str(out), "epub", cfg, cli.Glossary(""))

        self.assertTrue(ok)
        with zipfile.ZipFile(out) as zf:
            data = zf.read("c.xhtml").decode("utf-8")
        self.assertIn("译文 one", data)
        self.assertIn("two", data)

    def test_test_mode_does_not_mark_untested_paragraphs_translated(self):
        epub = self.tmp / "test_resume.epub"
        make_epub(epub, ["one", "two", "three"])
        self.config.test_enabled = True
        self.config.test_num = 1
        self.config.skip_failed = True
        self.config.merge_enabled = False

        with mock.patch.object(cli, "get_engine", return_value=FakeEngine):
            self.assertTrue(cli.translate_book(
                str(epub), str(self.tmp / "test.epub"), "epub",
                self.config, cli.Glossary("")))
            first_calls = FakeEngine.calls
            self.config.test_enabled = False
            self.config.skip_failed = False
            self.assertTrue(cli.translate_book(
                str(epub), str(self.tmp / "full.epub"), "epub",
                self.config, cli.Glossary("")))

        self.assertEqual(first_calls + 2, FakeEngine.calls)

    def test_request_interval_applies_between_requests(self):
        starts = []

        class TimedEngine:
            def translate(self, text: str, prompt: str = "") -> str:
                starts.append(time.monotonic())
                return f"译文 {text}"

        cache = TranslationCache(str(self.tmp / "interval.db"))
        paras = [Para(str(i), f"text {i}") for i in range(3)]
        cache.save_paragraphs([
            (p.id, p.id, "", p.original, False, None, None) for p in paras
        ])
        cfg = Config(engine="openai", max_error_count=10, merge_enabled=False)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=1, request_interval=0,
        )
        worker = cli.TranslationWorker(
            TimedEngine(), cache, cfg, cli.Glossary(""))

        asyncio.run(worker.translate_batch(
            paras, concurrency=1, interval=0.03))

        cache.close()
        gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        self.assertEqual(2, len(gaps))
        self.assertTrue(all(gap >= 0.025 for gap in gaps), gaps)

    def test_request_interval_applies_to_merge_fallback_requests(self):
        starts = []

        class InvalidMergeEngine:
            def translate(self, text: str, prompt: str = "") -> str:
                starts.append(time.monotonic())
                if text.strip().startswith("["):
                    return "not json"
                return f"译文 {text}"

        cache = TranslationCache(str(self.tmp / "fallback_rate.db"))
        paras = [Para(str(i), f"text {i}") for i in range(3)]
        cache.save_paragraphs([
            (p.id, p.id, "", p.original, False, None, None) for p in paras
        ])
        cfg = Config(engine="openai", merge_enabled=True, merge_length=100)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=1, request_interval=0,
        )
        worker = cli.TranslationWorker(
            InvalidMergeEngine(), cache, cfg, cli.Glossary(""))

        done, failed = asyncio.run(worker.translate_batch(
            paras, concurrency=3, interval=0.025))
        cache.close()

        self.assertEqual((3, 0), (done, failed))
        self.assertEqual(4, len(starts))
        gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        self.assertTrue(all(gap >= 0.02 for gap in gaps), gaps)

    def test_retry_after_delays_shared_request_limiter(self):
        starts = []

        class RateLimitedOnce:
            calls = 0

            def translate(self, text: str, prompt: str = "") -> str:
                starts.append(time.monotonic())
                type(self).calls += 1
                if type(self).calls == 1:
                    request = httpx.Request("POST", "https://example.test")
                    response = httpx.Response(
                        429, headers={"Retry-After": "0.04"}, request=request)
                    raise httpx.HTTPStatusError(
                        "rate limited", request=request, response=response)
                return f"译文 {text}"

        cache = TranslationCache(str(self.tmp / "retry_after.db"))
        cache.save_paragraphs([("p0", "m0", "", "hello", False, None, None)])
        cfg = Config(engine="openai", merge_enabled=False)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=2, retry_delay=0, request_interval=0)
        worker = cli.TranslationWorker(
            RateLimitedOnce(), cache, cfg, cli.Glossary(""))

        self.assertEqual(
            (1, 0),
            asyncio.run(worker.translate_batch(
                [Para("p0", "hello")], concurrency=1, interval=0)),
        )
        cache.close()

        self.assertGreaterEqual(starts[1] - starts[0], 0.035)

    def test_global_cooldown_applies_when_request_interval_is_zero(self):
        limiter = cli._RateLimiter(0)
        limiter.defer(0.04)
        started = time.monotonic()

        limiter.acquire(__import__("threading").Event())

        self.assertGreaterEqual(time.monotonic() - started, 0.035)

    def test_skip_failed_is_counted_but_not_cached(self):
        cache = TranslationCache(str(self.tmp / "skip_count.db"))
        cache.save_paragraphs([("p0", "m0", "", "hello", False, None, None)])
        cfg = Config(engine="openai", skip_failed=True, merge_enabled=False)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=1, request_interval=0,
        )
        worker = cli.TranslationWorker(
            AlwaysFailEngine(), cache, cfg, cli.Glossary(""))

        self.assertEqual(
            (0, 1),
            asyncio.run(worker.translate_batch(
                [Para("p0", "hello")], concurrency=1, interval=0)),
        )
        self.assertEqual(1, len(cache.get_untranslated()))
        cache.close()

    def test_global_rate_limit_does_not_hold_concurrency_slot(self):
        """限速等待在并发槽外；高并发时启动间隔仍受全局限速约束。"""
        starts = []
        active = 0
        max_active = 0
        lock = __import__("threading").Lock()

        class SlowEngine:
            def translate(self, text: str, prompt: str = "") -> str:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    starts.append(time.monotonic())
                try:
                    time.sleep(0.05)
                    return f"译文 {text}"
                finally:
                    with lock:
                        active -= 1

        cache = TranslationCache(str(self.tmp / "rate.db"))
        paras = [Para(str(i), f"text {i}") for i in range(4)]
        cache.save_paragraphs([
            (p.id, p.id, "", p.original, False, None, None) for p in paras
        ])
        cfg = Config(engine="openai", max_error_count=10, merge_enabled=False)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=1, request_interval=0,
        )
        worker = cli.TranslationWorker(
            SlowEngine(), cache, cfg, cli.Glossary(""))

        t0 = time.monotonic()
        asyncio.run(worker.translate_batch(
            paras, concurrency=4, interval=0.04))
        elapsed = time.monotonic() - t0
        cache.close()

        starts.sort()
        gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        self.assertEqual(3, len(gaps))
        # 全局限速：相邻启动约 >= interval
        self.assertTrue(all(gap >= 0.03 for gap in gaps), gaps)
        # 若 interval 占着并发槽串行睡，4 次会更接近 4*(0.04+0.05)；
        # 限速在槽外时，槽可重叠执行，总时长应明显小于串行上界。
        self.assertLess(elapsed, 0.35)
        self.assertGreaterEqual(max_active, 2)

    def test_empty_translation_uses_short_retry_budget(self):
        class EmptyEngine:
            calls = 0

            def translate(self, text: str, prompt: str = "") -> str:
                type(self).calls += 1
                raise RuntimeError('API 返回空译文: {"choices":[{"message":{}}]}')

        cache = TranslationCache(str(self.tmp / "empty.db"))
        cache.save_paragraphs([("p0", "m0", "", "hello", False, None, None)])
        cfg = Config(engine="openai", max_error_count=10, merge_enabled=False)
        cfg.engines["openai"] = EngineConfig(
            api_key="x", max_retries=5, retry_delay=0, request_interval=0,
        )
        EmptyEngine.calls = 0
        worker = cli.TranslationWorker(
            EmptyEngine(), cache, cfg, cli.Glossary(""))

        done, failed = asyncio.run(worker.translate_batch(
            [Para("p0", "hello")], concurrency=1, interval=0))

        cache.close()
        self.assertEqual((0, 1), (done, failed))
        # 空译文最多 2 次，而不是 max_retries=5
        self.assertEqual(2, EmptyEngine.calls)

    def test_classify_translation_error_kinds(self):
        self.assertEqual(
            "permanent",
            cli._classify_translation_error(RuntimeError("401 Unauthorized")),
        )
        self.assertEqual(
            "rate_limit",
            cli._classify_translation_error(RuntimeError("429 Too Many Requests")),
        )
        self.assertEqual(
            "empty",
            cli._classify_translation_error(RuntimeError("API 返回空译文: {}")),
        )
        self.assertEqual(
            "transient",
            cli._classify_translation_error(RuntimeError("connection reset")),
        )
        request = httpx.Request("POST", "https://example.test")
        response = httpx.Response(400, request=request)
        error = httpx.HTTPStatusError("bad request", request=request, response=response)
        self.assertEqual("permanent", cli._classify_translation_error(error))
        self.assertEqual(
            "permanent",
            cli._classify_translation_error(
                ValueError("OpenAI extra 不能覆盖保留字段: ['messages']")
            ),
        )

    def test_merged_result_rejects_non_string_translation(self):
        worker = cli.TranslationWorker(
            FakeEngine(None, None, None),
            TranslationCache(":memory:", persistence=False),
            self.config,
            cli.Glossary(""),
        )
        with self.assertRaises(ValueError):
            worker._parse_merged_result('[{"id":"0","text":null}]', {"0"})
        worker.cache.close()

    def test_cache_key_tracks_request_settings_but_not_output_style(self):
        source = self.tmp / "source.bin"
        source.write_bytes(b"book")
        element = mock.Mock(uid="u", page_href="p", original="text")
        cfg = Config(engine="openai", translation_style="red")
        cfg.engines["openai"] = EngineConfig(
            api_key="x", temperature=0.1, top_p=0.8, extra={"seed": 1},
        )

        key1 = cli._build_cache_key(str(source), [element], cfg, cli.Glossary(""))
        cfg.translation_style = "blue"
        self.assertEqual(
            key1,
            cli._build_cache_key(str(source), [element], cfg, cli.Glossary("")),
        )
        cfg.engines["openai"].temperature = 0.2
        self.assertNotEqual(
            key1,
            cli._build_cache_key(str(source), [element], cfg, cli.Glossary("")),
        )


if __name__ == "__main__":
    unittest.main()
