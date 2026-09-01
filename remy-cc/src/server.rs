//! IPC server: loopback TCP listener and JSON Lines commands.
//!
//! The session token prevents accidental cross-user connections; it is not a
//! security boundary.

use std::fs::{self};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::time::Duration;

use crate::logging::JsonLogger;
use crate::protocol::{Request, Response, ScannerStatus, MAX_LINE_BYTES, PROTOCOL_VERSION};
use crate::scheduler::SchedulerHandle;
use crate::state::{ListJobs, StateError, SubmitJob, STATE_SCHEMA_VERSION};
use crate::ui_host::{OpenOutcome, UiHost};

pub const PORT_FILE: &str = "daemon.port";
pub const TOKEN_FILE: &str = "daemon.token";
const IO_TIMEOUT: Duration = Duration::from_secs(2);
const TOKEN_CHARS: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const TOKEN_LENGTH: usize = 43;

pub fn serve(
    run_dir: &Path,
    daemon_version: &str,
    logger: &JsonLogger,
    scheduler: &SchedulerHandle,
    scanner: ScannerStatus,
    ui_host: &UiHost,
) -> io::Result<()> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();

    let token = generate_token();
    write_token(run_dir, &token)?;
    write_port(run_dir, port)?;

    logger.log(
        "info",
        "ipc_listening",
        serde_json::json!({
            "port": port,
            "protocol_version": PROTOCOL_VERSION,
            "state_schema_version": STATE_SCHEMA_VERSION,
        }),
    )?;

    loop {
        let (stream, _) = listener.accept()?;
        match handle_connection(
            &stream,
            &token,
            daemon_version,
            logger,
            scheduler,
            &scanner,
            ui_host,
        )? {
            ConnectionOutcome::Continue => {}
            ConnectionOutcome::Shutdown => {
                logger.log("info", "ipc_shutdown_requested", serde_json::Value::Null)?;
                return Ok(());
            }
        }
    }
}

enum ConnectionOutcome {
    Continue,
    Shutdown,
}

