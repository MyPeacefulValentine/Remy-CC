//! End-to-end tests driving the built binary via `CARGO_BIN_EXE_remy-daemon`.
//!
//! Real sleeps below are bounded readiness polling (process synchronization),
//! not time-behavior assertions; guideline §5.6 applies to the latter.

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant};

const POLL_INTERVAL: Duration = Duration::from_millis(50);
const READY_TIMEOUT: Duration = Duration::from_secs(10);

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_remy-daemon")
}

fn run(home: &Path, args: &[&str]) -> Output {
    Command::new(bin())
        .args(args)
        .env("REMY_CC_HOME", home)
        .output()
        .expect("run remy-daemon")
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
        let child = Command::new(bin())
            .args(["start", "--foreground"])
            .env("REMY_CC_HOME", home)
            .env("REMY_SCANNER_PROVIDER", "python")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn foreground daemon");
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
    assert_eq!(
        connection
            .query_row("SELECT status FROM jobs WHERE id = 2", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
        "running"
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
    assert_eq!(
        connection
            .query_row(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('running', 'cancel_requested')",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
        1
    );
    assert_eq!(
        connection
            .query_row("SELECT COUNT(*) FROM jobs", [], |row| row.get::<_, i64>(0))
            .unwrap(),
        3
    );
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
