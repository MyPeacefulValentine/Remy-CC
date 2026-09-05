//! Read-only acceptance of the v1/v2-era installer manifest
//! (`~/.claude/.installer_manifest.json`, written by the retired Python
//! `install.py` before the v3 dual-root layout) and the user-approved
//! cleanup it drives. Registered exception: docs/RETIREMENT.md — this is
//! the one place the v2 record shapes stay readable, scoped to the install
//! conflict-resolution branch.
//!
//! Fail-closed disposal: a recorded file is deleted only when its on-disk
//! sha256 equals the recorded sha256 (byte identity proves the retired
//! installer wrote it). Records that mismatch, lack a hash, escape
//! `claude_root`, or contain `..` are retained and reported instead.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

use super::storage;
use super::{lock, InstallError};

/// One legacy record retained (not deleted) and the reason why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RetainedEntry {
    pub(crate) path: String,
    pub(crate) reason: RetainReason,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RetainReason {
    HashMismatch,
    NoHash,
    OutsideClaudeRoot,
    Missing,
}

impl RetainReason {
    pub(crate) fn describe(self, language: &str) -> &'static str {
        let zh = language == "zh-CN";
        match self {
            Self::HashMismatch => {
                if zh {
                    "内容与旧安装记录不一致（可能被修改过）"
                } else {
                    "content differs from the old install record (possibly modified)"
                }
            }
            Self::NoHash => {
                if zh {
                    "旧记录缺少哈希，无法核验"
                } else {
                    "the old record carries no hash to verify against"
                }
            }
            Self::OutsideClaudeRoot => {
                if zh {
                    "位于 Claude 目录之外，超出清理范围"
                } else {
                    "outside the Claude directory, beyond cleanup scope"
                }
            }
            Self::Missing => {
                if zh {
                    "文件已不存在"
                } else {
                    "the file no longer exists"
                }
            }
        }
    }
}

/// The disposal plan derived from a parsed legacy manifest: absolute paths
/// verified byte-identical to their records (safe to delete), everything
/// else retained with a reason.
#[derive(Debug)]
pub(crate) struct LegacyPlan {
    pub(crate) manifest_path: PathBuf,
    pub(crate) old_version: String,
    pub(crate) deletable: Vec<PathBuf>,
    pub(crate) retained: Vec<RetainedEntry>,
}

/// Parses `<claude_root>/.installer_manifest.json` when present. `Ok(None)`
/// when the file does not exist; `Err` when it exists but is unreadable or
/// does not match the v1/v2 shapes (`version` string + `files` array; the
/// v2-only keys are ignored). The caller downgrades that error to a warning.
pub(crate) fn inspect(claude_root: &Path) -> Result<Option<LegacyPlan>, InstallError> {
    let manifest_path = claude_root.join(super::ops::LEGACY_MANIFEST_NAME);
    if !manifest_path.is_file() {
        return Ok(None);
    }
    let invalid = || InstallError::metadata("legacy installer manifest is unreadable");
    let document = storage::load_json(&manifest_path).map_err(|_| invalid())?;
    let object = document.as_object().ok_or_else(invalid)?;
    let old_version = object
        .get("version")
        .and_then(Value::as_str)
        .ok_or_else(invalid)?
        .to_string();
    let records = object
        .get("files")
        .and_then(Value::as_array)
        .ok_or_else(invalid)?;

    let mut deletable = Vec::new();
    let mut retained = Vec::new();
    for record in records {
        let Some(path_text) = record.get("path").and_then(Value::as_str) else {
            return Err(invalid());
        };
        let Some(resolved) = resolve_record_path(path_text, claude_root) else {
            retained.push(RetainedEntry {
                path: path_text.to_string(),
                reason: RetainReason::OutsideClaudeRoot,
            });
            continue;
        };
        if !resolved.is_file() {
            retained.push(RetainedEntry {
                path: path_text.to_string(),
                reason: RetainReason::Missing,
            });
            continue;
        }
        let Some(recorded) = record.get("sha256").and_then(Value::as_str) else {
            retained.push(RetainedEntry {
                path: path_text.to_string(),
                reason: RetainReason::NoHash,
            });
            continue;
        };
        match storage::sha256_file(&resolved) {
            Ok(disk) if disk == recorded => deletable.push(resolved),
            _ => retained.push(RetainedEntry {
                path: path_text.to_string(),
                reason: RetainReason::HashMismatch,
            }),
        }
    }
    Ok(Some(LegacyPlan {
        manifest_path,
        old_version,
        deletable,
        retained,
    }))
}

/// `_resolve_record_path` port with the containment gate: v1 wrote absolute
/// paths, v2 wrote POSIX paths relative to the Claude home. Records that
/// escape `claude_root` (including via `..`) resolve to `None` — this
/// module never deletes outside the Claude directory.
fn resolve_record_path(path_text: &str, claude_root: &Path) -> Option<PathBuf> {
    let record = Path::new(path_text);
    if record
        .components()
        .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return None;
    }
    let resolved = if record.is_absolute() {
        record.to_path_buf()
    } else {
        claude_root.join(record)
    };
    if resolved.starts_with(claude_root) {
        Some(resolved)
    } else {
        None
    }
}

