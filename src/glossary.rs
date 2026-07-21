use std::{collections::HashMap, fs, path::Path};

use anyhow::{Context, Result};
use regex::Regex;
use sha2::{Digest, Sha256};

#[derive(Clone, Debug, Default)]
pub struct Glossary {
    pub pairs: Vec<(String, String)>,
    tokens: Vec<String>,
    source_pattern: Option<Regex>,
}

impl Glossary {
    pub fn load(path: &Path, inline: &HashMap<String, String>) -> Result<Self> {
        let mut values = HashMap::new();
        if !path.as_os_str().is_empty() {
            let content = fs::read_to_string(path)
                .with_context(|| format!("术语表文件不存在: {}", path.display()))?;
            let content = content.trim_start_matches('\u{feff}').trim();
            let groups = Regex::new(r"\r?\n\s*\r?\n")?;
            for group in groups.split(content) {
                let mut lines = group.lines();
                let source = lines.next().unwrap_or("").trim();
                let target = lines.next().unwrap_or(source).trim();
                if !source.is_empty() {
                    values.insert(source.to_owned(), target.to_owned());
                }
            }
        }
        values.extend(
            inline
                .iter()
                .map(|(source, target)| (source.clone(), target.clone())),
        );
        let mut pairs = values.into_iter().collect::<Vec<_>>();
        pairs.sort_by(|a, b| {
            b.0.chars()
                .count()
                .cmp(&a.0.chars().count())
                .then_with(|| a.0.cmp(&b.0))
        });
        let json = serde_json::to_vec(&pairs)?;
        let digest = format!("{:x}", Sha256::digest(json));
        let tokens = pairs
            .iter()
            .enumerate()
            .map(|(i, _)| format!("{{{{etg_{}_{i:06}}}}}", &digest[..12]))
            .collect();
        let source_pattern = (!pairs.is_empty()).then(|| {
            Regex::new(
                &pairs
                    .iter()
                    .map(|x| regex::escape(&x.0))
                    .collect::<Vec<_>>()
                    .join("|"),
            )
            .unwrap()
        });
        Ok(Self {
            pairs,
            tokens,
            source_pattern,
        })
    }

    pub fn apply(&self, text: &str) -> String {
        let Some(pattern) = &self.source_pattern else {
            return text.into();
        };
        pattern
            .replace_all(text, |caps: &regex::Captures| {
                let source = caps.get(0).unwrap().as_str();
                let index = self.pairs.iter().position(|x| x.0 == source).unwrap();
                self.tokens[index].clone()
            })
            .into_owned()
    }

    pub fn restore(&self, text: &str) -> String {
        self.pairs
            .iter()
            .zip(&self.tokens)
            .fold(text.to_owned(), |value, ((_, target), token)| {
                let inner = &token[2..token.len() - 2];
                Regex::new(&format!(r"\{{\{{\s*{}\s*\}}\}}", regex::escape(inner)))
                    .unwrap()
                    .replace_all(&value, regex::NoExpand(target))
                    .into_owned()
            })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn longest_terms_and_spaced_tokens_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("g.txt");
        fs::write(&path, "AI\n人工智能\n\nAI model\nAI模型\n").unwrap();
        let glossary = Glossary::load(&path, &HashMap::new()).unwrap();
        let protected = glossary.apply("AI model beats AI");
        assert_eq!(glossary.restore(&protected), "AI模型 beats 人工智能");
        let spaced = protected.replace("{{", "{{ ").replace("}}", " }}");
        assert_eq!(glossary.restore(&spaced), "AI模型 beats 人工智能");
    }

    #[test]
    fn inline_overrides_file_and_single_line_protects() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("g.txt");
        fs::write(&path, "OpenAI\n\nAI\nold\n").unwrap();
        let glossary = Glossary::load(
            &path,
            &[("AI".into(), "人工智能".into())].into_iter().collect(),
        )
        .unwrap();
        assert_eq!(
            glossary.restore(&glossary.apply("OpenAI AI")),
            "OpenAI 人工智能"
        );
    }
}
