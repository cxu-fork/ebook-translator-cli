import shutil
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree
from pathlib import Path
from types import SimpleNamespace
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

    def test_repack_escapes_opf_path_in_container_xml(self):
        oebps = self.tmp / "OEBPS & Text"
        oebps.mkdir()
        (self.tmp / "mimetype").write_text(
            "application/epub+zip", encoding="ascii")
        (oebps / "content.opf").write_text("<package/>", encoding="utf-8")

        epub_path = converter._repack_epub_from_dir(str(self.tmp))

        with zipfile.ZipFile(epub_path) as zf:
            container = zf.read("META-INF/container.xml")
        root = ElementTree.fromstring(container)
        rootfile = next(iter(root.iter(
            "{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
        )))
        self.assertEqual("OEBPS & Text/content.opf", rootfile.attrib["full-path"])

    def test_repack_does_not_include_output_epub_inside_itself(self):
        mobi7 = self.tmp / "mobi7"
        mobi7.mkdir()
        (self.tmp / "HDImages").mkdir()
        (mobi7 / "content.opf").write_text("<package/>", encoding="utf-8")
        (mobi7 / "book.html").write_text("<html/>", encoding="utf-8")

        epub_path = converter._repack_epub_from_dir(str(self.tmp))

        with zipfile.ZipFile(epub_path) as zf:
            self.assertNotIn("output.epub", zf.namelist())

    def test_ebook_convert_failure_preserves_existing_output(self):
        output = self.tmp / "book.azw3"
        output.write_bytes(b"GOOD")

        def fail_conversion(cmd, **_kwargs):
            Path(cmd[2]).write_bytes(b"PARTIAL")
            return SimpleNamespace(returncode=1, stderr="failed")

        with mock.patch.object(
            converter, "find_ebook_convert", return_value="ebook-convert"
        ), mock.patch.object(
            converter.subprocess, "run", side_effect=fail_conversion
        ):
            with self.assertRaises(converter.ConverterError):
                converter._ebook_convert(
                    "book.epub", str(output), "azw3")

        self.assertEqual(b"GOOD", output.read_bytes())
        self.assertEqual([output], list(self.tmp.iterdir()))

    def test_kindle_copy_failure_preserves_existing_output(self):
        output = self.tmp / "book.epub"
        output.write_bytes(b"GOOD")
        generated = self.tmp / "generated.epub"
        generated.write_bytes(b"EPUB")

        def broken_copy(_source, target):
            Path(target).write_bytes(b"PARTIAL")
            raise OSError("disk full")

        with mock.patch(
            "ebook_translator.vendor.kindleunpack.kindleunpack.unpackBook"
        ), mock.patch.object(
            converter, "_repack_epub_from_dir", return_value=str(generated)
        ), mock.patch.object(
            converter.shutil, "copy2", side_effect=broken_copy
        ), self.assertRaises(OSError):
            converter._kindleunpack_to_epub("book.mobi", str(output))

        self.assertEqual(b"GOOD", output.read_bytes())

    def test_ebook_convert_atomically_replaces_output_on_success(self):
        output = self.tmp / "book.epub"
        output.write_bytes(b"OLD")

        def successful_conversion(cmd, **_kwargs):
            Path(cmd[2]).write_bytes(b"NEW")
            return SimpleNamespace(returncode=0, stderr="")

        with mock.patch.object(
            converter, "find_ebook_convert", return_value="ebook-convert"
        ), mock.patch.object(
            converter.subprocess, "run", side_effect=successful_conversion
        ):
            result = converter._ebook_convert(
                "book.mobi", str(output), "epub")

        self.assertEqual(str(output), result)
        self.assertEqual(b"NEW", output.read_bytes())
        self.assertEqual([output], list(self.tmp.iterdir()))

    def test_invalid_custom_ebook_convert_path_does_not_fall_back(self):
        missing = str(self.tmp / "missing-ebook-convert")

        with mock.patch.object(converter.shutil, "which") as which:
            with self.assertRaisesRegex(
                converter.ConverterError, "配置的 ebook-convert 路径无效"
            ):
                converter.find_ebook_convert(missing)

        which.assert_not_called()

    def test_custom_ebook_convert_command_name_uses_path_lookup(self):
        with mock.patch.object(
            converter.shutil, "which", return_value="/usr/local/bin/ebook-convert"
        ) as which:
            result = converter.find_ebook_convert("ebook-convert")

        self.assertEqual("/usr/local/bin/ebook-convert", result)
        which.assert_called_once_with("ebook-convert")

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
