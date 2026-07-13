use std::{
    collections::HashMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Component, Path, PathBuf},
    sync::{Arc, Mutex, OnceLock},
    time::{Instant, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result, anyhow, bail};
use clap::Parser;
use indicatif::{ProgressBar, ProgressStyle};
use serde_json::{Value, json};
use tempfile::Builder;

use crate::{
    cache::{TranslationCache, md5},
    config::{Config, MAX_CONCURRENCY},
    converter::{convert, convert_to_epub, find_ebook_convert},
    engine::Engine,
    epub::{ExtractedElement, cache_rows, extract_from_epub, write_translated_epub},
    fsutil::replace,
    glossary::Glossary,
    worker::TranslationWorker,
};

const SUPPORTED: &[&str] = &[
    "epub", "mobi", "azw3", "azw", "fb2", "pdf", "rtf", "txt", "docx", "html", "htm", "odt", "pdb",
    "cbz", "cbr",
];

static LOG: OnceLock<Mutex<File>> = OnceLock::new();

#[derive(Parser, Debug)]
#[command(
    name = "ebook-translator",
    version,
    about = "无头命令行批量电子书翻译工具"
)]
struct Args {
    /// 输入目录或单个电子书文件路径
    input: PathBuf,
    /// 输出目录
    output: PathBuf,
    /// 输出格式 (epub, mobi, azw3)
    #[arg(short = 'o', long, default_value = "epub", value_parser = ["epub", "mobi", "azw3"])]
    output_format: String,
    /// 配置文件路径
    #[arg(short = 'c', long)]
    config: Option<PathBuf>,
    /// 翻译引擎
    #[arg(short = 'e', long, value_parser = ["openai", "claude", "deepseek"])]
    engine: Option<String>,
    /// 源语言
    #[arg(short = 's', long)]
    source_lang: Option<String>,
    /// 目标语言
    #[arg(short = 't', long)]
    target_lang: Option<String>,
    /// 并发翻译数
    #[arg(long)]
    concurrency: Option<usize>,
    /// 覆盖已存在的输出文件
    #[arg(short = 'f', long)]
    force: bool,
    /// 禁用翻译缓存
    #[arg(long)]
    no_cache: bool,
    /// 跳过翻译失败的段落
    #[arg(long)]
    skip_failed: bool,
    /// 日志输出到文件
    #[arg(long)]
    log_file: Option<PathBuf>,
    /// 预览模式
    #[arg(long)]
    dry_run: bool,
    /// 仅翻译前几段
    #[arg(long = "test")]
    test_enabled: bool,
    /// 测试模式翻译段落数
    #[arg(long)]
    test_num: Option<usize>,
    /// 重翻译的页面文件名
    #[arg(long)]
    retranslate_file: Option<String>,
    /// 重翻译起始文本
    #[arg(long)]
    retranslate_start: Option<String>,
    /// 重翻译结束文本
    #[arg(long)]
    retranslate_end: Option<String>,
}

pub async fn run() -> i32 {
    let args = Args::parse();
    match run_inner(args).await {
        Ok(code) => code,
        Err(error) => {
            eprintln!("错误: {error:#}");
            log("ERROR", &format!("{error:#}"));
            1
        }
    }
}

