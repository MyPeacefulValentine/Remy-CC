//! MCP-key subset of remy_config with the lenient (strict=False) semantics
//! the Python MCP server runs under: an invalid value emits a diagnostic and
//! falls through to the next candidate layer (environment > project > user >
//! default) instead of failing startup. Field defaults and ranges mirror
//! PARAM_REGISTRY; a Python-side contract test asserts they never drift.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use serde_json::Value;

const CONFIG_SCHEMA_VERSION: &str = "1.0.0";
const CONFIG_FILE_NAME: &str = "remy-config.json";
/// Markers remy_config.discover_project_root probes under `.claude/`.
const ROOT_MARKERS: &[&str] = &[CONFIG_FILE_NAME, "logic_index_config", "logic_index.db"];

#[derive(Debug, Clone)]
pub struct McpConfig {
    pub server_enabled: bool,
    pub bfs_max_depth: i64,
    pub result_limit: i64,
    pub flow_max_depth: i64,
    pub flow_max_visited: i64,
    pub nav_clusters: i64,
    pub nav_files: i64,
    pub nav_symbols: i64,
    pub llm_api_key: String,
    pub llm_base_url: String,
    pub llm_model: String,
    pub llm_max_tokens: i64,
    pub llm_retry_limit: i64,
    pub llm_timeout: i64,
    pub llm_tls_insecure: bool,
    pub db_path: PathBuf,
    pub diagnostics: Vec<String>,
}

enum Kind {
    Int { min: i64, max: i64 },
    Bool,
    Text { allow_empty: bool },
    ProjectPath,
}

struct Field {
    key: &'static str,
    default: &'static str,
    kind: Kind,
}

const FIELDS: &[Field] = &[
    Field {
        key: "REMY_MCP_SERVER_ENABLED",
        default: "true",
        kind: Kind::Bool,
    },
    Field {
        key: "REMY_MCP_BFS_MAX_DEPTH",
        default: "5",
        kind: Kind::Int { min: 1, max: 10 },
    },
    Field {
        key: "REMY_MCP_RESULT_LIMIT",
        default: "50",
        kind: Kind::Int { min: 10, max: 500 },
    },
    Field {
        key: "REMY_MCP_STATIC_ONLY_DEFAULT",
        default: "false",
        kind: Kind::Bool,
    },
    Field {
        key: "REMY_FLOW_MAX_DEPTH",
        default: "15",
        kind: Kind::Int { min: 1, max: 50 },
    },
    Field {
        key: "REMY_FLOW_MAX_VISITED",
        default: "2000",
        kind: Kind::Int {
            min: 100,
            max: 50000,
        },
    },
    Field {
        key: "REMY_NAVIGATE_CANDIDATE_CLUSTERS",
        default: "5",
        kind: Kind::Int { min: 1, max: 50 },
    },
    Field {
        key: "REMY_NAVIGATE_CANDIDATE_FILES",
        default: "10",
        kind: Kind::Int { min: 1, max: 50 },
    },
    Field {
        key: "REMY_NAVIGATE_CANDIDATE_SYMBOLS",
        default: "10",
        kind: Kind::Int { min: 1, max: 50 },
    },
    Field {
        key: "REMY_LLM_API_KEY",
        default: "",
        kind: Kind::Text { allow_empty: true },
    },
    Field {
        key: "REMY_LLM_BASE_URL",
        default: "https://api.deepseek.com/v1/chat/completions",
        kind: Kind::Text { allow_empty: false },
    },
    Field {
        key: "REMY_LLM_MODEL",
        default: "deepseek-v4-flash",
        kind: Kind::Text { allow_empty: false },
    },
    Field {
        key: "REMY_LLM_MAX_TOKENS",
        default: "32768",
        kind: Kind::Int {
            min: 1024,
            max: 1048576,
        },
    },
    Field {
        key: "REMY_LLM_RETRY_LIMIT",
        default: "8",
        kind: Kind::Int { min: 0, max: 32 },
    },
    Field {
        key: "REMY_LLM_TIMEOUT",
        default: "300",
        kind: Kind::Int { min: 30, max: 3600 },
    },
    Field {
        key: "REMY_LLM_TLS_INSECURE",
        default: "false",
        kind: Kind::Bool,
    },
    Field {
        key: "REMY_LOGIC_INDEX_DB_PATH",
        default: ".claude/logic_index.db",
        kind: Kind::ProjectPath,
    },
];

