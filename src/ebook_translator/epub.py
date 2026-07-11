"""EPUB extraction, translation injection, and repackaging.

This module handles:
  - Reading an EPUB (zip container)
  - Parsing OPF manifest/spine/metadata
  - Extracting translatable elements from XHTML content documents
  - Injecting translations back into the XHTML DOM
  - Writing the translated EPUB back out
"""
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
from typing import Any

from lxml import etree

# XHTML namespace
NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_OPF = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# ponytail: fixed safety ceilings; make configurable only if real large books hit them.
_MAX_MEMBER_SIZE = 256 * 1024 * 1024
_MAX_TOTAL_SIZE = 4 * 1024 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 1000

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
    tree = etree.parse(zf.open("META-INF/container.xml"), _xml_parser())
    rootfiles = [el for el in tree.iter() if _localname(el.tag) == "rootfile"]
    if not rootfiles:
        raise ValueError("No rootfile found in container.xml")
    return unquote(rootfiles[0].attrib["full-path"]).replace("\\", "/")


def _validate_archive(zf: zipfile.ZipFile):
    total = 0
    names: set[str] = set()
    for item in zf.infolist():
        if item.filename in names:
            raise ValueError(f"EPUB 包含重复成员: {item.filename}")
        names.add(item.filename)
        total += item.file_size
        if item.file_size > _MAX_MEMBER_SIZE:
            raise ValueError(f"EPUB 成员过大: {item.filename}")
        if total > _MAX_TOTAL_SIZE:
            raise ValueError("EPUB 解压后总大小过大")
        if (item.file_size > 10 * 1024 * 1024
                and item.compress_size > 0
                and item.file_size / item.compress_size > _MAX_COMPRESSION_RATIO):
            raise ValueError(f"EPUB 成员压缩比异常: {item.filename}")


def _parse_opf(zf: zipfile.ZipFile, opf_path: str):
    tree = etree.parse(zf.open(opf_path), _xml_parser())
    root = tree.getroot()

    manifest: dict[str, str] = {}
    for item in (el for el in root.iter() if _localname(el.tag) == "item"):
        manifest[item.attrib.get("href", "")] = item.attrib.get("media-type", "")

    id_to_href: dict[str, str] = {}
    for item in (el for el in root.iter() if _localname(el.tag) == "item"):
        id_to_href[item.attrib.get("id", "")] = item.attrib.get("href", "")

    spine_hrefs: list[str] = []
    for itemref in (el for el in root.iter() if _localname(el.tag) == "itemref"):
        sid = itemref.attrib.get("idref", "")
        if sid in id_to_href:
            spine_hrefs.append(id_to_href[sid])

    metadata_el = next(
        (el for el in root.iter() if _localname(el.tag) == "metadata"), None)
    return manifest, spine_hrefs, metadata_el


def _resolve_href(base: str, href: str) -> str:
    base_dir = posixpath.dirname(base.replace("\\", "/"))
    href = unquote(urlsplit(href).path).replace("\\", "/")
    resolved = posixpath.normpath(posixpath.join(base_dir, href)) if base_dir else href
    return "" if resolved == "." else resolved


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
def _localname(tag) -> str:
    """Return the local part of an element tag.

    lxml Comment / PI / Entity nodes expose ``.tag`` as a Cython callable, not a
    string. Using ``x in tag`` on those raises TypeError; treat them as empty.
    """
    if not isinstance(tag, str):
        return ""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _xml_parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False, load_dtd=False, no_network=True,
    )


def _is_element_node(el: etree._Element) -> bool:
    """True for real elements; False for comments, PIs, entities."""
    return isinstance(getattr(el, "tag", None), str)