async fn run_inner(args: Args) -> Result<i32> {
    let mut config =
        Config::load(args.config.as_deref()).map_err(|x| anyhow!("配置错误: {x:#}"))?;
    apply_overrides(&args, &mut config)?;
    init_log(&config.log_file)?;
    log(
        "INFO",
        &format!("启动 ebook-translator v{}", env!("CARGO_PKG_VERSION")),
    );

    let books = collect_books(&args.input)?;
    if books.is_empty() {
        bail!(
            "在 {} 中未找到支持的电子书文件\n支持的格式: {}",
            args.input.display(),
            SUPPORTED.join(", ")
        );
    }
    if args.dry_run {
        println!("找到 {} 本书:\n", books.len());
        for book in &books {
            println!(
                "  {}  ({})",
                book.file_name().unwrap_or_default().to_string_lossy(),
                human_size(book)
            );
        }
        return Ok(0);
    }
    fs::create_dir_all(&args.output)?;
    let output_dir = args.output.canonicalize().context("输出目录不可访问")?;
    let mut outputs = HashMap::new();
    for book in &books {
        let stem = book.file_stem().and_then(|x| x.to_str()).unwrap_or("book");
        let output = output_dir.join(format!("{stem}.{}", args.output_format));
        if let Some(previous) = outputs.insert(output.clone(), book.clone()) {
            bail!(
                "输入文件输出名冲突: {} 和 {}",
                previous.display(),
                book.display()
            );
        }
        if canonical_target(book)? == canonical_target(&output)? {
            bail!("拒绝覆盖输入文件: {}", book.display());
        }
    }
    let glossary = Glossary::load(&config.glossary_path).map_err(|x| anyhow!("配置错误: {x:#}"))?;
    if args.output_format != "epub" {
        find_ebook_convert(&config.ebook_convert_path)?;
    }

    let progress = ProgressBar::new(books.len() as u64);
    progress.set_style(
        ProgressStyle::with_template("{msg} {bar:30} {pos}/{len} [{elapsed_precise}]").unwrap(),
    );
    progress.set_message("总进度");
    let start = Instant::now();
    let mut succeeded = 0;
    let mut failed = 0;
    let mut skipped = 0;
    for book in books {
        let stem = book.file_stem().and_then(|x| x.to_str()).unwrap_or("book");
        let output = output_dir.join(format!("{stem}.{}", args.output_format));
        if output.exists() && !args.force {
            progress.println(format!(
                "  跳过: {stem} (输出文件已存在，使用 --force 覆盖)"
            ));
            skipped += 1;
            progress.inc(1);
            continue;
        }
        let result = tokio::select! {
            result = translate_book(&book, &output, &args.output_format, config.clone(), glossary.clone()) => result,
            _ = tokio::signal::ctrl_c() => {
                progress.finish_and_clear();
                eprintln!("翻译被中断，进度已保存");
                return Ok(130);
            }
        };
        match result {
            Ok(true) => succeeded += 1,
            Ok(false) => failed += 1,
            Err(error) => {
                progress.println(format!("  处理失败 {stem}: {error:#}"));
                log("ERROR", &format!("处理失败 {}: {error:#}", book.display()));
                failed += 1;
            }
        }
        progress.inc(1);
    }
    progress.finish_and_clear();
    eprintln!("\n  翻译完成\n\n  成功: {succeeded}");
    if failed > 0 {
        eprintln!("  失败: {failed}");
    }
    if skipped > 0 {
        eprintln!("  跳过: {skipped}");
    }
    eprintln!("  用时: {:.1} 分钟\n", start.elapsed().as_secs_f64() / 60.0);
    Ok(if failed > 0 { 1 } else { 0 })
}

fn apply_overrides(args: &Args, config: &mut Config) -> Result<()> {
    if let Some(value) = &args.engine {
        config.engine.clone_from(value);
    }
    if let Some(value) = &args.source_lang {
        config.source_lang.clone_from(value);
    }
    if let Some(value) = &args.target_lang {
        config.target_lang.clone_from(value);
    }
    if let Some(value) = args.concurrency.filter(|value| *value > 0) {
        if value > MAX_CONCURRENCY {
            bail!("concurrency 不能大于 {MAX_CONCURRENCY}");
        }
        let mut engine = config.engine_config(None);
        engine.concurrency = value;
        config.engines.insert(config.engine.clone(), engine);
    }
    if args.no_cache {
        config.cache_enabled = false;
    }
    if args.skip_failed {
        config.skip_failed = true;
    }
    if let Some(value) = &args.log_file {
        config.log_file = value.clone();
    }
    if args.test_enabled {
        config.test_enabled = true;
    }
    if config.test_enabled {
        config.skip_failed = true;
    }
    if let Some(value) = args.test_num.filter(|value| *value > 0) {
        config.test_num = value;
    }
    if let Some(value) = &args.retranslate_file {
        config.retranslate_file.clone_from(value);
    }
    if let Some(value) = &args.retranslate_start {
        config.retranslate_start.clone_from(value);
    }
    if let Some(value) = &args.retranslate_end {
        config.retranslate_end.clone_from(value);
    }
    config.validate()
}

