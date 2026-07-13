use std::{
    cell::RefCell,
    collections::{HashMap, HashSet},
    fs::{self, File},
    io::{Read, Write},
    path::Path,
    rc::Rc,
};

use anyhow::{Context, Result, anyhow, bail};
use html5ever::{parse_document, serialize, serialize::SerializeOpts, tendril::TendrilSink};
use markup5ever::{Attribute, QualName, local_name, ns, serialize::TraversalScope};
use markup5ever_rcdom::{Handle, Node, NodeData, RcDom, SerializableHandle};
use percent_encoding::percent_decode_str;
use quick_xml::{Reader, events::Event};
use regex::Regex;
use tempfile::NamedTempFile;
use zip::{ZipArchive, ZipWriter, write::SimpleFileOptions};

use crate::{
    cache::{Paragraph, md5},
    fsutil::replace,
};

const MAX_MEMBER_SIZE: u64 = 256 * 1024 * 1024;
const MAX_TOTAL_SIZE: u64 = 4 * 1024 * 1024 * 1024;
const MAX_COMPRESSION_RATIO: u64 = 1000;
const BLOCK_TAGS: &[&str] = &[
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "th",
    "td",
    "caption",
    "blockquote",
    "dt",
    "dd",
    "figcaption",
    "summary",
    "div",
    "article",
    "section",
    "header",
    "footer",
    "aside",
    "nav",
    "main",
];
const SKIP_TAGS: &[&str] = &[
    "script", "style", "svg", "math", "img", "video", "audio", "object", "embed",
];

#[derive(Clone, Debug)]
pub struct ExtractedElement {
    pub uid: String,
    pub raw_html: String,
    pub original: String,
    pub ignored: bool,
    pub page_href: String,
}

#[derive(Clone, Debug, Default)]
pub struct EpubMeta {
    pub title: String,
}

#[derive(Clone, Debug)]
struct Package {
    manifest: HashMap<String, String>,
    spine: Vec<String>,
    title: String,
}

pub fn extract_from_epub(
    path: &Path,
    only_files: &str,
    exclude_files: &str,
    translate_tags: &str,
    exclude_tags: &str,
) -> Result<(Vec<ExtractedElement>, EpubMeta)> {
    let mut archive = ZipArchive::new(File::open(path)?)?;
    validate_archive(&mut archive)?;
    let opf_path = read_container(&mut archive)?;
    let package = parse_package(&read_member(&mut archive, &opf_path)?)?;
    let names = archive
        .file_names()
        .map(str::to_owned)
        .collect::<HashSet<_>>();
    let only = parse_set(only_files);
    let excluded_files = parse_set(exclude_files);
    let translated_tags = parse_set(translate_tags);
    let excluded_tags = parse_set(exclude_tags);
    let mut elements = Vec::new();

    for href in &package.spine {
        let basename = href.rsplit('/').next().unwrap_or(href).to_lowercase();
        let href_key = href.to_lowercase();
        if !only.is_empty() && !only.contains(&basename) && !only.contains(&href_key) {
            continue;
        }
        if excluded_files.contains(&basename) || excluded_files.contains(&href_key) {
            continue;
        }
        let resolved = resolve_href(&opf_path, href);
        if !names.contains(&resolved) {
            bail!("EPUB 正文文件不存在: {href}");
        }
        let media = package
            .manifest
            .get(href)
            .map(String::as_str)
            .unwrap_or("")
            .to_lowercase();
        if !media.contains("html")
            && !matches!(media.as_str(), "application/xml" | "text/xml")
            && !resolved.to_lowercase().ends_with(".xhtml")
            && !resolved.to_lowercase().ends_with(".html")
            && !resolved.to_lowercase().ends_with(".htm")
        {
            continue;
        }
        let dom = parse_page(&read_member(&mut archive, &resolved)?);
        let body = find_element(&dom.document, "body")
            .ok_or_else(|| anyhow!("EPUB 正文缺少 body: {href}"))?;
        extract_nodes(&body, href, &translated_tags, &excluded_tags, &mut elements)?;
    }

    Ok((
        elements,
        EpubMeta {
            title: package.title,
        },
    ))
}