def _extract_text(el: etree._Element) -> str:
    parts: list[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        if _is_element_node(child):
            parts.append(_extract_text(child))
        if child.tail:
            parts.append(child.tail)
    raw = "".join(parts)
    return re.sub(r"\s+", " ", raw).strip()


def _should_skip(el: etree._Element) -> bool:
    if not _is_element_node(el):
        return True
    return _localname(el.tag) in SKIP_TAGS


def _is_inline_only(el: etree._Element, block_tags: set[str] | None = None) -> bool:
    tags = block_tags if block_tags is not None else BLOCK_TAGS
    for desc in el.iter():
        if desc is el:
            continue
        if not _is_element_node(desc):
            continue
        if _localname(desc.tag) in tags:
            return False
    return True



# ---------------------------------------------------------------------------
# Non-translatable text detection
# ---------------------------------------------------------------------------
_RE_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_RE_ISBN = re.compile(
    r"ISBN(?:-1[03])?\s*:?\s*[0-9X][0-9X\-\s]{8,}", re.IGNORECASE)
_RE_FIGURE = re.compile(
    r"(?:Figure|Fig\.?|Table)\s*[:#.]?\s*"
    r"[A-Z0-9]+(?:[.\-][A-Z0-9]+)*[.:]?",
    re.IGNORECASE,
)


def _is_non_translatable(text: str) -> bool:
    """Detect URLs, ISBNs, pure numbers, figure/listing references."""
    stripped = text.strip()
    if not stripped:
        return True
    if _RE_URL.fullmatch(stripped):
        return True
    if _RE_ISBN.fullmatch(stripped):
        return True
    # Pure number (including formatted: 1,234.56, -3.14e2, 99%)
    if re.fullmatch(r"[\d,.\-+eE\s%]+", stripped):
        return True
    if _RE_FIGURE.fullmatch(stripped):
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
        if desc is el or not _is_element_node(desc):
            continue
        if _localname(desc.tag) in exclude_tags:
            return True
    return False


def _parse_content_page(data: bytes) -> tuple[etree._Element, bool]:
    """Parse a content document.

    Returns ``(tree, recovered)`` where ``recovered`` means the XML parser failed
    and the HTML recovery parser was used. KindleUnpack MOBI7 output often lands
    in this bucket; extraction and injection must handle it consistently.
    """
    try:
        return etree.fromstring(data, parser=_xml_parser()), False
    except etree.XMLSyntaxError:
        parser = etree.HTMLParser(recover=True, no_network=True)
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
            if not _is_element_node(child):
                continue
            tag = _localname(child.tag)
            if _should_skip(child) or tag in effective_exclude:
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
        _validate_archive(zf)
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
            base_name_key = base_name.lower()
            href_key = href.lower()
            if _only and base_name_key not in _only and href_key not in _only:
                continue
            if _exc_files and (base_name_key in _exc_files or href_key in _exc_files):
                continue
            resolved = _resolve_href(opf_path, href)
            if resolved not in zf.namelist():
                raise ValueError(f"EPUB 正文文件不存在: {href}")
            media_type = manifest.get(href, "") or ""
            if not isinstance(media_type, str):
                media_type = str(media_type)
            content_type = media_type.lower()
            if ("html" not in content_type
                    and content_type not in {"application/xml", "text/xml"}
                    and not resolved.lower().endswith((".xhtml", ".html", ".htm"))):
                continue
            data = zf.read(resolved)
            try:
                tree, _recovered = _parse_content_page(data)
            except Exception as e:
                raise ValueError(f"EPUB 正文解析失败: {href}: {e}") from e
            body = _find_body(tree)
            if body is None:
                raise ValueError(f"EPUB 正文缺少 body: {href}")
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
def _apply_translation_attributes(el: etree._Element, style: str = ""):
    classes = el.get("class", "").split()
    if "et-translation" not in classes:
        classes.append("et-translation")
    el.set("class", " ".join(classes))
    if style:
        current = el.get("style", "").strip()
        el.set("style", f"{current.rstrip(';')}; {style}" if current else style)


def _make_translation_fragment(el: etree._Element, translation: str,
                               style: str = "") -> etree._Element:
    el_tag = el.tag if isinstance(el.tag, str) else ""
    tag = f"{{{NS_XHTML}}}span" if el_tag.startswith("{") else "span"
    frag = etree.Element(tag)
    _apply_translation_attributes(frag, style)
    display = frag.get("style", "").strip()
    frag.set("style", f"display: block; {display}" if display else "display: block")
    frag.text = translation
    return frag


def _inject_translation(el: etree._Element, translation: str,
                        position: str = "below", style: str = ""):
    if position == "only":
        for child in list(el):
            el.remove(child)
        el.text = translation
        _apply_translation_attributes(el, style)
        return

    frag = _make_translation_fragment(el, translation, style=style)
    if position == "above":
        frag.tail = el.text
        el.text = None
        el.insert(0, frag)
    else:
        el.append(frag)


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
            if not _is_element_node(child) or _should_skip(child):
                continue
            text = _extract_text(child)
            if not text:
                continue
            tag = _localname(child.tag)
            if tag in effective_exclude:
                continue
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
    output_dir = os.path.dirname(output_path) or "."
    os.makedirs(output_dir, exist_ok=True)
    fd, tmp_output = tempfile.mkstemp(
        prefix=".et-epub-", suffix=".epub", dir=output_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(epub_path, "r") as zin:
            _validate_archive(zin)
            opf_path = _read_container(zin)
            _manifest, spine_hrefs, _ = _parse_opf(zin, opf_path)
            resolved_map = {_resolve_href(opf_path, h): h for h in spine_hrefs}

            with zipfile.ZipFile(tmp_output, "w") as zout:
                for item in zin.infolist():
                    if item.filename in resolved_map:
                        data = zin.read(item)
                        href = resolved_map[item.filename]
                        data, injected = _inject_into_page(
                            data, href, translations, position,
                            style=style, translate_tags=_t_tags,
                            exclude_tags=_e_tags)
                        injected_total += injected
                        zout.writestr(item, data)
                    else:
                        with zin.open(item) as source, zout.open(
                            item, "w", force_zip64=True
                        ) as target:
                            shutil.copyfileobj(source, target, 1024 * 1024)
        if expected_count is None:
            expected_count = len(translations)
        if injected_total != expected_count:
            raise ValueError(
                f"译文注入数量不匹配: 预期 {expected_count}, 实际 {injected_total}"
            )
        os.replace(tmp_output, output_path)
        return injected_total
    finally:
        if os.path.exists(tmp_output):
            os.remove(tmp_output)


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