fn handle_connection(
    stream: &TcpStream,
    token: &str,
    daemon_version: &str,
    logger: &JsonLogger,
    scheduler: &SchedulerHandle,
    scanner: &ScannerStatus,
    ui_host: &UiHost,
) -> io::Result<ConnectionOutcome> {
    let _ = stream.set_read_timeout(Some(IO_TIMEOUT));
    let _ = stream.set_write_timeout(Some(IO_TIMEOUT));

    let reader = BufReader::new(stream.take(MAX_LINE_BYTES));
    let mut writer = BufWriter::new(stream);

    for line in reader.lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => return Ok(ConnectionOutcome::Continue),
        };

        let request: Request = match serde_json::from_str(&line) {
            Ok(request) => request,
            Err(_) => {
                write_response(
                    &mut writer,
                    &Response::error("invalid_request", "request is not valid protocol JSON"),
                );
                continue;
            }
        };

        if request.token() != token {
            write_response(
                &mut writer,
                &Response::error("bad_token", "session token does not match"),
            );
            return Ok(ConnectionOutcome::Continue);
        }

        if let Some((protocol_version, state_schema_version)) = request.business_versions() {
            if protocol_version != PROTOCOL_VERSION {
                write_response(
                    &mut writer,
                    &Response::error(
                        "incompatible_protocol",
                        format!(
                            "client protocol {protocol_version}; daemon protocol {PROTOCOL_VERSION}"
                        ),
                    ),
                );
                continue;
            }
            if state_schema_version != STATE_SCHEMA_VERSION {
                write_response(
                    &mut writer,
                    &Response::error(
                        "incompatible_state_schema",
                        format!(
                            "client state schema {state_schema_version}; daemon state schema {STATE_SCHEMA_VERSION}"
                        ),
                    ),
                );
                continue;
            }
        }

        match request {
            Request::Hello { .. } => {
                write_response(&mut writer, &Response::hello(daemon_version.to_string()));
            }
            Request::Ping { .. } => write_response(&mut writer, &Response::Ack),
            Request::Shutdown { .. } => {
                write_response(&mut writer, &Response::Ack);
                return Ok(ConnectionOutcome::Shutdown);
            }
            Request::SubmitJob {
                project_path,
                db_path,
                file_path,
                priority,
                ..
            } => match scheduler.submit(SubmitJob {
                project_path,
                db_path,
                file_path,
                priority,
            }) {
                Ok(result) => {
                    let _ = logger.log(
                        "info",
                        if result.created {
                            "job_submitted"
                        } else {
                            "job_reused"
                        },
                        serde_json::json!({"job_id": result.job.id}),
                    );
                    write_response(
                        &mut writer,
                        &Response::Submitted {
                            job: result.job,
                            created: result.created,
                        },
                    );
                }
                Err(error) if error.is_fatal() => return Err(io::Error::other(error)),
                Err(error) => write_response(&mut writer, &state_error_response(error)),
            },
            Request::PromoteJob {
                job_id, priority, ..
            } => match scheduler.promote(job_id, priority) {
                Ok(result) => write_response(
                    &mut writer,
                    &Response::Promoted {
                        job: result.job,
                        changed: result.changed,
                    },
                ),
                Err(error) if error.is_fatal() => return Err(io::Error::other(error)),
                Err(error) => write_response(&mut writer, &state_error_response(error)),
            },
            Request::GetJob { job_id, .. } => match scheduler.get(job_id) {
                Ok(job) => write_response(&mut writer, &Response::Job { job }),
                Err(error) if error.is_fatal() => return Err(io::Error::other(error)),
                Err(error) => write_response(&mut writer, &state_error_response(error)),
            },
            Request::CancelJob { job_id, .. } => match scheduler.cancel(job_id) {
                Ok(result) => {
                    if result.changed {
                        let _ = logger.log(
                            "info",
                            "job_cancel_updated",
                            serde_json::json!({
                                "job_id": result.job.id,
                                "status": result.job.status,
                            }),
                        );
                    }
                    write_response(
                        &mut writer,
                        &Response::Cancelled {
                            job: result.job,
                            changed: result.changed,
                        },
                    );
                }
                Err(error) if error.is_fatal() => return Err(io::Error::other(error)),
                Err(error) => write_response(&mut writer, &state_error_response(error)),
            },
            Request::ListJobs {
                project_path,
                file_path,
                status,
                job_type,
                limit,
                ..
            } => {
                let applied_limit = limit.unwrap_or(50).clamp(1, 200);
                let filters = crate::protocol::JobFilters {
                    project_path: project_path.clone(),
                    file_path: file_path.clone(),
                    status,
                    job_type: job_type.clone(),
                };
                match scheduler.list(ListJobs {
                    project_path,
                    file_path,
                    status,
                    job_type,
                    limit: applied_limit,
                }) {
                    Ok(jobs) => write_response(
                        &mut writer,
                        &Response::JobList {
                            jobs,
                            limit: applied_limit,
                            filters,
                        },
                    ),
                    Err(error) if error.is_fatal() => return Err(io::Error::other(error)),
                    Err(error) => write_response(&mut writer, &state_error_response(error)),
                }
            }
            Request::StatusSnapshot { .. } => match scheduler.status() {
                Ok((active_jobs, recent_errors)) => write_response(
                    &mut writer,
                    &Response::Status {
                        active_jobs,
                        recent_errors,
                        scanner: scanner.clone(),
                        ui: ui_host.status(),
                    },
                ),
                Err(error) if error.is_fatal() => return Err(io::Error::other(error)),
                Err(error) => write_response(&mut writer, &state_error_response(error)),
            },
            Request::OpenConfigUi { mode, target, .. } => {
                let combination_valid = match mode.as_str() {
                    "global" => target.is_none(),
                    "project" => target.is_some(),
                    _ => false,
                };
                if !combination_valid {
                    write_response(
                        &mut writer,
                        &Response::error(
                            "invalid_request",
                            "mode must be \"global\" without target or \"project\" with target",
                        ),
                    );
                    continue;
                }
                match ui_host.open(&mode, target.as_deref()) {
                    OpenOutcome::Ready { url, token, .. } => {
                        write_response(&mut writer, &Response::ConfigUi { url, token });
                    }
                    OpenOutcome::Conflict {
                        mode,
                        target,
                        pid,
                        port,
                    } => write_response(
                        &mut writer,
                        &Response::error(
                            "ui_conflict",
                            format!(
                                "config UI already running with mode={mode} target={} (pid {pid}, port {port})",
                                target.as_deref().unwrap_or("-")
                            ),
                        ),
                    ),
                    OpenOutcome::SpawnFailed { diagnostic } => write_response(
                        &mut writer,
                        &Response::error("ui_spawn_failed", diagnostic),
                    ),
                }
            }
        }
    }
    Ok(ConnectionOutcome::Continue)
}