pub fn user_home() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
}

pub fn discover_project_root(start: &Path) -> Option<PathBuf> {
    let mut current = start;
    loop {
        let claude_dir = current.join(".claude");
        if ROOT_MARKERS
            .iter()
            .any(|name| claude_dir.join(name).exists())
        {
            return Some(current.to_path_buf());
        }
        current = current.parent()?;
    }
}

/// Lenient file read: unreadable or malformed files contribute no values,
/// only a diagnostic (load_config strict=False behavior).
fn read_config_values(path: &Path, diagnostics: &mut Vec<String>) -> HashMap<String, String> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(_) => return HashMap::new(),
    };
    let document: Value = match serde_json::from_slice(&bytes) {
        Ok(document) => document,
        Err(error) => {
            diagnostics.push(format!("invalid config file {}: {error}", path.display()));
            return HashMap::new();
        }
    };
    if document.get("schema_version").and_then(Value::as_str) != Some(CONFIG_SCHEMA_VERSION) {
        diagnostics.push(format!("unsupported config schema in {}", path.display()));
        return HashMap::new();
    }
    let Some(values) = document.get("values").and_then(Value::as_object) else {
        diagnostics.push(format!(
            "config values must be an object in {}",
            path.display()
        ));
        return HashMap::new();
    };
    let mut result = HashMap::new();
    for (key, value) in values {
        match value.as_str() {
            Some(text) => {
                result.insert(key.clone(), text.to_string());
            }
            None => diagnostics.push(format!(
                "config field {key:?} must be a string in {}",
                path.display()
            )),
        }
    }
    result
}

enum Coerced {
    Int(i64),
    Bool(bool),
    Text(String),
}

fn coerce(field: &Field, raw: &str) -> Result<Coerced, String> {
    if raw.is_empty() {
        let allow_empty = matches!(field.kind, Kind::Text { allow_empty: true });
        if !allow_empty {
            return Err(format!("{} must not be empty", field.key));
        }
    }
    match field.kind {
        Kind::Int { min, max } => {
            let value: i64 = raw
                .parse()
                .map_err(|_| format!("{} has invalid int syntax", field.key))?;
            if value < min {
                return Err(format!("{} must be >= {min}", field.key));
            }
            if value > max {
                return Err(format!("{} must be <= {max}", field.key));
            }
            Ok(Coerced::Int(value))
        }
        Kind::Bool => match raw.to_lowercase().as_str() {
            "true" => Ok(Coerced::Bool(true)),
            "false" => Ok(Coerced::Bool(false)),
            _ => Err(format!("{} has invalid bool syntax", field.key)),
        },
        Kind::Text { .. } | Kind::ProjectPath => Ok(Coerced::Text(raw.to_string())),
    }
}

fn expand_user(value: &str, home: Option<&Path>) -> PathBuf {
    if let Some(rest) = value
        .strip_prefix("~/")
        .or_else(|| value.strip_prefix("~\\"))
    {
        if let Some(home) = home {
            return home.join(rest);
        }
    }
    PathBuf::from(value)
}

fn resolve_db_path(raw: &str, project_root: Option<&Path>, home: Option<&Path>) -> PathBuf {
    let expanded = expand_user(raw, home);
    if expanded.is_absolute() {
        return expanded;
    }
    match project_root {
        Some(root) => root.join(expanded),
        None => expanded,
    }
}

