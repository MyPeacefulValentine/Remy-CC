//! Scanner worker supervision with bounded output capture.
//!
//! Jobs run under the provider snapshotted at claim time: the python arm
//! keeps the R2 probe/marker-cancel semantics byte for byte, the rust arm
//! re-execs the daemon binary's `scan` subcommand, reads its parameters from
//! rconfig (no python probe, empty secret set) and cancels by kill.

use std::env;
use std::fs;
use std::io::{self, BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::state::{Job, JOB_TYPE_FULL_SCAN, PROVIDER_RUST};

const OUTPUT_LIMIT: usize = 64 * 1024;
const POLL_INTERVAL: Duration = Duration::from_millis(20);
const FINALIZE_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug)]
pub enum WorkerEvent {
    Progress {
        job_id: i64,
        current: i64,
        message: String,
    },
    Complete {
        job_id: i64,
        outcome: WorkerOutcome,
    },
}

#[derive(Debug)]
pub enum WorkerOutcome {
    Succeeded(Value),
    Failed(Value),
}

#[derive(Debug)]
struct WorkerConfig {
    lock_timeout: f64,
    scan_timeout: u64,
    secret_values: Vec<String>,
}

#[derive(Debug)]
struct Captured {
    bytes: Vec<u8>,
    truncated: bool,
}

enum ScannerRuntime {
    Python { python: PathBuf, script: PathBuf },
    Rust { binary: PathBuf },
}

pub fn spawn(job: Job, sender: Sender<WorkerEvent>) -> io::Result<Arc<AtomicBool>> {
    let runtime = if job.provider == PROVIDER_RUST {
        ScannerRuntime::Rust {
            binary: crate::runtime::rust_scanner_binary()?,
        }
    } else {
        let (python, script) = crate::runtime::scanner_runtime()?;
        ScannerRuntime::Python { python, script }
    };
    let cancel = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&cancel);
    thread::Builder::new()
        .name(format!("remy-worker-{}", job.id))
        .spawn(move || supervise(job, runtime, sender, flag))?;
    Ok(cancel)
}

fn supervise(
    job: Job,
    runtime: ScannerRuntime,
    sender: Sender<WorkerEvent>,
    cancel: Arc<AtomicBool>,
) {
    let outcome = supervise_inner(&job, &runtime, &sender, &cancel)
        .unwrap_or_else(|error| WorkerOutcome::Failed(supervisor_error(&error)));
    let _ = sender.send(WorkerEvent::Complete {
        job_id: job.id,
        outcome,
    });
}

fn supervisor_error(error: &io::Error) -> Value {
    let message = error.to_string();
    let (kind, stage) = if message.contains("target_db_unavailable") {
        ("target_db_unavailable", "runtime_validation")
    } else if message.contains("worker configuration")
        || message.contains("worker_config")
        || message.contains("lock_timeout missing")
        || message.contains("scan_timeout missing")
        || message.contains("secret_values missing")
    {
        ("config_invalid", "runtime_validation")
    } else if message.contains("lock_timeout") {
        ("lock_timeout", "waiting_for_scan_lock")
    } else if message.contains("scan_timeout") {
        ("scan_timeout", "scanning")
    } else if message.contains("invalid_output") || error.kind() == io::ErrorKind::InvalidData {
        ("invalid_output", "finalizing")
    } else {
        ("spawn_failed", "runtime_validation")
    };
    error_value(kind, &message, None, stage, "", false, Vec::new())
}

