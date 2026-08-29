//! Deferred-deletion register.
//!
//! File format and semantics are the v1 contract the retired v3 installer
//! also wrote (`{"schema_version": 1, "paths": [...]}`
//! at `<remy root>/install/pending_deletes.json`): registration merges and
//! deduplicates preserving order and tolerates a corrupt register; sweeping
//! silently drops entries outside the two managed roots, keeps entries whose
//! deletion fails, and never propagates errors into the surrounding
//! operation.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use super::storage;

pub(crate) const PENDING_SCHEMA_VERSION: u64 = 1;

pub(crate) fn pending_deletes_path(remy_root: &Path) -> PathBuf {
    remy_root.join("install").join("pending_deletes.json")
}

pub(crate) struct PendingDeletes {
    path: PathBuf,
    claude_root: PathBuf,
    remy_root: PathBuf,
}

impl PendingDeletes {
    pub(crate) fn new(claude_root: &Path, remy_root: &Path) -> Self {
        Self {
            path: pending_deletes_path(remy_root),
            claude_root: claude_root.to_path_buf(),
            remy_root: remy_root.to_path_buf(),
        }
    }

    /// Merge-registers `paths`, deduplicating while preserving order; a
    /// corrupt register is replaced. Failures are reported to the caller
    /// (registration is the one pending-deletes step whose failure matters:
    /// losing it strands residue forever).
    pub(crate) fn register(&self, paths: &[PathBuf]) -> std::io::Result<()> {
        let mut merged: Vec<String> = Vec::new();
        if let Ok(document) = storage::load_json(&self.path) {
            if let Some(entries) = document.get("paths").and_then(Value::as_array) {
                for entry in entries {
                    if let Some(text) = entry.as_str() {
                        if !merged.iter().any(|existing| existing == text) {
                            merged.push(text.to_string());
                        }
                    }
                }
            }
        }
        for path in paths {
            let text = path.to_string_lossy().into_owned();
            if !merged.iter().any(|existing| existing == &text) {
                merged.push(text);
            }
        }
        storage::atomic_write_json(
            &self.path,
            &json!({"schema_version": PENDING_SCHEMA_VERSION, "paths": merged}),
        )
    }

    /// Best-effort deletion of registered residues; see the module contract.
    pub(crate) fn sweep(&self) {
        if !self.path.exists() {
            return;
        }
        let paths: Vec<String> = storage::load_json(&self.path)
            .ok()
            .and_then(|document| {
                document
                    .get("paths")
                    .and_then(Value::as_array)
                    .map(|entries| {
                        entries
                            .iter()
                            .filter_map(Value::as_str)
                            .map(str::to_string)
                            .collect()
                    })
            })
            .unwrap_or_default();
        let mut remaining = Vec::new();
        for text in paths {
            let path = PathBuf::from(&text);
            if !self.is_managed(&path) {
                continue;
            }
            match fs::remove_file(&path) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(_) => remaining.push(text),
            }
        }
        if remaining.is_empty() {
            let _ = fs::remove_file(&self.path);
        } else {
            let _ = storage::atomic_write_json(
                &self.path,
                &json!({"schema_version": PENDING_SCHEMA_VERSION, "paths": remaining}),
            );
        }
    }

    fn is_managed(&self, path: &Path) -> bool {
        let resolved = path.canonicalize().unwrap_or_else(|_| path.to_path_buf());
        for root in [&self.claude_root, &self.remy_root] {
            let root = root.canonicalize().unwrap_or_else(|_| root.clone());
            if resolved.starts_with(&root) {
                return true;
            }
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn setup() -> (tempfile::TempDir, PendingDeletes, PathBuf, PathBuf) {
        let dir = tempfile::tempdir().expect("tempdir");
        let claude = dir.path().join("claude");
        let remy = dir.path().join("remy");
        fs::create_dir_all(&claude).expect("claude root");
        fs::create_dir_all(&remy).expect("remy root");
        let pending = PendingDeletes::new(&claude, &remy);
        (dir, pending, claude, remy)
    }

    #[test]
    fn register_merges_and_deduplicates_preserving_order() {
        let (_dir, pending, claude, _remy) = setup();
        let first = claude.join("a.bak");
        let second = claude.join("b.bak");
        pending
            .register(&[first.clone(), second.clone()])
            .expect("register");
        pending
            .register(&[second.clone(), claude.join("c.bak")])
            .expect("register");
        let document = storage::load_json(&pending.path).expect("load");
        let entries: Vec<&str> = document["paths"]
            .as_array()
            .expect("paths")
            .iter()
            .filter_map(Value::as_str)
            .collect();
        assert_eq!(entries.len(), 3);
        assert_eq!(entries[0], first.to_string_lossy());
        assert_eq!(document["schema_version"], 1);
    }

    #[test]
    fn register_replaces_a_corrupt_register() {
        let (_dir, pending, claude, _remy) = setup();
        fs::create_dir_all(pending.path.parent().unwrap()).expect("dir");
        fs::write(&pending.path, "{broken").expect("write");
        pending.register(&[claude.join("x.bak")]).expect("register");
        let document = storage::load_json(&pending.path).expect("load");
        assert_eq!(document["paths"].as_array().expect("paths").len(), 1);
    }

    #[test]
    fn sweep_deletes_managed_entries_and_removes_the_register() {
        let (_dir, pending, claude, _remy) = setup();
        let target = claude.join("stale.bak");
        fs::write(&target, b"residue").expect("write");
        pending
            .register(std::slice::from_ref(&target))
            .expect("register");
        pending.sweep();
        assert!(!target.exists());
        assert!(!pending.path.exists());
    }

    #[test]
    fn sweep_drops_unmanaged_entries_without_deleting() {
        let (dir, pending, _claude, _remy) = setup();
        let outside = dir.path().join("outside.txt");
        fs::write(&outside, b"keep me").expect("write");
        pending
            .register(std::slice::from_ref(&outside))
            .expect("register");
        pending.sweep();
        assert!(outside.exists());
        assert!(!pending.path.exists());
    }

    #[test]
    fn sweep_tolerates_missing_targets_and_corrupt_register() {
        let (_dir, pending, claude, _remy) = setup();
        pending
            .register(&[claude.join("never-existed.bak")])
            .expect("register");
        pending.sweep();
        assert!(!pending.path.exists());
        fs::create_dir_all(pending.path.parent().unwrap()).expect("dir");
        fs::write(&pending.path, "{broken").expect("write");
        pending.sweep();
        assert!(!pending.path.exists());
    }

    #[cfg(windows)]
    #[test]
    fn sweep_keeps_entries_whose_deletion_fails() {
        use std::os::windows::fs::OpenOptionsExt;
        let (_dir, pending, claude, _remy) = setup();
        let locked = claude.join("locked.bak");
        fs::write(&locked, b"residue").expect("write");
        // FILE_SHARE_READ only: exclude delete sharing so remove_file fails,
        // matching how a running executable or Python-held handle behaves.
        let _handle = fs::OpenOptions::new()
            .read(true)
            .share_mode(0x1)
            .open(&locked)
            .expect("hold open");
        pending
            .register(std::slice::from_ref(&locked))
            .expect("register");
        pending.sweep();
        assert!(pending.path.exists(), "entry must survive a failed delete");
        let document = storage::load_json(&pending.path).expect("load");
        assert_eq!(document["paths"].as_array().expect("paths").len(), 1);
    }
}
