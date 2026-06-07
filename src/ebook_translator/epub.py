"""EPUB extraction, translation injection, and repackaging.

This module handles:
  - Reading an EPUB (zip container)
  - Parsing OPF manifest/spine/metadata
  - Extracting translatable elements from XHTML content documents
  - Injecting translations back into the XHTML DOM
  - Writing the translated EPUB back out
"""
import posixpath
import re
import zipfile
from dataclasses import dataclass
from urllib.parse import unquote, urldefrag
from typing import Any

from lxml import etree

# XHTML namespace
NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# Block-level elements used as text boundaries. A node becomes a translation
# unit only when it has no block-level descendants, so containers do not swallow
# whole chapters.
BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "th", "td",
    "caption", "blockquote", "dt", "dd", "figcaption",
    "summary", "div", "article", "section", "header", "footer",
    "aside", "nav", "main",
}

# Elements to skip entirely (children not visited)
SKIP_TAGS = {"script", "style", "svg", "math", "img", "video", "audio", "object", "embed"}


from .cache import md5 as _md5


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class ExtractedElement:
    """One translatable unit extracted from the XHTML DOM."""
    uid: str               # stable id: md5(page_href + ":" + page_local_index)
    raw_html: str          # serialised XHTML of the element
    original: str          # plain-text content
    ignored: bool = False
    page_href: str = ""


# ---------------------------------------------------------------------------
# EPUB I/O helpers
# ---------------------------------------------------------------------------
def _read_container(zf: zipfile.ZipFile) -> str:
    tree = etree.parse(zf.open("META-INF/container.xml"))
    rootfiles = tree.findall(f".//{{{CONTAINER_NS}}}rootfile")
    if not rootfiles:
        raise ValueError("No rootfile found in container.xml")
    return rootfiles[0].attrib["full-path"]


def _parse_opf(zf: zipfile.ZipFile, opf_path: str):
    tree = etree.parse(zf.open(opf_path))
    root = tree.getroot()

    manifest: dict[str, str] = {}
    for item in root.iter(f"{{{NS_OPF}}}item"):
        manifest[item.attrib.get("href", "")] = item.attrib.get("media-type", "")

    id_to_href: dict[str, str] = {}
    for item in root.iter(f"{{{NS_OPF}}}item"):
        id_to_href[item.attrib.get("id", "")] = item.attrib.get("href", "")

    spine_hrefs: list[str] = []
    for itemref in root.iter(f"{{{NS_OPF}}}itemref"):
        sid = itemref.attrib.get("idref", "")
        if sid in id_to_href:
            spine_hrefs.append(id_to_href[sid])

    metadata_el = root.find(f"{{{NS_OPF}}}metadata")
    return manifest, spine_hrefs, metadata_el


def _resolve_href(base: str, href: str) -> str:
    base_dir = posixpath.dirname(base.replace("\\", "/"))
    href = urldefrag(href)[0]
    href = unquote(href).replace("\\", "/")
    resolved = posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else href
    return "" if resolved == "." else resolved


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
def _localname(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _extract_text(el: etree._Element) -> str:
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_extract_text(child))
        if child.tail:
            parts.append(child.tail)
    raw = "".join(parts)
    return re.sub(r"\s+", " ", raw).strip()


def _should_skip(el: etree._Element) -> bool:
    return _localname(el.tag) in SKIP_TAGS


def _is_inline_only(el: etree._Element, block_tags: set[str] | None = None) -> bool:
    tags = block_tags if block_tags is not None else BLOCK_TAGS
    for desc in el.iter():
        if desc is el:
            continue
        if _localname(desc.tag) in tags:
            return False
    return True



# ---------------------------------------------------------------------------
# Non-translatable text detection
# ---------------------------------------------------------------------------
_RE_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
_RE_ISBN = re.compile(r"ISBN[:\s]*\d", re.IGNORECASE)
_RE_FIGURE = re.compile(
    r"^(Figure|Fig\.?|Table|Listing|Source)[\s:]", re.IGNORECASE)


def _is_non_translatable(text: str) -> bool:
    """Detect URLs, ISBNs, pure numbers, figure/listing references."""
    stripped = text.strip()
    if not stripped:
        return True
    if _RE_URL.search(stripped):
        return True
    if _RE_ISBN.search(stripped):
        return True
    # Pure number (including formatted: 1,234.56, -3.14e2, 99%)
    if re.fullmatch(r"[\d,.\-+eE\s%]+", stripped):
        return True
    if _RE_FIGURE.match(stripped):
        return True
    return False