fn supervise_inner(
    job: &Job,
    runtime: &ScannerRuntime,
    sender: &Sender<WorkerEvent>,
    cancel: &AtomicBool,
) -> io::Result<WorkerOutcome> {
    if !Path::new(&job.project_path).is_dir() {
        return Ok(WorkerOutcome::Failed(error_value(
            "project_unavailable",
            "project directory is unavailable",
            None,
            "runtime_validation",
            "",
            false,
            Vec::new(),
        )));
    }
    validate_target_db(&job.target_db_path)?;
    let full_scan = job.job_type == JOB_TYPE_FULL_SCAN;
    let (mut command, lock_timeout, scan_timeout, secrets, kill_on_cancel) = match runtime {
        ScannerRuntime::Python { python, script } => {
            let config = read_config(&(python.clone(), script.clone()), &job.project_path)?;
            let scan_timeout = if full_scan {
                full_scan_timeout(&job.project_path)?
            } else {
                config.scan_timeout
            };
            let mut command = Command::new(python);
            command
                .arg(script)
                .args([
                    "--result-json",
                    "--cwd",
                    &job.project_path,
                    "--lock-timeout",
                    &config.lock_timeout.to_string(),
                ])
                .env("REMY_LOGIC_INDEX_DB_PATH", &job.target_db_path)
                .env("PYTHONIOENCODING", "utf-8");
            if !full_scan {
                command.args(["--files", &job.file_path]);
            }
            (
                command,
                config.lock_timeout,
                scan_timeout,
                config.secret_values,
                false,
            )
        }
        ScannerRuntime::Rust { binary } => {
            let pconfig =
                scanner_core::rconfig::load(Path::new(&job.project_path)).map_err(|error| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("worker configuration invalid: {error}"),
                    )
                })?;
            let scan_timeout = if full_scan {
                pconfig.full_scan_timeout as u64
            } else {
                pconfig.struct_scan_timeout as u64
            };
            let mut command = Command::new(binary);
            command
                .arg("scan")
                .args(["--root", &job.project_path, "--db", &job.target_db_path])
                .args(["--result-json", "--progress-json"])
                .args(["--lock-timeout", &pconfig.scan_lock_timeout.to_string()]);
            if !full_scan {
                command.args(["--files", &job.file_path]);
            }
            (
                command,
                pconfig.scan_lock_timeout,
                scan_timeout,
                Vec::new(),
                true,
            )
        }
    };
    let _ = sender.send(WorkerEvent::Progress {
        job_id: job.id,
        current: 1,
        message: "waiting_for_scan_lock".to_string(),
    });
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let kill_flag = kill_on_cancel.then_some(cancel);
    let (lock_sender, lock_receiver) = mpsc::sync_channel(1);
    let stdout = read_stdout(
        child.stdout.take().expect("piped stdout"),
        lock_sender,
        sender.clone(),
        job.id,
    );
    let stderr = read_pipe(child.stderr.take().expect("piped stderr"));
    let status = match wait_for_lock(
        &mut child,
        &lock_receiver,
        Instant::now() + Duration::from_secs_f64(lock_timeout.max(0.0) + 5.0),
        kill_flag,
    )? {
        Some(status) => status,
        None => wait_until(
            &mut child,
            Instant::now() + Duration::from_secs(scan_timeout),
            "scan_timeout",
            kill_flag,
        )?,
    };
    let finalize_deadline = Instant::now() + FINALIZE_TIMEOUT;
    let stdout = join_reader(stdout, finalize_deadline, "stdout")?;
    let stderr = join_reader(stderr, finalize_deadline, "stderr")?;
    evaluate(status, stdout, stderr, &secrets, kill_on_cancel)
}

fn full_scan_timeout(project: &str) -> io::Result<u64> {
    scanner_core::rconfig::load(Path::new(project))
        .map(|config| config.full_scan_timeout as u64)
        .map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("worker configuration invalid: {error}"),
            )
        })
}

fn validate_target_db(value: &str) -> io::Result<()> {
    let path = Path::new(value);
    let parent = path.parent().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "target database has no parent")
    })?;
    fs::create_dir_all(parent)
        .map_err(|error| io::Error::new(error.kind(), format!("target_db_unavailable: {error}")))
}

fn read_config(runtime: &(PathBuf, PathBuf), project: &str) -> io::Result<WorkerConfig> {
    let mut child = Command::new(&runtime.0)
        .arg(&runtime.1)
        .args(["--worker-config-json", "--cwd", project])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let stdout = read_pipe(child.stdout.take().expect("piped stdout"));
    let stderr = read_pipe(child.stderr.take().expect("piped stderr"));
    let status = wait_until(
        &mut child,
        Instant::now() + FINALIZE_TIMEOUT,
        "worker configuration timeout",
        None,
    )?;
    let deadline = Instant::now() + FINALIZE_TIMEOUT;
    let stdout = join_reader(stdout, deadline, "configuration stdout")?;
    let stderr = join_reader(stderr, deadline, "configuration stderr")?;
    if stdout.truncated || stderr.truncated {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "worker configuration output exceeded 65536 bytes",
        ));
    }
    if !status.success() {
        return Err(io::Error::other(format!(
            "worker configuration probe failed: {}",
            String::from_utf8_lossy(&stderr.bytes)
        )));
    }
    let value: Value = serde_json::from_slice(&stdout.bytes)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    if value.get("type").and_then(Value::as_str) != Some("worker_config")
        || value.get("schema_version").and_then(Value::as_u64) != Some(1)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid worker configuration contract",
        ));
    }
    Ok(WorkerConfig {
        lock_timeout: value["lock_timeout"]
            .as_f64()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "lock_timeout missing"))?,
        scan_timeout: value["scan_timeout"]
            .as_u64()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "scan_timeout missing"))?,
        secret_values: value["secret_values"]
            .as_array()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "secret_values missing"))?
            .iter()
            .filter_map(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .collect(),
    })
}