pub fn load() -> McpConfig {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    load_from(&cwd, user_home(), &|key| std::env::var(key).ok())
}

/// `env` is an injection seam: the dogfooding Claude Code session exports
/// real REMY_* values into every child process, so tests must be able to
/// blank the environment layer without mutating process env (which races
/// parallel tests).
fn load_from(cwd: &Path, home: Option<PathBuf>, env: &dyn Fn(&str) -> Option<String>) -> McpConfig {
    let mut diagnostics = Vec::new();
    let project_root = discover_project_root(cwd);
    let user_values = match &home {
        Some(home) => read_config_values(
            &home.join(".claude").join(CONFIG_FILE_NAME),
            &mut diagnostics,
        ),
        None => HashMap::new(),
    };
    let project_values = match &project_root {
        Some(root) => read_config_values(
            &root.join(".claude").join(CONFIG_FILE_NAME),
            &mut diagnostics,
        ),
        None => HashMap::new(),
    };

    let mut ints: HashMap<&str, i64> = HashMap::new();
    let mut bools: HashMap<&str, bool> = HashMap::new();
    let mut texts: HashMap<&str, String> = HashMap::new();

    for field in FIELDS {
        let mut candidates: Vec<(&str, String)> = Vec::with_capacity(4);
        if let Some(value) = env(field.key) {
            candidates.push(("environment", value));
        }
        if let Some(value) = project_values.get(field.key) {
            candidates.push(("project", value.clone()));
        }
        if let Some(value) = user_values.get(field.key) {
            candidates.push(("user", value.clone()));
        }
        candidates.push(("default", field.default.to_string()));
        for (source, raw) in candidates {
            match coerce(field, &raw) {
                Ok(Coerced::Int(value)) => {
                    ints.insert(field.key, value);
                }
                Ok(Coerced::Bool(value)) => {
                    bools.insert(field.key, value);
                }
                Ok(Coerced::Text(value)) => {
                    texts.insert(field.key, value);
                }
                Err(message) => {
                    diagnostics.push(format!("{message} (source={source})"));
                    continue;
                }
            }
            break;
        }
    }

    let db_raw = texts
        .remove("REMY_LOGIC_INDEX_DB_PATH")
        .unwrap_or_else(|| ".claude/logic_index.db".to_string());
    let db_path = resolve_db_path(&db_raw, project_root.as_deref(), home.as_deref());

    McpConfig {
        server_enabled: bools["REMY_MCP_SERVER_ENABLED"],
        bfs_max_depth: ints["REMY_MCP_BFS_MAX_DEPTH"],
        result_limit: ints["REMY_MCP_RESULT_LIMIT"],
        flow_max_depth: ints["REMY_FLOW_MAX_DEPTH"],
        flow_max_visited: ints["REMY_FLOW_MAX_VISITED"],
        nav_clusters: ints["REMY_NAVIGATE_CANDIDATE_CLUSTERS"],
        nav_files: ints["REMY_NAVIGATE_CANDIDATE_FILES"],
        nav_symbols: ints["REMY_NAVIGATE_CANDIDATE_SYMBOLS"],
        llm_api_key: texts.remove("REMY_LLM_API_KEY").unwrap_or_default(),
        llm_base_url: texts.remove("REMY_LLM_BASE_URL").unwrap_or_default(),
        llm_model: texts.remove("REMY_LLM_MODEL").unwrap_or_default(),
        llm_max_tokens: ints["REMY_LLM_MAX_TOKENS"],
        llm_retry_limit: ints["REMY_LLM_RETRY_LIMIT"],
        llm_timeout: ints["REMY_LLM_TIMEOUT"],
        llm_tls_insecure: bools["REMY_LLM_TLS_INSECURE"],
        db_path,
        diagnostics,
    }
}

