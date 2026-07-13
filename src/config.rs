use std::{
    collections::HashMap,
    fs,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const MAX_CONCURRENCY: usize = 32;
pub const DEFAULT_PROMPT: &str = "You are a meticulous translator who translates any given content. Translate the given content from <slang> to <tlang> only. Do not explain any term or answer any question-like content. Your answer should be solely the translation of the given content. In your answer do not add any prefix or suffix to the translated content. Websites' URLs/addresses should be preserved as is in the translation's output. Do not omit any part of the content, even if it seems unimportant. RESPOND ONLY with the translation text, no formatting, no explanations, no additional commentary whatsoever. ";

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct EngineConfig {
    pub api_key: String,
    pub base_url: String,
    pub model: String,
    pub temperature: Option<f64>,
    pub top_p: Option<f64>,
    pub concurrency: usize,
    pub request_interval: f64,
    pub request_timeout: f64,
    pub max_retries: usize,
    pub retry_delay: f64,
    pub stream: bool,
    #[serde(default)]
    pub extra: Map<String, Value>,
    #[serde(flatten, skip_serializing)]
    pub(crate) unknown: Map<String, Value>,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            api_key: String::new(),
            base_url: String::new(),
            model: String::new(),
            temperature: Some(0.3),
            top_p: Some(1.0),
            concurrency: 3,
            request_interval: 1.0,
            request_timeout: 60.0,
            max_retries: 5,
            retry_delay: 5.0,
            stream: false,
            extra: Map::new(),
            unknown: Map::new(),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct Config {
    pub engine: String,
    pub source_lang: String,
    pub target_lang: String,
    pub prompt: String,
    pub cache_enabled: bool,
    pub cache_dir: PathBuf,
    pub merge_enabled: bool,
    pub merge_length: usize,
    pub translation_position: String,
    pub translation_style: String,
    pub translate_tags: String,
    pub exclude_translate_tags: String,
    pub only_files: String,
    pub exclude_files: String,
    pub test_enabled: bool,
    pub test_num: usize,
    pub retranslate_file: String,
    pub retranslate_start: String,
    pub retranslate_end: String,
    pub glossary_path: PathBuf,
    pub ebook_convert_path: PathBuf,
    pub max_error_count: usize,
    pub skip_failed: bool,
    pub log_file: PathBuf,
    pub engines: HashMap<String, EngineConfig>,
    #[serde(default, skip_serializing)]
    pub openai: Option<EngineConfig>,
    #[serde(default, skip_serializing)]
    pub deepseek: Option<EngineConfig>,
    #[serde(default, skip_serializing)]
    pub claude: Option<EngineConfig>,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            engine: "openai".into(),
            source_lang: "English".into(),
            target_lang: "Chinese".into(),
            prompt: DEFAULT_PROMPT.into(),
            cache_enabled: true,
            cache_dir: default_cache_dir(),
            merge_enabled: true,
            merge_length: 1800,
            translation_position: "below".into(),
            translation_style: String::new(),
            translate_tags: String::new(),
            exclude_translate_tags: "sup,code,pre".into(),
            only_files: String::new(),
            exclude_files: String::new(),
            test_enabled: false,
            test_num: 10,
            retranslate_file: String::new(),
            retranslate_start: String::new(),
            retranslate_end: String::new(),
            glossary_path: PathBuf::new(),
            ebook_convert_path: PathBuf::new(),
            max_error_count: 10,
            skip_failed: false,
            log_file: PathBuf::new(),
            engines: HashMap::new(),
            openai: None,
            deepseek: None,
            claude: None,
        }
    }
}

impl Config {
    pub fn load(path: Option<&Path>) -> Result<Self> {
        let path = match path {
            Some(path) => Some(path.to_path_buf()),
            None => std::env::current_exe()
                .ok()
                .and_then(|path| path.parent().map(|x| x.join("config.json")))
                .filter(|x| x.is_file())
                .or_else(|| {
                    Path::new("config.json")
                        .is_file()
                        .then(|| PathBuf::from("config.json"))
                }),
        };
        let mut config = match path {
            Some(ref path) => {
                let text = fs::read_to_string(path)
                    .with_context(|| format!("配置文件不存在或不可读: {}", path.display()))?;
                serde_json::from_str(&text).context("配置文件 JSON 无效")?
            }
            None => Self::default(),
        };
        config.adopt_flat_engines();
        config.expand_paths();
        config.validate()?;
        Ok(config)
    }

