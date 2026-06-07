import json
import shutil
import tempfile
import unittest
import zipfile
import asyncio
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

    def test_glossary_uses_longest_terms_first_and_restores_spaced_tokens(self):
        path = self.tmp / "glossary.txt"
        path.write_text(
            "AI\n人工智能\n\nAI model\nAI模型\n",
            encoding="utf-8",
        )
        glossary = cli.Glossary(str(path))

        protected = glossary.apply("AI model beats AI")
        self.assertIn("{{id_000000}}", protected)
        self.assertIn("{{id_000001}}", protected)
        self.assertNotIn("{{id_000001}} model", protected)
        self.assertEqual(
            "AI模型 beats 人工智能",
            glossary.restore("{{ id_000000 }} beats {{ id_000001 }}"),
        )

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
        cfg = Config(engine="openai", max_error_count=1)
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
        cli._do_retranslate(cache, [], cfg)

        paras = cache.get_all()
        p0 = next(p for p in paras if p.id == "p0")
        p1 = next(p for p in paras if p.id == "p1")
        self.assertEqual("trans 0", p0.translation)
        self.assertIsNone(p1.translation)
        cache.close()

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


if __name__ == "__main__":
    unittest.main()
