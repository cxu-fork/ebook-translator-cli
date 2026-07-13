use std::{
    collections::HashSet,
    fmt,
    time::{Duration, SystemTime},
};

use anyhow::{Context, Result, anyhow, bail};
use futures_util::StreamExt;
use reqwest::{Client, Response, Url};
use serde_json::{Map, Value, json};

use crate::config::EngineConfig;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EngineKind {
    OpenAi,
    DeepSeek,
    Claude,
}

#[derive(Debug)]
pub struct ApiError {
    pub status: Option<u16>,
    pub retry_after: Option<Duration>,
    message: String,
}

impl fmt::Display for ApiError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ApiError {}

pub struct Engine {
    pub kind: EngineKind,
    pub config: EngineConfig,
    pub endpoint: Url,
    pub model: String,
    source_lang: String,
    target_lang: String,
    client: Client,
}

impl Engine {
    pub fn new(
        name: &str,
        config: EngineConfig,
        source_lang: &str,
        target_lang: &str,
    ) -> Result<Self> {
        let (kind, default_base, suffix, default_model) = match name {
            "openai" => (
                EngineKind::OpenAi,
                "https://api.openai.com/v1",
                "/chat/completions",
                "gpt-4o-mini",
            ),
            "deepseek" => (
                EngineKind::DeepSeek,
                "https://api.deepseek.com/v1",
                "/chat/completions",
                "deepseek-chat",
            ),
            "claude" => (
                EngineKind::Claude,
                "https://api.anthropic.com",
                "/v1/messages",
                "claude-sonnet-4-20250514",
            ),
            _ => bail!("未知翻译引擎: {name}"),
        };
        if config.api_key.is_empty() {
            bail!(
                "{} 引擎需要设置 api_key",
                if kind == EngineKind::Claude {
                    "Anthropic"
                } else {
                    "OpenAI"
                }
            );
        }
        let base = if config.base_url.is_empty() {
            default_base
        } else {
            &config.base_url
        };
        let endpoint = endpoint_url(base, suffix)?;
        let model = if !config.model.is_empty() {
            config.model.clone()
        } else if config.base_url.is_empty() || base.trim_end_matches('/') == default_base {
            default_model.into()
        } else {
            String::new()
        };
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs_f64(config.request_timeout))
            .pool_max_idle_per_host(config.concurrency.max(4))
            .build()?;
        Ok(Self {
            kind,
            config,
            endpoint,
            model,
            source_lang: source_lang.into(),
            target_lang: target_lang.into(),
            client,
        })
    }

    pub async fn translate(&self, text: &str, prompt: &str) -> Result<String> {
        let prompt = prompt
            .replace("<tlang>", &self.target_lang)
            .replace("<slang>", &self.source_lang);
        let body = self.body(text, &prompt, self.config.stream)?;
        let mut request = self
            .client
            .post(self.endpoint.clone())
            .json(&body)
            .header("content-type", "application/json");
        request = match self.kind {
            EngineKind::Claude => request
                .header("x-api-key", &self.config.api_key)
                .header("anthropic-version", "2023-06-01"),
            _ => request.bearer_auth(&self.config.api_key),
        };
        let response = request.send().await?;
        let response = check_status(response).await?;
        let result = if self.config.stream {
            self.parse_stream(response).await?
        } else {
            self.parse_response(response.json().await?).await?
        };
        let result = result.trim().to_owned();
        if result.is_empty() {
            bail!("API 返回空译文");
        }
        Ok(result)
    }

    pub fn body(&self, text: &str, prompt: &str, stream: bool) -> Result<Value> {
        let reserved: HashSet<&str> = match self.kind {
            EngineKind::Claude => [
                "model",
                "system",
                "messages",
                "temperature",
                "top_p",
                "stream",
            ]
            .into_iter()
            .collect(),
            _ => ["messages", "model", "temperature", "top_p", "stream"]
                .into_iter()
                .collect(),
        };
        if let Some(key) = self
            .config
            .extra
            .keys()
            .find(|key| reserved.contains(key.as_str()))
        {
            bail!("引擎 extra 不能覆盖保留字段: {key}");
        }
        let mut body = Map::new();
        match self.kind {
            EngineKind::Claude => {
                body.insert("model".into(), json!(self.model));
                body.insert(
                    "max_tokens".into(),
                    self.config
                        .extra
                        .get("max_tokens")
                        .cloned()
                        .unwrap_or(json!(4096)),
                );
                body.insert("system".into(), json!(prompt));
                body.insert("messages".into(), json!([{"role":"user", "content":text}]));
            }
            _ => {
                body.insert(
                    "messages".into(),
                    json!([
                        {"role":"system", "content":prompt}, {"role":"user", "content":text}
                    ]),
                );
                if !self.model.is_empty() {
                    body.insert("model".into(), json!(self.model));
                }
            }
        }
        if let Some(value) = self.config.temperature {
            body.insert("temperature".into(), json!(value));
        }
        if let Some(value) = self.config.top_p {
            body.insert("top_p".into(), json!(value));
        }
        if stream {
            body.insert("stream".into(), json!(true));
        }
        for (key, value) in &self.config.extra {
            if self.kind == EngineKind::Claude && key == "max_tokens" {
                continue;
            }
            body.insert(key.clone(), value.clone());
        }
        Ok(Value::Object(body))
    }

    async fn parse_response(&self, data: Value) -> Result<String> {
        match self.kind {
            EngineKind::Claude => {
                match data.get("stop_reason").and_then(Value::as_str) {
                    Some("max_tokens") => bail!("API 输出被截断 (stop_reason=max_tokens)"),
                    Some("refusal") => bail!("API 输出被内容过滤 (stop_reason=refusal)"),
                    _ => {}
                }
                let text = data
                    .get("content")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(|block| block.get("text").and_then(Value::as_str))
                    .collect::<String>();
                if text.is_empty() {
                    bail!("API 返回空结果: {}", truncate_json(&data));
                }
                Ok(text)
            }
            _ => {
                let choice = data
                    .get("choices")
                    .and_then(Value::as_array)
                    .and_then(|x| x.first())
                    .ok_or_else(|| anyhow!("API 返回空结果: {}", truncate_json(&data)))?;
                check_openai_finish(choice)?;
                let message = choice.get("message").unwrap_or(&Value::Null);
                if message.get("refusal").is_some_and(|x| !x.is_null()) {
                    bail!("API 输出被内容过滤 (refusal)");
                }
                message
                    .get("content")
                    .and_then(Value::as_str)
                    .filter(|x| !x.is_empty())
                    .or_else(|| choice.get("text").and_then(Value::as_str))
                    .map(str::to_owned)
                    .ok_or_else(|| anyhow!("API 返回空译文: {}", truncate_json(&data)))
            }
        }
    }

    async fn parse_stream(&self, response: Response) -> Result<String> {
        let mut bytes = response.bytes_stream();
        let mut pending = String::new();
        let mut output = String::new();
        let mut completed = false;
        while let Some(chunk) = bytes.next().await {
            pending.push_str(std::str::from_utf8(&chunk?).context("API 流式响应不是 UTF-8")?);
            while let Some(pos) = pending.find('\n') {
                let line = pending[..pos].trim_end_matches('\r').trim().to_owned();
                pending.drain(..=pos);
                parse_sse_line(self.kind, &line, &mut output, &mut completed)?;
            }
        }
        if !pending.trim().is_empty() {
            parse_sse_line(
                self.kind,
                pending.trim_end_matches('\r').trim(),
                &mut output,
                &mut completed,
            )?;
        }
        if !completed {
            bail!("API 流式响应未完整结束");
        }
        Ok(output)
    }
}

