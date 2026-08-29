//! Scanner provider validation and publication.
//!
//! The rust provider is the sole production provider. At daemon startup a
//! state.db without a published rust row (fresh, or carrying a historical
//! python row) triggers the two-level probe; only a fully validated binary
//! is published, and an actual publication schedules one background
//! full_scan per registered project. Validation failure publishes nothing
//! and surfaces a diagnostic (no runtime auto-degradation).

use std::fs;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::logging::JsonLogger;
use crate::protocol::ScannerStatus;
use crate::state::{StateStore, PROVIDER_RUST};

const PROBE_TIMEOUT: Duration = Duration::from_secs(120);
const PROBE_LOCK_TIMEOUT: &str = "10";
const PROBE_POLL: Duration = Duration::from_millis(20);

const PROBE_SOURCES: &[(&str, &str)] = &[
    (
        "probe.py",
        "def probe_add(a, b):\n    return a + b\n\n\ndef probe_caller():\n    return probe_add(1, 2)\n",
    ),
    (
        "probe.c",
        "int probe_add(int a, int b) { return a + b; }\nint probe_caller(void) { return probe_add(1, 2); }\n",
    ),
    (
        "probe.ts",
        "export function probeAdd(a: number, b: number): number {\n  return a + b;\n}\nexport function probeCaller(): number {\n  return probeAdd(1, 2);\n}\n",
    ),
    (
        "probe.rs",
        "pub fn probe_add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\npub fn probe_caller() -> i32 {\n    probe_add(1, 2)\n}\n",
    ),
];

pub struct SyncOutcome {
    pub status: ScannerStatus,
    pub full_scan_project_ids: Vec<i64>,
}

pub fn sync(
    store: &mut StateStore,
    daemon_version: &str,
    logger: &JsonLogger,
) -> io::Result<SyncOutcome> {
    let outcome = sync_with(store, daemon_version, || {
        let binary = std::env::current_exe()
            .map_err(|error| format!("cannot resolve current executable: {error}"))?;
        validate_rust(&binary, daemon_version)
    })?;
    let _ = logger.log(
        if outcome.status.diagnostic.is_some() {
            "warning"
        } else {
            "info"
        },
        "provider_sync",
        serde_json::json!({
            "desired": outcome.status.desired,
            "published": outcome
                .status
                .published
                .as_ref()
                .map(|row| row.provider.clone()),
            "diagnostic": outcome.status.diagnostic,
            "full_scan_projects": outcome.full_scan_project_ids.len(),
        }),
    );
    Ok(outcome)
}

fn sync_with(
    store: &mut StateStore,
    daemon_version: &str,
    validate: impl Fn() -> Result<String, String>,
) -> io::Result<SyncOutcome> {
    let published = store.published_provider().map_err(io::Error::other)?;
    if published
        .as_ref()
        .is_some_and(|row| row.provider == PROVIDER_RUST)
    {
        return Ok(SyncOutcome {
            status: ScannerStatus {
                desired: PROVIDER_RUST.to_string(),
                published,
                diagnostic: None,
            },
            full_scan_project_ids: Vec::new(),
        });
    }

    let probe_summary = match validate() {
        Ok(summary) => summary,
        Err(message) => {
            return Ok(SyncOutcome {
                status: ScannerStatus {
                    desired: PROVIDER_RUST.to_string(),
                    published,
                    diagnostic: Some(message),
                },
                full_scan_project_ids: Vec::new(),
            });
        }
    };

    let row = store
        .publish_provider(PROVIDER_RUST, daemon_version, &probe_summary)
        .map_err(io::Error::other)?;
    let full_scan_project_ids = store.project_ids().map_err(io::Error::other)?;
    Ok(SyncOutcome {
        status: ScannerStatus {
            desired: PROVIDER_RUST.to_string(),
            published: Some(row),
            diagnostic: None,
        },
        full_scan_project_ids,
    })
}

