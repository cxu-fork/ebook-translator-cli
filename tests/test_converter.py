import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from ebook_translator import converter


class ConverterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="et_converter_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_repack_epub_container_points_to_nested_opf(self):
        root = self.tmp / "Book"
        oebps = root / "OEBPS"
        oebps.mkdir(parents=True)
        (oebps / "content.opf").write_text("<package/>", encoding="utf-8")
        (oebps / "chapter.xhtml").write_text("<html/>", encoding="utf-8")

        epub_path = converter._repack_epub_from_dir(str(self.tmp))

        self.assertIsNotNone(epub_path)
        with zipfile.ZipFile(epub_path) as zf:
            self.assertIn("OEBPS/content.opf", zf.namelist())
            container = zf.read("META-INF/container.xml").decode("utf-8")
        self.assertIn('full-path="OEBPS/content.opf"', container)

    def test_kindleunpack_fallback_error_includes_original_failure(self):
        with mock.patch.object(
            converter, "_kindleunpack_to_epub",
            side_effect=RuntimeError("broken mobi"),
        ), mock.patch.object(
            converter, "_ebook_convert",
            side_effect=converter.ConverterError("ebook-convert missing"),
        ):
            with self.assertRaises(converter.ConverterError) as ctx:
                converter.convert("book.mobi", "out.epub", "epub")

        message = str(ctx.exception)
        self.assertIn("KindleUnpack 错误: broken mobi", message)
        self.assertIn("ebook-convert 错误: ebook-convert missing", message)


if __name__ == "__main__":
    unittest.main()