def _parse_tag_set(s: str) -> set[str]:
    """Parse a comma-separated tag string into a set of lowercased names."""
    if not s:
        return set()
    return {t.strip().lower() for t in s.split(",") if t.strip()}


def _has_excluded_child(el: etree._Element, exclude_tags: set[str]) -> bool:
    """Return True if el has any descendant whose local tag is in exclude_tags."""
    for desc in el.iter():
        if desc is not el and _localname(desc.tag) in exclude_tags:
            return True
    return False


def _parse_content_page(data: bytes) -> tuple[etree._Element, bool]:
    """Parse a content document.

    Returns ``(tree, recovered)`` where ``recovered`` means the XML parser failed
    and the HTML recovery parser was used. KindleUnpack MOBI7 output often lands
    in this bucket; extraction and injection must handle it consistently.
    """
    try:
        return etree.fromstring(data), False
    except etree.XMLSyntaxError:
        parser = etree.HTMLParser(recover=True)
        return etree.fromstring(data, parser=parser), True


def _find_body(tree: etree._Element) -> etree._Element | None:
    body = tree.find(f".//{{{NS_XHTML}}}body")
    if body is None:
        body = tree.find(".//body")
    return body


def _extract_elements(root: etree._Element, page_href: str,
                      translate_tags: set[str] | None = None,
                      exclude_tags: set[str] | None = None) -> list[ExtractedElement]:
    """Walk the DOM and collect block-level translatable elements.
    Index is page-local (0-based per page) so uid is stable regardless of
    extraction order across pages.
    """
    results: list[ExtractedElement] = []
    idx = 0
    effective_blocks = translate_tags if translate_tags else None
    effective_exclude = exclude_tags or set()

    def walk(parent: etree._Element):
        nonlocal idx
        for child in list(parent):
            tag = _localname(child.tag)
            if _should_skip(child):
                continue
            text = _extract_text(child)
            if not text:
                continue
            # When translate_tags is set, only extract matching tags.
            if effective_blocks and tag not in effective_blocks:
                walk(child)
                continue
            if (_is_inline_only(child, effective_blocks)
                    and not _has_excluded_child(child, effective_exclude)
                    and not _is_non_translatable(text)):
                raw = etree.tostring(child, encoding="unicode", with_tail=False)
                uid = _md5(f"{page_href}:{idx}")
                results.append(ExtractedElement(
                    uid=uid, raw_html=raw, original=text, page_href=page_href,
                ))
                idx += 1
            else:
                walk(child)

    walk(root)
    return results


# ---------------------------------------------------------------------------
# Public API: extraction
# ---------------------------------------------------------------------------
def extract_from_epub(epub_path: str,
                      only_files: str = "",
                      exclude_files: str = "",
                      translate_tags: str = "",
                      exclude_translate_tags: str = "") -> tuple[list[ExtractedElement], dict]:
    elements: list[ExtractedElement] = []
    meta: dict[str, Any] = {"title": "", "spine_hrefs": []}

    with zipfile.ZipFile(epub_path, "r") as zf:
        opf_path = _read_container(zf)
        manifest, spine_hrefs, metadata_el = _parse_opf(zf, opf_path)

        if metadata_el is not None:
            for child in metadata_el:
                if _localname(child.tag) == "title" and child.text:
                    meta["title"] = child.text.strip()
                    break

        meta["spine_hrefs"] = spine_hrefs

        _only = _parse_tag_set(only_files)
        _exc_files = _parse_tag_set(exclude_files)
        _t_tags = _parse_tag_set(translate_tags)
        _e_tags = _parse_tag_set(exclude_translate_tags)

        for href in spine_hrefs:
            # only_files / exclude_files filtering (filename-based)
            base_name = href.rsplit("/", 1)[-1] if "/" in href else href
            if _only and base_name not in _only and href not in _only:
                continue
            if _exc_files and (base_name in _exc_files or href in _exc_files):
                continue
            resolved = _resolve_href(opf_path, href)
            if resolved not in zf.namelist():
                continue
            media_type = manifest.get(href, "")
            if "html" not in media_type and "xml" not in media_type:
                continue
            data = zf.read(resolved)
            try:
                tree, _recovered = _parse_content_page(data)
            except Exception:
                continue
            body = _find_body(tree)
            if body is None:
                continue
            elements.extend(_extract_elements(
                body, href, translate_tags=_t_tags, exclude_tags=_e_tags))

    return elements, meta