fn read_stdout(
    pipe: impl Read + Send + 'static,
    lock_sender: mpsc::SyncSender<()>,
    event_sender: Sender<WorkerEvent>,
    job_id: i64,
) -> thread::JoinHandle<Captured> {
    thread::spawn(move || {
        let mut reader = BufReader::new(pipe);
        let mut retained = Vec::new();
        let mut truncated = false;
        let mut line = Vec::new();
        loop {
            line.clear();
            match reader.read_until(b'\n', &mut line) {
                Ok(0) | Err(_) => break,
                Ok(count) => {
                    let remaining = OUTPUT_LIMIT.saturating_sub(retained.len());
                    retained.extend_from_slice(&line[..count.min(remaining)]);
                    if count > remaining {
                        truncated = true;
                    }
                    if serde_json::from_slice::<Value>(&line)
                        .ok()
                        .is_some_and(|value| {
                            value.get("type").and_then(Value::as_str) == Some("progress")
                                && value.get("stage").and_then(Value::as_str)
                                    == Some("lock_acquired")
                        })
                    {
                        let _ = lock_sender.try_send(());
                        let _ = event_sender.send(WorkerEvent::Progress {
                            job_id,
                            current: 2,
                            message: "scanning".to_string(),
                        });
                    }
                }
            }
        }
        Captured {
            bytes: retained,
            truncated,
        }
    })
}

fn read_pipe(mut pipe: impl Read + Send + 'static) -> thread::JoinHandle<Captured> {
    thread::spawn(move || {
        let mut retained = Vec::new();
        let mut truncated = false;
        let mut buffer = [0_u8; 8192];
        loop {
            match pipe.read(&mut buffer) {
                Ok(0) | Err(_) => break,
                Ok(count) => {
                    let remaining = OUTPUT_LIMIT.saturating_sub(retained.len());
                    retained.extend_from_slice(&buffer[..count.min(remaining)]);
                    if count > remaining {
                        truncated = true;
                    }
                }
            }
        }
        Captured {
            bytes: retained,
            truncated,
        }
    })
}

fn cancelled_kill(
    child: &mut Child,
    cancel: Option<&AtomicBool>,
) -> io::Result<Option<ExitStatus>> {
    if cancel.is_some_and(|flag| flag.load(Ordering::Relaxed)) {
        child.kill()?;
        return child.wait().map(Some);
    }
    Ok(None)
}

fn wait_for_lock(
    child: &mut Child,
    receiver: &Receiver<()>,
    deadline: Instant,
    cancel: Option<&AtomicBool>,
) -> io::Result<Option<ExitStatus>> {
    loop {
        match receiver.try_recv() {
            Ok(()) => return Ok(None),
            Err(TryRecvError::Disconnected) => {}
            Err(TryRecvError::Empty) => {}
        }
        if let Some(status) = child.try_wait()? {
            return Ok(Some(status));
        }
        if let Some(status) = cancelled_kill(child, cancel)? {
            return Ok(Some(status));
        }
        if Instant::now() >= deadline {
            child.kill()?;
            let _ = child.wait();
            return Err(io::Error::new(io::ErrorKind::TimedOut, "lock_timeout"));
        }
        thread::sleep(POLL_INTERVAL);
    }
}

fn wait_until(
    child: &mut Child,
    deadline: Instant,
    kind: &'static str,
    cancel: Option<&AtomicBool>,
) -> io::Result<ExitStatus> {
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(status);
        }
        if let Some(status) = cancelled_kill(child, cancel)? {
            return Ok(status);
        }
        if Instant::now() >= deadline {
            child.kill()?;
            let _ = child.wait();
            return Err(io::Error::new(io::ErrorKind::TimedOut, kind));
        }
        thread::sleep(POLL_INTERVAL);
    }
}

fn join_reader(
    handle: thread::JoinHandle<Captured>,
    deadline: Instant,
    name: &str,
) -> io::Result<Captured> {
    while !handle.is_finished() {
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!("{name} finalization timed out"),
            ));
        }
        thread::sleep(POLL_INTERVAL);
    }
    handle
        .join()
        .map_err(|_| io::Error::other(format!("{name} reader panicked")))
}