fn state_error_response(error: StateError) -> Response {
    let code = match error {
        StateError::InvalidInput(_) => "invalid_request",
        StateError::NotFound(_) => "not_found",
        StateError::NotCancellable(_) => "not_cancellable",
        StateError::InvalidTransition { .. } => "invalid_transition",
        StateError::Sqlite(_) | StateError::Io(_) | StateError::Corrupt(_) => "storage_error",
    };
    Response::error(code, error.to_string())
}

fn write_response(writer: &mut impl Write, response: &Response) {
    if let Ok(json) = serde_json::to_string(response) {
        let _ = writeln!(writer, "{json}");
        let _ = writer.flush();
    }
}

fn generate_token() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};

    let state = RandomState::new();
    let mut token = String::with_capacity(TOKEN_LENGTH);
    let mut round: u64 = 0;
    while token.len() < TOKEN_LENGTH {
        let mut hasher = state.build_hasher();
        hasher.write_u64(round);
        let mut bits = hasher.finish();
        for _ in 0..10 {
            if token.len() == TOKEN_LENGTH {
                break;
            }
            token.push(TOKEN_CHARS[(bits & 63) as usize] as char);
            bits >>= 6;
        }
        round += 1;
    }
    token
}

fn write_token(run_dir: &Path, token: &str) -> io::Result<()> {
    fs::create_dir_all(run_dir)?;
    let path = run_dir.join(TOKEN_FILE);
    fs::write(&path, token)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn write_port(run_dir: &Path, port: u16) -> io::Result<()> {
    fs::write(run_dir.join(PORT_FILE), port.to_string())
}

pub fn clean_stale_endpoints(run_dir: &Path) -> io::Result<Vec<&'static str>> {
    let mut removed = Vec::new();
    for name in [PORT_FILE, TOKEN_FILE] {
        match fs::remove_file(run_dir.join(name)) {
            Ok(()) => removed.push(name),
            Err(err) if err.kind() == io::ErrorKind::NotFound => {}
            Err(err) => return Err(err),
        }
    }
    Ok(removed)
}

pub fn read_port(run_dir: &Path) -> Option<u16> {
    fs::read_to_string(run_dir.join(PORT_FILE))
        .ok()?
        .trim()
        .parse()
        .ok()
}

