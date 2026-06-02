import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree

from ebook_translator.epub import (
    _extract_text, _resolve_href, extract_from_epub, write_translated_epub,
)


def make_epub(path: Path, body: str, href: str = "c.xhtml",
              zip_name: str | None = None, media_type: str = "application/xhtml+xml"):
    zip_name = zip_name or href
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
            f"<manifest><item id='c' href='{href}' media-type='{media_type}'/>"
            "</manifest><spine><itemref idref='c'/></spine></package>",
        )
        zf.writestr(zip_name, body)


class EpubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="et_epub_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recovered_html_is_injected(self):
        epub = self.tmp / "bad.epub"
        make_epub(
            epub,
            "<html><body><p class=noquote>Hello</p><p>World</p></body></html>",
            href="book.html",
            media_type="text/html",
        )

        elements, _meta = extract_from_epub(str(epub))
        self.assertEqual(["Hello", "World"], [e.original for e in elements])

        out = self.tmp / "out.epub"
        translations = {elements[0].uid: "你好", elements[1].uid: "世界"}
        injected = write_translated_epub(
            str(epub), str(out), translations, expected_count=2)

        self.assertEqual(2, injected)
        with zipfile.ZipFile(out) as zf:
            data = zf.read("book.html")
        self.assertEqual(2, data.count(b"et-translation"))

    def test_nested_blocks_are_separate_translation_units(self):
        epub = self.tmp / "nested.epub"
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p>A</p><div><p>B</p><p>C</p></div></body></html>",
        )

        elements, _meta = extract_from_epub(str(epub))
        self.assertEqual(["A", "B", "C"], [e.original for e in elements])

        out = self.tmp / "out.epub"
        translations = {el.uid: f"T{i}" for i, el in enumerate(elements)}
        injected = write_translated_epub(
            str(epub), str(out), translations, expected_count=3)

        self.assertEqual(3, injected)
        with zipfile.ZipFile(out) as zf:
            data = zf.read("c.xhtml").decode("utf-8")
        self.assertIn("T0", data)
        self.assertIn("T1", data)
        self.assertIn("T2", data)

    def test_percent_encoded_href_resolves_zip_member(self):
        epub = self.tmp / "href.epub"
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p>Hello</p></body></html>",
            href="Text/chapter%201.xhtml",
            zip_name="Text/chapter 1.xhtml",
        )

        elements, _meta = extract_from_epub(str(epub))
        self.assertEqual(1, len(elements))
        self.assertEqual("Hello", elements[0].original)

    def test_resolve_href_always_uses_zip_posix_separator(self):
        self.assertEqual(
            "OEBPS/Text/chapter 1.xhtml",
            _resolve_href("OEBPS/content.opf", "Text/chapter%201.xhtml"),
        )
        self.assertNotIn("\\", _resolve_href(
            "OEBPS\\content.opf", "Text\\chapter.xhtml"))

    def test_extract_text_does_not_insert_spaces_inside_inline_text(self):
        el = etree.fromstring("<p>He<em>l</em>lo &amp; bye</p>")

        self.assertEqual("Hello & bye", _extract_text(el))


if __name__ == "__main__":
    unittest.main()
