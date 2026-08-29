//! Interactive install surfaces: language resolution, PATH registration,
//! and the post-install configuration pointer.
//!
//! Scope per the H8-B4 disposition: language and PATH prompts are native
//! (isatty-gated, EOF falls back to the deployed configuration, with the
//! semantics inherited from the retired v3 installer); the interactive
//! API-key flow is not ported — configuration stays owned by `remy-cc
//! config`.

use std::io::{BufRead, IsTerminal, Write};
use std::path::Path;

use serde_json::Value;

use super::storage;

/// `--lang` flag > non-interactive/non-tty fallback to the deployed config >
/// bilingual prompt (EOF falls back to the deployed config).
pub(crate) fn resolve_language(
    flag: Option<&str>,
    non_interactive: bool,
    claude_root: &Path,
) -> String {
    match flag {
        Some("en") => return "en".to_string(),
        Some("zh-CN") => return "zh-CN".to_string(),
        _ => {}
    }
    if non_interactive || !std::io::stdin().is_terminal() {
        return existing_config_lang(claude_root);
    }
    println!("Select language / 选择语言:");
    println!("  1. English");
    println!("  2. 简体中文");
    print!("Choice / 选择 [1]: ");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    match std::io::stdin().lock().read_line(&mut line) {
        Ok(0) | Err(_) => existing_config_lang(claude_root),
        Ok(_) if line.trim() == "2" => "zh-CN".to_string(),
        Ok(_) => "en".to_string(),
    }
}

/// `REMY_LANG` from the deployed remy-config.json; `"en"` when the file is
/// missing, unreadable, or holds an unsupported value.
pub(crate) fn existing_config_lang(claude_root: &Path) -> String {
    let value = storage::load_json(&claude_root.join("remy-config.json"))
        .ok()
        .and_then(|document| {
            document
                .get("values")
                .and_then(|values| values.get("REMY_LANG"))
                .and_then(Value::as_str)
                .map(str::to_string)
        });
    match value.as_deref() {
        Some("zh-CN") => "zh-CN".to_string(),
        _ => "en".to_string(),
    }
}

/// Persists `REMY_LANG` into the deployed remy-config.json, preserving other
/// values; a missing or corrupt document is replaced with a fresh one.
pub(crate) fn save_language(claude_root: &Path, lang: &str) -> std::io::Result<()> {
    let path = claude_root.join("remy-config.json");
    let mut document = storage::load_json(&path)
        .ok()
        .filter(|value| value.get("values").is_some_and(Value::is_object))
        .unwrap_or_else(|| serde_json::json!({"schema_version": "1.0.0", "values": {}}));
    document["values"]["REMY_LANG"] = Value::String(lang.to_string());
    storage::atomic_write_json(&path, &document)
}

/// PATH registration for `<remy root>/bin` (the retired v3 installer's
/// register_path semantics with the v4 target directory). Interactive-only
/// side effects: non-interactive runs print the manual instruction.
pub(crate) fn register_path(bin_dir: &Path, non_interactive: bool) {
    let bin_text = bin_dir.to_string_lossy().into_owned();
    if path_contains(&bin_text) {
        println!("  [i] {bin_text} is already on PATH.");
        return;
    }
    if non_interactive || !std::io::stdin().is_terminal() {
        println!("  [i] Add {bin_text} to PATH to invoke remy-cc directly.");
        return;
    }
    print!("Add {bin_text} to PATH? / 将其加入 PATH？ [Y/n]: ");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    let answer = match std::io::stdin().lock().read_line(&mut line) {
        Ok(0) | Err(_) => String::new(),
        Ok(_) => line.trim().to_lowercase(),
    };
    if answer == "n" {
        println!("  [i] Add {bin_text} to PATH manually to invoke remy-cc directly.");
        return;
    }
    register_path_platform(&bin_text);
}

