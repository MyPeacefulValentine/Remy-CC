//! Narrow four-level replication of remy_config for the scanner-consumed
//! parameters (environment > project > user > default, per-field range
//! validation). Strict semantics: StructScanner.__init__ loads the full
//! registry with strict=True, so an invalid value fails the Python scan
//! before postprocessing runs; failing the Rust scan at load time
//! replicates that outcome for the replicated subset. Fields outside this
//! subset are not validated here (same narrowing as the R2.3 HookConfig).

use serde_json::Value;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

const CONFIG_SCHEMA_VERSION: &str = "1.0.0";
const CONFIG_FILE_NAME: &str = "remy-config.json";

/// remy_config PARAM_REGISTRY defaults and ranges for the replicated keys;
/// a Python-side contract test asserts these never drift.
#[derive(Debug, Clone, PartialEq)]
pub struct PostprocessConfig {
    pub filter_small: bool,
    pub cluster_density_threshold: f64,
    pub cluster_max_size: i64,
    pub cluster_entry_count: i64,
    pub synth_interface_fanout_cap: i64,
    pub synth_event_fanout_cap: i64,
    pub resolve_fanout_cap: i64,
    pub resolve_score_same_file: i64,
    pub resolve_score_direct_import: i64,
    pub resolve_score_global: i64,
    pub file_kind_min_symbols: i64,
    pub file_kind_low_cohesion_threshold: f64,
    pub scan_lock_timeout: f64,
    /// Parsed for contract parity with PARAM_REGISTRY; the consumer is the
    /// R3.5b daemon worker (scan-job timeout), not the scanner itself.
    pub struct_scan_timeout: i64,
}

pub fn user_home() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
}

fn read_config_values(path: &Path) -> Result<HashMap<String, String>, String> {
    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(_) => return Ok(HashMap::new()),
    };
    let document: Value = serde_json::from_slice(&bytes).map_err(|error| {
        format!(
            "Invalid Remy configuration file {}: {error}",
            path.display()
        )
    })?;
    if document.get("schema_version").and_then(Value::as_str) != Some(CONFIG_SCHEMA_VERSION) {
        return Err(format!(
            "Unsupported Remy configuration schema in {}",
            path.display()
        ));
    }
    let Some(values) = document.get("values").and_then(Value::as_object) else {
        return Err(format!(
            "Remy configuration values must be an object in {}",
            path.display()
        ));
    };
    let mut result = HashMap::new();
    for (key, value) in values {
        let Some(text) = value.as_str() else {
            return Err(format!(
                "Remy configuration field {key:?} must be a string in {}",
                path.display()
            ));
        };
        result.insert(key.clone(), text.to_string());
    }
    Ok(result)
}

fn candidates(
    key: &str,
    project: &HashMap<String, String>,
    user: &HashMap<String, String>,
    default: &str,
) -> Vec<(&'static str, String)> {
    let mut values = Vec::with_capacity(4);
    if let Ok(value) = std::env::var(key) {
        values.push(("environment", value));
    }
    if let Some(value) = project.get(key) {
        values.push(("project", value.clone()));
    }
    if let Some(value) = user.get(key) {
        values.push(("user", value.clone()));
    }
    values.push(("default", default.to_string()));
    values
}

fn int_setting(
    key: &str,
    project: &HashMap<String, String>,
    user: &HashMap<String, String>,
    default: &str,
    minimum: i64,
    maximum: i64,
) -> Result<i64, String> {
    let (source, raw) = candidates(key, project, user, default).swap_remove(0);
    let value: i64 = raw
        .trim()
        .parse()
        .map_err(|_| format!("{key} has invalid int syntax (source={source})"))?;
    if value < minimum {
        return Err(format!("{key} must be >= {minimum} (source={source})"));
    }
    if value > maximum {
        return Err(format!("{key} must be <= {maximum} (source={source})"));
    }
    Ok(value)
}