def build_cache_rows(elements: list[ExtractedElement]) -> list[tuple]:
    rows = []
    for i, el in enumerate(elements):
        rows.append((
            el.uid, _md5(f"{i}{el.original}"), el.raw_html, el.original,
            el.ignored, None, el.page_href,
        ))
    return rows


# ---------------------------------------------------------------------------
# Translation injection
# ---------------------------------------------------------------------------
def _make_translation_fragment(el: etree._Element, translation: str,
                               style: str = "") -> etree._Element:
    tag = f"{{{NS_XHTML}}}div" if el.tag.startswith("{") else "div"
    frag = etree.Element(tag)
    frag.set("class", "et-translation")
    if style:
        frag.set("style", style)
    frag.text = translation
    return frag


def _inject_translation(el: etree._Element, translation: str,
                        position: str = "below", style: str = ""):
    frag = _make_translation_fragment(el, translation, style=style)

    if position == "only":
        parent = el.getparent()
        if parent is not None:
            idx = list(parent).index(el)
            parent.remove(el)
            parent.insert(idx, frag)
    elif position == "above":
        el.addprevious(frag)
    else:
        el.addnext(frag)


def _inject_recursive(parent: etree._Element, page_href: str,
                      translations: dict[str, str], position: str,
                      translate_tags: set[str] | None = None,
                      exclude_tags: set[str] | None = None,
                      style: str = "") -> int:
    """Walk the DOM, injecting translations using the same page-local uid
    scheme as extraction.
    """
    idx = 0
    effective_blocks = translate_tags if translate_tags else None
    effective_exclude = exclude_tags or set()

    def walk(node: etree._Element) -> int:
        nonlocal idx
        injected = 0
        for child in list(node):
            if _should_skip(child):
                continue
            text = _extract_text(child)
            if not text:
                continue
            tag = _localname(child.tag)
            if effective_blocks and tag not in effective_blocks:
                injected += walk(child)
                continue
            if (_is_inline_only(child, effective_blocks)
                    and not _has_excluded_child(child, effective_exclude)
                    and not _is_non_translatable(text)):
                uid = _md5(f"{page_href}:{idx}")
                trans = translations.get(uid)
                if trans:
                    _inject_translation(child, trans, position, style=style)
                    injected += 1
                idx += 1
            else:
                injected += walk(child)
        return injected

    return walk(parent)


def write_translated_epub(
    epub_path: str,
    output_path: str,
    translations: dict[str, str],
    position: str = "below",
    expected_count: int | None = None,
    style: str = "",
    translate_tags: str = "",
    exclude_translate_tags: str = "",
) -> int:
    injected_total = 0
    _t_tags = _parse_tag_set(translate_tags)
    _e_tags = _parse_tag_set(exclude_translate_tags)
    with zipfile.ZipFile(epub_path, "r") as zin:
        opf_path = _read_container(zin)
        manifest, spine_hrefs, _ = _parse_opf(zin, opf_path)

        resolved_map = {_resolve_href(opf_path, h): h for h in spine_hrefs}

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename in resolved_map:
                    href = resolved_map[item.filename]
                    data, injected = _inject_into_page(
                        data, href, translations, position,
                        style=style, translate_tags=_t_tags,
                        exclude_tags=_e_tags)
                    injected_total += injected
                zout.writestr(item, data)
    if expected_count is None:
        expected_count = len(translations)
    if expected_count and injected_total != expected_count:
        import logging
        logging.warning(
            "译文注入数量不匹配: 预期 %d, 实际 %d", expected_count, injected_total
        )
    return injected_total


def _inject_into_page(data: bytes, page_href: str,
                      translations: dict[str, str],
                      position: str,
                      style: str = "",
                      translate_tags: set[str] | None = None,
                      exclude_tags: set[str] | None = None) -> tuple[bytes, int]:
    try:
        tree, recovered = _parse_content_page(data)
    except Exception:
        return data, 0

    body = _find_body(tree)
    if body is None:
        return data, 0

    injected = _inject_recursive(body, page_href, translations, position,
                                 translate_tags=translate_tags,
                                 exclude_tags=exclude_tags,
                                 style=style)
    if recovered:
        output = etree.tostring(tree, encoding="utf-8", method="html")
    else:
        output = etree.tostring(tree, encoding="utf-8", xml_declaration=True)
    return output, injected