fn path_contains(bin_text: &str) -> bool {
    let target = normalize_path_entry(bin_text);
    std::env::var("PATH")
        .unwrap_or_default()
        .split(if cfg!(windows) { ';' } else { ':' })
        .any(|entry| !entry.is_empty() && normalize_path_entry(entry) == target)
}

fn normalize_path_entry(entry: &str) -> String {
    let normalized = entry.replace('/', std::path::MAIN_SEPARATOR_STR);
    let trimmed = normalized.trim_end_matches(std::path::MAIN_SEPARATOR);
    if cfg!(windows) {
        trimmed.to_lowercase()
    } else {
        trimmed.to_string()
    }
}

#[cfg(windows)]
fn register_path_platform(bin_text: &str) {
    use std::process::Command;
    // Empty-read guard: an empty baseline is accepted only when a successful
    // listing proves the value is absent. The key itself always exists, so a
    // failed query is never proof of an absent value — writing on top of one
    // would replace a populated user PATH with just the bin directory.
    let output = match Command::new("reg")
        .args(["query", "HKCU\\Environment"])
        .output()
    {
        Ok(output) if output.status.success() => output,
        _ => {
            println!("  [!] Could not read the user PATH; add {bin_text} manually.");
            return;
        }
    };
    let current = parse_reg_path_value(&String::from_utf8_lossy(&output.stdout));
    let new_path = if current.is_empty() {
        bin_text.to_string()
    } else {
        format!("{current};{bin_text}")
    };
    if new_path.len() > 1024 {
        println!("  [!] User PATH is too long for setx; add {bin_text} manually.");
        return;
    }
    let result = Command::new("setx").args(["PATH", &new_path]).output();
    match result {
        Ok(output) if output.status.success() => {
            println!("  [+] PATH updated for new terminals.");
        }
        _ => println!("  [!] Could not update PATH; add {bin_text} manually."),
    }
}

/// Extracts the PATH data from a `reg query HKCU\Environment` listing.
/// A value line is `<name> <REG_ type> <data>`; the data keeps any internal
/// whitespace (the old four-space split truncated data containing runs of
/// spaces). Returns an empty string when no PATH value line is present —
/// with a successful listing that is proof of absence, the one legitimate
/// empty baseline.
#[cfg(windows)]
fn parse_reg_path_value(stdout: &str) -> String {
    for line in stdout.lines() {
        let trimmed = line.trim();
        let mut tokens = trimmed.split_whitespace();
        let (name, kind) = match (tokens.next(), tokens.next()) {
            (Some(name), Some(kind)) => (name, kind),
            _ => continue,
        };
        if !name.eq_ignore_ascii_case("path") || !kind.starts_with("REG_") {
            continue;
        }
        let after_name = trimmed[name.len()..].trim_start();
        return after_name[kind.len()..].trim().to_string();
    }
    String::new()
}

#[cfg(not(windows))]
fn register_path_platform(bin_text: &str) {
    use std::fs::OpenOptions;
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/bash".to_string());
    let rc_name = if shell.contains("zsh") {
        ".zshrc"
    } else {
        ".bashrc"
    };
    let Some(home) = std::env::var_os("HOME").map(std::path::PathBuf::from) else {
        println!("  [!] HOME is unset; add {bin_text} to PATH manually.");
        return;
    };
    let rc_file = home.join(rc_name);
    if let Ok(content) = std::fs::read_to_string(&rc_file) {
        if content.contains(bin_text) {
            println!("  [i] {bin_text} is already registered in {rc_name}.");
            return;
        }
    }
    let result = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&rc_file)
        .and_then(|mut file| writeln!(file, "\n# Remy-CC CLI\nexport PATH=\"$PATH:{bin_text}\""));
    match result {
        Ok(()) => println!("  [+] PATH registered in {rc_name}; restart the shell."),
        Err(_) => println!("  [!] Could not update {rc_name}; add {bin_text} manually."),
    }
}