fn float_setting(
    key: &str,
    project: &HashMap<String, String>,
    user: &HashMap<String, String>,
    default: &str,
    minimum: f64,
    maximum: Option<f64>,
) -> Result<f64, String> {
    let (source, raw) = candidates(key, project, user, default).swap_remove(0);
    let value: f64 = raw
        .trim()
        .parse()
        .map_err(|_| format!("{key} has invalid float syntax (source={source})"))?;
    if !value.is_finite() {
        return Err(format!("{key} has invalid float syntax (source={source})"));
    }
    if value < minimum {
        return Err(format!("{key} must be >= {minimum} (source={source})"));
    }
    if let Some(maximum) = maximum {
        if value > maximum {
            return Err(format!("{key} must be <= {maximum} (source={source})"));
        }
    }
    Ok(value)
}

fn bool_setting(
    key: &str,
    project: &HashMap<String, String>,
    user: &HashMap<String, String>,
    default: &str,
) -> Result<bool, String> {
    let (source, raw) = candidates(key, project, user, default).swap_remove(0);
    match raw.to_lowercase().as_str() {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err(format!("{key} has invalid bool syntax (source={source})")),
    }
}

pub fn load(root: &Path) -> Result<PostprocessConfig, String> {
    load_from(root, user_home())
}