async fn translate_book(
    input: &Path,
    output: &Path,
    output_format: &str,
    config: Config,
    glossary: Glossary,
) -> Result<bool> {
    let input_format = extension(input);
    if !SUPPORTED.contains(&input_format.as_str()) {
        return Ok(false);
    }
    log("INFO", &format!("开始处理: {}", input.display()));
    let converted = convert_to_epub(input, &config.ebook_convert_path).context("格式转换失败")?;
    let (elements, meta) = extract_from_epub(
        &converted.path,
        &config.only_files,
        &config.exclude_files,
        &config.translate_tags,
        &config.exclude_translate_tags,
    )
    .context("EPUB 解析失败")?;
    if elements.is_empty() {
        eprintln!("  未找到可翻译内容: {}", input.display());
        return Ok(false);
    }
    let title = if meta.title.is_empty() {
        input
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned()
    } else {
        meta.title
    };
    let cache_key = cache_key(input, &elements, &config, &glossary)?;
    let cache_path = config
        .cache_dir
        .join("books")
        .join(format!("{cache_key}.db"));
    let cache = Arc::new(TranslationCache::open(&cache_path, config.cache_enabled)?);
    cache.set_info("title", &title)?;
    cache.set_info("engine", &config.engine)?;
    cache.set_info("target_lang", &config.target_lang)?;
    cache.set_info("source", &input.canonicalize()?.display().to_string())?;
    cache.save_paragraphs(&cache_rows(&elements))?;
    if !config.retranslate_file.is_empty() && !config.retranslate_start.is_empty() {
        retranslate(&cache, &config)?;
    }
    let mut untranslated = cache.untranslated()?;
    let (already, total) = cache.counts()?;
    if config.test_enabled && config.test_num > 0 {
        untranslated.truncate(config.test_num);
    }

    if !untranslated.is_empty() {
        let engine = Engine::new(
            &config.engine,
            config.engine_config(None),
            &config.source_lang,
            &config.target_lang,
        )?;
        let worker = TranslationWorker::new(engine, cache.clone(), config.clone(), glossary);
        let (done, failed) = worker.translate_batch(untranslated).await;
        eprintln!("  {title}: {total} 段, 完成 {done}, 缓存 {already}, 失败 {failed}");
        if failed > 0 && !config.skip_failed {
            return Ok(false);
        }
    }
    let all = cache.all()?;
    let missing = all.iter().filter(|x| x.translation.is_none()).count();
    if missing > 0 && !config.skip_failed {
        return Ok(false);
    }
    let translations = all
        .into_iter()
        .filter_map(|x| x.translation.map(|value| (x.id, value)))
        .collect::<HashMap<_, _>>();
    let output_dir = output.parent().unwrap_or_else(|| Path::new("."));
    let translated = Builder::new()
        .prefix(".et-translated-")
        .suffix(".epub")
        .tempfile_in(output_dir)?;
    let translated_path = translated.path().to_owned();
    drop(translated);
    write_translated_epub(
        &converted.path,
        &translated_path,
        &translations,
        &config.translation_position,
        translations.len(),
        &config.translation_style,
        &config.translate_tags,
        &config.exclude_translate_tags,
    )?;
    if output_format == "epub" {
        replace(&translated_path, output)?;
    } else if let Err(error) = convert(
        &translated_path,
        output,
        output_format,
        &config.ebook_convert_path,
    ) {
        let fallback = fallback_epub(output, input);
        replace(&translated_path, &fallback)?;
        eprintln!(
            "输出转换失败({error:#})，已回退保存为 EPUB: {}",
            fallback.display()
        );
        return Ok(false);
    } else {
        let _ = fs::remove_file(&translated_path);
    }
    log(
        "INFO",
        &format!("处理完成: {} -> {}", input.display(), output.display()),
    );
    Ok(true)
}

fn retranslate(cache: &TranslationCache, config: &Config) -> Result<()> {
    let mut ids = Vec::new();
    let mut in_range = false;
    for paragraph in cache.all_with_ignored()? {
        if !config.retranslate_file.is_empty() {
            let Some(page) = &paragraph.page else {
                continue;
            };
            if config.retranslate_file != *page
                && Path::new(page)
                    .file_name()
                    .is_none_or(|x| x != config.retranslate_file.as_str())
            {
                continue;
            }
        }
        if paragraph.original.contains(&config.retranslate_start) {
            in_range = true;
        }
        if in_range {
            ids.push(paragraph.id);
        }
        if in_range
            && !config.retranslate_end.is_empty()
            && paragraph.original.contains(&config.retranslate_end)
        {
            break;
        }
    }
    let cleared = cache.clear_translations(&ids)?;
    eprintln!("  重翻译: 已清除 {cleared} 段缓存");
    Ok(())
}

fn cache_key(
    input: &Path,
    elements: &[ExtractedElement],
    config: &Config,
    glossary: &Glossary,
) -> Result<String> {
    let element_signature = md5(&serde_json::to_string(
        &elements
            .iter()
            .map(|x| json!([x.uid, x.page_href, x.original]))
            .collect::<Vec<_>>(),
    )?);
    let engine = config.engine_config(None);
    let payload = json!({
        "cache_version": 4, "source_content_md5": file_md5(input)?, "element_signature": element_signature,
        "engine": config.engine, "source_lang": config.source_lang, "target_lang": config.target_lang,
        "prompt": config.prompt, "model": engine.model, "base_url": engine.base_url,
        "temperature": engine.temperature, "top_p": engine.top_p, "extra": engine.extra,
        "translate_tags": config.translate_tags, "exclude_translate_tags": config.exclude_translate_tags,
        "glossary": glossary.pairs,
    });
    Ok(md5(&python_json(&payload)))
}