fn valid_progress_line(value: &Value, rust: bool) -> bool {
    if rust {
        let Some(object) = value.as_object() else {
            return false;
        };
        object
            .keys()
            .all(|key| matches!(key.as_str(), "type" | "stage" | "elapsed_ms" | "files"))
            && matches!(
                value.get("stage").and_then(Value::as_str),
                Some("lock_acquired" | "parse_done" | "postprocess_done")
            )
    } else {
        has_exact_keys(value, &["type", "stage"])
            && value.get("stage").and_then(Value::as_str) == Some("lock_acquired")
    }
}

fn evaluate(
    status: ExitStatus,
    stdout: Captured,
    stderr: Captured,
    secrets: &[String],
    rust: bool,
) -> io::Result<WorkerOutcome> {
    if stdout.truncated {
        return Ok(WorkerOutcome::Failed(error_value(
            "invalid_output",
            "worker stdout exceeded 65536 bytes",
            status.code(),
            "finalizing",
            &sanitize(&String::from_utf8_lossy(&stderr.bytes), secrets),
            stderr.truncated,
            Vec::new(),
        )));
    }
    let text = std::str::from_utf8(&stdout.bytes)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid_output"))?;
    let mut terminal = None;
    for line in text.lines() {
        let value: Value = serde_json::from_str(line)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        match value.get("type").and_then(Value::as_str) {
            Some("progress") if terminal.is_none() => {
                if !valid_progress_line(&value, rust) {
                    return Ok(WorkerOutcome::Failed(error_value(
                        "invalid_output",
                        "worker emitted an unknown progress stage",
                        status.code(),
                        "finalizing",
                        "",
                        false,
                        Vec::new(),
                    )));
                }
            }
            Some("scan_result")
                if terminal.is_none()
                    && has_exact_keys(
                        &value,
                        &[
                            "type",
                            "schema_version",
                            "outcome",
                            "successful_paths",
                            "failed_paths",
                            "deleted_paths",
                            "postprocess_complete",
                            "errors",
                        ],
                    ) =>
            {
                terminal = Some(value)
            }
            _ => {
                return Ok(WorkerOutcome::Failed(error_value(
                    "invalid_output",
                    "worker emitted an invalid event sequence",
                    status.code(),
                    "finalizing",
                    &sanitize(&String::from_utf8_lossy(&stderr.bytes), secrets),
                    stderr.truncated,
                    Vec::new(),
                )))
            }
        }
    }
    let Some(result) = terminal else {
        return Ok(WorkerOutcome::Failed(error_value(
            "invalid_output",
            "worker emitted no scan_result",
            status.code(),
            "finalizing",
            &sanitize(&String::from_utf8_lossy(&stderr.bytes), secrets),
            stderr.truncated,
            Vec::new(),
        )));
    };
    if !valid_scan_result(&result) {
        return Ok(WorkerOutcome::Failed(error_value(
            "invalid_output",
            "worker scan_result fields are invalid",
            status.code(),
            "finalizing",
            "",
            false,
            Vec::new(),
        )));
    }
    let outcome = result.get("outcome").and_then(Value::as_str);
    match (status.code(), outcome) {
        (Some(0), Some("success")) | (Some(2), Some("partial")) => {
            Ok(WorkerOutcome::Succeeded(result))
        }
        _ => Ok(WorkerOutcome::Failed(error_value(
            "worker_failed",
            "scanner worker failed",
            status.code(),
            "scanning",
            &sanitize(&String::from_utf8_lossy(&stderr.bytes), secrets),
            stderr.truncated,
            result
                .get("errors")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default(),
        ))),
    }
}

fn has_exact_keys(value: &Value, expected: &[&str]) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    object.len() == expected.len() && expected.iter().all(|key| object.contains_key(*key))
}

fn valid_string_array(value: &Value) -> bool {
    value
        .as_array()
        .is_some_and(|items| items.iter().all(Value::is_string))
}

fn valid_error_array(value: &Value) -> bool {
    value.as_array().is_some_and(|items| {
        items.iter().all(|item| {
            has_exact_keys(item, &["stage", "path", "message"])
                && item.get("stage").is_some_and(Value::is_string)
                && item
                    .get("path")
                    .is_some_and(|path| path.is_null() || path.is_string())
                && item.get("message").is_some_and(Value::is_string)
        })
    })
}

fn valid_scan_result(result: &Value) -> bool {
    matches!(
        result.get("outcome").and_then(Value::as_str),
        Some("success" | "partial" | "failed")
    ) && result.get("schema_version").and_then(Value::as_u64) == Some(1)
        && valid_string_array(&result["successful_paths"])
        && valid_string_array(&result["failed_paths"])
        && valid_string_array(&result["deleted_paths"])
        && result["postprocess_complete"].is_boolean()
        && valid_error_array(&result["errors"])
}

