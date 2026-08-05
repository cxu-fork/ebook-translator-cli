use std::{collections::HashMap, fs, path::Path, sync::OnceLock};

use anyhow::{Context, Result, bail};
use regex::Regex;
use sha2::{Digest, Sha256};

#[derive(Clone, Debug, Default)]
pub struct Glossary {
    pub pairs: Vec<(String, String)>,
    tokens: Vec<String>,
    token_patterns: Vec<Regex>,
    indices: HashMap<String, usize>,
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
        values.extend(inline.iter().filter_map(|(source, target)| {
            let source = source.trim();
            (!source.is_empty()).then(|| (source.to_owned(), target.trim().to_owned()))
        }));
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
            .collect::<Vec<_>>();
        let token_patterns = tokens
            .iter()
            .map(|token| {
                let inner = &token[2..token.len() - 2];
                Regex::new(&format!(r"\{{\{{\s*{}\s*\}}\}}", regex::escape(inner)))
            })
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let indices = pairs
            .iter()
            .enumerate()
            .map(|(index, (source, _))| (source.clone(), index))
            .collect();
        let source_pattern = (!pairs.is_empty())
            .then(|| {
                Regex::new(
                    &pairs
                        .iter()
                        .map(|(source, _)| term_pattern(source))
                        .collect::<Vec<_>>()
                        .join("|"),
                )
            })
            .transpose()
            .context("术语表过大，无法编译匹配规则")?;
        Ok(Self {
            pairs,
            tokens,
            token_patterns,
            indices,
            source_pattern,
        })
    }

    pub fn apply(&self, text: &str) -> String {
        if self.source_pattern.is_none() {
            return text.into();
        }
        let mut output = String::with_capacity(text.len());
        let mut end = 0;
        for token in markup_token_pattern().find_iter(text) {
            output.push_str(&self.apply_plain(&text[end..token.start()]));
            output.push_str(token.as_str());
            end = token.end();
        }
        output.push_str(&self.apply_plain(&text[end..]));
        output
    }

    fn apply_plain(&self, text: &str) -> String {
        self.source_pattern.as_ref().map_or_else(
            || text.to_owned(),
            |pattern| {
                pattern
                    .replace_all(text, |caps: &regex::Captures| {
                        let source = caps.get(0).unwrap().as_str();
                        self.tokens[self.indices[source]].clone()
                    })
                    .into_owned()
            },
        )
    }

    pub fn restore(&self, protected: &str, translated: &str) -> Result<String> {
        if !protected.contains("{{etg_") {
            return Ok(translated.into());
        }
        let mut output = translated.to_owned();
        for (((_, target), token), pattern) in self
            .pairs
            .iter()
            .zip(&self.tokens)
            .zip(&self.token_patterns)
        {
            let expected = protected.matches(token).count();
            if expected == 0 {
                continue;
            }
            let actual = pattern.find_iter(&output).count();
            if expected != actual {
                bail!("译文破坏了术语占位符: {token}");
            }
            output = pattern
                .replace_all(&output, regex::NoExpand(target))
                .into_owned();
        }
        if output.contains("{{etg_") {
            bail!("译文包含未知术语占位符");
        }
        Ok(output)
    }
}

fn term_pattern(source: &str) -> String {
    let mut pattern = regex::escape(source);
    if source
        .chars()
        .next()
        .is_some_and(|c| c.is_ascii_alphanumeric())
    {
        pattern.insert_str(0, r"(?-u:\b)");
    }
    if source
        .chars()
        .last()
        .is_some_and(|c| c.is_ascii_alphanumeric())
    {
        pattern.push_str(r"(?-u:\b)");
    }
    pattern
}

fn markup_token_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(r"\{\{etm_[ocn]_\d+\}\}").unwrap())
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
        assert_eq!(
            glossary.restore(&protected, &protected).unwrap(),
            "AI模型 beats 人工智能"
        );
        let spaced = protected.replace("{{", "{{ ").replace("}}", " }}");
        assert_eq!(
            glossary.restore(&protected, &spaced).unwrap(),
            "AI模型 beats 人工智能"
        );
        assert!(glossary.restore(&protected, "AI model beats").is_err());
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
            glossary
                .restore(&glossary.apply("OpenAI AI"), &glossary.apply("OpenAI AI"))
                .unwrap(),
            "OpenAI 人工智能"
        );
    }

    #[test]
    fn inline_terms_are_trimmed_bounded_and_do_not_touch_markup_tokens() {
        let glossary = Glossary::load(
            Path::new(""),
            &[
                ("".into(), "bad".into()),
                ("AI".into(), "人工智能".into()),
                ("etm".into(), "bad".into()),
            ]
            .into_iter()
            .collect(),
        )
        .unwrap();
        let original = "OpenAI AI {{etm_o_100000}}text{{etm_c_100000}}";
        let protected = glossary.apply(original);
        assert!(protected.starts_with("OpenAI {{etg_"));
        assert!(protected.contains("{{etm_o_100000}}"));
        assert_eq!(
            glossary.restore(&protected, &protected).unwrap(),
            "OpenAI 人工智能 {{etm_o_100000}}text{{etm_c_100000}}"
        );
    }
}
