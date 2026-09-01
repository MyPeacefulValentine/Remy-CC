//! End-to-end tests driving the built binary via `CARGO_BIN_EXE_remy-cc`.
//!
//! Real sleeps below are bounded readiness polling (process synchronization),
//! not time-behavior assertions; guideline §5.6 applies to the latter.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

const POLL_INTERVAL: Duration = Duration::from_millis(50);
const READY_TIMEOUT: Duration = Duration::from_secs(10);
// Mirrored wire constants (integration tests cannot import bin-crate items).
const PROTOCOL_VERSION: u32 = 6;
const STATE_SCHEMA_VERSION: u32 = 2;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_remy-cc")
}

fn run(home: &Path, args: &[&str]) -> Output {
    Command::new(bin())
        .args(args)
        .env("REMY_CC_HOME", home)
        .output()
        .expect("run remy-cc")
}

fn exit_code(output: &Output) -> i32 {
    output.status.code().expect("exit code")
}

fn is_running(home: &Path) -> bool {
    exit_code(&run(home, &["status"])) == 0
}

fn wait_until(mut predicate: impl FnMut() -> bool) -> bool {
    let deadline = Instant::now() + READY_TIMEOUT;
    while Instant::now() < deadline {
        if predicate() {
            return true;
        }
        sleep(POLL_INTERVAL);
    }
    predicate()
}

/// Foreground daemon child; killed on drop so a failing assertion cannot leak
/// a process into the test environment.
struct ForegroundDaemon {
    child: Child,
    home: PathBuf,
}

impl ForegroundDaemon {
    fn spawn(home: &Path) -> Self {
        Self::spawn_with_env(home, &[])
    }

    fn spawn_with_env(home: &Path, extra_env: &[(&str, &str)]) -> Self {
        let mut command = Command::new(bin());
        command
            .args(["start", "--foreground"])
            .env("REMY_CC_HOME", home)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        for (key, value) in extra_env {
            command.env(key, value);
        }
        let child = command.spawn().expect("spawn foreground daemon");
        let child_pid = child.id();
        let daemon = Self {
            child,
            home: home.to_path_buf(),
        };
        let run_dir = daemon.home.join("run");
        assert!(
            wait_until(|| {
                let published_pid = std::fs::read_to_string(run_dir.join("daemon.pid"))
                    .ok()
                    .and_then(|value| value.trim().parse::<u32>().ok());
                let published_port = std::fs::read_to_string(run_dir.join("daemon.port"))
                    .ok()
                    .and_then(|value| value.trim().parse::<u16>().ok());
                let token_published = std::fs::read_to_string(run_dir.join("daemon.token"))
                    .map(|value| !value.trim().is_empty())
                    .unwrap_or(false);
                published_pid == Some(child_pid)
                    && published_port.is_some_and(|port| port != 0)
                    && token_published
            }),
            "daemon did not publish its endpoint"
        );
        assert!(is_running(&daemon.home), "published daemon is not running");
        daemon
    }

