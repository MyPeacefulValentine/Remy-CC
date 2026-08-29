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
use std::time::{Duration, SystemTime};

use serde_json::{json, Value};

use super::storage;

pub(crate) const PENDING_SCHEMA_VERSION: u64 = 1;

/// Age gate for the orphan sweep: younger staging files may belong to a
/// live writer and are left alone.
const STALE_STAGING_MAX_AGE: Duration = Duration::from_secs(24 * 60 * 60);

/// The `.old-{pid}` aside name (`with_extension`: the final extension is
/// replaced, not appended).
pub(crate) fn aside_path(target: &Path) -> PathBuf {
    target.with_extension(format!("old-{}", std::process::id()))
}

/// The two staging shapes an interrupted run leaves behind:
/// `.{name}.remy-stage-{pid}` and `.{name}.{pid}.tmp`. Dot prefix plus a
/// pure-digit pid segment keeps deployed files and the Python-side
/// `.{name}.tmp.{pid}.{tid}` shape out of the match.
fn is_stale_staging_name(name: &str) -> bool {
    if !name.starts_with('.') {
        return false;
    }
    if let Some((_, pid)) = name.rsplit_once(".remy-stage-") {
        return !pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit());
    }
    if let Some(rest) = name.strip_suffix(".tmp") {
        if let Some((_, pid)) = rest.rsplit_once('.') {
            return !pid.is_empty() && pid.bytes().all(|b| b.is_ascii_digit());
        }
    }
    false
}

pub(crate) fn pending_deletes_path(remy_root: &Path) -> PathBuf {
    remy_root
        .join(super::INSTALL_STATE_DIR)
        .join("pending_deletes.json")
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
    /// losing it strands residue forever); callers surface a failed
    /// registration as a warning naming the stranded path instead of
    /// aborting the operation.
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

    /// Registers one residue, returning the warning text (no output
    /// prefix) on failure; the caller picks the reporting channel.
    pub(crate) fn register_or_warn(&self, residue: &Path) -> Option<String> {
        let paths = [residue.to_path_buf()];
        if self.register(&paths).is_err() {
            Some(format!(
                "could not record {} for deferred deletion; remove it manually",
                residue.display()
            ))
        } else {
            None
        }
    }

    /// Best-effort deletion of registered residues and stale staging
    /// orphans; see the module contract.
    pub(crate) fn sweep(&self) {
        self.sweep_register();
        self.sweep_stale_staging();
    }

    fn sweep_register(&self) {
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

    /// Deletes staging orphans under the two managed roots: name matches a
    /// staging shape AND age exceeds the gate. Unreadable metadata skips
    /// the file; unreadable directories are skipped silently.
    fn sweep_stale_staging(&self) {
        let now = SystemTime::now();
        for root in [&self.claude_root, &self.remy_root] {
            let mut stack = vec![root.to_path_buf()];
            while let Some(directory) = stack.pop() {
                let Ok(entries) = fs::read_dir(&directory) else {
                    continue;
                };
                for entry in entries.flatten() {
                    let path = entry.path();
                    if entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false) {
                        stack.push(path);
                        continue;
                    }
                    let name = entry.file_name().to_string_lossy().into_owned();
                    if !is_stale_staging_name(&name) {
                        continue;
                    }
                    let stale = entry
                        .metadata()
                        .and_then(|metadata| metadata.modified())
                        .ok()
                        .and_then(|mtime| now.duration_since(mtime).ok())
                        .is_some_and(|age| age > STALE_STAGING_MAX_AGE);
                    if stale {
                        let _ = fs::remove_file(&path);
                    }
                }
            }
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
    fn staging_name_predicate_matches_only_the_two_rust_shapes() {
        assert!(is_stale_staging_name(".CLAUDE.md.remy-stage-123"));
        assert!(is_stale_staging_name(".manifest.json.99.tmp"));
        assert!(is_stale_staging_name("..claude.json.99.tmp"));
        assert!(!is_stale_staging_name("CLAUDE.md"));
        assert!(!is_stale_staging_name(".gitignore"));
        assert!(!is_stale_staging_name(".remy-config.json.tmp.123.456"));
        assert!(!is_stale_staging_name(".x.remy-stage-12a"));
        assert!(!is_stale_staging_name(".x..tmp"));
        assert!(!is_stale_staging_name("CLAUDE.old-123"));
    }

    #[test]
    fn sweep_removes_only_aged_staging_orphans() {
        let (_dir, pending, claude, remy) = setup();
        let nested = claude.join("hooks");
        fs::create_dir_all(&nested).expect("nested dir");
        let aged_stage = nested.join(".pre_tool_guard.py.remy-stage-4242");
        let aged_tmp = remy.join(".manifest.json.4242.tmp");
        let fresh_stage = claude.join(".CLAUDE.md.remy-stage-4242");
        let aged_python_shape = claude.join(".remy-config.json.tmp.4242.7");
        let deployed = nested.join("pre_tool_guard.py");
        for file in [
            &aged_stage,
            &aged_tmp,
            &fresh_stage,
            &aged_python_shape,
            &deployed,
        ] {
            fs::write(file, b"x").expect("write");
        }
        let old = SystemTime::now() - Duration::from_secs(48 * 60 * 60);
        for file in [&aged_stage, &aged_tmp, &aged_python_shape] {
            let handle = fs::OpenOptions::new().write(true).open(file).expect("open");
            handle
                .set_times(fs::FileTimes::new().set_modified(old))
                .expect("set mtime");
        }
        pending.sweep();
        assert!(!aged_stage.exists(), "aged stage orphan must be removed");
        assert!(!aged_tmp.exists(), "aged tmp orphan must be removed");
        assert!(fresh_stage.exists(), "fresh staging must survive");
        assert!(
            aged_python_shape.exists(),
            "python-shape staging must survive"
        );
        assert!(deployed.exists(), "deployed files must survive");
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