pub fn read_token(run_dir: &Path) -> Option<String> {
    let token = fs::read_to_string(run_dir.join(TOKEN_FILE)).ok()?;
    let token = token.trim();
    if token.is_empty() {
        None
    } else {
        Some(token.to_string())
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::{Duration, SystemTime};

    use super::*;
    use crate::clock::fake::FakeClock;
    use crate::clock::Clock;
    use crate::state::{JobStatus, StateStore};

    fn clock() -> Arc<dyn Clock> {
        Arc::new(FakeClock::new(
            SystemTime::UNIX_EPOCH + Duration::from_secs(1_754_000_000),
        ))
    }

    fn test_logger(dir: &Path) -> JsonLogger {
        JsonLogger::new(dir, 1024 * 1024, clock()).unwrap()
    }

    fn test_scanner_status() -> ScannerStatus {
        ScannerStatus {
            desired: "python".to_string(),
            published: None,
            diagnostic: None,
        }
    }

    fn test_ui_host(dir: &Path) -> UiHost {
        UiHost::new(
            None,
            dir.join("config_ui.py"),
            crate::ui_host::DEFAULT_REPORT_TIMEOUT,
            std::sync::Arc::new(test_logger(&dir.join("ui-log"))),
        )
    }

    fn request_line(value: serde_json::Value) -> String {
        let mut line = value.to_string();
        line.push('\n');
        line
    }

    fn roundtrip(stream: &TcpStream, line: &str) -> serde_json::Value {
        let mut writer = BufWriter::new(stream);
        writer.write_all(line.as_bytes()).unwrap();
        writer.flush().unwrap();
        let mut reader = BufReader::new(stream);
        let mut response = String::new();
        reader.read_line(&mut response).unwrap();
        serde_json::from_str(&response).unwrap()
    }

    #[test]
    fn generate_token_has_expected_length_and_charset() {
        let token = generate_token();
        assert_eq!(token.len(), TOKEN_LENGTH);
        assert!(token.bytes().all(|b| TOKEN_CHARS.contains(&b)));
        assert_ne!(generate_token(), generate_token());
    }

    #[test]
    fn token_and_port_files_roundtrip_and_cleanup() {
        let dir = tempfile::tempdir().unwrap();
        write_token(dir.path(), "secret").unwrap();
        write_port(dir.path(), 45678).unwrap();
        assert_eq!(read_token(dir.path()).as_deref(), Some("secret"));
        assert_eq!(read_port(dir.path()), Some(45678));
        assert_eq!(
            clean_stale_endpoints(dir.path()).unwrap(),
            vec![PORT_FILE, TOKEN_FILE]
        );
        assert!(clean_stale_endpoints(dir.path()).unwrap().is_empty());
    }

    #[test]
    fn serve_answers_jobs_and_rejects_wrong_versions() {
        let dir = tempfile::tempdir().unwrap();
        let run_dir = dir.path().join("run");
        let project = dir.path().join("project");
        fs::create_dir_all(&project).unwrap();
        let logger = test_logger(&dir.path().join("log"));
        let (state, _) = StateStore::open(&dir.path().join("state"), clock()).unwrap();
        let (scheduler, scheduler_thread) = crate::scheduler::start(state, clock()).unwrap();

        let run_dir_clone = run_dir.clone();
        let scheduler_clone = scheduler.clone();
        let ui_dir = dir.path().to_path_buf();
        let handle = std::thread::spawn(move || {
            serve(
                &run_dir_clone,
                "0.1.0-test",
                &logger,
                &scheduler_clone,
                test_scanner_status(),
                &test_ui_host(&ui_dir),
            )
            .unwrap();
        });

        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let port = loop {
            if let Some(port) = read_port(&run_dir) {
                break port;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "port file not written"
            );
            std::thread::sleep(Duration::from_millis(10));
        };
        let token = read_token(&run_dir).unwrap();
        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();

        let hello = roundtrip(
            &stream,
            &request_line(serde_json::json!({
                "cmd": "hello", "protocol_version": PROTOCOL_VERSION, "token": token,
            })),
        );
        assert_eq!(hello["type"], "hello");
        assert_eq!(hello["state_schema_version"], STATE_SCHEMA_VERSION);

        let incompatible = roundtrip(
            &stream,
            &request_line(serde_json::json!({
                "cmd": "submit_job", "protocol_version": 999,
                "state_schema_version": STATE_SCHEMA_VERSION, "token": token,
                "project_path": project, "db_path": dir.path().join("index.db"),
                "file_path": "src/main.py", "priority": "interactive",
            })),
        );
        assert_eq!(incompatible["code"], "incompatible_protocol");

        let submitted = roundtrip(
            &stream,
            &request_line(serde_json::json!({
                "cmd": "submit_job", "protocol_version": PROTOCOL_VERSION,
                "state_schema_version": STATE_SCHEMA_VERSION, "token": token,
                "project_path": project, "db_path": dir.path().join("index.db"),
                "file_path": "src/main.py", "priority": "interactive",
            })),
        );
        assert_eq!(submitted["type"], "submitted");
        assert_eq!(submitted["created"], true);
        assert_eq!(submitted["job"]["status"], "pending");
        let job_id = submitted["job"]["id"].as_i64().unwrap();

        let cancelled = roundtrip(
            &stream,
            &request_line(serde_json::json!({
                "cmd": "cancel_job", "protocol_version": PROTOCOL_VERSION,
                "state_schema_version": STATE_SCHEMA_VERSION, "token": token,
                "job_id": job_id,
            })),
        );
        assert_eq!(
            cancelled["job"]["status"],
            JobStatus::CancelRequested.as_db()
        );

        let project_without_target = roundtrip(
            &stream,
            &request_line(serde_json::json!({
                "cmd": "open_config_ui", "protocol_version": PROTOCOL_VERSION,
                "state_schema_version": STATE_SCHEMA_VERSION, "token": token,
                "mode": "project", "target": null,
            })),
        );
        assert_eq!(project_without_target["code"], "invalid_request");

        let global_with_target = roundtrip(
            &stream,
            &request_line(serde_json::json!({
                "cmd": "open_config_ui", "protocol_version": PROTOCOL_VERSION,
                "state_schema_version": STATE_SCHEMA_VERSION, "token": token,
                "mode": "global", "target": "/repo",
            })),
        );
        assert_eq!(global_with_target["code"], "invalid_request");

        let unknown_mode = roundtrip(
            &stream,
            &request_line(serde_json::json!({
                "cmd": "open_config_ui", "protocol_version": PROTOCOL_VERSION,
                "state_schema_version": STATE_SCHEMA_VERSION, "token": token,
                "mode": "other", "target": null,
            })),
        );
        assert_eq!(unknown_mode["code"], "invalid_request");

        let shutdown = roundtrip(
            &stream,
            &request_line(serde_json::json!({"cmd": "shutdown", "token": token})),
        );
        assert_eq!(shutdown, serde_json::json!({"type": "ack"}));
        handle.join().unwrap();
        scheduler.shutdown();
        scheduler_thread.join().unwrap();
    }

    #[test]
    fn bad_token_is_rejected_and_connection_closed() {
        let dir = tempfile::tempdir().unwrap();
        let run_dir = dir.path().join("run");
        let logger = test_logger(&dir.path().join("log"));
        let (state, _) = StateStore::open(&dir.path().join("state"), clock()).unwrap();
        let (scheduler, scheduler_thread) = crate::scheduler::start(state, clock()).unwrap();
        let run_dir_clone = run_dir.clone();
        let scheduler_clone = scheduler.clone();
        let ui_dir = dir.path().to_path_buf();
        let handle = std::thread::spawn(move || {
            serve(
                &run_dir_clone,
                "0.1.0-test",
                &logger,
                &scheduler_clone,
                test_scanner_status(),
                &test_ui_host(&ui_dir),
            )
            .unwrap();
        });
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let port = loop {
            if let Some(port) = read_port(&run_dir) {
                break port;
            }
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        };
        let token = read_token(&run_dir).unwrap();
        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let rejected = roundtrip(
            &stream,
            &request_line(serde_json::json!({"cmd": "ping", "token": "wrong"})),
        );
        assert_eq!(rejected["code"], "bad_token");
        let mut reader = BufReader::new(&stream);
        let mut rest = String::new();
        reader.read_line(&mut rest).unwrap();
        assert!(rest.is_empty());

        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        assert_eq!(
            roundtrip(
                &stream,
                &request_line(serde_json::json!({"cmd": "shutdown", "token": token}))
            )["type"],
            "ack"
        );
        handle.join().unwrap();
        scheduler.shutdown();
        scheduler_thread.join().unwrap();
    }

    #[test]
    fn invalid_json_gets_error_and_connection_stays_usable() {
        let dir = tempfile::tempdir().unwrap();
        let run_dir = dir.path().join("run");
        let logger = test_logger(&dir.path().join("log"));
        let (state, _) = StateStore::open(&dir.path().join("state"), clock()).unwrap();
        let (scheduler, scheduler_thread) = crate::scheduler::start(state, clock()).unwrap();
        let run_dir_clone = run_dir.clone();
        let scheduler_clone = scheduler.clone();
        let ui_dir = dir.path().to_path_buf();
        let handle = std::thread::spawn(move || {
            serve(
                &run_dir_clone,
                "0.1.0-test",
                &logger,
                &scheduler_clone,
                test_scanner_status(),
                &test_ui_host(&ui_dir),
            )
            .unwrap();
        });
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let port = loop {
            if let Some(port) = read_port(&run_dir) {
                break port;
            }
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(10));
        };
        let token = read_token(&run_dir).unwrap();
        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        assert_eq!(
            roundtrip(&stream, "this is not json\n")["code"],
            "invalid_request"
        );
        assert_eq!(
            roundtrip(
                &stream,
                &request_line(serde_json::json!({"cmd": "shutdown", "token": token}))
            )["type"],
            "ack"
        );
        handle.join().unwrap();
        scheduler.shutdown();
        scheduler_thread.join().unwrap();
    }
}