    fn kill(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for ForegroundDaemon {
    fn drop(&mut self) {
        self.kill();
    }
}

#[test]
fn second_start_is_rejected_while_running() {
    let home = tempfile::tempdir().unwrap();
    let _daemon = ForegroundDaemon::spawn(home.path());

    let detached = run(home.path(), &["start"]);
    assert_eq!(exit_code(&detached), 1);
    assert!(String::from_utf8_lossy(&detached.stderr).contains("already running"));

    let foreground = run(home.path(), &["start", "--foreground"]);
    assert_eq!(exit_code(&foreground), 1);
    assert!(String::from_utf8_lossy(&foreground.stderr).contains("already running"));
}

#[test]
fn status_tracks_lock_state() {
    let home = tempfile::tempdir().unwrap();

    let before = run(home.path(), &["status"]);
    assert_eq!(exit_code(&before), 1);
    assert!(String::from_utf8_lossy(&before.stdout).contains("not running"));

    let mut daemon = ForegroundDaemon::spawn(home.path());
    let during = run(home.path(), &["status"]);
    assert_eq!(exit_code(&during), 0);
    assert!(String::from_utf8_lossy(&during.stdout).contains("running"));

    daemon.kill();
    assert!(wait_until(|| !is_running(home.path())));
}

#[test]
fn stop_is_idempotent_when_not_running() {
    let home = tempfile::tempdir().unwrap();
    for _ in 0..2 {
        let output = run(home.path(), &["stop"]);
        assert_eq!(exit_code(&output), 0);
        assert!(String::from_utf8_lossy(&output.stdout).contains("not running"));
    }
}

#[test]
fn stop_terminates_running_daemon() {
    let home = tempfile::tempdir().unwrap();
    let mut daemon = ForegroundDaemon::spawn(home.path());

    let output = run(home.path(), &["stop"]);
    assert_eq!(exit_code(&output), 0);
    assert!(String::from_utf8_lossy(&output.stdout).contains("stopped"));
    assert!(wait_until(|| !is_running(home.path())));
    let _ = daemon.child.wait();
}

#[test]
fn force_kill_releases_lock_and_allows_restart() {
    let home = tempfile::tempdir().unwrap();

    let mut first = ForegroundDaemon::spawn(home.path());
    first.kill();
    assert!(
        wait_until(|| !is_running(home.path())),
        "lock not released after force kill"
    );

    let _second = ForegroundDaemon::spawn(home.path());
    assert!(is_running(home.path()));
}

#[test]
fn process_restart_recovers_persisted_job_states_idempotently() {
    let home = tempfile::tempdir().unwrap();
    let project = tempfile::tempdir().unwrap();
    let mut first = ForegroundDaemon::spawn(home.path());
    first.kill();
    assert!(wait_until(|| !is_running(home.path())));

    let connection = rusqlite::Connection::open(home.path().join("state.db")).unwrap();
    connection
        .execute(
            "INSERT INTO projects(id, project_key, project_path, db_path, created_at, last_seen_at) \
             VALUES (1, ?1, ?1, ?2, 1000, 1000)",
            rusqlite::params![
                project.path().to_string_lossy(),
                project.path().join("logic_index.db").to_string_lossy()
            ],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO jobs( \
                 id, project_id, job_type, target_db_path, file_path, priority, status, \
                 created_at, started_at, dedupe_key \
             ) VALUES (1, 1, 'incremental_scan', ?1, 'a.py', 1, 'running', 1000, 1100, 'incremental_scan:a.py')",
            rusqlite::params![project.path().join("logic_index.db").to_string_lossy()],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO jobs( \
                 id, project_id, job_type, target_db_path, file_path, priority, status, \
                 created_at, dedupe_key \
             ) VALUES (2, 1, 'incremental_scan', ?1, 'a.py', 1, 'pending', 1200, 'incremental_scan:a.py')",
            rusqlite::params![project.path().join("logic_index.db").to_string_lossy()],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO jobs( \
                 id, project_id, job_type, target_db_path, file_path, priority, status, \
                 created_at, started_at, dedupe_key \
             ) VALUES (3, 1, 'incremental_scan', ?1, 'b.py', 1, 'cancel_requested', 1000, 1100, 'incremental_scan:b.py')",
            rusqlite::params![project.path().join("logic_index.db").to_string_lossy()],
        )
        .unwrap();
    drop(connection);

    let mut second = ForegroundDaemon::spawn(home.path());
    let connection = rusqlite::Connection::open(home.path().join("state.db")).unwrap();
    let first_row: (String, Option<i64>) = connection
        .query_row(
            "SELECT status, superseded_by_job_id FROM jobs WHERE id = 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(first_row, ("superseded".to_string(), Some(2)));
    // The requeued job is claimed immediately; the rust scan of a missing
    // file can finish before this query runs, so accept any progressed state.
    let second_status: String = connection
        .query_row("SELECT status FROM jobs WHERE id = 2", [], |row| row.get(0))
        .unwrap();
    assert!(
        ["pending", "running", "succeeded"].contains(&second_status.as_str()),
        "job 2 status: {second_status}"
    );
    assert_eq!(
        connection
            .query_row("SELECT status FROM jobs WHERE id = 3", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
        "cancelled"
    );
    drop(connection);

    second.kill();
    assert!(wait_until(|| !is_running(home.path())));
    let _third = ForegroundDaemon::spawn(home.path());
    let connection = rusqlite::Connection::open(home.path().join("state.db")).unwrap();
    assert!(
        connection
            .query_row(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('running', 'cancel_requested')",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap()
            <= 1
    );
    assert_eq!(
        connection
            .query_row("SELECT COUNT(*) FROM jobs", [], |row| row.get::<_, i64>(0))
            .unwrap(),
        3
    );
    let second_status: String = connection
        .query_row("SELECT status FROM jobs WHERE id = 2", [], |row| row.get(0))
        .unwrap();
    assert_ne!(second_status, "superseded");
}

#[test]
fn higher_state_schema_prevents_endpoint_publication() {
    let home = tempfile::tempdir().unwrap();
    let db_path = home.path().join("state.db");
    let connection = rusqlite::Connection::open(&db_path).unwrap();
    connection
        .pragma_update(None, "user_version", 99_u32)
        .unwrap();
    drop(connection);

    let output = run(home.path(), &["start", "--foreground"]);

    assert_eq!(exit_code(&output), 2);
    assert!(String::from_utf8_lossy(&output.stderr).contains("newer than supported"));
    assert!(!home.path().join("run").join("daemon.port").exists());
    assert!(!home.path().join("run").join("daemon.pid").exists());
    let connection = rusqlite::Connection::open(db_path).unwrap();
    let version: u32 = connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    assert_eq!(version, 99);
}

#[test]
fn start_cleans_residue_from_previous_crash() {
    let home = tempfile::tempdir().unwrap();
    let run_dir = home.path().join("run");
    std::fs::create_dir_all(&run_dir).unwrap();
    std::fs::write(run_dir.join("daemon.port"), "1").unwrap();
    std::fs::write(run_dir.join("daemon.token"), "stale-token").unwrap();
    std::fs::write(run_dir.join("daemon.pid"), "99999").unwrap();

    let _daemon = ForegroundDaemon::spawn(home.path());

    assert!(
        wait_until(|| {
            std::fs::read_to_string(run_dir.join("daemon.port"))
                .map(|p| p.trim() != "1")
                .unwrap_or(false)
        }),
        "stale port file was not replaced"
    );
    let token = std::fs::read_to_string(run_dir.join("daemon.token")).unwrap();
    assert_ne!(token.trim(), "stale-token");
    let pid = std::fs::read_to_string(run_dir.join("daemon.pid")).unwrap();
    assert_ne!(pid.trim(), "99999");

    let status = run(home.path(), &["status"]);
    assert_eq!(exit_code(&status), 0);
    let stdout = String::from_utf8_lossy(&status.stdout);
    assert!(stdout.contains("running"), "stdout: {stdout}");
    assert!(!stdout.contains("ipc-unresponsive"), "stdout: {stdout}");
}

#[test]
fn residue_without_lock_reads_as_not_running() {
    let home = tempfile::tempdir().unwrap();
    let run_dir = home.path().join("run");
    std::fs::create_dir_all(&run_dir).unwrap();
    std::fs::write(run_dir.join("daemon.port"), "1").unwrap();
    std::fs::write(run_dir.join("daemon.token"), "stale-token").unwrap();
    std::fs::write(run_dir.join("daemon.pid"), "99999").unwrap();

    let status = run(home.path(), &["status"]);
    assert_eq!(exit_code(&status), 1);
    assert!(String::from_utf8_lossy(&status.stdout).contains("not running"));

    let stop = run(home.path(), &["stop"]);
    assert_eq!(exit_code(&stop), 0);
    assert!(String::from_utf8_lossy(&stop.stdout).contains("not running"));
}

/// Best-effort teardown for a detached daemon: `stop`, then direct kill via
/// the recorded pid if the lock is still held.
struct DetachedCleanup {
    home: PathBuf,
}

impl Drop for DetachedCleanup {
    fn drop(&mut self) {
        if !is_running(&self.home) {
            return;
        }
        let _ = run(&self.home, &["stop"]);
        if !is_running(&self.home) {
            return;
        }
        if let Ok(pid) = std::fs::read_to_string(self.home.join("run").join("daemon.pid")) {
            let pid = pid.trim().to_string();
            #[cfg(windows)]
            let _ = Command::new("taskkill").args(["/PID", &pid, "/F"]).status();
            #[cfg(unix)]
            let _ = Command::new("kill").args(["-KILL", &pid]).status();
        }
    }
}

#[test]
fn detached_start_stop_roundtrip() {
    let home = tempfile::tempdir().unwrap();
    let _cleanup = DetachedCleanup {
        home: home.path().to_path_buf(),
    };

    let started = run(home.path(), &["start"]);
    assert_eq!(
        exit_code(&started),
        0,
        "stderr: {}",
        String::from_utf8_lossy(&started.stderr)
    );
    assert!(String::from_utf8_lossy(&started.stdout).contains("started"));
    assert!(is_running(home.path()));

    let stopped = run(home.path(), &["stop"]);
    assert_eq!(exit_code(&stopped), 0);
    assert!(wait_until(|| !is_running(home.path())));
}

#[test]
fn stop_with_corrupt_pid_file_succeeds_via_ipc() {
    let home = tempfile::tempdir().unwrap();
    let mut daemon = ForegroundDaemon::spawn(home.path());
    std::fs::write(home.path().join("run").join("daemon.pid"), "not-a-pid").unwrap();

    let output = run(home.path(), &["stop"]);
    assert_eq!(exit_code(&output), 0);
    assert!(String::from_utf8_lossy(&output.stdout).contains("stopped"));
    assert!(wait_until(|| !is_running(home.path())));
    let _ = daemon.child.wait();
}

#[test]
fn stop_with_corrupt_pid_and_unreachable_ipc_exits_2() {
    let home = tempfile::tempdir().unwrap();
    let _daemon = ForegroundDaemon::spawn(home.path());
    let run_dir = home.path().join("run");
    std::fs::write(run_dir.join("daemon.pid"), "not-a-pid").unwrap();
    std::fs::remove_file(run_dir.join("daemon.port")).unwrap();

    let output = run(home.path(), &["stop"]);
    assert_eq!(exit_code(&output), 2);
    assert!(String::from_utf8_lossy(&output.stderr).contains("pid file is unreadable"));
    assert!(is_running(home.path()));
}

#[test]
fn foreground_daemon_writes_json_log() {
    let home = tempfile::tempdir().unwrap();
    let _daemon = ForegroundDaemon::spawn(home.path());

    let log_path = home.path().join("log").join("daemon.log");
    assert!(wait_until(|| log_path.exists()));
    let content = std::fs::read_to_string(&log_path).unwrap();
    let mut saw_started = false;
    for line in content.lines() {
        let value: serde_json::Value = serde_json::from_str(line).expect("each line is JSON");
        if value["event"] == "daemon_started" {
            saw_started = true;
            assert_eq!(value["level"], "info");
            assert!(value["ts"].is_u64());
            assert!(value["pid"].is_u64());
        }
    }
    assert!(saw_started, "daemon_started event missing");
}

#[test]
fn restart_starts_a_stopped_daemon_and_replaces_a_running_one() {
    let home = tempfile::tempdir().unwrap();

    let first = run(home.path(), &["restart"]);
    assert_eq!(exit_code(&first), 0, "restart must start when stopped");
    assert!(wait_until(|| is_running(home.path())));
    let first_pid = std::fs::read_to_string(home.path().join("run").join("daemon.pid"))
        .expect("pid file")
        .trim()
        .to_string();

    let second = run(home.path(), &["restart"]);
    assert_eq!(
        exit_code(&second),
        0,
        "restart must replace a running daemon"
    );
    assert!(wait_until(|| is_running(home.path())));
    let second_pid = std::fs::read_to_string(home.path().join("run").join("daemon.pid"))
        .expect("pid file")
        .trim()
        .to_string();
    assert_ne!(first_pid, second_pid, "restart must spawn a new process");

    let stopped = run(home.path(), &["stop"]);
    assert_eq!(exit_code(&stopped), 0);
}

fn python_on_path() -> Option<&'static str> {
    for candidate in ["python", "python3"] {
        let probe = Command::new(candidate)
            .args([
                "-c",
                "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        if probe.map(|status| status.success()).unwrap_or(false) {
            return Some(candidate);
        }
    }
    None
}

/// Fake managed arm: reports a fixed port and its own pid, then blocks on
/// stdin until EOF (the packet-A lifecycle contract shape).
const FAKE_UI_SCRIPT: &str = r#"
import json, os, sys
print(json.dumps({"port": 45678, "token": "secret-ui-token-for-grep", "pid": os.getpid()}), flush=True)
sys.stdin.buffer.read()
"#;

/// Claude root holding the fake managed arm at the deployed location.
fn write_fake_claude_root(home: &Path, script_body: &str) -> PathBuf {
    let claude_root = home.join("claude");
    let remy_src = claude_root.join("remy-src");
    std::fs::create_dir_all(&remy_src).expect("remy-src dir");
    std::fs::write(remy_src.join("config_ui.py"), script_body).expect("fake config_ui.py");
    claude_root
}

fn ipc_request(home: &Path, request: &serde_json::Value) -> serde_json::Value {
    let run_dir = home.join("run");
    let port: u16 = std::fs::read_to_string(run_dir.join("daemon.port"))
        .expect("port file")
        .trim()
        .parse()
        .expect("port");
    let stream = TcpStream::connect(("127.0.0.1", port)).expect("connect");
    let mut writer = std::io::BufWriter::new(&stream);
    writeln!(writer, "{request}").expect("send");
    writer.flush().expect("flush");
    let mut line = String::new();
    BufReader::new(&stream).read_line(&mut line).expect("read");
    serde_json::from_str(&line).expect("response JSON")
}

fn open_config_ui(home: &Path, mode: &str, target: Option<&str>) -> serde_json::Value {
    let token = std::fs::read_to_string(home.join("run").join("daemon.token"))
        .expect("token file")
        .trim()
        .to_string();
    ipc_request(
        home,
        &serde_json::json!({
            "cmd": "open_config_ui",
            "protocol_version": PROTOCOL_VERSION,
            "state_schema_version": STATE_SCHEMA_VERSION,
            "token": token,
            "mode": mode,
            "target": target,
        }),
    )
}

fn read_daemon_log(home: &Path) -> String {
    let mut content = String::new();
    for name in ["daemon.log", "daemon.log.1"] {
        if let Ok(part) = std::fs::read_to_string(home.join("log").join(name)) {
            content.push_str(&part);
        }
    }
    content
}

#[test]
fn open_config_ui_is_idempotent_conflicts_and_never_logs_the_token() {
    let Some(_python) = python_on_path() else {
        return;
    };
    let home = tempfile::tempdir().unwrap();
    let claude_root = write_fake_claude_root(home.path(), FAKE_UI_SCRIPT);
    let mut daemon = ForegroundDaemon::spawn_with_env(
        home.path(),
        &[("CLAUDE_CONFIG_DIR", claude_root.to_str().unwrap())],
    );

    let first = open_config_ui(home.path(), "global", None);
    assert_eq!(first["type"], "config_ui", "response: {first}");
    assert_eq!(first["url"], "http://127.0.0.1:45678");
    assert_eq!(first["token"], "secret-ui-token-for-grep");

    let second = open_config_ui(home.path(), "global", None);
    assert_eq!(second["type"], "config_ui");
    assert_eq!(second["url"], first["url"]);

    let conflict = open_config_ui(home.path(), "project", Some("/repo"));
    assert_eq!(conflict["type"], "error");
    assert_eq!(conflict["code"], "ui_conflict");
    assert!(
        conflict["message"]
            .as_str()
            .unwrap()
            .contains("mode=global"),
        "message: {}",
        conflict["message"]
    );

    let status = run(home.path(), &["status", "--json"]);
    let payload: serde_json::Value =
        serde_json::from_str(String::from_utf8_lossy(&status.stdout).trim()).unwrap();
    assert_eq!(payload["ui"]["port"], 45678);
    assert_eq!(payload["ui"]["mode"], "global");
    assert!(payload["ui"].get("token").is_none());

    daemon.kill();
    let log = read_daemon_log(home.path());
    assert!(log.contains("config_ui_started"), "log: {log}");
    assert!(
        !log.contains("secret-ui-token-for-grep"),
        "UI session token leaked into the daemon log"
    );
    let stdout = String::from_utf8_lossy(&status.stdout);
    assert!(!log.is_empty() && !stdout.contains("secret-ui-token-for-grep"));
}

#[test]
fn config_ui_child_exits_on_daemon_stop_via_stdin_eof() {
    let Some(_python) = python_on_path() else {
        return;
    };
    let home = tempfile::tempdir().unwrap();
    let marker = home.path().join("ui-exited.marker");
    let script = format!(
        r#"
import json, os, sys, atexit
atexit.register(lambda: open({marker:?}, "w").close())
print(json.dumps({{"port": 45678, "token": "t", "pid": os.getpid()}}), flush=True)
sys.stdin.buffer.read()
"#,
        marker = marker.to_str().unwrap()
    );
    let claude_root = write_fake_claude_root(home.path(), &script);
    let _daemon = ForegroundDaemon::spawn_with_env(
        home.path(),
        &[("CLAUDE_CONFIG_DIR", claude_root.to_str().unwrap())],
    );

    let opened = open_config_ui(home.path(), "global", None);
    assert_eq!(opened["type"], "config_ui");

    let stopped = run(home.path(), &["stop"]);
    assert_eq!(exit_code(&stopped), 0);
    assert!(
        wait_until(|| marker.exists()),
        "managed child did not exit on daemon stop"
    );
}

#[test]
fn config_ui_report_timeout_kills_child_and_reports_spawn_failed() {
    let Some(_python) = python_on_path() else {
        return;
    };
    let home = tempfile::tempdir().unwrap();
    let claude_root = write_fake_claude_root(
        home.path(),
        "import sys\nsys.stderr.write('starting slowly')\nsys.stderr.flush()\nsys.stdin.buffer.read()\n",
    );
    let _daemon = ForegroundDaemon::spawn_with_env(
        home.path(),
        &[
            ("CLAUDE_CONFIG_DIR", claude_root.to_str().unwrap()),
            ("REMY_CC_UI_REPORT_TIMEOUT_SECS", "1"),
        ],
    );

    let response = open_config_ui(home.path(), "global", None);
    assert_eq!(response["type"], "error", "response: {response}");
    assert_eq!(response["code"], "ui_spawn_failed");
    assert!(
        response["message"]
            .as_str()
            .unwrap()
            .contains("did not report"),
        "message: {}",
        response["message"]
    );

    let status = run(home.path(), &["status", "--json"]);
    let payload: serde_json::Value =
        serde_json::from_str(String::from_utf8_lossy(&status.stdout).trim()).unwrap();
    assert!(payload["ui"].is_null(), "ui slot must be empty: {payload}");
}

#[test]
fn config_ui_exit_without_report_surfaces_stderr_tail() {
    let Some(_python) = python_on_path() else {
        return;
    };
    let home = tempfile::tempdir().unwrap();
    let claude_root = write_fake_claude_root(
        home.path(),
        "import sys\nprint('Error: Another config UI instance is running.', file=sys.stderr)\nsys.exit(1)\n",
    );
    let _daemon = ForegroundDaemon::spawn_with_env(
        home.path(),
        &[("CLAUDE_CONFIG_DIR", claude_root.to_str().unwrap())],
    );

    let response = open_config_ui(home.path(), "global", None);
    assert_eq!(response["type"], "error");
    assert_eq!(response["code"], "ui_spawn_failed");
    assert!(
        response["message"]
            .as_str()
            .unwrap()
            .contains("Another config UI instance"),
        "message: {}",
        response["message"]
    );
}

#[test]
fn config_command_prints_the_url_only_and_is_idempotent() {
    let Some(_python) = python_on_path() else {
        return;
    };
    let home = tempfile::tempdir().unwrap();
    let claude_root = write_fake_claude_root(home.path(), FAKE_UI_SCRIPT);
    let _daemon = ForegroundDaemon::spawn_with_env(
        home.path(),
        &[("CLAUDE_CONFIG_DIR", claude_root.to_str().unwrap())],
    );

    let first = run(home.path(), &["config"]);
    assert_eq!(
        exit_code(&first),
        0,
        "stderr: {}",
        String::from_utf8_lossy(&first.stderr)
    );
    let stdout = String::from_utf8_lossy(&first.stdout);
    assert_eq!(stdout.trim(), "http://127.0.0.1:45678");
    assert!(
        !stdout.contains("secret-ui-token-for-grep"),
        "UI session token leaked to the config stdout"
    );

    let second = run(home.path(), &["config"]);
    assert_eq!(exit_code(&second), 0);
    assert_eq!(
        String::from_utf8_lossy(&second.stdout).trim(),
        "http://127.0.0.1:45678"
    );

    let conflict = run(
        home.path(),
        &["config", "--path", home.path().to_str().unwrap()],
    );
    assert_eq!(exit_code(&conflict), 2);
    assert!(
        String::from_utf8_lossy(&conflict.stderr).contains("ui_conflict"),
        "stderr: {}",
        String::from_utf8_lossy(&conflict.stderr)
    );
}

#[test]
fn config_command_rejects_a_missing_project_path() {
    let home = tempfile::tempdir().unwrap();
    let missing = home.path().join("no-such-dir");
    let output = run(
        home.path(),
        &["config", "--path", missing.to_str().unwrap()],
    );
    assert_eq!(exit_code(&output), 2);
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("directory not found"),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn logs_prints_the_tail_and_reports_a_missing_file() {
    let home = tempfile::tempdir().unwrap();
    let missing = run(home.path(), &["logs"]);
    assert_eq!(exit_code(&missing), 1);
    assert!(String::from_utf8_lossy(&missing.stderr).contains("no log file"));

    let log_dir = home.path().join("log");
    std::fs::create_dir_all(&log_dir).expect("log dir");
    std::fs::write(log_dir.join("daemon.log"), "line-1\nline-2\nline-3\n").expect("log");

    let full = run(home.path(), &["logs"]);
    assert_eq!(exit_code(&full), 0);
    assert_eq!(
        String::from_utf8_lossy(&full.stdout),
        "line-1\nline-2\nline-3\n"
    );

    let tail = run(home.path(), &["logs", "--tail", "2"]);
    assert_eq!(exit_code(&tail), 0);
    assert_eq!(String::from_utf8_lossy(&tail.stdout), "line-2\nline-3\n");
}