fn parse_sse_line(
    kind: EngineKind,
    line: &str,
    output: &mut String,
    completed: &mut bool,
) -> Result<()> {
    if !line.starts_with("data:") {
        return Ok(());
    }
    let data = line[5..].trim();
    if data == "[DONE]" {
        *completed = true;
        return Ok(());
    }
    let event: Value = serde_json::from_str(data).context("API 流式响应 JSON 无效")?;
    match kind {
        EngineKind::Claude => parse_anthropic_event(&event, output, completed),
        _ => parse_openai_event(&event, output, completed),
    }
}

async fn check_status(response: Response) -> Result<Response> {
    if response.status().is_success() {
        return Ok(response);
    }
    let status = response.status().as_u16();
    let retry_after = response
        .headers()
        .get("retry-after")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| {
            value
                .parse::<f64>()
                .ok()
                .map(Duration::from_secs_f64)
                .or_else(|| {
                    httpdate::parse_http_date(value)
                        .ok()
                        .and_then(|at| at.duration_since(SystemTime::now()).ok())
                })
        });
    let body = response.text().await.unwrap_or_default();
    Err(ApiError {
        status: Some(status),
        retry_after,
        message: format!(
            "HTTP {status}: {}",
            body.chars().take(2000).collect::<String>()
        ),
    }
    .into())
}

