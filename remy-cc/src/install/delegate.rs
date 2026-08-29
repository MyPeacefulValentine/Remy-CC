//! Delegation of the configuration and summary command families to the
//! deployed Python CLI (the summary runtime and config UI stay
//! Python-owned; the binary only routes).
//!
//! Python resolution order: the recorded runtime descriptor, then `python`
//! and `python3` from PATH. A missing interpreter or missing deployed CLI
//! is reported against the v2.0.0 runtime-prerequisite declaration.

use std::path::Path;
use std::process::{Command, ExitCode};

use serde_json::Value;

use super::{pyprobe, resolve_roots, storage};

pub(crate) fn run_delegated(subcommand: &str, args: &[String]) -> ExitCode {
    let roots = match resolve_roots() {
        Ok(roots) => roots,
        Err(message) => {
            eprintln!("remy-cc {subcommand}: {message}");
            return ExitCode::from(2);
        }
    };
    let cli = roots.claude.join("remy-src").join("cli.py");
    if !cli.is_file() {
        eprintln!(
            "remy-cc {subcommand}: deployed Python CLI is missing at {}; run remy-cc install first",
            cli.display()
        );
        return ExitCode::from(2);
    }
    let Some(python) = resolve_python(&roots.remy) else {
        eprintln!(
            "remy-cc {subcommand}: no usable Python interpreter found; Python 3.10+ is a runtime prerequisite for the configuration and summary commands"
        );
        return ExitCode::from(2);
    };
    match delegated_status(&python, &cli, subcommand, args) {
        Ok(code) => ExitCode::from(code.min(255) as u8),
        Err(message) => {
            eprintln!("remy-cc {subcommand}: {message}");
            ExitCode::from(2)
        }
    }
}

pub(crate) fn delegated_status(
    python: &str,
    cli: &Path,
    subcommand: &str,
    args: &[String],
) -> Result<i32, String> {
    let status = Command::new(python)
        .arg(cli)
        .arg(subcommand)
        .args(args)
        .status()
        .map_err(|error| format!("cannot spawn {python}: {error}"))?;
    Ok(status.code().unwrap_or(2))
}

/// The recorded runtime descriptor wins; PATH probing is the fallback.
pub(crate) fn resolve_python(remy_root: &Path) -> Option<String> {
    let descriptor_path = remy_root.join("runtime").join("python.json");
    if let Ok(descriptor) = storage::load_json(&descriptor_path) {
        if let Some(executable) = descriptor.get("executable").and_then(Value::as_str) {
            if pyprobe::probe_executable(executable).is_ok() {
                return Some(executable.to_string());
            }
        }
    }
    for candidate in ["python", "python3"] {
        if pyprobe::probe_executable(candidate).is_ok() {
            return Some(candidate.to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn python_on_path() -> Option<String> {
        for candidate in ["python", "python3"] {
            if pyprobe::probe_executable(candidate).is_ok() {
                return Some(candidate.to_string());
            }
        }
        None
    }

    #[test]
    fn delegated_status_forwards_subcommand_args_and_exit_code() {
        let Some(python) = python_on_path() else {
            return;
        };
        let dir = tempfile::tempdir().expect("tempdir");
        let cli = dir.path().join("cli.py");
        std::fs::write(
            &cli,
            "import sys\nassert sys.argv[1:] == ['summary-audit', '--node-ref', 'file:x.py'], sys.argv\nsys.exit(7)\n",
        )
        .expect("cli");
        let code = delegated_status(
            &python,
            &cli,
            "summary-audit",
            &["--node-ref".to_string(), "file:x.py".to_string()],
        )
        .expect("status");
        assert_eq!(code, 7);
    }

    #[test]
    fn resolve_python_prefers_the_recorded_descriptor() {
        let Some(python) = python_on_path() else {
            return;
        };
        let descriptor = pyprobe::probe_executable(&python).expect("probe");
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("runtime").join("python.json");
        storage::atomic_write(
            &path,
            &storage::canonical_json_bytes(&descriptor.to_value()),
        )
        .expect("write");
        let resolved = resolve_python(dir.path()).expect("resolved");
        assert_eq!(resolved, descriptor.executable);
    }

    #[test]
    fn resolve_python_falls_back_to_path_when_the_descriptor_is_stale() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("runtime").join("python.json");
        storage::atomic_write(
            &path,
            &storage::canonical_json_bytes(&serde_json::json!({
                "executable": dir.path().join("gone-python").to_string_lossy(),
            })),
        )
        .expect("write");
        let resolved = resolve_python(dir.path());
        match python_on_path() {
            Some(_) => assert!(resolved.is_some(), "PATH fallback must engage"),
            None => assert!(resolved.is_none()),
        }
    }
}
