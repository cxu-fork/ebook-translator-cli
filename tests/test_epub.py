import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

from ebook_translator.epub import (
    _extract_text, _resolve_href, extract_from_epub, write_translated_epub,
    _inject_translation, _is_non_translatable, _localname, _parse_content_page,
    _validate_archive,
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

    def test_percent_encoded_container_rootfile_resolves_zip_member(self):
        epub = self.tmp / "container_href.epub"
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr(
                "META-INF/container.xml",
                "<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
                "<rootfiles><rootfile full-path='OEBPS/content%20file.opf' "
                "media-type='application/oebps-package+xml'/></rootfiles></container>",
            )
            zf.writestr(
                "OEBPS/content file.opf",
                "<package xmlns='http://www.idpf.org/2007/opf'>"
                "<metadata/><manifest><item id='c' href='chapter.xhtml' "
                "media-type='application/xhtml+xml'/></manifest>"
                "<spine><itemref idref='c'/></spine></package>",
            )
            zf.writestr(
                "OEBPS/chapter.xhtml",
                "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
                "<p>Hello</p></body></html>",
            )

        elements, _ = extract_from_epub(str(epub))

        self.assertEqual(["Hello"], [el.original for el in elements])

    def test_opf_without_namespace_is_supported(self):
        epub = self.tmp / "no_namespace.epub"
        with zipfile.ZipFile(epub, "w") as zf:
            zf.writestr("mimetype", "application/epub+zip")
            zf.writestr(
                "META-INF/container.xml",
                "<container><rootfiles><rootfile full-path='content.opf'/>"
                "</rootfiles></container>",
            )
            zf.writestr(
                "content.opf",
                "<package><metadata><title>T</title></metadata>"
                "<manifest><item id='c' href='c.xhtml' "
                "media-type='application/xhtml+xml'/></manifest>"
                "<spine><itemref idref='c'/></spine></package>",
            )
            zf.writestr("c.xhtml", "<html><body><p>Hello</p></body></html>")

        elements, meta = extract_from_epub(str(epub))

        self.assertEqual("T", meta["title"])
        self.assertEqual(["Hello"], [element.original for element in elements])

    def test_missing_spine_document_is_not_silently_skipped(self):
        epub = self.tmp / "missing.epub"
        make_epub(epub, "<html><body><p>Hello</p></body></html>",
                  href="missing.xhtml", zip_name="other.xhtml")

        with self.assertRaisesRegex(ValueError, "正文文件不存在"):
            extract_from_epub(str(epub))

    def test_xml_external_entities_are_not_expanded(self):
        tree, _ = _parse_content_page(
            b"<!DOCTYPE html [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]>"
            b"<html><body><p>safe &xxe;</p></body></html>"
        )

        self.assertEqual("safe", _extract_text(tree.find(".//p")))

    def test_file_filters_are_case_insensitive(self):
        epub = self.tmp / "case.epub"
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p>Hello</p></body></html>",
            href="Text/Chapter.xhtml",
        )

        included, _ = extract_from_epub(
            str(epub), only_files="chapter.XHTML")
        excluded, _ = extract_from_epub(
            str(epub), exclude_files="CHAPTER.xhtml")

        self.assertEqual(["Hello"], [el.original for el in included])
        self.assertEqual([], excluded)

    def test_resolve_href_always_uses_zip_posix_separator(self):
        self.assertEqual(
            "OEBPS/Text/chapter 1.xhtml",
            _resolve_href("OEBPS/content.opf", "Text/chapter%201.xhtml"),
        )
        self.assertNotIn("\\", _resolve_href(
            "OEBPS\\content.opf", "Text\\chapter.xhtml"))
        self.assertEqual(
            "OEBPS/Text/chapter.xhtml",
            _resolve_href(
                "OEBPS/content.opf", "Text/chapter.xhtml?edition=1#part"),
        )

    def test_extract_text_does_not_insert_spaces_inside_inline_text(self):
        el = etree.fromstring("<p>He<em>l</em>lo &amp; bye</p>")

        self.assertEqual("Hello & bye", _extract_text(el))

    def test_comments_and_pis_do_not_break_extraction(self):
        """lxml Comment/PI nodes have non-string .tag; must not TypeError."""
        epub = self.tmp / "comment.epub"
        make_epub(
            epub,
            "<?xml version='1.0'?>"
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<!-- page break -->"
            "<?somepi keep me?>"
            "<p>Hello</p>"
            "<!-- another -->"
            "<p>World</p>"
            "</body></html>",
        )

        elements, _meta = extract_from_epub(str(epub))
        self.assertEqual(["Hello", "World"], [e.original for e in elements])

        out = self.tmp / "out.epub"
        translations = {elements[0].uid: "你好", elements[1].uid: "世界"}
        injected = write_translated_epub(
            str(epub), str(out), translations, expected_count=2)
        self.assertEqual(2, injected)

    def test_localname_handles_non_string_tag(self):
        self.assertEqual("", _localname(None))
        self.assertEqual("", _localname(object()))
        self.assertEqual("p", _localname("{http://www.w3.org/1999/xhtml}p"))
        self.assertEqual("div", _localname("div"))


    def test_non_translatable_detects_urls_and_numbers(self):
        self.assertTrue(_is_non_translatable("https://example.com"))
        self.assertTrue(_is_non_translatable("http://foo.bar/path"))
        self.assertTrue(_is_non_translatable("123,456.78"))
        self.assertTrue(_is_non_translatable("99%"))
        self.assertTrue(_is_non_translatable("ISBN: 978-0-13-468599-1"))
        self.assertTrue(_is_non_translatable("Figure 3"))
        self.assertTrue(_is_non_translatable("Table 1"))
        self.assertTrue(_is_non_translatable(""))
        self.assertFalse(_is_non_translatable("Hello world"))
        self.assertFalse(_is_non_translatable("The quick brown fox"))
        self.assertFalse(_is_non_translatable(
            "Read more at https://example.com today"))
        self.assertFalse(_is_non_translatable(
            "Figure 3: System architecture overview"))
        self.assertFalse(_is_non_translatable(
            "The ISBN: 978-0-13-468599-1 identifies this edition"))

    def test_translate_tags_restricts_extraction(self):
        epub = self.tmp / "tags.epub"
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p>paragraph</p><h1>heading</h1><blockquote>quote</blockquote>"
            "</body></html>",
        )
        # Only extract <p> tags
        elements, _ = extract_from_epub(str(epub), translate_tags="p")
        self.assertEqual(1, len(elements))
        self.assertEqual("paragraph", elements[0].original)

    def test_exclude_translate_tags_skips_elements_with_children(self):
        epub = self.tmp / "exclude.epub"
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p>normal paragraph</p>"
            "<p>has <code>code</code> inside</p>"
            "<p>another normal</p>"
            "</body></html>",
        )
        elements, _ = extract_from_epub(str(epub), exclude_translate_tags="code")
        originals = [e.original for e in elements]
        self.assertEqual(["normal paragraph", "another normal"], originals)

    def test_excluded_tag_itself_and_subtree_are_not_extracted(self):
        epub = self.tmp / "exclude_self.epub"
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<pre>print hello</pre>"
            "<code><span>code words</span></code>"
            "<p>normal</p></body></html>",
        )

        elements, _ = extract_from_epub(
            str(epub), exclude_translate_tags="pre,code")

        self.assertEqual(["normal"], [el.original for el in elements])

    def test_injection_stays_inside_structural_element(self):
        root = etree.fromstring("<ul><li>Hello</li></ul>")

        _inject_translation(root[0], "你好", "below")

        self.assertEqual(["li"], [_localname(child.tag) for child in root])
        self.assertEqual("span", _localname(root[0][0].tag))
        self.assertEqual("你好", _extract_text(root[0][0]))
        self.assertIn("display: block", root[0][0].get("style"))

    def test_only_position_preserves_element_attributes_and_tail(self):
        root = etree.fromstring(
            "<body><p id='anchor' class='original'><em>Hello</em></p>TAIL</body>")
        paragraph = root[0]

        _inject_translation(paragraph, "你好", "only", style="color: red")

        self.assertIs(paragraph, root[0])
        self.assertEqual("p", _localname(paragraph.tag))
        self.assertEqual("anchor", paragraph.get("id"))
        self.assertIn("original", paragraph.get("class").split())
        self.assertIn("et-translation", paragraph.get("class").split())
        self.assertEqual("TAIL", paragraph.tail)
        self.assertEqual("你好", _extract_text(paragraph))
        self.assertEqual(0, len(paragraph))
        self.assertIn("color: red", paragraph.get("style"))

    def test_write_with_style_attribute(self):
        epub = self.tmp / "style.epub"
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p>Hello</p></body></html>",
        )
        elements, _ = extract_from_epub(str(epub))
        self.assertEqual(1, len(elements))

        out = self.tmp / "out.epub"
        translations = {elements[0].uid: "你好"}
        write_translated_epub(
            str(epub), str(out), translations,
            expected_count=1, style="color: blue; font-size: 12px",
        )
        with zipfile.ZipFile(out) as zf:
            data = zf.read("c.xhtml").decode("utf-8")
        self.assertIn("color: blue", data)
        self.assertIn("et-translation", data)

    def test_non_translatable_elements_marked_ignored(self):
        epub = self.tmp / "ignored.epub"
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p>https://example.com</p>"
            "<p>Real text here</p>"
            "<p>Figure 1</p>"
            "</body></html>",
        )
        elements, _ = extract_from_epub(str(epub))
        # Non-translatable should be excluded (not extracted at all)
        originals = [e.original for e in elements]
        self.assertIn("Real text here", originals)
        self.assertNotIn("https://example.com", originals)
        self.assertNotIn("Figure 1", originals)

    def test_non_translatable_injection_alignment(self):
        epub = self.tmp / "alignment.epub"
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p>First paragraph</p>"
            "<p>https://example.com</p>"
            "<p>Second paragraph</p>"
            "</body></html>",
        )
        elements, _ = extract_from_epub(str(epub))
        self.assertEqual(2, len(elements))
        self.assertEqual("First paragraph", elements[0].original)
        self.assertEqual("Second paragraph", elements[1].original)

        out = self.tmp / "out.epub"
        translations = {
            elements[0].uid: "第一段",
            elements[1].uid: "第二段",
        }
        write_translated_epub(str(epub), str(out), translations, expected_count=2)

        with zipfile.ZipFile(out) as zf:
            data = zf.read("c.xhtml").decode("utf-8")
        
        tree = etree.fromstring(data.encode("utf-8"))
        body = tree.find(".//body")
        if body is None:
            body = tree.find(".//{http://www.w3.org/1999/xhtml}body")
        children = list(body)

        self.assertEqual(3, len(children))
        self.assertEqual("p", _localname(children[0].tag))
        self.assertEqual("第一段", _extract_text(children[0][-1]))

        self.assertEqual("p", _localname(children[1].tag))
        self.assertEqual("https://example.com", _extract_text(children[1]))

        self.assertEqual("p", _localname(children[2].tag))
        self.assertEqual("第二段", _extract_text(children[2][-1]))

    def test_injection_count_mismatch_removes_output_and_raises(self):
        epub = self.tmp / "mismatch.epub"
        out = self.tmp / "out.epub"
        out.write_bytes(b"existing")
        make_epub(
            epub,
            "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
            "<p>Hello</p></body></html>",
        )

        with self.assertRaisesRegex(ValueError, "译文注入数量不匹配"):
            write_translated_epub(
                str(epub), str(out), {"missing": "你好"}, expected_count=1)

        self.assertEqual(b"existing", out.read_bytes())

    def test_archive_size_limits_reject_zip_bombs(self):
        archive = SimpleNamespace(infolist=lambda: [SimpleNamespace(
            filename="bomb.xhtml",
            file_size=257 * 1024 * 1024,
            compress_size=1024,
        )])

        with self.assertRaisesRegex(ValueError, "成员过大"):
            _validate_archive(archive)


if __name__ == "__main__":
    unittest.main()