/// Injection point for tests: `load` resolves the real user home; mutating
/// HOME/USERPROFILE in-process would race parallel tests instead.
fn load_from(root: &Path, home: Option<PathBuf>) -> Result<PostprocessConfig, String> {
    let user_values = match home {
        Some(home) => read_config_values(&home.join(".claude").join(CONFIG_FILE_NAME))?,
        None => HashMap::new(),
    };
    let project_values = read_config_values(&root.join(".claude").join(CONFIG_FILE_NAME))?;
    let p = &project_values;
    let u = &user_values;
    Ok(PostprocessConfig {
        filter_small: bool_setting("REMY_LOGIC_INDEX_FILTER_SMALL", p, u, "false")?,
        cluster_density_threshold: float_setting(
            "REMY_CLUSTER_DENSITY_THRESHOLD",
            p,
            u,
            "0.5",
            0.0,
            None,
        )?,
        cluster_max_size: int_setting("REMY_CLUSTER_MAX_SIZE", p, u, "15", 2, 200)?,
        cluster_entry_count: int_setting("REMY_CLUSTER_ENTRY_COUNT", p, u, "3", 1, 20)?,
        synth_interface_fanout_cap: int_setting(
            "REMY_SYNTH_INTERFACE_FANOUT_CAP",
            p,
            u,
            "10",
            1,
            100,
        )?,
        synth_event_fanout_cap: int_setting("REMY_SYNTH_EVENT_FANOUT_CAP", p, u, "20", 1, 200)?,
        resolve_fanout_cap: int_setting("REMY_RESOLVE_FANOUT_CAP", p, u, "10", 1, 100)?,
        resolve_score_same_file: int_setting("REMY_RESOLVE_SCORE_SAME_FILE", p, u, "2", 0, 100)?,
        resolve_score_direct_import: int_setting(
            "REMY_RESOLVE_SCORE_DIRECT_IMPORT",
            p,
            u,
            "1",
            0,
            100,
        )?,
        resolve_score_global: int_setting("REMY_RESOLVE_SCORE_GLOBAL", p, u, "0", 0, 100)?,
        file_kind_min_symbols: int_setting("REMY_FILE_KIND_MIN_SYMBOLS", p, u, "5", 1, 50)?,
        file_kind_low_cohesion_threshold: float_setting(
            "REMY_FILE_KIND_LOW_COHESION_THRESHOLD",
            p,
            u,
            "0.25",
            0.0,
            Some(1.0),
        )?,
        scan_lock_timeout: float_setting(
            "REMY_INDEX_SCAN_LOCK_TIMEOUT",
            p,
            u,
            "30",
            0.0,
            Some(300.0),
        )?,
        struct_scan_timeout: int_setting("REMY_STRUCT_SCAN_TIMEOUT", p, u, "60", 10, 300)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Isolated (project, home) directory pair so the real
    /// ~/.claude/remy-config.json never leaks into assertions.
    fn isolated() -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        std::fs::create_dir_all(&home).unwrap();
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
        let config = load_from(dir.path(), Some(home)).unwrap();
        assert!(!config.filter_small);
        assert_eq!(config.cluster_density_threshold, 0.5);
        assert_eq!(config.cluster_max_size, 15);
        assert_eq!(config.cluster_entry_count, 3);
        assert_eq!(config.synth_interface_fanout_cap, 10);
        assert_eq!(config.synth_event_fanout_cap, 20);
        assert_eq!(config.resolve_fanout_cap, 10);
        assert_eq!(config.resolve_score_same_file, 2);
        assert_eq!(config.resolve_score_direct_import, 1);
        assert_eq!(config.resolve_score_global, 0);
        assert_eq!(config.file_kind_min_symbols, 5);
        assert_eq!(config.file_kind_low_cohesion_threshold, 0.25);
        assert_eq!(config.scan_lock_timeout, 30.0);
        assert_eq!(config.struct_scan_timeout, 60);
    }

    #[test]
    fn scan_timeout_keys_fail_strict_out_of_range() {
        let (dir, home) = isolated();
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_STRUCT_SCAN_TIMEOUT": "5"}}"#,
        );
        let error = load_from(dir.path(), Some(home.clone())).unwrap_err();
        assert!(error.contains("REMY_STRUCT_SCAN_TIMEOUT"), "{error}");
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_INDEX_SCAN_LOCK_TIMEOUT": "301"}}"#,
        );
        let error = load_from(dir.path(), Some(home)).unwrap_err();
        assert!(error.contains("REMY_INDEX_SCAN_LOCK_TIMEOUT"), "{error}");
    }

    #[test]
    fn project_file_overrides_user_file_and_defaults() {
        let (dir, home) = isolated();
        write_config(
            &home,
            r#"{"schema_version": "1.0.0", "values": {"REMY_CLUSTER_MAX_SIZE": "99", "REMY_CLUSTER_ENTRY_COUNT": "5"}}"#,
        );
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_CLUSTER_MAX_SIZE": "42", "REMY_LOGIC_INDEX_FILTER_SMALL": "True"}}"#,
        );
        let config = load_from(dir.path(), Some(home)).unwrap();
        assert_eq!(config.cluster_max_size, 42);
        assert_eq!(config.cluster_entry_count, 5);
        assert!(config.filter_small);
    }

    #[test]
    fn environment_beats_project_file() {
        let (dir, home) = isolated();
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_RESOLVE_FANOUT_CAP": "20"}}"#,
        );
        std::env::set_var("REMY_RESOLVE_FANOUT_CAP", "30");
        let config = load_from(dir.path(), Some(home));
        std::env::remove_var("REMY_RESOLVE_FANOUT_CAP");
        assert_eq!(config.unwrap().resolve_fanout_cap, 30);
    }

    #[test]
    fn out_of_range_value_fails_strict() {
        let (dir, home) = isolated();
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_CLUSTER_MAX_SIZE": "1"}}"#,
        );
        let error = load_from(dir.path(), Some(home)).unwrap_err();
        assert!(error.contains("REMY_CLUSTER_MAX_SIZE"), "{error}");
    }

    #[test]
    fn invalid_syntax_and_non_finite_fail_strict() {
        let (dir, home) = isolated();
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_CLUSTER_DENSITY_THRESHOLD": "inf"}}"#,
        );
        assert!(load_from(dir.path(), Some(home.clone())).is_err());
        write_config(
            dir.path(),
            r#"{"schema_version": "1.0.0", "values": {"REMY_FILE_KIND_MIN_SYMBOLS": "five"}}"#,
        );
        assert!(load_from(dir.path(), Some(home)).is_err());
    }

    #[test]
    fn mismatched_schema_version_fails_strict() {
        let (dir, home) = isolated();
        write_config(dir.path(), r#"{"schema_version": "9.9.9", "values": {}}"#);
        assert!(load_from(dir.path(), Some(home)).is_err());
    }
}
