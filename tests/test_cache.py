import shutil
import tempfile
import unittest
from pathlib import Path

from ebook_translator.cache import TranslationCache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="et_cache_test_"))
        self.cache = TranslationCache(str(self.tmp / "cache.db"))

    def tearDown(self):
        self.cache.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_queries_preserve_document_insertion_order(self):
        self.cache.save_paragraphs([
            ("p2", "m2", "", "second", False, None, None),
            ("p1", "m1", "", "first", False, None, None),
            ("p3", "m3", "", "third", True, None, None),
        ])

        self.assertEqual(["p2", "p1"], [p.id for p in self.cache.get_all()])
        self.assertEqual(
            ["p2", "p1"], [p.id for p in self.cache.get_untranslated()])
        self.assertEqual(
            ["p2", "p1", "p3"],
            [p.id for p in self.cache.get_all_with_ignored()],
        )

    def test_bulk_translation_update_uses_one_transaction(self):
        self.cache.save_paragraphs([
            ("p1", "m1", "", "first", False, None, None),
            ("p2", "m2", "", "second", False, None, None),
        ])
        statements = []
        self.cache.conn.set_trace_callback(statements.append)

        self.cache.update_translations([
            ("p1", "一", "engine", "zh"),
            ("p2", "二", "engine", "zh"),
        ])

        self.assertEqual(["一", "二"], [p.translation for p in self.cache.get_all()])
        self.assertEqual(1, sum(sql == "BEGIN " for sql in statements))
        self.assertEqual(1, sum(sql == "COMMIT" for sql in statements))


if __name__ == "__main__":
    unittest.main()
