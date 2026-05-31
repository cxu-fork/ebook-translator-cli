"""EPUB extraction, translation injection, and repackaging.

This module handles:
  - Reading an EPUB (zip container)
  - Parsing OPF manifest/spine/metadata
  - Extracting translatable elements from XHTML content documents
  - Injecting translations back into the XHTML DOM
  - Writing the translated EPUB back out
"""
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

# XHTML namespace
NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# Block-level elements whose text content is a translation unit
BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "th", "td",
    "caption", "blockquote", "dt", "dd", "figcaption",
    "summary", "div", "article", "section", "header", "footer",
    "aside", "nav", "main",
}

# Elements to skip entirely (children not visited)
SKIP_TAGS = {"script", "style", "svg", "math", "img", "video", "audio", "object", "embed"}


def _md5(text: str) -> str:
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()


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
    base_dir = os.path.dirname(base)
    return os.path.normpath(os.path.join(base_dir, href)) if base_dir else href


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
    raw = " ".join(parts)
    return re.sub(r"\s+", " ", raw).strip()


def _should_skip(el: etree._Element) -> bool:
    return _localname(el.tag) in SKIP_TAGS


def _is_inline_only(el: etree._Element) -> bool:
    for child in el:
        if _localname(child.tag) in BLOCK_TAGS:
            return False
    return True


def _extract_elements(root: etree._Element, page_href: str) -> list[ExtractedElement]:
    """Walk the DOM and collect block-level translatable elements.
    Index is page-local (0-based per page) so uid is stable regardless of
    extraction order across pages.
    """
    results: list[ExtractedElement] = []
    idx = 0

    def walk(parent: etree._Element):
        nonlocal idx
        for child in list(parent):
            tag = _localname(child.tag)
            if _should_skip(child):
                continue
            text = _extract_text(child)
            if not text:
                continue
            if tag in BLOCK_TAGS or _is_inline_only(child):
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
def extract_from_epub(epub_path: str) -> tuple[list[ExtractedElement], dict]:
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

        for href in spine_hrefs:
            resolved = _resolve_href(opf_path, href)
            if resolved not in zf.namelist():
                continue
            media_type = manifest.get(href, "")
            if "html" not in media_type and "xml" not in media_type:
                continue
            data = zf.read(resolved)
            try:
                tree = etree.fromstring(data)
            except etree.XMLSyntaxError:
                # HTML 格式（如 KindleUnpack 输出的 MOBI7）用 lenient parser
                try:
                    parser = etree.HTMLParser(recover=True)
                    tree = etree.fromstring(data, parser=parser)
                except Exception:
                    continue
            body = tree.find(f".//{{{NS_XHTML}}}body")
            if body is None:
                body = tree.find(".//body")
            if body is None:
                continue
            elements.extend(_extract_elements(body, href))

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
def _inject_translation(el: etree._Element, translation: str,
                        position: str = "below"):
    try:
        frag = etree.fromstring(
            f"<div xmlns='{NS_XHTML}' class='et-translation'>{translation}</div>"
        )
    except etree.XMLSyntaxError:
        frag = etree.SubElement(etree.Element("dummy"), f"{{{NS_XHTML}}}div")
        frag.set("class", "et-translation")
        frag.text = translation

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
                      translations: dict[str, str], position: str):
    """Walk the DOM, injecting translations using the same page-local uid
    scheme as extraction.
    """
    idx = 0
    for child in list(parent):
        tag = _localname(child.tag)
        if _should_skip(child):
            continue
        text = _extract_text(child)
        if not text:
            continue
        if tag in BLOCK_TAGS or _is_inline_only(child):
            uid = _md5(f"{page_href}:{idx}")
            trans = translations.get(uid)
            if trans:
                _inject_translation(child, trans, position)
            idx += 1
        else:
            _inject_recursive(child, page_href, translations, position)


def write_translated_epub(
    epub_path: str,
    output_path: str,
    translations: dict[str, str],
    position: str = "below",
):
    with zipfile.ZipFile(epub_path, "r") as zin:
        opf_path = _read_container(zin)
        manifest, spine_hrefs, _ = _parse_opf(zin, opf_path)

        resolved_map = {_resolve_href(opf_path, h): h for h in spine_hrefs}

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename in resolved_map:
                    href = resolved_map[item.filename]
                    data = _inject_into_page(data, href, translations, position)
                zout.writestr(item, data)


def _inject_into_page(data: bytes, page_href: str,
                      translations: dict[str, str],
                      position: str) -> bytes:
    try:
        tree = etree.fromstring(data)
    except etree.XMLSyntaxError:
        return data

    body = tree.find(f".//{{{NS_XHTML}}}body")
    if body is None:
        body = tree.find(".//body")
    if body is None:
        return data

    _inject_recursive(body, page_href, translations, position)
    return etree.tostring(tree, encoding="utf-8", xml_declaration=True)