pub fn cache_rows(elements: &[ExtractedElement]) -> Vec<Paragraph> {
    elements
        .iter()
        .enumerate()
        .map(|(index, element)| Paragraph {
            id: element.uid.clone(),
            md5: md5(&format!("{index}{}", element.original)),
            raw: element.raw_html.clone(),
            original: element.original.clone(),
            ignored: element.ignored,
            attributes: None,
            page: Some(element.page_href.clone()),
            translation: None,
            engine_name: None,
            target_lang: None,
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
pub fn write_translated_epub(
    input: &Path,
    output: &Path,
    translations: &HashMap<String, String>,
    position: &str,
    expected_count: usize,
    style: &str,
    translate_tags: &str,
    exclude_tags: &str,
) -> Result<usize> {
    let mut archive = ZipArchive::new(File::open(input)?)?;
    validate_archive(&mut archive)?;
    let opf_path = read_container(&mut archive)?;
    let package = parse_package(&read_member(&mut archive, &opf_path)?)?;
    let resolved = package
        .spine
        .iter()
        .map(|href| (resolve_href(&opf_path, href), href.clone()))
        .collect::<HashMap<_, _>>();
    let translated_tags = parse_set(translate_tags);
    let excluded_tags = parse_set(exclude_tags);
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    let parent = output.parent().unwrap_or_else(|| Path::new("."));
    let temp = NamedTempFile::new_in(parent)?;
    let mut writer = ZipWriter::new(temp.reopen()?);
    let mut injected = 0;

    for index in 0..archive.len() {
        let mut member = archive.by_index(index)?;
        let name = member.name().to_owned();
        if let Some(href) = resolved.get(&name) {
            let options = SimpleFileOptions::default()
                .compression_method(member.compression())
                .unix_permissions(member.unix_mode().unwrap_or(0o644));
            let mut data = Vec::with_capacity(member.size() as usize);
            member.read_to_end(&mut data)?;
            let (data, count) = inject_page(
                &data,
                href,
                translations,
                position,
                style,
                &translated_tags,
                &excluded_tags,
            )?;
            injected += count;
            writer.start_file(name, options)?;
            writer.write_all(&data)?;
        } else {
            writer.raw_copy_file(member)?;
        }
    }
    writer.finish()?;
    if injected != expected_count {
        bail!("译文注入数量不匹配: 预期 {expected_count}, 实际 {injected}");
    }
    let (_, temp_path) = temp.keep()?;
    replace(&temp_path, output)?;
    Ok(injected)
}

fn validate_archive(archive: &mut ZipArchive<File>) -> Result<()> {
    let mut total = 0u64;
    let mut names = HashSet::new();
    for index in 0..archive.len() {
        let member = archive.by_index(index)?;
        if !names.insert(member.name().to_owned()) {
            bail!("EPUB 包含重复成员: {}", member.name());
        }
        if member.size() > MAX_MEMBER_SIZE {
            bail!("EPUB 成员过大: {}", member.name());
        }
        total = total.saturating_add(member.size());
        if total > MAX_TOTAL_SIZE {
            bail!("EPUB 解压后总大小过大");
        }
        if member.size() > 10 * 1024 * 1024
            && member.compressed_size() > 0
            && member.size() / member.compressed_size() > MAX_COMPRESSION_RATIO
        {
            bail!("EPUB 成员压缩比异常: {}", member.name());
        }
    }
    Ok(())
}

fn read_member(archive: &mut ZipArchive<File>, name: &str) -> Result<Vec<u8>> {
    let mut data = Vec::new();
    archive
        .by_name(name)
        .with_context(|| format!("EPUB 成员不存在: {name}"))?
        .read_to_end(&mut data)?;
    Ok(data)
}

fn read_container(archive: &mut ZipArchive<File>) -> Result<String> {
    let data = read_member(archive, "META-INF/container.xml")?;
    let mut reader = Reader::from_reader(data.as_slice());
    loop {
        match reader.read_event()? {
            Event::Start(event) | Event::Empty(event)
                if event.local_name().as_ref() == b"rootfile" =>
            {
                for attr in event.attributes() {
                    let attr = attr?;
                    if attr.key.local_name().as_ref() == b"full-path" {
                        let path = attr.decode_and_unescape_value(reader.decoder())?;
                        return Ok(percent_decode_str(&path)
                            .decode_utf8_lossy()
                            .replace('\\', "/"));
                    }
                }
            }
            Event::Eof => break,
            _ => {}
        }
    }
    bail!("No rootfile found in container.xml")
}

fn parse_package(data: &[u8]) -> Result<Package> {
    let mut reader = Reader::from_reader(data);
    reader.config_mut().trim_text(true);
    let mut id_to_href = HashMap::new();
    let mut manifest = HashMap::new();
    let mut spine_ids = Vec::new();
    let mut title = String::new();
    let mut in_title = false;
    loop {
        match reader.read_event()? {
            Event::Start(event) | Event::Empty(event) if event.local_name().as_ref() == b"item" => {
                let mut id = String::new();
                let mut href = String::new();
                let mut media = String::new();
                for attr in event.attributes() {
                    let attr = attr?;
                    let value = attr
                        .decode_and_unescape_value(reader.decoder())?
                        .into_owned();
                    match attr.key.local_name().as_ref() {
                        b"id" => id = value,
                        b"href" => href = value,
                        b"media-type" => media = value,
                        _ => {}
                    }
                }
                manifest.insert(href.clone(), media);
                id_to_href.insert(id, href);
            }
            Event::Start(event) | Event::Empty(event)
                if event.local_name().as_ref() == b"itemref" =>
            {
                for attr in event.attributes() {
                    let attr = attr?;
                    if attr.key.local_name().as_ref() == b"idref" {
                        spine_ids.push(
                            attr.decode_and_unescape_value(reader.decoder())?
                                .into_owned(),
                        );
                    }
                }
            }
            Event::Start(event) if event.local_name().as_ref() == b"title" => in_title = true,
            Event::Text(text) if in_title && title.is_empty() => {
                title = text.decode()?.trim().to_owned()
            }
            Event::End(event) if event.local_name().as_ref() == b"title" => in_title = false,
            Event::Eof => break,
            _ => {}
        }
    }
    let spine = spine_ids
        .into_iter()
        .filter_map(|id| id_to_href.get(&id).cloned())
        .collect();
    Ok(Package {
        manifest,
        spine,
        title,
    })
}

pub fn resolve_href(base: &str, href: &str) -> String {
    let base = base.replace('\\', "/");
    let base_dir = base.rsplit_once('/').map(|x| x.0).unwrap_or("");
    let href = href
        .split(['?', '#'])
        .next()
        .unwrap_or("")
        .replace('\\', "/");
    let decoded = percent_decode_str(&href).decode_utf8_lossy();
    let joined = format!("{base_dir}/{decoded}");
    let mut parts = Vec::new();
    for part in joined.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            part => parts.push(part),
        }
    }
    parts.join("/")
}

fn parse_page(data: &[u8]) -> RcDom {
    let text = String::from_utf8_lossy(data);
    // External entities are never resolved; removing the declaration also keeps
    // entity names out of translation text.
    let without_doctype = strip_doctype(&text);
    parse_document(RcDom::default(), Default::default()).one(without_doctype)
}

fn strip_doctype(text: &str) -> String {
    let Some(start) = text.to_ascii_lowercase().find("<!doctype") else {
        return text.into();
    };
    let bytes = text.as_bytes();
    let mut depth: usize = 0;
    let mut quote = None;
    for (index, byte) in bytes.iter().copied().enumerate().skip(start) {
        if quote == Some(byte) {
            quote = None;
            continue;
        }
        if quote.is_none() && matches!(byte, b'\'' | b'\"') {
            quote = Some(byte);
            continue;
        }
        if quote.is_none() {
            if byte == b'[' {
                depth += 1;
            }
            if byte == b']' {
                depth = depth.saturating_sub(1);
            }
            if byte == b'>' && depth == 0 {
                let mut output = text.to_owned();
                output.replace_range(start..=index, "");
                return output;
            }
        }
    }
    text[..start].to_owned()
}

fn find_element(node: &Handle, name: &str) -> Option<Handle> {
    if tag(node) == Some(name) {
        return Some(node.clone());
    }
    for child in node.children.borrow().iter() {
        if let Some(found) = find_element(child, name) {
            return Some(found);
        }
    }
    None
}

fn tag(node: &Handle) -> Option<&str> {
    match &node.data {
        NodeData::Element { name, .. } => Some(name.local.as_ref()),
        _ => None,
    }
}

fn extract_text(node: &Handle) -> String {
    fn collect(node: &Handle, text: &mut String) {
        match &node.data {
            NodeData::Text { contents } => text.push_str(&contents.borrow()),
            NodeData::Element { .. } | NodeData::Document => {
                for child in node.children.borrow().iter() {
                    collect(child, text);
                }
            }
            _ => {}
        }
    }
    let mut text = String::new();
    collect(node, &mut text);
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn extract_nodes(
    parent: &Handle,
    page: &str,
    translated_tags: &HashSet<String>,
    excluded_tags: &HashSet<String>,
    output: &mut Vec<ExtractedElement>,
) -> Result<()> {
    #[allow(clippy::too_many_arguments)]
    fn walk(
        parent: &Handle,
        page: &str,
        translated_tags: &HashSet<String>,
        excluded_tags: &HashSet<String>,
        output: &mut Vec<ExtractedElement>,
        index: &mut usize,
    ) -> Result<()> {
        let children = parent.children.borrow().clone();
        for child in children {
            let Some(name) = tag(&child) else {
                continue;
            };
            if SKIP_TAGS.contains(&name) || excluded_tags.contains(name) {
                continue;
            }
            let text = extract_text(&child);
            if text.is_empty() {
                continue;
            }
            if !translated_tags.is_empty() && !translated_tags.contains(name) {
                walk(&child, page, translated_tags, excluded_tags, output, index)?;
            } else if inline_only(&child, translated_tags)
                && !has_excluded_child(&child, excluded_tags)
                && !is_non_translatable(&text)
            {
                output.push(ExtractedElement {
                    uid: md5(&format!("{page}:{}", *index)),
                    raw_html: serialize_node(&child)?,
                    original: text,
                    ignored: false,
                    page_href: page.into(),
                });
                *index += 1;
            } else {
                walk(&child, page, translated_tags, excluded_tags, output, index)?;
            }
        }
        Ok(())
    }
    walk(parent, page, translated_tags, excluded_tags, output, &mut 0)
}

fn inline_only(node: &Handle, translated_tags: &HashSet<String>) -> bool {
    let blocks = |name: &str| {
        if translated_tags.is_empty() {
            BLOCK_TAGS.contains(&name)
        } else {
            translated_tags.contains(name)
        }
    };
    fn descendants(node: &Handle, blocks: &dyn Fn(&str) -> bool) -> bool {
        for child in node.children.borrow().iter() {
            if tag(child).is_some_and(blocks) || !descendants(child, blocks) {
                return false;
            }
        }
        true
    }
    descendants(node, &blocks)
}

fn has_excluded_child(node: &Handle, excluded: &HashSet<String>) -> bool {
    node.children.borrow().iter().any(|child| {
        tag(child).is_some_and(|name| excluded.contains(name))
            || has_excluded_child(child, excluded)
    })
}

pub fn is_non_translatable(text: &str) -> bool {
    let value = text.trim();
    if value.is_empty() {
        return true;
    }
    let url = Regex::new(r"(?i)^(?:https?://|www\.)\S+$").unwrap();
    let isbn = Regex::new(r"(?i)^ISBN(?:-1[03])?\s*:?\s*[0-9X][0-9X\-\s]{8,}$").unwrap();
    let figure =
        Regex::new(r"(?i)^(?:Figure|Fig\.?|Table)\s*[:#.]?\s*[A-Z0-9]+(?:[.\-][A-Z0-9]+)*[.:]?$")
            .unwrap();
    let number = Regex::new(r"^[\d,.\-+eE\s%]+$").unwrap();
    url.is_match(value) || isbn.is_match(value) || figure.is_match(value) || number.is_match(value)
}

fn parse_set(value: &str) -> HashSet<String> {
    value
        .split(',')
        .map(str::trim)
        .filter(|x| !x.is_empty())
        .map(str::to_lowercase)
        .collect()
}

fn serialize_node(node: &Handle) -> Result<String> {
    let mut output = Vec::new();
    let options = SerializeOpts {
        traversal_scope: TraversalScope::IncludeNode,
        ..Default::default()
    };
    serialize(
        &mut output,
        &SerializableHandle::from(node.clone()),
        options,
    )?;
    Ok(String::from_utf8(output)?)
}

fn inject_page(
    data: &[u8],
    page: &str,
    translations: &HashMap<String, String>,
    position: &str,
    style: &str,
    translated_tags: &HashSet<String>,
    excluded_tags: &HashSet<String>,
) -> Result<(Vec<u8>, usize)> {
    let dom = parse_page(data);
    let Some(body) = find_element(&dom.document, "body") else {
        return Ok((data.to_vec(), 0));
    };
    let count = inject_nodes(
        &body,
        page,
        translations,
        position,
        style,
        translated_tags,
        excluded_tags,
    );
    let mut output = Vec::new();
    serialize(
        &mut output,
        &SerializableHandle::from(dom.document),
        Default::default(),
    )?;
    Ok((output, count))
}

fn inject_nodes(
    parent: &Handle,
    page: &str,
    translations: &HashMap<String, String>,
    position: &str,
    style: &str,
    translated_tags: &HashSet<String>,
    excluded_tags: &HashSet<String>,
) -> usize {
    #[allow(clippy::too_many_arguments)]
    fn walk(
        parent: &Handle,
        page: &str,
        translations: &HashMap<String, String>,
        position: &str,
        style: &str,
        translated_tags: &HashSet<String>,
        excluded_tags: &HashSet<String>,
        index: &mut usize,
    ) -> usize {
        let mut injected = 0;
        let children = parent.children.borrow().clone();
        for child in children {
            let Some(name) = tag(&child) else {
                continue;
            };
            if SKIP_TAGS.contains(&name) || excluded_tags.contains(name) {
                continue;
            }
            let text = extract_text(&child);
            if text.is_empty() {
                continue;
            }
            if !translated_tags.is_empty() && !translated_tags.contains(name) {
                injected += walk(
                    &child,
                    page,
                    translations,
                    position,
                    style,
                    translated_tags,
                    excluded_tags,
                    index,
                );
            } else if inline_only(&child, translated_tags)
                && !has_excluded_child(&child, excluded_tags)
                && !is_non_translatable(&text)
            {
                let uid = md5(&format!("{page}:{}", *index));
                if let Some(translation) = translations.get(&uid).filter(|x| !x.is_empty()) {
                    inject_translation(&child, translation, position, style);
                    injected += 1;
                }
                *index += 1;
            } else {
                injected += walk(
                    &child,
                    page,
                    translations,
                    position,
                    style,
                    translated_tags,
                    excluded_tags,
                    index,
                );
            }
        }
        injected
    }
    walk(
        parent,
        page,
        translations,
        position,
        style,
        translated_tags,
        excluded_tags,
        &mut 0,
    )
}

fn inject_translation(element: &Handle, translation: &str, position: &str, style: &str) {
    if position == "only" {
        element.children.borrow_mut().clear();
        append(
            element,
            Node::new(NodeData::Text {
                contents: RefCell::new(translation.into()),
            }),
        );
        apply_attributes(element, style, false);
        return;
    }
    let fragment = Node::new(NodeData::Element {
        name: QualName::new(None, ns!(html), local_name!("span")),
        attrs: RefCell::new(Vec::new()),
        template_contents: RefCell::new(None),
        mathml_annotation_xml_integration_point: false,
    });
    apply_attributes(&fragment, style, true);
    append(
        &fragment,
        Node::new(NodeData::Text {
            contents: RefCell::new(translation.into()),
        }),
    );
    fragment.parent.set(Some(Rc::downgrade(element)));
    if position == "above" {
        element.children.borrow_mut().insert(0, fragment);
    } else {
        element.children.borrow_mut().push(fragment);
    }
}

fn append(parent: &Handle, child: Handle) {
    child.parent.set(Some(Rc::downgrade(parent)));
    parent.children.borrow_mut().push(child);
}

fn apply_attributes(element: &Handle, style: &str, block: bool) {
    let NodeData::Element { attrs, .. } = &element.data else {
        return;
    };
    let mut attrs = attrs.borrow_mut();
    let class = attrs.iter_mut().find(|x| x.name.local.as_ref() == "class");
    if let Some(class) = class {
        if !class
            .value
            .split_whitespace()
            .any(|x| x == "et-translation")
        {
            class.value.push_slice(" et-translation");
        }
    } else {
        attrs.push(attr("class", "et-translation"));
    }
    let mut value = if block {
        "display: block".to_owned()
    } else {
        String::new()
    };
    if !style.is_empty() {
        if !value.is_empty() {
            value.push_str("; ");
        }
        value.push_str(style);
    }
    if !value.is_empty() {
        if let Some(existing) = attrs.iter_mut().find(|x| x.name.local.as_ref() == "style") {
            if !existing.value.is_empty() {
                existing.value.push_slice("; ");
            }
            existing.value.push_slice(&value);
        } else {
            attrs.push(attr("style", &value));
        }
    }
}

fn attr(name: &str, value: &str) -> Attribute {
    Attribute {
        name: QualName::new(None, ns!(), name.into()),
        value: value.into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use zip::write::SimpleFileOptions;

    fn make_epub(path: &Path, body: &str) {
        let file = File::create(path).unwrap();
        let mut zip = ZipWriter::new(file);
        let options = SimpleFileOptions::default();
        for (name, data) in [
            ("mimetype", "application/epub+zip"),
            (
                "META-INF/container.xml",
                "<container><rootfiles><rootfile full-path='content.opf'/></rootfiles></container>",
            ),
            (
                "content.opf",
                "<package><metadata><title>T</title></metadata><manifest><item id='c' href='c.xhtml' media-type='application/xhtml+xml'/></manifest><spine><itemref idref='c'/></spine></package>",
            ),
            ("c.xhtml", body),
        ] {
            zip.start_file(name, options).unwrap();
            zip.write_all(data.as_bytes()).unwrap();
        }
        zip.finish().unwrap();
    }

    #[test]
    fn nested_extract_and_inject_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let input = dir.path().join("in.epub");
        let output = dir.path().join("out.epub");
        make_epub(
            &input,
            "<html><body><p>A</p><div><p>B</p><p>https://x.test</p><p>C</p></div></body></html>",
        );
        let (elements, meta) = extract_from_epub(&input, "", "", "", "").unwrap();
        assert_eq!(meta.title, "T");
        assert_eq!(
            elements
                .iter()
                .map(|x| x.original.as_str())
                .collect::<Vec<_>>(),
            ["A", "B", "C"]
        );
        let translations = elements
            .iter()
            .enumerate()
            .map(|(i, x)| (x.uid.clone(), format!("T{i}")))
            .collect();
        assert_eq!(
            write_translated_epub(
                &input,
                &output,
                &translations,
                "below",
                3,
                "color: blue",
                "",
                ""
            )
            .unwrap(),
            3
        );
        let mut archive = ZipArchive::new(File::open(output).unwrap()).unwrap();
        let html = String::from_utf8(read_member(&mut archive, "c.xhtml").unwrap()).unwrap();
        assert_eq!(html.matches("et-translation").count(), 3);
        assert!(html.contains("color: blue"));
    }

    #[test]
    fn filters_and_non_translatable_match_contract() {
        assert!(is_non_translatable("ISBN: 978-0-13-468599-1"));
        assert!(is_non_translatable("Figure 3"));
        assert!(!is_non_translatable("Figure 3: a diagram"));
        assert_eq!(
            resolve_href("OEBPS/content.opf", "Text/chapter%201.xhtml?q=1#x"),
            "OEBPS/Text/chapter 1.xhtml"
        );
    }

    #[test]
    fn only_replaces_contents_but_keeps_element() {
        let dom =
            parse_page(b"<html><body><p id='x' class='original'><em>Hello</em></p></body></html>");
        let p = find_element(&dom.document, "p").unwrap();
        inject_translation(&p, "translated", "only", "color:red");
        let html = serialize_node(&p).unwrap();
        assert!(html.contains("id=\"x\""));
        assert!(html.contains("original et-translation"));
        assert!(html.contains("translated"));
        assert!(!html.contains("<em"));
    }
}