pub fn endpoint_url(base: &str, suffix: &str) -> Result<Url> {
    let mut url = Url::parse(base).with_context(|| format!("API base_url 无效: {base}"))?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        bail!("API base_url 无效: {base}");
    }
    let path = url.path().trim_end_matches('/');
    if !path.ends_with(suffix) {
        let addition = if suffix.starts_with("/v1/") && path.ends_with("/v1") {
            &suffix[3..]
        } else {
            suffix
        };
        url.set_path(&format!("{path}{addition}"));
    }
    Ok(url)
}

fn check_openai_finish(choice: &Value) -> Result<()> {
    match choice.get("finish_reason").and_then(Value::as_str) {
        Some("length") => bail!("API 输出被截断 (finish_reason=length)"),
        Some("content_filter") => bail!("API 输出被内容过滤 (finish_reason=content_filter)"),
        Some("insufficient_system_resource") => {
            bail!("API 资源暂时不足 (finish_reason=insufficient_system_resource)")
        }
        _ => Ok(()),
    }
}

fn parse_openai_event(event: &Value, output: &mut String, completed: &mut bool) -> Result<()> {
    if let Some(error) = event.get("error") {
        bail!("API 流式错误: {error}");
    }
    let Some(choices) = event.get("choices").and_then(Value::as_array) else {
        if event.get("usage").is_some() || event.get("type").and_then(Value::as_str) == Some("ping")
        {
            return Ok(());
        }
        bail!("API 流式响应缺少 choices");
    };
    let choice = choices
        .first()
        .ok_or_else(|| anyhow!("API 流式响应缺少 choices"))?;
    check_openai_finish(choice)?;
    if choice.get("finish_reason").is_some_and(|x| !x.is_null()) {
        *completed = true;
    }
    let delta = choice
        .get("delta")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow!("API 流式响应 delta 格式无效"))?;
    if delta.get("refusal").is_some_and(|x| !x.is_null()) {
        bail!("API 输出被内容过滤 (refusal)");
    }
    if let Some(value) = delta.get("content") {
        output.push_str(
            value
                .as_str()
                .ok_or_else(|| anyhow!("API 流式译文不是字符串"))?,
        );
    }
    Ok(())
}

fn parse_anthropic_event(event: &Value, output: &mut String, completed: &mut bool) -> Result<()> {
    match event.get("type").and_then(Value::as_str).unwrap_or("") {
        "error" => bail!("API 流式错误: {}", event.get("error").unwrap_or(event)),
        "message_stop" => *completed = true,
        "message_delta" => match event.pointer("/delta/stop_reason").and_then(Value::as_str) {
            Some("max_tokens") => bail!("API 输出被截断 (stop_reason=max_tokens)"),
            Some("refusal") => bail!("API 输出被内容过滤 (stop_reason=refusal)"),
            _ => {}
        },
        "content_block_delta" => {
            if let Some(value) = event.pointer("/delta/text") {
                output.push_str(
                    value
                        .as_str()
                        .ok_or_else(|| anyhow!("API 流式译文不是字符串"))?,
                );
            }
        }
        _ => {}
    }
    Ok(())
}

fn truncate_json(value: &Value) -> String {
    value.to_string().chars().take(500).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(base_url: &str) -> EngineConfig {
        EngineConfig {
            api_key: "x".into(),
            base_url: base_url.into(),
            ..Default::default()
        }
    }

    #[test]
    fn endpoint_is_not_duplicated_and_keeps_query() {
        assert_eq!(
            endpoint_url("https://x/v1/chat/completions?q=1", "/chat/completions")
                .unwrap()
                .as_str(),
            "https://x/v1/chat/completions?q=1"
        );
        assert_eq!(
            endpoint_url("https://x/v1?q=1", "/v1/messages")
                .unwrap()
                .as_str(),
            "https://x/v1/messages?q=1"
        );
        assert!(endpoint_url("x/v1", "/chat/completions").is_err());
    }

    #[test]
    fn custom_openai_endpoint_does_not_force_model() {
        let engine = Engine::new("openai", cfg("https://x/v1"), "en", "zh").unwrap();
        assert!(engine.model.is_empty());
        assert!(engine.body("x", "p", false).unwrap().get("model").is_none());
    }

    #[test]
    fn deepseek_defaults_are_distinct() {
        let engine = Engine::new("deepseek", cfg(""), "en", "zh").unwrap();
        assert_eq!(engine.model, "deepseek-chat");
        assert_eq!(
            engine.endpoint.as_str(),
            "https://api.deepseek.com/v1/chat/completions"
        );
    }
}
