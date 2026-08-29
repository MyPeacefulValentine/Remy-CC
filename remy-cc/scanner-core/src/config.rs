//! logic_index_config parsing, exclusion matching, and layer assignment
//! (StructScanner._load_config / _matches_exclusion / _is_excluded /
//! _is_path_excluded / _match_file_to_layer).
//!
//! Unlike the Python scanner, a missing config file is NOT materialized
//! from the default template — the Rust scanner never writes into the
//! scanned tree. The fallback exclusion list is the same seven entries
//! Python uses when no config exists.

use crate::fnmatch::fnmatch;
use std::path::Path;

pub const CONFIG_FILE: &str = ".claude/logic_index_config";

const DEFAULT_EXCLUSIONS: &[&str] = &[
    ".git/",
    "__pycache__/",
    "venv/",
    "node_modules/",
    ".claude/",
    "dist/",
    "build/",
];

#[derive(Debug, Clone)]
pub struct LayerDef {
    pub name: String,
    pub patterns: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct ScanConfig {
    pub exclusions: Vec<String>,
    pub layers: Vec<LayerDef>,
}

impl ScanConfig {
    pub fn load(root_dir: &Path) -> ScanConfig {
        let config_path = root_dir.join(CONFIG_FILE);
        match std::fs::read_to_string(&config_path) {
            Ok(content) => Self::parse(&content),
            Err(_) => ScanConfig {
                exclusions: DEFAULT_EXCLUSIONS.iter().map(|s| s.to_string()).collect(),
                layers: Vec::new(),
            },
        }
    }

    pub fn parse(content: &str) -> ScanConfig {
        let mut exclusions = Vec::new();
        let mut layers = Vec::new();
        for raw in content.lines() {
            let line = raw.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            if let Some(pattern) = line.strip_prefix('!') {
                exclusions.push(pattern.to_string());
            } else if let Some(rest) = line.strip_prefix("@layer:") {
                if let Some((name, patterns_str)) = rest.split_once('=') {
                    let patterns: Vec<String> = patterns_str
                        .split(',')
                        .map(|p| p.trim())
                        .filter(|p| !p.is_empty())
                        .map(|p| p.to_string())
                        .collect();
                    let name = name.trim();
                    if !name.is_empty() && !patterns.is_empty() {
                        layers.push(LayerDef {
                            name: name.to_string(),
                            patterns,
                        });
                    }
                }
            }
        }
        ScanConfig { exclusions, layers }
    }

    fn matches_exclusion(candidate: &str, pattern: &str) -> bool {
        if fnmatch(candidate, pattern) {
            return true;
        }
        if let Some(stripped) = pattern.strip_prefix("**/") {
            return fnmatch(candidate, stripped);
        }
        false
    }

    /// StructScanner._is_excluded for a walk entry already known to be a
    /// directory or a file (`rel_path` uses forward slashes, no leading dot).
    pub fn is_excluded(&self, rel_path: &str, is_dir: bool) -> bool {
        if rel_path == "." {
            return false;
        }
        let basename = rel_path.rsplit('/').next().unwrap_or(rel_path);
        for pattern in &self.exclusions {
            let must_be_dir = pattern.ends_with('/');
            let clean_pattern = pattern.trim_end_matches('/');
            if must_be_dir && !is_dir {
                continue;
            }
            if Self::matches_exclusion(basename, clean_pattern)
                || Self::matches_exclusion(rel_path, clean_pattern)
            {
                return true;
            }
        }
        false
    }

    /// StructScanner._is_path_excluded: pure-path variant used for stored
    /// relative paths (directory patterns match any ancestor segment).
    pub fn is_path_excluded(&self, rel_path: &str) -> bool {
        let rel_path = rel_path.replace('\\', "/");
        let parts: Vec<&str> = rel_path.split('/').collect();
        let basename = parts[parts.len() - 1];
        for pattern in &self.exclusions {
            let must_be_dir = pattern.ends_with('/');
            let clean_pattern = pattern.trim_end_matches('/');
            if must_be_dir {
                for i in 0..parts.len() - 1 {
                    let segment = parts[i];
                    let cumulative = parts[..i + 1].join("/");
                    if Self::matches_exclusion(segment, clean_pattern)
                        || Self::matches_exclusion(&cumulative, clean_pattern)
                    {
                        return true;
                    }
                }
            } else if Self::matches_exclusion(basename, clean_pattern)
                || Self::matches_exclusion(&rel_path, clean_pattern)
            {
                return true;
            }
        }
        false
    }

    /// StructScanner._match_file_to_layer: first layer whose pattern equals
    /// a lower-cased path segment (or its plural form) wins.
    pub fn match_file_to_layer(&self, rel_path: &str) -> String {
        let lowered = rel_path.replace('\\', "/").to_lowercase();
        let segments: Vec<&str> = lowered.split('/').collect();
        for layer in &self.layers {
            for segment in &segments {
                for pattern in &layer.patterns {
                    if *segment == pattern.as_str() || *segment == format!("{pattern}s").as_str() {
                        return layer.name.clone();
                    }
                }
            }
        }
        "Core".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_exclusions_and_layers() {
        let config = ScanConfig::parse(
            "# comment\n!**/.git/\n!*.log\n@layer:Test Layer=test,spec\n@layer:Bad=\n",
        );
        assert_eq!(config.exclusions, vec!["**/.git/", "*.log"]);
        assert_eq!(config.layers.len(), 1);
        assert_eq!(config.layers[0].name, "Test Layer");
        assert_eq!(config.layers[0].patterns, vec!["test", "spec"]);
    }

    #[test]
    fn directory_patterns_require_directories() {
        let config = ScanConfig::parse("!build/\n");
        assert!(config.is_excluded("build", true));
        assert!(!config.is_excluded("build", false));
        assert!(config.is_path_excluded("build/x.c"));
        assert!(!config.is_path_excluded("build"));
    }

    #[test]
    fn double_star_prefix_matches_bare_name() {
        let config = ScanConfig::parse("!**/__pycache__/\n");
        assert!(config.is_excluded("__pycache__", true));
        assert!(config.is_excluded("a/b/__pycache__", true));
        assert!(config.is_path_excluded("a/__pycache__/m.c"));
    }

    #[test]
    fn layer_matching_uses_lowercase_segments_and_plurals() {
        let config = ScanConfig::parse("@layer:Test Layer=test\n@layer:Utility Layer=util\n");
        assert_eq!(config.match_file_to_layer("Tests/x.c"), "Test Layer");
        assert_eq!(config.match_file_to_layer("src/util/y.c"), "Utility Layer");
        assert_eq!(config.match_file_to_layer("src/y.c"), "Core");
    }
}