/// Level 1: `--version` re-exec handshake against the daemon's own version.
/// Level 2: micro-corpus scan into a throwaway database, checked against the
/// scan_result v1 contract, the exit code, and the logic index schema anchor.
pub fn validate_rust(binary: &Path, daemon_version: &str) -> Result<String, String> {
    let output = run_probe(Command::new(binary).arg("--version"))?;
    if !output.status_success {
        return Err(format!("version probe exited {:?}", output.exit_code));
    }
    if !output.stdout.contains(daemon_version) {
        return Err(format!(
            "version probe answered {:?}, expected {daemon_version}",
            output.stdout.trim()
        ));
    }

    let corpus = ProbeCorpus::materialize()?;
    let db = corpus.root.join("probe_logic_index.db");
    let scan = run_probe(
        Command::new(binary)
            .arg("scan")
            .arg("--root")
            .arg(&corpus.root)
            .arg("--db")
            .arg(&db)
            .args(["--result-json", "--lock-timeout", PROBE_LOCK_TIMEOUT]),
    )?;
    let result = parse_scan_result(&scan)?;
    let schema_version: String = rusqlite::Connection::open(&db)
        .and_then(|conn| {
            conn.query_row("SELECT value FROM meta WHERE key='version'", [], |row| {
                row.get(0)
            })
        })
        .map_err(|error| format!("probe database check failed: {error}"))?;
    if schema_version != scanner_core::SCHEMA_VERSION {
        return Err(format!(
            "probe database schema {schema_version}, expected {}",
            scanner_core::SCHEMA_VERSION
        ));
    }
    Ok(format!(
        "{{\"version\":\"{daemon_version}\",\"schema_version\":\"{schema_version}\",\"files\":{result}}}"
    ))
}

fn parse_scan_result(probe: &ProbeOutput) -> Result<usize, String> {
    if !probe.status_success {
        return Err(format!(
            "scan probe exited {:?}: {}",
            probe.exit_code,
            probe.stderr.trim()
        ));
    }
    let terminal = probe
        .stdout
        .lines()
        .rfind(|line| !line.trim().is_empty())
        .ok_or_else(|| "scan probe emitted no output".to_string())?;
    let value: Value = serde_json::from_str(terminal)
        .map_err(|error| format!("scan probe terminal line is not JSON: {error}"))?;
    if value.get("type").and_then(Value::as_str) != Some("scan_result")
        || value.get("schema_version").and_then(Value::as_u64) != Some(1)
        || value.get("outcome").and_then(Value::as_str) != Some("success")
    {
        return Err(format!(
            "scan probe violated the scan_result contract: {value}"
        ));
    }
    let successful = value
        .get("successful_paths")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    if successful != PROBE_SOURCES.len() {
        return Err(format!(
            "scan probe indexed {successful} of {} corpus files",
            PROBE_SOURCES.len()
        ));
    }
    Ok(successful)
}

struct ProbeCorpus {
    root: PathBuf,
}

impl ProbeCorpus {
    fn materialize() -> Result<Self, String> {
        let root =
            std::env::temp_dir().join(format!("remy_provider_probe_rust_{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root)
            .map_err(|error| format!("cannot create probe corpus directory: {error}"))?;
        for (name, source) in PROBE_SOURCES {
            fs::write(root.join(name), source)
                .map_err(|error| format!("cannot write probe corpus file {name}: {error}"))?;
        }
        Ok(Self { root })
    }
}

impl Drop for ProbeCorpus {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

struct ProbeOutput {
    status_success: bool,
    exit_code: Option<i32>,
    stdout: String,
    stderr: String,
}

fn run_probe(command: &mut Command) -> Result<ProbeOutput, String> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("probe spawn failed: {error}"))?;
    let stdout = read_probe_pipe(child.stdout.take().expect("piped stdout"));
    let stderr = read_probe_pipe(child.stderr.take().expect("piped stderr"));
    let status = wait_probe(&mut child)?;
    let stdout = stdout
        .join()
        .map_err(|_| "probe stdout reader panicked".to_string())?;
    let stderr = stderr
        .join()
        .map_err(|_| "probe stderr reader panicked".to_string())?;
    Ok(ProbeOutput {
        status_success: status.success(),
        exit_code: status.code(),
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
    })
}

fn read_probe_pipe(mut pipe: impl Read + Send + 'static) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut collected = Vec::new();
        let _ = pipe.read_to_end(&mut collected);
        collected
    })
}