/// Executes an approved plan under the install lock: deletes the verified
/// files, drops settings.json hook entries whose command references a
/// deleted script's file name, and renames the legacy manifest to `.bak`.
/// Returns the deleted paths and any warnings.
pub(crate) fn execute(
    plan: &LegacyPlan,
    claude_root: &Path,
    remy_root: &Path,
) -> Result<(Vec<PathBuf>, Vec<String>), InstallError> {
    let _lock = lock::acquire(remy_root)?;
    let mut warnings = Vec::new();
    let mut deleted = Vec::new();
    let mut deleted_scripts: Vec<String> = Vec::new();
    for target in &plan.deletable {
        match fs::remove_file(target) {
            Ok(()) => {
                if let Some(name) = target.file_name() {
                    deleted_scripts.push(name.to_string_lossy().into_owned());
                }
                deleted.push(target.clone());
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => warnings.push(format!("could not delete {}: {error}", target.display())),
        }
    }
    remove_dead_hook_entries(claude_root, &deleted_scripts, &mut warnings);
    prune_empty_dirs(&deleted, claude_root);

    let bak = plan
        .manifest_path
        .with_file_name(format!("{}.bak", super::ops::LEGACY_MANIFEST_NAME));
    if let Err(error) = fs::rename(&plan.manifest_path, &bak) {
        warnings.push(format!(
            "could not rename the legacy manifest to .bak: {error}"
        ));
    }
    Ok((deleted, warnings))
}

/// Drops hook entries from settings.json whose command references one of
/// the just-deleted script file names (a registration pointing at a deleted
/// script fails on every session). Separator-normalized match, the
/// `is_legacy_default` technique. Unreadable settings only warn.
fn remove_dead_hook_entries(claude_root: &Path, deleted_scripts: &[String], warnings: &mut Vec<String>) {
    if deleted_scripts.is_empty() {
        return;
    }
    let script_names: Vec<&String> = deleted_scripts
        .iter()
        .filter(|name| name.ends_with(".py"))
        .collect();
    if script_names.is_empty() {
        return;
    }
    let settings_path = claude_root.join("settings.json");
    if !settings_path.is_file() {
        return;
    }
    let Ok(mut document) = storage::load_json(&settings_path) else {
        warnings.push("settings.json is unreadable; stale hook entries were not cleaned".to_string());
        return;
    };
    let Some(hooks) = document.get_mut("hooks").and_then(Value::as_object_mut) else {
        return;
    };
    let mut changed = false;
    let mut empty_events = Vec::new();
    for (event, entries) in hooks.iter_mut() {
        let Some(entries) = entries.as_array_mut() else {
            continue;
        };
        for entry in entries.iter_mut() {
            let Some(list) = entry.get_mut("hooks").and_then(Value::as_array_mut) else {
                continue;
            };
            let before = list.len();
            list.retain(|hook| {
                let command = hook
                    .get("command")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .replace('\\', "/");
                !script_names.iter().any(|name| command.contains(name.as_str()))
            });
            changed |= list.len() != before;
        }
        entries.retain(|entry| {
            entry
                .get("hooks")
                .and_then(Value::as_array)
                .is_none_or(|list| !list.is_empty())
        });
        if entries.is_empty() {
            empty_events.push(event.clone());
        }
    }
    for event in empty_events {
        hooks.remove(&event);
        changed = true;
    }
    if changed {
        if let Err(error) = storage::atomic_write_json(&settings_path, &document) {
            warnings.push(format!(
                "could not update settings.json after hook cleanup: {error}"
            ));
        }
    }
}

/// Removes directories emptied by the deletions, walking up to (but never
/// including) `claude_root`.
fn prune_empty_dirs(deleted: &[PathBuf], claude_root: &Path) {
    let mut parents: std::collections::BTreeSet<PathBuf> = std::collections::BTreeSet::new();
    for target in deleted {
        let mut parent = target.parent();
        while let Some(directory) = parent {
            if directory == claude_root {
                break;
            }
            parents.insert(directory.to_path_buf());
            parent = directory.parent();
        }
    }
    for directory in parents.iter().rev() {
        let _ = fs::remove_dir(directory);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn write_manifest(claude_root: &Path, value: &Value) {
        fs::create_dir_all(claude_root).expect("dirs");
        storage::atomic_write_json(
            &claude_root.join(super::super::ops::LEGACY_MANIFEST_NAME),
            value,
        )
        .expect("manifest");
    }

    fn seed(claude_root: &Path, relative: &str, bytes: &[u8]) -> String {
        let target = claude_root.join(relative);
        fs::create_dir_all(target.parent().unwrap()).expect("dirs");
        fs::write(&target, bytes).expect("seed");
        storage::sha256_hex(bytes)
    }

    #[test]
    fn absent_manifest_inspects_to_none() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert!(inspect(dir.path()).expect("inspect").is_none());
    }

    #[test]
    fn corrupt_or_unshaped_manifests_error_without_touching_disk() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join(super::super::ops::LEGACY_MANIFEST_NAME);
        fs::write(&path, b"{broken").expect("corrupt");
        assert!(inspect(dir.path()).is_err());
        write_manifest(dir.path(), &json!({"files": []}));
        assert!(inspect(dir.path()).is_err(), "missing version key");
        assert!(path.is_file() || dir.path().join(super::super::ops::LEGACY_MANIFEST_NAME).is_file());
    }

    #[test]
    fn hash_gate_partitions_records_and_v1_absolute_paths_resolve() {
        let dir = tempfile::tempdir().expect("tempdir");
        let claude = dir.path();
        let matching = seed(claude, "hooks/old_hook.py", b"payload");
        seed(claude, "hooks/modified.py", b"edited by user");
        let v1_absolute = claude.join("style.md");
        let v1_hash = seed(claude, "style.md", b"v1 content");
        write_manifest(
            claude,
            &json!({
                "version": "1.2.0",
                "files": [
                    {"path": "hooks/old_hook.py", "sha256": matching},
                    {"path": "hooks/modified.py", "sha256": "0".repeat(64)},
                    {"path": "hooks/no_hash.py"},
                    {"path": v1_absolute.to_string_lossy(), "sha256": v1_hash},
                    {"path": "../escape.md", "sha256": "0".repeat(64)},
                    {"path": "/outside/claude.md", "sha256": "0".repeat(64)},
                    {"path": "hooks/gone.py", "sha256": "0".repeat(64)}
                ]
            }),
        );
        seed(claude, "hooks/no_hash.py", b"unverifiable");
        let plan = inspect(claude).expect("inspect").expect("plan");
        assert_eq!(plan.old_version, "1.2.0");
        assert_eq!(
            plan.deletable,
            vec![claude.join("hooks/old_hook.py"), v1_absolute.clone()]
        );
        let reasons: Vec<RetainReason> = plan.retained.iter().map(|r| r.reason).collect();
        assert_eq!(
            reasons,
            vec![
                RetainReason::HashMismatch,
                RetainReason::NoHash,
                RetainReason::OutsideClaudeRoot,
                RetainReason::OutsideClaudeRoot,
                RetainReason::Missing,
            ]
        );
    }

    #[test]
    fn execute_deletes_prunes_cleans_hooks_and_renames_the_manifest() {
        let dir = tempfile::tempdir().expect("tempdir");
        let claude = dir.path().join("claude");
        let remy = dir.path().join("remy");
        let hash = seed(&claude, "hooks/tree_system/dead_hook.py", b"payload");
        let kept = seed(&claude, "hooks/user_hook.py", b"user's own");
        storage::atomic_write_json(
            &claude.join("settings.json"),
            &json!({"hooks": {
                "PreToolUse": [
                    {"matcher": "Read", "hooks": [
                        {"type": "command", "command": "python \"~/.claude/hooks/tree_system/dead_hook.py\""},
                        {"type": "command", "command": "python \"~/.claude/hooks/user_hook.py\""}
                    ]}
                ],
                "PostToolUse": [
                    {"matcher": "Edit", "hooks": [
                        {"type": "command", "command": "python \"C:\\\\Users\\\\x\\\\.claude\\\\hooks\\\\tree_system\\\\dead_hook.py\""}
                    ]}
                ]
            }}),
        )
        .expect("settings");
        write_manifest(
            &claude,
            &json!({
                "version": "1.4.4",
                "files": [
                    {"path": "hooks/tree_system/dead_hook.py", "sha256": hash},
                    {"path": "hooks/user_hook.py", "sha256": "0".repeat(64)}
                ]
            }),
        );
        let plan = inspect(&claude).expect("inspect").expect("plan");
        let (deleted, warnings) = execute(&plan, &claude, &remy).expect("execute");
        assert_eq!(warnings, Vec::<String>::new());
        assert_eq!(deleted, vec![claude.join("hooks/tree_system/dead_hook.py")]);
        assert!(!claude.join("hooks/tree_system").exists(), "pruned");
        assert!(claude.join("hooks/user_hook.py").is_file());
        assert_eq!(storage::sha256_file(&claude.join("hooks/user_hook.py")).expect("hash"), kept);
        assert!(!claude.join(super::super::ops::LEGACY_MANIFEST_NAME).exists());
        assert!(claude
            .join(format!("{}.bak", super::super::ops::LEGACY_MANIFEST_NAME))
            .is_file());
        let settings = storage::load_json(&claude.join("settings.json")).expect("settings");
        let pre = settings["hooks"]["PreToolUse"][0]["hooks"]
            .as_array()
            .expect("list");
        assert_eq!(pre.len(), 1);
        assert!(pre[0]["command"].as_str().expect("cmd").contains("user_hook.py"));
        assert!(settings["hooks"].get("PostToolUse").is_none(), "emptied event removed");
    }
}
