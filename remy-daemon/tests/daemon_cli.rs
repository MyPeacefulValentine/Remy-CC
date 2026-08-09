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
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn foreground daemon");
        let daemon = Self {
            child,
            home: home.to_path_buf(),
        };
        assert!(
            wait_until(|| is_running(&daemon.home)),
            "daemon did not reach running state"
        );
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
