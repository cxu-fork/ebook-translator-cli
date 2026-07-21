use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::{Result, anyhow, bail};
use futures_util::{StreamExt, stream::FuturesUnordered};
use rand::RngExt;
use regex::Regex;
use tokio::sync::{Mutex, Semaphore};

use crate::{
    cache::{Paragraph, TranslationCache},
    config::{Config, MAX_CONCURRENCY},
    engine::{ApiError, Engine},
    epub::accept_markup_translation,
    glossary::Glossary,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ErrorKind {
    Permanent,
    RateLimit,
    Empty,
    Transient,
}

struct RateLimiter {
    interval: Duration,
    next: Mutex<Instant>,
}

impl RateLimiter {
    fn new(seconds: f64) -> Self {
        Self {
            interval: Duration::from_secs_f64(seconds.max(0.0)),
            next: Mutex::new(Instant::now()),
        }
    }

    async fn acquire(&self, stopped: &AtomicBool) -> Result<()> {
        loop {
            if stopped.load(Ordering::Relaxed) {
                bail!("翻译批次已停止");
            }
            let mut next = self.next.lock().await;
            let now = Instant::now();
            if *next <= now {
                *next = now + self.interval;
                return Ok(());
            }
            let wait = *next - now;
            drop(next);
            tokio::time::sleep(wait.min(Duration::from_millis(250))).await;
        }
    }

    async fn defer(&self, wait: Duration) {
        let mut next = self.next.lock().await;
        *next = (*next).max(Instant::now() + wait);
    }
}

pub struct TranslationWorker {
    engine: Arc<Engine>,
    cache: Arc<TranslationCache>,
    config: Arc<Config>,
    glossary: Arc<Glossary>,
    limiter: Arc<RateLimiter>,
    semaphore: Arc<Semaphore>,
    stopped: Arc<AtomicBool>,
    abort_count: Arc<AtomicUsize>,
}

impl TranslationWorker {
    pub fn new(
        engine: Engine,
        cache: Arc<TranslationCache>,
        config: Config,
        glossary: Glossary,
    ) -> Self {
        let interval = config.engine_config(None).request_interval;
        let concurrency = config
            .engine_config(None)
            .concurrency
            .clamp(1, MAX_CONCURRENCY);
        Self {
            engine: Arc::new(engine),
            cache,
            config: Arc::new(config),
            glossary: Arc::new(glossary),
            limiter: Arc::new(RateLimiter::new(interval)),
            semaphore: Arc::new(Semaphore::new(concurrency)),
            stopped: Arc::new(AtomicBool::new(false)),
            abort_count: Arc::new(AtomicUsize::new(0)),
        }
    }

    pub async fn translate_batch(&self, paragraphs: Vec<Paragraph>) -> (usize, usize) {
        let groups = merge_groups(
            &paragraphs,
            self.config.merge_enabled,
            self.config.merge_length,
        );
        let mut tasks = FuturesUnordered::new();
        for group in groups {
            tasks.push(async move {
                let result = if self.stopped.load(Ordering::Relaxed) {
                    Err(anyhow!("翻译批次已停止"))
                } else {
                    self.translate_group(&group).await
                };
                (group, result)
            });
        }
        let mut done = 0;
        let mut failed = 0;
        while let Some((group, result)) = tasks.next().await {
            if self.stopped.load(Ordering::Relaxed) {
                failed += group.len();
                continue;
            }
            match result {
                Ok(translations) => {
                    let updates = group
                        .iter()
                        .map(|paragraph| {
                            let translation =
                                translations.get(&paragraph.id).cloned().unwrap_or_default();
                            (
                                paragraph.id.clone(),
                                translation,
                                self.config.engine.clone(),
                                self.config.target_lang.clone(),
                            )
                        })
                        .collect::<Vec<_>>();
                    if updates.iter().any(|x| x.1.trim().is_empty())
                        || self.cache.update_translations(&updates).is_err()
                    {
                        failed += group.len();
                    } else {
                        done += group.len();
                        self.abort_count.store(0, Ordering::Relaxed);
                    }
                }
                Err(error) => {
                    failed += group.len();
                    eprintln!(
                        "  翻译失败: {} -> {error:#}",
                        group
                            .first()
                            .map(|x| x.original.chars().take(60).collect::<String>())
                            .unwrap_or_default()
                    );
                    if !self.config.skip_failed {
                        let count = self.abort_count.fetch_add(1, Ordering::Relaxed) + 1;
                        if self.config.max_error_count > 0 && count >= self.config.max_error_count {
                            self.stopped.store(true, Ordering::Relaxed);
                        }
                    }
                }
            }
        }
        (done, failed)
    }

    async fn translate_group(&self, group: &[Paragraph]) -> Result<HashMap<String, String>> {
        if group.len() == 1 {
            let paragraph = &group[0];
            return Ok([(
                paragraph.id.clone(),
                self.translate_paragraph(paragraph).await?,
            )]
            .into());
        }
        // ponytail: markup-bearing paragraphs never merge; free models drop tokens under multi-paragraph prompts
        let original = group
            .iter()
            .map(|paragraph| self.glossary.apply(&paragraph.original))
            .collect::<Vec<_>>()
            .join("\n\n");
        let response = self
            .translate_one(&original, self.config.effective_prompt())
            .await?;
        let parts = Regex::new(r"\r?\n\s*\r?\n")?
            .split(response.trim())
            .map(str::trim)
            .collect::<Vec<_>>();
        if parts.len() == group.len() {
            let mut translations = HashMap::new();
            let mut valid = true;
            for (paragraph, translation) in group.iter().zip(parts) {
                match accept_markup_translation(
                    &paragraph.original,
                    &self.glossary.restore(translation),
                ) {
                    Ok(translation) => {
                        translations.insert(paragraph.id.clone(), translation);
                    }
                    Err(_) => {
                        valid = false;
                        break;
                    }
                }
            }
            if valid {
                return Ok(translations);
            }
            eprintln!("  合并翻译损坏 HTML 占位符，回退逐段");
        } else {
            eprintln!(
                "  合并翻译段落数不匹配（预期 {}, 实际 {}），回退逐段",
                group.len(),
                parts.len()
            );
        }
        let mut fallback = HashMap::new();
        for paragraph in group {
            fallback.insert(
                paragraph.id.clone(),
                self.translate_paragraph(paragraph).await?,
            );
        }
        Ok(fallback)
    }

    async fn translate_paragraph(&self, paragraph: &Paragraph) -> Result<String> {
        let original = self.glossary.apply(&paragraph.original);
        let has_tokens = paragraph.original.contains("{{etm_");
        let prompt = markup_prompt(self.config.effective_prompt(), has_tokens);
        for attempt in 0..=usize::from(has_tokens) {
            let result = self.translate_one(&original, &prompt).await?;
            let restored = self.glossary.restore(result.trim());
            match accept_markup_translation(&paragraph.original, &restored) {
                Ok(translation) => return Ok(translation),
                Err(error) if attempt == 0 && has_tokens => {
                    eprintln!("  模型损坏 HTML 占位符，自动重试一次: {error}");
                }
                Err(error) => return Err(error),
            }
        }
        unreachable!()
    }

    async fn translate_one(&self, text: &str, prompt: &str) -> Result<String> {
        let config = self.config.engine_config(None);
        for attempt in 1..=config.max_retries.max(1) {
            self.limiter.acquire(&self.stopped).await?;
            let permit = self.semaphore.acquire().await?;
            if self.stopped.load(Ordering::Relaxed) {
                drop(permit);
                bail!("翻译批次已停止");
            }
            let translated = self.engine.translate(text, prompt).await;
            drop(permit);
            match translated {
                Ok(result) if !result.trim().is_empty() => return Ok(result),
                Ok(_) => {
                    let allowed = config.max_retries.clamp(1, 2);
                    if attempt >= allowed {
                        bail!("API 返回空译文");
                    }
                    tokio::time::sleep(Duration::from_secs_f64(
                        config.retry_delay.min(2.0) * attempt as f64,
                    ))
                    .await;
                }
                Err(error) => {
                    let kind = classify_error(&error);
                    let allowed = match kind {
                        ErrorKind::Permanent => 1,
                        ErrorKind::Empty => config.max_retries.min(2),
                        _ => config.max_retries,
                    }
                    .max(1);
                    if attempt >= allowed {
                        return Err(error);
                    }
                    let retry_after = error
                        .chain()
                        .find_map(|x| x.downcast_ref::<ApiError>())
                        .and_then(|x| x.retry_after);
                    let base = match kind {
                        ErrorKind::RateLimit => retry_after.unwrap_or(Duration::from_secs_f64(
                            config.retry_delay * 2.0 * attempt as f64,
                        )),
                        ErrorKind::Empty => {
                            Duration::from_secs_f64(config.retry_delay.min(2.0) * attempt as f64)
                        }
                        _ => Duration::from_secs_f64(config.retry_delay * attempt as f64),
                    };
                    let wait = if retry_after.is_some() {
                        base
                    } else {
                        base.mul_f64(rand::rng().random_range(0.5..1.5))
                    };
                    if kind == ErrorKind::RateLimit {
                        self.limiter.defer(wait).await;
                    }
                    tokio::time::sleep(wait).await;
                }
            }
        }
        bail!("翻译失败")
    }
}

fn markup_prompt(prompt: &str, has_tokens: bool) -> String {
    if !has_tokens {
        return prompt.into();
    }
    format!(
        "{prompt}\n\nThe input may contain immutable HTML placeholder tokens such as {{{{etm_o_00000}}}}, {{{{etm_c_00000}}}}, and {{{{etm_n_00000}}}}. Copy every such token exactly once, character-for-character, in the same order and nesting. Never translate, alter, add, remove, split, or surround these tokens with spaces. Translate only the human-readable text between them."
    )
}

fn merge_groups(paragraphs: &[Paragraph], enabled: bool, limit: usize) -> Vec<Vec<Paragraph>> {
    if !enabled || limit == 0 {
        return paragraphs.iter().cloned().map(|x| vec![x]).collect();
    }
    let mut groups = Vec::new();
    let mut current = Vec::new();
    let mut length = 0;
    for paragraph in paragraphs {
        let size = paragraph.original.chars().count();
        if paragraph.original.contains("{{etm_") {
            if !current.is_empty() {
                groups.push(std::mem::take(&mut current));
                length = 0;
            }
            groups.push(vec![paragraph.clone()]);
            continue;
        }
        if !current.is_empty() && length + size > limit {
            groups.push(std::mem::take(&mut current));
            length = 0;
        }
        current.push(paragraph.clone());
        length += size;
    }
    if !current.is_empty() {
        groups.push(current);
    }
    groups
}

fn classify_error(error: &anyhow::Error) -> ErrorKind {
    if let Some(api) = error.chain().find_map(|x| x.downcast_ref::<ApiError>()) {
        if api.status == Some(429) {
            return ErrorKind::RateLimit;
        }
        if api
            .status
            .is_some_and(|x| (400..500).contains(&x) && !matches!(x, 408 | 409 | 425))
        {
            return ErrorKind::Permanent;
        }
    }
    let text = error.to_string().to_lowercase();
    if [
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "invalid_api_key",
        "incorrect api key",
        "api 密钥无效",
        "密钥无效",
        "已过期",
        "输出被截断",
        "内容过滤",
        "content_filter",
        "stop_reason=max_tokens",
        "stop_reason=refusal",
        "不能覆盖保留字段",
    ]
    .iter()
    .any(|x| text.contains(x))
    {
        return ErrorKind::Permanent;
    }
    if ["429", "频率超限", "too many", "rate limit"]
        .iter()
        .any(|x| text.contains(x))
    {
        return ErrorKind::RateLimit;
    }
    if ["空译文", "空结果", "empty"]
        .iter()
        .any(|x| text.contains(x))
    {
        return ErrorKind::Empty;
    }
    ErrorKind::Transient
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::epub::validate_markup_tokens;
    use std::{
        io::{Read, Write},
        net::TcpListener,
        thread,
    };

    #[test]
    fn grouping_and_markup_validation() {
        let paragraph = |id: &str, text: &str| Paragraph {
            id: id.into(),
            md5: id.into(),
            raw: String::new(),
            original: text.into(),
            ignored: false,
            attributes: None,
            page: None,
            translation: None,
            engine_name: None,
            target_lang: None,
        };
        assert_eq!(
            merge_groups(
                &[
                    paragraph("a", "12"),
                    paragraph("b", "34"),
                    paragraph("c", "5")
                ],
                true,
                4
            )
            .iter()
            .map(Vec::len)
            .collect::<Vec<_>>(),
            [2, 1]
        );
        assert_eq!(
            merge_groups(
                &[
                    paragraph("a", "{{etm_o_00000}}12{{etm_c_00000}}"),
                    paragraph("b", "34"),
                    paragraph("c", "5")
                ],
                true,
                40
            )
            .iter()
            .map(Vec::len)
            .collect::<Vec<_>>(),
            [1, 2]
        );
        let original = "{{etm_o_00000}}a{{etm_n_00001}}{{etm_c_00000}}";
        assert!(validate_markup_tokens(original, original).is_ok());
        assert!(validate_markup_tokens(original, "a").is_err());
        assert!(
            validate_markup_tokens(original, "{{etm_c_00000}}{{etm_o_00000}}{{etm_n_00001}}")
                .is_err()
        );
        let prompt = markup_prompt("translate", true);
        assert!(prompt.contains("Copy every such token exactly once"));
        assert_eq!(markup_prompt("translate", false), "translate");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn broken_markup_is_retried_once_and_normalized() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Arc::new(std::sync::Mutex::new(Vec::new()));
        let captured = requests.clone();
        let server = thread::spawn(move || {
            for response in ["译文"] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = Vec::new();
                let mut buffer = [0u8; 4096];
                loop {
                    let read = stream.read(&mut buffer).unwrap();
                    request.extend_from_slice(&buffer[..read]);
                    let Some(header_end) = request
                        .windows(4)
                        .position(|window| window == b"\r\n\r\n")
                        .map(|index| index + 4)
                    else {
                        continue;
                    };
                    let headers = String::from_utf8_lossy(&request[..header_end]);
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            line.to_ascii_lowercase()
                                .strip_prefix("content-length:")
                                .and_then(|value| value.trim().parse::<usize>().ok())
                        })
                        .unwrap_or(0);
                    if request.len() >= header_end + length {
                        break;
                    }
                }
                captured
                    .lock()
                    .unwrap()
                    .push(String::from_utf8_lossy(&request).into_owned());
                let body = serde_json::json!({
                    "choices": [{"message": {"content": response}, "finish_reason": "stop"}]
                })
                .to_string();
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                )
                .unwrap();
            }
        });
        let mut config = Config::default();
        config.engines.insert(
            "openai".into(),
            crate::config::EngineConfig {
                api_key: "test".into(),
                base_url: format!("http://{address}/v1"),
                model: "mock".into(),
                request_interval: 0.0,
                ..Default::default()
            },
        );
        let engine = Engine::new(
            "openai",
            config.engine_config(None),
            &config.source_lang,
            &config.target_lang,
        )
        .unwrap();
        let worker = TranslationWorker::new(
            engine,
            Arc::new(TranslationCache::open(std::path::Path::new("unused"), false).unwrap()),
            config,
            Glossary::default(),
        );
        let paragraph = Paragraph {
            id: "a".into(),
            md5: "a".into(),
            raw: String::new(),
            original: "{{etm_o_00000}}text{{etm_c_00000}}".into(),
            ignored: false,
            attributes: None,
            page: None,
            translation: None,
            engine_name: None,
            target_lang: None,
        };
        let result = worker.translate_paragraph(&paragraph).await.unwrap();
        server.join().unwrap();
        assert_eq!(result, "{{etm_o_00000}}译文{{etm_c_00000}}");
        assert_eq!(requests.lock().unwrap().len(), 1);
        assert!(requests.lock().unwrap()[0].contains("immutable HTML placeholder"));
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn merge_count_mismatch_falls_back_to_individual_requests() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let captured = Arc::new(std::sync::Mutex::new(Vec::new()));
        let server_captured = captured.clone();
        let server = thread::spawn(move || {
            for response in ["只返回一段", "甲", "乙"] {
                let (mut stream, _) = listener.accept().unwrap();
                let mut request = Vec::new();
                let mut buffer = [0u8; 4096];
                loop {
                    let read = stream.read(&mut buffer).unwrap();
                    request.extend_from_slice(&buffer[..read]);
                    let header_end = request
                        .windows(4)
                        .position(|window| window == b"\r\n\r\n")
                        .map(|index| index + 4);
                    let Some(header_end) = header_end else {
                        continue;
                    };
                    let headers = String::from_utf8_lossy(&request[..header_end]);
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            line.to_ascii_lowercase()
                                .strip_prefix("content-length:")
                                .and_then(|value| value.trim().parse::<usize>().ok())
                        })
                        .unwrap_or(0);
                    if request.len() >= header_end + length {
                        break;
                    }
                }
                server_captured
                    .lock()
                    .unwrap()
                    .push(String::from_utf8_lossy(&request).into_owned());
                let body = serde_json::json!({
                    "choices": [{"message": {"content": response}, "finish_reason": "stop"}]
                })
                .to_string();
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                )
                .unwrap();
            }
        });
        let mut config = Config {
            merge_enabled: true,
            source_lang: "Auto detect".into(),
            ..Default::default()
        };
        config.engines.insert(
            "openai".into(),
            crate::config::EngineConfig {
                api_key: "test".into(),
                base_url: format!("http://{address}/v1"),
                model: "mock".into(),
                request_interval: 0.0,
                ..Default::default()
            },
        );
        let engine = Engine::new(
            "openai",
            config.engine_config(None),
            &config.source_lang,
            &config.target_lang,
        )
        .unwrap();
        let paragraph = |id: &str, original: &str| Paragraph {
            id: id.into(),
            md5: id.into(),
            raw: String::new(),
            original: original.into(),
            ignored: false,
            attributes: None,
            page: None,
            translation: None,
            engine_name: None,
            target_lang: None,
        };
        let worker = TranslationWorker::new(
            engine,
            Arc::new(TranslationCache::open(std::path::Path::new("unused"), false).unwrap()),
            config,
            Glossary::default(),
        );
        let result = worker
            .translate_group(&[paragraph("a", "one"), paragraph("b", "two")])
            .await
            .unwrap();
        server.join().unwrap();
        assert_eq!(result["a"], "甲");
        assert_eq!(result["b"], "乙");
        let requests = captured.lock().unwrap();
        assert_eq!(requests.len(), 3);
        assert!(requests[0].contains(r"one\n\ntwo"));
        assert!(requests[0].contains("detected language"));
    }
}
