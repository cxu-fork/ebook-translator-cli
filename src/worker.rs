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
    fallback: Vec<Arc<Engine>>,
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
            fallback: Vec::new(),
            cache,
            config: Arc::new(config),
            glossary: Arc::new(glossary),
            limiter: Arc::new(RateLimiter::new(interval)),
            semaphore: Arc::new(Semaphore::new(concurrency)),
            stopped: Arc::new(AtomicBool::new(false)),
            abort_count: Arc::new(AtomicUsize::new(0)),
        }
    }



    pub fn with_fallback(mut self, engine: Engine) -> Self {
        self.fallback.push(Arc::new(engine));
        self
    }

    fn message(&self, message: impl AsRef<str>) {
        use std::io::Write;
        let mut stderr = std::io::stderr().lock();
        let _ = writeln!(&mut stderr, "{}", message.as_ref());
        let _ = stderr.flush();
    }

    fn record_failure(&self) {
        // ponytail: never stop the whole batch on repeated failures; each
        // paragraph independently exhausts primary + fallback chain.
        let _ = self.abort_count.fetch_add(1, Ordering::Relaxed);
    }

    pub async fn translate_batch(&self, paragraphs: Vec<Paragraph>) -> (usize, usize) {
        let groups = merge_groups(
            &paragraphs,
            self.config.merge_enabled,
            self.config.merge_length,
        );
        let total_groups = groups.len();
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
        let mut last_heartbeat = Instant::now();
        while let Some((group, result)) = tasks.next().await {
            if done > 0 && done % 25 == 0 {
                let now = Instant::now();
                if now.duration_since(last_heartbeat) >= Duration::from_secs(30) {
                    self.message(format!(
                        "  进度: {done}/{total_groups} 段, 失败 {failed}"
                    ));
                    last_heartbeat = now;
                }
            }
            match result {
                Ok(translations) => {
                    let updates = group
                        .iter()
                        .map(|paragraph| {
                            translations
                                .get(&paragraph.id)
                                .filter(|value| !value.trim().is_empty())
                                .cloned()
                                .map(|translation| {
                                    (
                                        paragraph.id.clone(),
                                        translation,
                                        self.config.engine.clone(),
                                        self.config.target_lang.clone(),
                                    )
                                })
                        })
                        .collect::<Option<Vec<_>>>();
                    let saved = updates
                        .ok_or_else(|| anyhow!("合并翻译缺少段落或返回空译文"))
                        .and_then(|updates| self.cache.update_translations(&updates));
                    match saved {
                        Ok(()) => {
                            done += group.len();
                            self.abort_count.store(0, Ordering::Relaxed);
                        }
                        Err(error) => {
                            failed += group.len();
                            self.message(format!("  缓存写入失败: {error:#}"));
                            self.record_failure();
                        }
                    }
                }
                Err(error) => {
                    failed += group.len();
                    self.message(format!(
                        "  翻译失败: {} -> {error:#}",
                        group
                            .first()
                            .map(|x| x.original.chars().take(60).collect::<String>())
                            .unwrap_or_default()
                    ));
                    self.record_failure();
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
        // ponytail: use simple double-newline separator instead of JSON merge —
        // free models handle plain text far more reliably than structured JSON
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
            for (paragraph, translation) in group.iter().zip(parts) {
                let restored = self.glossary.restore(&paragraph.original, translation)?;
                let translation = accept_markup_translation(&paragraph.original, &restored)?;
                translations.insert(paragraph.id.clone(), translation);
            }
            return Ok(translations);
        }
        self.message(format!(
            "  合并翻译段落数不匹配（预期 {}, 实际 {}），回退逐段",
            group.len(),
            parts.len()
        ));
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
        if self.fallback.is_empty() {
            // No fallback: keep a hard deadline so one paragraph cannot stall the batch.
            tokio::time::timeout(
                Duration::from_secs(180),
                self.translate_paragraph_inner(paragraph, &original),
            )
            .await
            .map_err(|_| anyhow!("段落翻译超时（180 秒）"))?
        } else {
            // With a fallback chain, the last fallback retries forever by design.
            self.translate_paragraph_inner(paragraph, &original).await
        }
    }

    async fn translate_paragraph_inner(
        &self,
        paragraph: &Paragraph,
        original: &str,
    ) -> Result<String> {
        let mut last_error = match self
            .translate_paragraph_with(&self.engine, paragraph, original)
            .await
        {
            Ok(translation) => return Ok(translation),
            Err(error) => error,
        };
        let total = self.fallback.len();
        for (index, fallback) in self.fallback.iter().enumerate() {
            self.message(format!(
                "  渠道 {} 失败，使用兜底渠道 #{}: {last_error:#}",
                index + 1,
                index + 2
            ));
            match self
                .translate_paragraph_with(fallback, paragraph, original)
                .await
            {
                Ok(translation) => return Ok(translation),
                Err(error) => last_error = error,
            }
            // The LAST fallback must never give up: retry forever until it
            // produces a valid translation. It is slow but reliable by design.
            if index + 1 == total {
                self.message(format!(
                    "  最后兜底渠道失败，无限重试: {last_error:#}"
                ));
                loop {
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    match self
                        .translate_paragraph_with(fallback, paragraph, original)
                        .await
                    {
                        Ok(translation) => return Ok(translation),
                        Err(error) => {
                            last_error = error;
                            self.message(format!("  最后兜底渠道重试失败: {last_error:#}"));
                        }
                    }
                }
            }
        }
        Err(last_error)
    }

    async fn translate_paragraph_with(
        &self,
        engine: &Engine,
        paragraph: &Paragraph,
        original: &str,
    ) -> Result<String> {
        let has_markup = paragraph.original.contains("{{etm_");
        let has_glossary = original.contains("{{etg_");
        let prompt = protected_prompt(self.config.effective_prompt(), has_markup, has_glossary);
        for attempt in 0..=usize::from(has_markup || has_glossary) {
            let result = self.translate_one_with(engine, original, &prompt).await?;
            let restored = match self.glossary.restore(original, result.trim()) {
                Ok(restored) => restored,
                Err(error) if attempt == 0 => {
                    self.message(format!("  模型损坏术语占位符，自动重试一次: {error}"));
                    continue;
                }
                Err(error) => return Err(error),
            };
            match accept_markup_translation(&paragraph.original, &restored) {
                Ok(translation) => return Ok(translation),
                Err(error) if attempt == 0 && has_markup => {
                    self.message(format!("  模型损坏 HTML 占位符，自动重试一次: {error}"));
                }
                Err(error) => return Err(error),
            }
        }
        unreachable!()
    }

    async fn translate_one(&self, text: &str, prompt: &str) -> Result<String> {
        self.translate_one_with(&self.engine, text, prompt).await
    }

    async fn translate_one_with(
        &self,
        engine: &Engine,
        text: &str,
        prompt: &str,
    ) -> Result<String> {
        let config = self.config.engine_config(None);
        for attempt in 1..=config.max_retries.max(1) {
            self.limiter.acquire(&self.stopped).await?;
            let permit = self.semaphore.acquire().await?;
            if self.stopped.load(Ordering::Relaxed) {
                drop(permit);
                bail!("翻译批次已停止");
            }
            // Hard cap each HTTP attempt so a hung gateway cannot stall a paragraph forever.
            let attempt_timeout = Duration::from_secs_f64(
                config.request_timeout.clamp(10.0, 60.0),
            );
            let translated = tokio::time::timeout(attempt_timeout, engine.translate(text, prompt))
                .await
                .map_err(|_| anyhow!("请求超时（{attempt_timeout:?}）"))
                .and_then(|result| result);
            drop(permit);
            match translated {
                Ok(result) if !result.trim().is_empty() => return Ok(result),
                Ok(_) => {
                    let allowed = config.max_retries.clamp(1, 2);
                    if attempt >= allowed {
                        bail!("API 返回空译文");
                    }
                    self.sleep_or_stop(Duration::from_secs_f64(
                        config.retry_delay.min(2.0) * attempt as f64,
                    ))
                    .await?;
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
                    let server_wait = retry_after;
                    let base = match kind {
                        ErrorKind::RateLimit => {
                            Duration::from_secs_f64(config.retry_delay * 2.0 * attempt as f64)
                        }
                        ErrorKind::Empty => {
                            Duration::from_secs_f64(config.retry_delay.min(2.0) * attempt as f64)
                        }
                        _ => Duration::from_secs_f64(config.retry_delay * attempt as f64),
                    };
                    let wait = if let Some(wait) = server_wait {
                        wait
                    } else {
                        base.mul_f64(rand::rng().random_range(0.5..1.5))
                    };
                    if kind == ErrorKind::RateLimit {
                        self.limiter.defer(wait).await;
                    }
                    self.sleep_or_stop(wait).await?;
                }
            }
        }
        bail!("翻译失败")
    }

    async fn sleep_or_stop(&self, wait: Duration) -> Result<()> {
        let deadline = Instant::now() + wait;
        loop {
            if self.stopped.load(Ordering::Relaxed) {
                bail!("翻译批次已停止");
            }
            let now = Instant::now();
            if now >= deadline {
                return Ok(());
            }
            tokio::time::sleep((deadline - now).min(Duration::from_millis(250))).await;
        }
    }
}

fn protected_prompt(prompt: &str, has_markup: bool, has_glossary: bool) -> String {
    if !has_markup && !has_glossary {
        return prompt.into();
    }
    let mut tokens = Vec::new();
    if has_markup {
        tokens.push("HTML tokens such as {{etm_o_00000}}, {{etm_c_00000}}, and {{etm_n_00000}}");
    }
    if has_glossary {
        tokens.push("glossary tokens such as {{etg_0123456789ab_000000}}");
    }
    format!(
        "{prompt}\n\nThe input contains immutable {}. Copy every token exactly once, character-for-character, in the same order. Never translate, alter, add, remove, split, or surround these tokens with spaces. Translate only the human-readable text between them.",
        tokens.join(" and ")
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
        if matches!(api.status, Some(401) | Some(403)) {
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
        let prompt = protected_prompt("translate", true, false);
        assert!(prompt.contains("Copy every token exactly once"));
        assert_eq!(protected_prompt("translate", false, false), "translate");
        assert!(protected_prompt("translate", false, true).contains("glossary tokens"));
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
        assert!(requests.lock().unwrap()[0].contains("immutable HTML tokens"));
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn merge_count_mismatch_falls_back_to_individual_requests() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let captured = Arc::new(std::sync::Mutex::new(Vec::new()));
        let server_captured = captured.clone();
        let server = thread::spawn(move || {
            for response in [r#"[{"id":"0","text":"只返回一段"}]"#, "甲", "乙"] {
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
        assert!(requests[0].contains("one\\n\\ntwo"));
        assert!(requests[0].contains("detected language"));
    }
}
