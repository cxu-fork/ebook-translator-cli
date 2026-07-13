use std::{
    collections::{HashMap, HashSet},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};

use anyhow::{Result, anyhow, bail};
use futures_util::{StreamExt, stream::FuturesUnordered};
use rand::RngExt;
use serde_json::{Value, json};
use tokio::sync::{Mutex, Semaphore};

use crate::{
    cache::{Paragraph, TranslationCache},
    config::{Config, MAX_CONCURRENCY},
    engine::{ApiError, Engine},
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
            let result = self
                .translate_one(
                    &self.glossary.apply(&paragraph.original),
                    &self.config.prompt,
                )
                .await?;
            return Ok([(paragraph.id.clone(), self.glossary.restore(result.trim()))].into());
        }
        let segments = group.iter().enumerate().map(|(id, paragraph)| json!({"id": id.to_string(), "text": self.glossary.apply(&paragraph.original)})).collect::<Vec<_>>();
        let prompt = format!(
            "{}\n\nYou will receive a JSON array of segments. Translate each segment text independently and preserve all segment ids. Return only valid JSON in this exact shape: [{{\"id\":\"0\",\"text\":\"translated text\"}}]. Do not wrap it in Markdown and do not add explanations.",
            self.config.prompt
        );
        let response = self
            .translate_one(&serde_json::to_string(&segments)?, &prompt)
            .await?;
        let expected = (0..group.len()).map(|x| x.to_string()).collect();
        let merged = match parse_merged_result(&response, &expected) {
            Ok(result) => result,
            Err(error) => {
                eprintln!("  合并翻译解析失败，回退逐段: {error}");
                let mut fallback = HashMap::new();
                for paragraph in group {
                    let result = self
                        .translate_one(
                            &self.glossary.apply(&paragraph.original),
                            &self.config.prompt,
                        )
                        .await?;
                    fallback.insert(paragraph.id.clone(), self.glossary.restore(result.trim()));
                }
                return Ok(fallback);
            }
        };
        Ok(group
            .iter()
            .enumerate()
            .map(|(id, paragraph)| {
                (
                    paragraph.id.clone(),
                    self.glossary.restore(merged[&id.to_string()].trim()),
                )
            })
            .collect())
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

fn merge_groups(paragraphs: &[Paragraph], enabled: bool, limit: usize) -> Vec<Vec<Paragraph>> {
    if !enabled || limit == 0 {
        return paragraphs.iter().cloned().map(|x| vec![x]).collect();
    }
    let mut groups = Vec::new();
    let mut current = Vec::new();
    let mut length = 0;
    for paragraph in paragraphs {
        let size = paragraph.original.chars().count();
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

fn parse_merged_result(
    response: &str,
    expected: &HashSet<String>,
) -> Result<HashMap<String, String>> {
    let mut text = response.trim();
    if text.starts_with("```") {
        text = text
            .strip_prefix("```json")
            .or_else(|| text.strip_prefix("```JSON"))
            .or_else(|| text.strip_prefix("```"))
            .unwrap_or(text)
            .trim();
        text = text.strip_suffix("```").unwrap_or(text).trim();
    }
    if let Some(start) = [text.find('['), text.find('{')].into_iter().flatten().min() {
        text = &text[start..];
    }
    let mut data: Value = serde_json::from_str(text)?;
    if let Some(value) = data
        .get("segments")
        .or_else(|| data.get("translations"))
        .filter(|x| x.is_array())
    {
        data = value.clone();
    }
    let mut result = HashMap::new();
    match data {
        Value::Object(values) => {
            for (key, value) in values {
                result.insert(
                    key,
                    value
                        .as_str()
                        .ok_or_else(|| anyhow!("合并翻译返回的译文必须是字符串"))?
                        .to_owned(),
                );
            }
        }
        Value::Array(values) => {
            for value in values {
                let object = value
                    .as_object()
                    .ok_or_else(|| anyhow!("合并翻译返回的数组元素不是对象"))?;
                let id = object
                    .get("id")
                    .map(|x| {
                        x.as_str()
                            .map(str::to_owned)
                            .unwrap_or_else(|| x.to_string())
                    })
                    .unwrap_or_default();
                let text = object
                    .get("text")
                    .or_else(|| object.get("translation"))
                    .and_then(Value::as_str)
                    .ok_or_else(|| anyhow!("合并翻译返回的译文必须是字符串"))?;
                result.insert(id, text.to_owned());
            }
        }
        _ => bail!("合并翻译返回格式无效"),
    }
    if result.keys().cloned().collect::<HashSet<_>>() != *expected {
        bail!("合并翻译返回的 segment id 不完整");
    }
    if result.values().any(|x| x.trim().is_empty()) {
        bail!("合并翻译返回空译文");
    }
    Ok(result)
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

    #[test]
    fn grouping_and_merge_parser_cover_fallback_shapes() {
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
        let expected = ["0".into(), "1".into()].into();
        let result = parse_merged_result("```json\n{\"translations\":[{\"id\":\"0\",\"text\":\"a\"},{\"id\":\"1\",\"translation\":\"b\"}]}\n```", &expected).unwrap();
        assert_eq!(result["1"], "b");
        assert!(parse_merged_result("{\"0\": 1}", &["0".into()].into()).is_err());
    }
}