fn wait_probe(child: &mut Child) -> Result<std::process::ExitStatus, String> {
    let deadline = Instant::now() + PROBE_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => {}
            Err(error) => return Err(format!("probe wait failed: {error}")),
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!(
                "probe timed out after {}s",
                PROBE_TIMEOUT.as_secs()
            ));
        }
        thread::sleep(PROBE_POLL);
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::SystemTime;

    use super::*;
    use crate::clock::fake::FakeClock;
    use crate::clock::Clock;
    use crate::state::{JobPriority, SubmitJob};

    fn store_in(home: &Path) -> StateStore {
        let clock = Arc::new(FakeClock::new(
            SystemTime::UNIX_EPOCH + Duration::from_secs(1_750_000_000),
        ));
        StateStore::open(home, clock as Arc<dyn Clock>).unwrap().0
    }

    fn register_project(store: &mut StateStore, project: &Path) -> i64 {
        store
            .submit(SubmitJob {
                project_path: project.to_string_lossy().into_owned(),
                db_path: project
                    .join("logic_index.db")
                    .to_string_lossy()
                    .into_owned(),
                file_path: "a.py".to_string(),
                priority: JobPriority::Background,
            })
            .unwrap()
            .job
            .project_id
    }

    #[test]
    fn bootstrap_probes_publishes_rust_and_schedules_full_scans() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let mut store = store_in(home.path());
        let project_id = register_project(&mut store, project.path());

        let outcome = sync_with(&mut store, "0.2.0", || Ok("probe-ok".to_string())).unwrap();
        assert_eq!(
            outcome.status.published.as_ref().unwrap().provider,
            PROVIDER_RUST
        );
        assert_eq!(outcome.full_scan_project_ids, vec![project_id]);
        assert!(outcome.status.diagnostic.is_none());
        assert_eq!(
            store.published_provider().unwrap().unwrap().probe_summary,
            "probe-ok"
        );

        let repeat = sync_with(&mut store, "0.2.0", || -> Result<String, String> {
            panic!("published rust must not probe")
        })
        .unwrap();
        assert!(repeat.full_scan_project_ids.is_empty());
        assert!(repeat.status.diagnostic.is_none());
    }

    #[test]
    fn historical_python_row_is_replaced_after_probe() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let mut store = store_in(home.path());
        let project_id = register_project(&mut store, project.path());
        rusqlite::Connection::open(home.path().join("state.db"))
            .unwrap()
            .execute(
                "INSERT INTO published_provider(id, provider, daemon_version, verified_at, probe_summary) \
                 VALUES (1, 'python', '0.6.2', 1, '{}')",
                [],
            )
            .unwrap();

        let outcome = sync_with(&mut store, "0.2.0", || Ok("probe-ok".to_string())).unwrap();
        assert_eq!(
            outcome.status.published.as_ref().unwrap().provider,
            PROVIDER_RUST
        );
        assert_eq!(outcome.full_scan_project_ids, vec![project_id]);
        assert_eq!(
            store.published_provider().unwrap().unwrap().provider,
            PROVIDER_RUST
        );
    }

    #[test]
    fn failed_validation_publishes_nothing_and_reports_diagnostic() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let mut store = store_in(home.path());
        register_project(&mut store, project.path());

        let outcome = sync_with(&mut store, "0.2.0", || Err("probe exploded".to_string())).unwrap();
        assert!(outcome.status.published.is_none());
        assert!(outcome.full_scan_project_ids.is_empty());
        assert_eq!(outcome.status.diagnostic.as_deref(), Some("probe exploded"));
        assert!(store.published_provider().unwrap().is_none());
    }

    #[test]
    fn rust_probe_rejects_missing_binary() {
        let error = validate_rust(Path::new("remy-cc-does-not-exist"), "0.2.0").unwrap_err();
        assert!(error.contains("probe spawn failed"), "{error}");
    }
}
