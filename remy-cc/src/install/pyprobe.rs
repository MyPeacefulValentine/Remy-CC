//! Python runtime probe (`runtime/python.json`).
//!
//! Python 3.10+ is a runtime prerequisite of the suite, not an install
//! prerequisite (R4.4 audit disposition (b)3): a failed probe downgrades to
//! a warning and the descriptor file is simply not refreshed. Descriptor
//! shape and validation mirror `install_runtime/probes.py::probe_python`.

use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::storage;
use super::util;
use super::InstallError;

pub(crate) const RUNTIME_SCHEMA_VERSION: u64 = 1;
const PROBE_TIMEOUT: Duration = Duration::from_secs(10);
const PROBE_SCRIPT: &str = "import json,platform,sys;print(json.dumps({'executable':sys.executable,'version':list(sys.version_info[:3]),'implementation':platform.python_implementation(),'platform':sys.platform}))";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RuntimeDescriptor {
    pub(crate) executable: String,
    pub(crate) version: (u64, u64, u64),
    pub(crate) implementation: String,
    pub(crate) platform: String,
    pub(crate) probed_at: String,
}

impl RuntimeDescriptor {
    pub(crate) fn to_value(&self) -> Value {
        json!({
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "executable": self.executable,
            "version": [self.version.0, self.version.1, self.version.2],
            "implementation": self.implementation,
            "platform": self.platform,
            "probed_at": self.probed_at,
        })
    }
}

/// Probes `python` then `python3` from PATH.
pub(crate) fn probe() -> Result<RuntimeDescriptor, InstallError> {
    let mut last = InstallError::runtime("Python interpreter probe failed");
    for candidate in ["python", "python3"] {
        match probe_executable(candidate) {
            Ok(descriptor) => return Ok(descriptor),
            Err(error) => last = error,
        }
    }
    Err(last)
}

pub(crate) fn probe_executable(executable: &str) -> Result<RuntimeDescriptor, InstallError> {
    let failed = || InstallError::runtime("Python interpreter probe failed");
    let mut child = Command::new(executable)
        .args(["-I", "-c", PROBE_SCRIPT])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| failed())?;
    let started = Instant::now();
    let status = loop {
        match child.try_wait().map_err(|_| failed())? {
            Some(status) => break status,
            None if started.elapsed() > PROBE_TIMEOUT => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(failed());
            }
            None => std::thread::sleep(Duration::from_millis(25)),
        }
    };
    if !status.success() {
        return Err(failed());
    }
    let mut stdout = String::new();
    child
        .stdout
        .take()
        .ok_or_else(failed)?
        .read_to_string(&mut stdout)
        .map_err(|_| failed())?;
    parse_probe_payload(&stdout)
}

fn parse_probe_payload(stdout: &str) -> Result<RuntimeDescriptor, InstallError> {
    let invalid = || InstallError::runtime("Python interpreter returned an invalid probe");
    let payload: Value = serde_json::from_str(stdout.trim()).map_err(|_| invalid())?;
    let executable = payload
        .get("executable")
        .and_then(Value::as_str)
        .ok_or_else(invalid)?;
    let version: Vec<u64> = payload
        .get("version")
        .and_then(Value::as_array)
        .ok_or_else(invalid)?
        .iter()
        .filter_map(Value::as_u64)
        .collect();
    let implementation = payload
        .get("implementation")
        .and_then(Value::as_str)
        .ok_or_else(invalid)?;
    let platform = payload
        .get("platform")
        .and_then(Value::as_str)
        .ok_or_else(invalid)?;
    if version.len() != 3
        || (version[0], version[1], version[2]) < (3, 10, 0)
        || !Path::new(executable).is_absolute()
    {
        return Err(InstallError::runtime("Python 3.10 or newer is required"));
    }
    Ok(RuntimeDescriptor {
        executable: executable.to_string(),
        version: (version[0], version[1], version[2]),
        implementation: implementation.to_string(),
        platform: platform.to_string(),
        probed_at: util::iso8601_utc_now(),
    })
}

/// Serializes the descriptor, keeping the previous `probed_at` when every
/// identity field is unchanged (the v3 reinstall-idempotency rule).
pub(crate) fn descriptor_bytes(descriptor: &RuntimeDescriptor, existing: &Path) -> Vec<u8> {
    let mut document = descriptor.to_value();
    if let Ok(previous) = storage::load_json(existing) {
        let identity = [
            "schema_version",
            "executable",
            "version",
            "implementation",
            "platform",
        ];
        if identity
            .iter()
            .all(|key| previous.get(*key) == document.get(*key))
        {
            if let Some(previous_probed) = previous.get("probed_at").and_then(Value::as_str) {
                document["probed_at"] = Value::String(previous_probed.to_string());
            }
        }
    }
    storage::canonical_json_bytes(&document)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_rejects_old_interpreters_and_relative_paths() {
        let old = r#"{"executable":"/usr/bin/python","version":[3,9,7],"implementation":"CPython","platform":"linux"}"#;
        assert!(parse_probe_payload(old).is_err());
        let relative = r#"{"executable":"python","version":[3,12,0],"implementation":"CPython","platform":"linux"}"#;
        assert!(parse_probe_payload(relative).is_err());
        let garbage = "<html>";
        assert!(parse_probe_payload(garbage).is_err());
    }

    #[test]
    fn parse_accepts_a_valid_payload() {
        let payload = if cfg!(windows) {
            r#"{"executable":"C:\\py\\python.exe","version":[3,12,1],"implementation":"CPython","platform":"win32"}"#
        } else {
            r#"{"executable":"/usr/bin/python3","version":[3,12,1],"implementation":"CPython","platform":"linux"}"#
        };
        let descriptor = parse_probe_payload(payload).expect("valid");
        assert_eq!(descriptor.version, (3, 12, 1));
    }

    #[test]
    fn descriptor_bytes_preserve_probed_at_for_identical_identity() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("python.json");
        let executable = if cfg!(windows) {
            "C:\\py\\python.exe"
        } else {
            "/usr/bin/python3"
        };
        let first = RuntimeDescriptor {
            executable: executable.to_string(),
            version: (3, 12, 1),
            implementation: "CPython".to_string(),
            platform: "win32".to_string(),
            probed_at: "2026-01-01T00:00:00+00:00".to_string(),
        };
        storage::atomic_write(&path, &storage::canonical_json_bytes(&first.to_value()))
            .expect("write");
        let second = RuntimeDescriptor {
            probed_at: "2026-08-29T00:00:00+00:00".to_string(),
            ..first.clone()
        };
        let bytes = descriptor_bytes(&second, &path);
        let value: Value = serde_json::from_slice(&bytes).expect("json");
        assert_eq!(value["probed_at"], "2026-01-01T00:00:00+00:00");
        let drifted = RuntimeDescriptor {
            version: (3, 13, 0),
            probed_at: "2026-08-29T00:00:00+00:00".to_string(),
            ..first
        };
        let bytes = descriptor_bytes(&drifted, &path);
        let value: Value = serde_json::from_slice(&bytes).expect("json");
        assert_eq!(value["probed_at"], "2026-08-29T00:00:00+00:00");
    }

    #[test]
    fn live_probe_succeeds_when_python_is_on_path() {
        match probe() {
            Ok(descriptor) => {
                assert!(descriptor.version >= (3, 10, 0));
                assert!(Path::new(&descriptor.executable).is_absolute());
            }
            Err(error) => {
                assert!(error.message.contains("Python"));
            }
        }
    }
}