impl McpConfig {
    /// Mirror of remy_config.emit_diagnostics(prefix="MCPConfig"): stderr only.
    pub fn emit_diagnostics(&self) {
        for diagnostic in &self.diagnostics {
            eprintln!("[MCPConfig] {diagnostic}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The tempdir lives under the real user home, whose `.claude/` would
    /// otherwise be discovered as project root — plant a marker so
    /// discovery stops inside the tempdir.
    fn isolated() -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        std::fs::create_dir_all(&home).unwrap();
        let claude = dir.path().join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        std::fs::write(claude.join("logic_index_config"), "").unwrap();
        (dir, home)
    }

    fn write_config(dir: &Path, body: &str) {
        let claude = dir.join(".claude");
        std::fs::create_dir_all(&claude).unwrap();
        std::fs::write(claude.join(CONFIG_FILE_NAME), body).unwrap();
    }

    #[test]
    fn defaults_apply_without_any_config() {
        let (dir, home) = isolated();
        let config = load_from(dir.path(), Some(home), &|_| None);
        assert!(config.server_enabled);
        assert_eq!(config.bfs_max_depth, 5);
        assert_eq!(config.result_limit, 50);
        assert_eq!(config.flow_max_depth, 15);
        assert_eq!(config.flow_max_visited, 2000);
        assert_eq!(config.nav_clusters, 5);
        assert_eq!(config.nav_files, 10);
        assert_eq!(config.nav_symbols, 10);
        assert_eq!(config.llm_api_key, "");
        assert_eq!(config.llm_max_tokens, 32768);
        assert_eq!(config.llm_retry_limit, 8);
        assert_eq!(config.llm_timeout, 300);
        assert!(!config.llm_tls_insecure);
        assert!(config.diagnostics.is_empty());
    }

    #[test]
    fn invalid_value_falls_to_next_candidate_with_diagnostic() {
        let (dir, home) = isolated();
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_MCP_RESULT_LIMIT": "9999", "REMY_MCP_BFS_MAX_DEPTH": "7"}}"#,
        );
        let config = load_from(dir.path(), Some(home), &|_| None);
        assert_eq!(config.result_limit, 50);
        assert_eq!(config.bfs_max_depth, 7);
        assert!(config
            .diagnostics
            .iter()
            .any(|d| d.contains("REMY_MCP_RESULT_LIMIT") && d.contains("source=project")));
    }

    #[test]
    fn malformed_file_yields_diagnostic_not_failure() {
        let (dir, home) = isolated();
        write_config(dir.path(), "{not json");
        let config = load_from(dir.path(), Some(home), &|_| None);
        assert!(config.server_enabled);
        assert!(!config.diagnostics.is_empty());
    }

    #[test]
    fn project_overrides_user_and_db_path_resolves_from_root() {
        let (dir, home) = isolated();
        write_config(
            &home,
            r#"{"schema_version": "1.0.0", "values": {"REMY_MCP_RESULT_LIMIT": "20"}}"#,
        );
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_MCP_RESULT_LIMIT": "30"}}"#,
        );
        let nested = dir.path().join("src").join("deep");
        std::fs::create_dir_all(&nested).unwrap();
        let config = load_from(&nested, Some(home), &|_| None);
        assert_eq!(config.result_limit, 30);
        assert_eq!(config.db_path, dir.path().join(".claude/logic_index.db"));
    }

    #[test]
    fn environment_layer_beats_project_file() {
        let (dir, home) = isolated();
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_MCP_RESULT_LIMIT": "20"}}"#,
        );
        let env = |key: &str| (key == "REMY_MCP_RESULT_LIMIT").then(|| "30".to_string());
        let config = load_from(dir.path(), Some(home), &env);
        assert_eq!(config.result_limit, 30);
    }

    #[test]
    fn no_project_root_keeps_relative_db_path() {
        let resolved = resolve_db_path(".claude/logic_index.db", None, None);
        assert_eq!(resolved, PathBuf::from(".claude/logic_index.db"));
    }
}