/// Post-install pointer: interactive API configuration is owned by
/// `remy-cc config` (single configuration owner; H8-B4 disposition).
pub(crate) fn print_config_guidance(lang: &str) {
    if lang == "zh-CN" {
        println!("  [i] 运行 remy-cc config 配置 LLM API（摘要与导航功能需要）。");
    } else {
        println!("  [i] Run `remy-cc config` to configure the LLM API (used by summaries and navigation).");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn existing_lang_reads_deployed_value_and_falls_back() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert_eq!(existing_config_lang(dir.path()), "en");
        storage::atomic_write_json(
            &dir.path().join("remy-config.json"),
            &json!({"schema_version": "1.0.0", "values": {"REMY_LANG": "zh-CN"}}),
        )
        .expect("write");
        assert_eq!(existing_config_lang(dir.path()), "zh-CN");
        storage::atomic_write_json(
            &dir.path().join("remy-config.json"),
            &json!({"schema_version": "1.0.0", "values": {"REMY_LANG": "fr"}}),
        )
        .expect("write");
        assert_eq!(existing_config_lang(dir.path()), "en");
    }

    #[test]
    fn save_language_preserves_other_values_and_replaces_corrupt_documents() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("remy-config.json");
        storage::atomic_write_json(
            &path,
            &json!({"schema_version": "1.0.0", "values": {"OPENAI_MAX_WORKERS": "5"}}),
        )
        .expect("write");
        save_language(dir.path(), "zh-CN").expect("save");
        let document = storage::load_json(&path).expect("load");
        assert_eq!(document["values"]["REMY_LANG"], "zh-CN");
        assert_eq!(document["values"]["OPENAI_MAX_WORKERS"], "5");
        std::fs::write(&path, "{broken").expect("corrupt");
        save_language(dir.path(), "en").expect("save");
        let document = storage::load_json(&path).expect("load");
        assert_eq!(document["values"]["REMY_LANG"], "en");
        assert_eq!(document["schema_version"], "1.0.0");
    }

    #[test]
    fn resolve_language_honors_the_flag_before_everything() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert_eq!(resolve_language(Some("zh-CN"), true, dir.path()), "zh-CN");
        assert_eq!(resolve_language(Some("en"), true, dir.path()), "en");
    }

    #[test]
    fn non_interactive_resolution_uses_the_deployed_config() {
        let dir = tempfile::tempdir().expect("tempdir");
        storage::atomic_write_json(
            &dir.path().join("remy-config.json"),
            &json!({"schema_version": "1.0.0", "values": {"REMY_LANG": "zh-CN"}}),
        )
        .expect("write");
        assert_eq!(resolve_language(None, true, dir.path()), "zh-CN");
    }

    #[test]
    fn path_entry_normalization_compares_case_and_separators() {
        if cfg!(windows) {
            assert_eq!(normalize_path_entry("C:/Users/X/bin/"), "c:\\users\\x\\bin");
        } else {
            assert_eq!(normalize_path_entry("/usr/local/bin/"), "/usr/local/bin");
        }
    }

    #[cfg(windows)]
    #[test]
    fn reg_path_parsing_keeps_internal_whitespace_and_detects_absence() {
        let listing = "\r\nHKEY_CURRENT_USER\\Environment\r\n    Path    REG_EXPAND_SZ    C:\\a b;C:\\c    d;%USERPROFILE%\\bin\r\n    TEMP    REG_SZ    C:\\t\r\n";
        assert_eq!(
            parse_reg_path_value(listing),
            "C:\\a b;C:\\c    d;%USERPROFILE%\\bin"
        );
        let upper = "    PATH    REG_SZ    C:\\only\r\n";
        assert_eq!(parse_reg_path_value(upper), "C:\\only");
        let absent = "HKEY_CURRENT_USER\\Environment\r\n    TEMP    REG_SZ    C:\\t\r\n";
        assert_eq!(parse_reg_path_value(absent), "");
        assert_eq!(parse_reg_path_value(""), "");
    }
}