fn sanitize(message: &str, secrets: &[String]) -> String {
    let mut output = message.to_string();
    for secret in secrets {
        if !secret.is_empty() {
            output = output.replace(secret, "<redacted>");
        }
    }
    if let Some(home) = env::var_os("HOME").or_else(|| env::var_os("USERPROFILE")) {
        output = output.replace(&home.to_string_lossy().to_string(), "~");
    }
    output
}

fn error_value(
    kind: &str,
    message: &str,
    exit_code: Option<i32>,
    stage: &str,
    stderr: &str,
    stderr_truncated: bool,
    details: Vec<Value>,
) -> Value {
    json!({
        "schema_version": 1,
        "kind": kind,
        "message": message,
        "exit_code": exit_code,
        "stage": stage,
        "stderr": stderr,
        "stderr_truncated": stderr_truncated,
        "details": details,
    })
}

#[cfg(test)]
mod tests {
    use std::io::Cursor;

    use super::*;

    #[test]
    fn scan_result_validation_rejects_unknown_and_mistyped_fields() {
        let valid = json!({
            "type": "scan_result",
            "schema_version": 1,
            "outcome": "partial",
            "successful_paths": ["a.py"],
            "failed_paths": ["b.py"],
            "deleted_paths": [],
            "postprocess_complete": true,
            "errors": [{"stage": "file_scan", "path": "b.py", "message": "bad"}],
        });
        assert!(has_exact_keys(
            &valid,
            &[
                "type",
                "schema_version",
                "outcome",
                "successful_paths",
                "failed_paths",
                "deleted_paths",
                "postprocess_complete",
                "errors",
            ]
        ));
        assert!(valid_scan_result(&valid));

        let mut unknown = valid.clone();
        unknown["extra"] = json!(true);
        assert!(!has_exact_keys(
            &unknown,
            &[
                "type",
                "schema_version",
                "outcome",
                "successful_paths",
                "failed_paths",
                "deleted_paths",
                "postprocess_complete",
                "errors",
            ]
        ));
        let mut mistyped = valid;
        mistyped["successful_paths"] = json!([1]);
        assert!(!valid_scan_result(&mistyped));
    }

    #[test]
    fn progress_line_validation_is_provider_aware() {
        let python_line = json!({"type": "progress", "stage": "lock_acquired"});
        assert!(valid_progress_line(&python_line, false));
        assert!(valid_progress_line(&python_line, true));

        let rust_line = json!({
            "type": "progress", "stage": "parse_done", "elapsed_ms": 12, "files": 4,
        });
        assert!(valid_progress_line(&rust_line, true));
        assert!(!valid_progress_line(&rust_line, false));

        let unknown_stage = json!({"type": "progress", "stage": "weird", "elapsed_ms": 1});
        assert!(!valid_progress_line(&unknown_stage, true));
        let unknown_key = json!({"type": "progress", "stage": "parse_done", "extra": 1});
        assert!(!valid_progress_line(&unknown_key, true));
    }

    #[test]
    fn bounded_reader_drains_and_marks_truncation() {
        let input = vec![b'x'; OUTPUT_LIMIT + 4096];
        let captured = read_pipe(Cursor::new(input)).join().unwrap();
        assert_eq!(captured.bytes.len(), OUTPUT_LIMIT);
        assert!(captured.truncated);
    }

    #[test]
    fn sanitizer_replaces_secrets_and_home_prefix() {
        let home = env::var("HOME")
            .or_else(|_| env::var("USERPROFILE"))
            .unwrap();
        let output = sanitize(
            &format!("{home}/project token-value"),
            &["token-value".to_string()],
        );
        assert_eq!(output, "~/project <redacted>");
    }

    #[test]
    fn non_utf8_stdout_is_classified_as_invalid_output() {
        let bytes = vec![0xff];
        let error = std::str::from_utf8(&bytes).unwrap_err();
        let classified = supervisor_error(&io::Error::new(io::ErrorKind::InvalidData, error));
        assert_eq!(classified["kind"], "invalid_output");
        assert_eq!(classified["stage"], "finalizing");
    }

    #[test]
    fn supervisor_errors_have_stable_kinds() {
        let lock = supervisor_error(&io::Error::new(io::ErrorKind::TimedOut, "lock_timeout"));
        assert_eq!(lock["kind"], "lock_timeout");
        assert_eq!(lock["stage"], "waiting_for_scan_lock");
        let scan = supervisor_error(&io::Error::new(io::ErrorKind::TimedOut, "scan_timeout"));
        assert_eq!(scan["kind"], "scan_timeout");
        assert_eq!(scan["stage"], "scanning");
    }
}