fn python_json(value: &Value) -> String {
    match value {
        Value::Null => "null".into(),
        Value::Bool(x) => x.to_string(),
        Value::Number(x) => x.to_string(),
        Value::String(x) => serde_json::to_string(x).unwrap(),
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(python_json)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(values) => {
            let mut values = values.iter().collect::<Vec<_>>();
            values.sort_by_key(|x| x.0);
            format!(
                "{{{}}}",
                values
                    .into_iter()
                    .map(|(key, value)| format!(
                        "{}: {}",
                        serde_json::to_string(key).unwrap(),
                        python_json(value)
                    ))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
    }
}

fn file_md5(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut context = md5::Context::new();
    let mut buffer = [0; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        context.consume(&buffer[..read]);
    }
    Ok(format!("{:x}", context.finalize()))
}

fn collect_books(input: &Path) -> Result<Vec<PathBuf>> {
    if input.is_file() {
        return Ok(if SUPPORTED.contains(&extension(input).as_str()) {
            vec![input.to_owned()]
        } else {
            Vec::new()
        });
    }
    if !input.is_dir() {
        return Ok(Vec::new());
    }
    let mut books = fs::read_dir(input)?
        .filter_map(|x| x.ok().map(|x| x.path()))
        .filter(|x| x.is_file() && SUPPORTED.contains(&extension(x).as_str()))
        .collect::<Vec<_>>();
    books.sort();
    Ok(books)
}

fn extension(path: &Path) -> String {
    path.extension()
        .and_then(|x| x.to_str())
        .unwrap_or("")
        .to_lowercase()
}

fn canonical_target(path: &Path) -> Result<PathBuf> {
    if path.exists() {
        return Ok(path.canonicalize()?);
    }
    let parent = path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .canonicalize()?;
    Ok(normalize(
        &parent.join(path.file_name().unwrap_or_default()),
    ))
}

fn normalize(path: &Path) -> PathBuf {
    let mut output = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                output.pop();
            }
            Component::CurDir => {}
            other => output.push(other.as_os_str()),
        }
    }
    output
}

fn fallback_epub(output: &Path, input: &Path) -> PathBuf {
    let parent = output.parent().unwrap_or_else(|| Path::new("."));
    let stem = output
        .file_stem()
        .and_then(|x| x.to_str())
        .unwrap_or("book");
    for suffix in std::iter::once(".translated.epub".into())
        .chain((2..).map(|x| format!(".translated-{x}.epub")))
    {
        let candidate = parent.join(format!("{stem}{suffix}"));
        if !candidate.exists() && canonical_target(&candidate).ok() != canonical_target(input).ok()
        {
            return candidate;
        }
    }
    unreachable!()
}

fn human_size(path: &Path) -> String {
    let Ok(size) = fs::metadata(path).map(|x| x.len()) else {
        return "?".into();
    };
    if size < 1024 {
        format!("{size} B")
    } else if size < 1024 * 1024 {
        format!("{:.1} KB", size as f64 / 1024.0)
    } else {
        format!("{:.1} MB", size as f64 / 1024.0 / 1024.0)
    }
}

fn init_log(path: &Path) -> Result<()> {
    if path.as_os_str().is_empty() {
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let file = OpenOptions::new().create(true).append(true).open(path)?;
    let _ = LOG.set(Mutex::new(file));
    Ok(())
}

fn log(level: &str, message: &str) {
    let Some(file) = LOG.get() else {
        return;
    };
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    if let Ok(mut file) = file.lock() {
        let _ = writeln!(file, "{timestamp} [{level}] {message}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_json_is_sorted_and_spaced_like_python() {
        assert_eq!(
            python_json(&json!({"z":[1,"中"],"a":{"b":true}})),
            r#"{"a": {"b": true}, "z": [1, "中"]}"#
        );
    }

    #[test]
    fn collect_books_is_sorted_and_filters_formats() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("b.epub"), b"").unwrap();
        fs::write(dir.path().join("a.MOBI"), b"").unwrap();
        fs::write(dir.path().join("x.exe"), b"").unwrap();
        assert_eq!(
            collect_books(dir.path())
                .unwrap()
                .iter()
                .map(|x| x.file_name().unwrap().to_string_lossy())
                .collect::<Vec<_>>(),
            ["a.MOBI", "b.epub"]
        );
    }

    #[test]
    fn cache_key_matches_python_v4_format() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("source.bin");
        fs::write(&source, b"book").unwrap();
        let elements = vec![ExtractedElement {
            uid: "u".into(),
            raw_html: String::new(),
            original: "text".into(),
            ignored: false,
            page_href: "p".into(),
        }];
        let mut config = Config {
            engine: "openai".into(),
            ..Default::default()
        };
        let mut engine = crate::config::EngineConfig {
            api_key: "x".into(),
            temperature: Some(0.1),
            top_p: Some(0.8),
            ..Default::default()
        };
        engine.extra.insert("seed".into(), json!(1));
        config.engines.insert("openai".into(), engine);
        assert_eq!(
            cache_key(&source, &elements, &config, &Glossary::default()).unwrap(),
            "ebad9e885caca79288b2192849fbb5d7"
        );
    }
}