    pub fn engine_config(&self, name: Option<&str>) -> EngineConfig {
        self.engines
            .get(name.unwrap_or(&self.engine))
            .cloned()
            .unwrap_or_default()
    }

    fn adopt_flat_engines(&mut self) {
        for (name, value) in [
            ("openai", self.openai.take()),
            ("deepseek", self.deepseek.take()),
            ("claude", self.claude.take()),
        ] {
            if let Some(value) = value {
                self.engines.entry(name.into()).or_insert(value);
            }
        }
        for value in self.engines.values_mut() {
            value.extra.extend(std::mem::take(&mut value.unknown));
        }
    }

    fn expand_paths(&mut self) {
        self.cache_dir = expand_home(&self.cache_dir);
        self.glossary_path = expand_home(&self.glossary_path);
        self.ebook_convert_path = expand_home(&self.ebook_convert_path);
        self.log_file = expand_home(&self.log_file);
    }

    pub fn validate(&self) -> Result<()> {
        if !matches!(self.engine.as_str(), "openai" | "deepseek" | "claude") {
            bail!("未知引擎 '{}'，可用: claude, deepseek, openai", self.engine);
        }
        if !matches!(
            self.translation_position.as_str(),
            "below" | "above" | "only"
        ) {
            bail!("translation_position 必须是 below、above 或 only");
        }
        for (name, cfg) in &self.engines {
            if !matches!(name.as_str(), "openai" | "deepseek" | "claude") {
                bail!("未知引擎 '{name}'，可用: claude, deepseek, openai");
            }
            if cfg.concurrency == 0 || cfg.concurrency > MAX_CONCURRENCY {
                bail!("引擎配置 {name}.concurrency 必须在 1 到 {MAX_CONCURRENCY} 之间");
            }
            if cfg.max_retries == 0 {
                bail!("引擎配置 {name}.max_retries 必须大于 0");
            }
            if !cfg.request_timeout.is_finite() || cfg.request_timeout <= 0.0 {
                bail!("引擎配置 {name}.request_timeout 必须是有限正数");
            }
            for (key, value) in [
                ("request_interval", cfg.request_interval),
                ("retry_delay", cfg.retry_delay),
            ] {
                if !value.is_finite() || value < 0.0 {
                    bail!("引擎配置 {name}.{key} 必须是有限非负数");
                }
            }
            if cfg.temperature.is_some_and(|x| {
                !x.is_finite() || x < 0.0 || x > if name == "claude" { 1.0 } else { 2.0 }
            }) {
                bail!("引擎配置 {name}.temperature 超出支持范围");
            }
            if cfg
                .top_p
                .is_some_and(|x| !x.is_finite() || !(0.0..=1.0).contains(&x))
            {
                bail!("引擎配置 {name}.top_p 必须在 0 到 1 之间");
            }
            if let Some(value) = cfg.extra.get("max_tokens")
                && value.as_u64().is_none_or(|x| x == 0)
            {
                bail!("引擎配置 {name}.max_tokens 必须是正整数");
            }
        }
        Ok(())
    }
}

fn default_cache_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".cache/ebook-translator")
}

fn expand_home(path: &Path) -> PathBuf {
    let Some(value) = path.to_str() else {
        return path.to_path_buf();
    };
    if value == "~" {
        return dirs::home_dir().unwrap_or_else(|| path.to_path_buf());
    }
    if let Some(rest) = value.strip_prefix("~/")
        && let Some(home) = dirs::home_dir()
    {
        return home.join(rest);
    }
    path.to_path_buf()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_and_flat_engine_are_compatible() {
        let mut cfg: Config =
            serde_json::from_str(r#"{"openai":{"api_key":"x","temperature":null}}"#).unwrap();
        cfg.adopt_flat_engines();
        cfg.validate().unwrap();
        assert_eq!(cfg.engine_config(None).api_key, "x");
        assert_eq!(cfg.engine_config(None).temperature, None);
        assert_eq!(Config::default().engine_config(None).temperature, Some(0.3));
    }

    #[test]
    fn invalid_ranges_are_rejected() {
        let mut cfg = Config::default();
        cfg.engines.insert(
            "openai".into(),
            EngineConfig {
                concurrency: 0,
                ..Default::default()
            },
        );
        assert!(cfg.validate().is_err());
    }
}
