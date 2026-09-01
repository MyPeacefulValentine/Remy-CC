//! Managed config-UI subprocess host (R1.4).
//!
//! The daemon spawns `config_ui.py --managed`, holds the child's stdin write
//! end as the lifecycle anchor (dropping it is the shutdown signal — the
//! managed arm exits on stdin EOF), reads one JSON report line
//! `{port, token, pid}` from stdout, and pumps stderr into the JSON log with
//! the session token redacted. Instance state lives in memory only: the
//! child's lifetime is a subset of the daemon's, so there is nothing to
//! recover after a restart (state.db deviation argued in the R1.4 plan).

use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::logging::JsonLogger;

pub const DEFAULT_REPORT_TIMEOUT: Duration = Duration::from_secs(10);
const REAP_POLL_INTERVAL: Duration = Duration::from_millis(20);
const REAP_TIMEOUT: Duration = Duration::from_secs(5);
const STDERR_LOG_LIMIT: usize = 64 * 1024;
const DIAGNOSTIC_TAIL_BYTES: usize = 2048;
const OUTPUT_CAP: usize = 64 * 1024;

/// A running managed UI child. `stdin` is the lifecycle anchor; `token`
/// stays in memory and in IPC responses only, never in the log.
pub struct UiInstance {
    pub pid: u32,
    pub port: u16,
    pub token: String,
    pub mode: String,
    pub target: Option<String>,
    child: Child,
    stdin: Option<ChildStdin>,
}

pub enum OpenOutcome {
    /// Freshly spawned (`true`) or an existing same-mode instance (`false`);
    /// `created` is part of the observable idempotency contract (unit tests
    /// assert it), not consumed by the IPC response.
    Ready {
        url: String,
        token: String,
        #[allow(dead_code)]
        created: bool,
    },
    /// A live instance with a different mode/target occupies the slot.
    Conflict {
        mode: String,
        target: Option<String>,
        pid: u32,
        port: u16,
    },
    /// Spawn or report failure; `diagnostic` carries sanitized output tails.
    SpawnFailed { diagnostic: String },
}

pub struct UiHost {
    python: Option<String>,
    script: PathBuf,
    report_timeout: Duration,
    logger: Arc<JsonLogger>,
    instance: Mutex<Option<UiInstance>>,
}

impl UiHost {
    /// `python` is `None` when no usable interpreter was found; every open
    /// request then fails with the runtime-prerequisite diagnostic.
    pub fn new(
        python: Option<String>,
        script: PathBuf,
        report_timeout: Duration,
        logger: Arc<JsonLogger>,
    ) -> Self {
        Self {
            python,
            script,
            report_timeout,
            logger,
            instance: Mutex::new(None),
        }
    }

    /// Snapshot for status responses (token deliberately excluded).
    pub fn status(&self) -> Option<crate::protocol::UiStatus> {
        let mut slot = self.instance.lock().unwrap_or_else(|e| e.into_inner());
        Self::reap_exited(&mut slot);
        slot.as_ref().map(|instance| crate::protocol::UiStatus {
            pid: instance.pid,
            port: instance.port,
            mode: instance.mode.clone(),
            target: instance.target.clone(),
        })
    }

    /// Idempotent open: same mode/target returns the live instance, a live
    /// instance with a different mode/target is a conflict, otherwise spawn.
    pub fn open(&self, mode: &str, target: Option<&str>) -> OpenOutcome {
        let mut slot = self.instance.lock().unwrap_or_else(|e| e.into_inner());
        Self::reap_exited(&mut slot);
        if let Some(instance) = slot.as_ref() {
            if instance.mode == mode && instance.target.as_deref() == target {
                return OpenOutcome::Ready {
                    url: format!("http://127.0.0.1:{}", instance.port),
                    token: instance.token.clone(),
                    created: false,
                };
            }
            return OpenOutcome::Conflict {
                mode: instance.mode.clone(),
                target: instance.target.clone(),
                pid: instance.pid,
                port: instance.port,
            };
        }
        match self.spawn(mode, target) {
            Ok(instance) => {
                let outcome = OpenOutcome::Ready {
                    url: format!("http://127.0.0.1:{}", instance.port),
                    token: instance.token.clone(),
                    created: true,
                };
                let _ = self.logger.log(
                    "info",
                    "config_ui_started",
                    serde_json::json!({
                        "pid": instance.pid,
                        "port": instance.port,
                        "mode": instance.mode,
                        "target": instance.target,
                    }),
                );
                *slot = Some(instance);
                outcome
            }
            Err(diagnostic) => {
                let _ = self.logger.log(
                    "warning",
                    "config_ui_spawn_failed",
                    serde_json::json!({"diagnostic": diagnostic}),
                );
                OpenOutcome::SpawnFailed { diagnostic }
            }
        }
    }

    /// Drop the stdin write end (managed-arm shutdown signal) and wait
    /// briefly; a child ignoring EOF is left to its own exit path
    /// (crash-only INV-R3: the daemon never blocks its shutdown on the UI).
    pub fn shutdown(&self) {
        let mut slot = self.instance.lock().unwrap_or_else(|e| e.into_inner());
        let Some(mut instance) = slot.take() else {
            return;
        };
        drop(instance.stdin.take());
        let deadline = Instant::now() + REAP_TIMEOUT;
        while Instant::now() < deadline {
            match instance.child.try_wait() {
                Ok(Some(_)) => {
                    let _ = self.logger.log(
                        "info",
                        "config_ui_stopped",
                        serde_json::json!({"pid": instance.pid}),
                    );
                    return;
                }
                Ok(None) => thread::sleep(REAP_POLL_INTERVAL),
                Err(_) => break,
            }
        }
        let _ = self.logger.log(
            "warning",
            "config_ui_still_running_at_shutdown",
            serde_json::json!({"pid": instance.pid}),
        );
    }

    fn reap_exited(slot: &mut Option<UiInstance>) {
        if let Some(instance) = slot.as_mut() {
            if matches!(instance.child.try_wait(), Ok(Some(_))) {
                *slot = None;
            }
        }
    }

    fn spawn(&self, mode: &str, target: Option<&str>) -> Result<UiInstance, String> {
        let Some(python) = self.python.as_deref() else {
            return Err(
                "no usable Python interpreter found; Python 3.10+ is a runtime prerequisite for the config UI"
                    .to_string(),
            );
        };
        if !self.script.is_file() {
            return Err(format!(
                "deployed config_ui.py is missing at {}; run remy-cc install first",
                self.script.display()
            ));
        }
        let mut command = Command::new(python);
        command
            .arg(&self.script)
            .args(["--managed", "--mode", mode]);
        if let Some(target) = target {
            command.args(["--target", target]);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| format!("cannot spawn {python}: {error}"))?;
        let stdin = child.stdin.take();
        let stdout = child.stdout.take().expect("piped stdout");
        let stderr = child.stderr.take().expect("piped stderr");

        let (report_sender, report_receiver) = mpsc::sync_channel::<Result<Value, String>>(1);
        thread::spawn(move || {
            let mut line = String::new();
            let result = match BufReader::new(stdout).read_line(&mut line) {
                Ok(0) => Err("child exited without a report line".to_string()),
                Ok(_) => serde_json::from_str::<Value>(&line)
                    .map_err(|error| format!("report line is not valid JSON: {error}")),
                Err(error) => Err(format!("cannot read the report line: {error}")),
            };
            let _ = report_sender.send(result);
        });

        let report = match report_receiver.recv_timeout(self.report_timeout) {
            Ok(result) => result,
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "config UI did not report within {}s; killed",
                    self.report_timeout.as_secs()
                ));
            }
        };
        let report = match report {
            Ok(report) => report,
            Err(reason) => {
                let _ = child.kill();
                let stderr_tail = read_tail(stderr);
                let _ = child.wait();
                return Err(format!(
                    "{reason}; stderr tail: {}",
                    crate::worker::sanitize(&stderr_tail)
                ));
            }
        };
        if !crate::worker::has_exact_keys(&report, &["port", "token", "pid"]) {
            let _ = child.kill();
            let _ = child.wait();
            return Err("report line keys are not exactly {port, token, pid}".to_string());
        }
        let (Some(port), Some(token), Some(pid)) = (
            report.get("port").and_then(Value::as_u64),
            report.get("token").and_then(Value::as_str),
            report.get("pid").and_then(Value::as_u64),
        ) else {
            let _ = child.kill();
            let _ = child.wait();
            return Err("report line fields are mistyped".to_string());
        };
        let (Ok(port), Ok(pid)) = (u16::try_from(port), u32::try_from(pid)) else {
            let _ = child.kill();
            let _ = child.wait();
            return Err("report line port/pid are out of range".to_string());
        };
        if token.is_empty() {
            let _ = child.kill();
            let _ = child.wait();
            return Err("report line token is empty".to_string());
        }

        pump_stderr(stderr, token.to_string(), Arc::clone(&self.logger));

        Ok(UiInstance {
            pid,
            port,
            token: token.to_string(),
            mode: mode.to_string(),
            target: target.map(str::to_string),
            child,
            stdin,
        })
    }
}

/// Forward child stderr lines into the JSON log, redacting the session token
/// and stopping after `STDERR_LOG_LIMIT` logged bytes (the pipe keeps
/// draining so the child never blocks on a full stderr buffer).
fn pump_stderr(pipe: impl Read + Send + 'static, token: String, logger: Arc<JsonLogger>) {
    thread::spawn(move || {
        let mut reader = BufReader::new(pipe);
        let mut logged = 0_usize;
        let mut line = Vec::new();
        loop {
            line.clear();
            match reader.read_until(b'\n', &mut line) {
                Ok(0) | Err(_) => break,
                Ok(count) => {
                    if logged >= STDERR_LOG_LIMIT {
                        continue;
                    }
                    logged += count;
                    let text = String::from_utf8_lossy(&line);
                    let redacted = crate::worker::sanitize(text.trim_end_matches(['\r', '\n']))
                        .replace(&token, "[redacted]");
                    let _ = logger.log(
                        "info",
                        "config_ui_stderr",
                        serde_json::json!({"line": redacted}),
                    );
                    if logged >= STDERR_LOG_LIMIT {
                        let _ = logger.log(
                            "warning",
                            "config_ui_stderr_truncated",
                            serde_json::json!({"limit_bytes": STDERR_LOG_LIMIT}),
                        );
                    }
                }
            }
        }
    });
}

fn read_tail(pipe: impl Read) -> String {
    let mut bytes = Vec::new();
    let _ = pipe.take(OUTPUT_CAP as u64).read_to_end(&mut bytes);
    let start = bytes.len().saturating_sub(DIAGNOSTIC_TAIL_BYTES);
    String::from_utf8_lossy(&bytes[start..]).into_owned()
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::sync::Arc;
    use std::time::{Duration, SystemTime};

    use super::*;
    use crate::clock::fake::FakeClock;
    use crate::clock::Clock;

    fn test_logger(dir: &Path) -> Arc<JsonLogger> {
        let clock: Arc<dyn Clock> = Arc::new(FakeClock::new(
            SystemTime::UNIX_EPOCH + Duration::from_secs(1_754_000_000),
        ));
        Arc::new(JsonLogger::new(dir, 1024 * 1024, clock).unwrap())
    }

    fn python_on_path() -> Option<String> {
        for candidate in ["python", "python3"] {
            if crate::install::pyprobe::probe_executable(candidate).is_ok() {
                return Some(candidate.to_string());
            }
        }
        None
    }

    fn write_fake_ui(dir: &Path, body: &str) -> PathBuf {
        let script = dir.join("fake_ui.py");
        std::fs::write(&script, body).unwrap();
        script
    }

    const REPORTING_UI: &str = r#"
import json, os, sys
print(json.dumps({"port": 45678, "token": "fake-ui-token", "pid": os.getpid()}), flush=True)
sys.stdin.buffer.read()
"#;

    #[test]
    fn missing_python_fails_with_prerequisite_diagnostic() {
        let dir = tempfile::tempdir().unwrap();
        let host = UiHost::new(
            None,
            dir.path().join("config_ui.py"),
            DEFAULT_REPORT_TIMEOUT,
            test_logger(&dir.path().join("log")),
        );
        match host.open("global", None) {
            OpenOutcome::SpawnFailed { diagnostic } => {
                assert!(diagnostic.contains("Python 3.10+"), "{diagnostic}");
            }
            _ => panic!("expected SpawnFailed"),
        }
    }

    #[test]
    fn missing_script_fails_before_spawn() {
        let Some(python) = python_on_path() else {
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let host = UiHost::new(
            Some(python),
            dir.path().join("gone.py"),
            DEFAULT_REPORT_TIMEOUT,
            test_logger(&dir.path().join("log")),
        );
        match host.open("global", None) {
            OpenOutcome::SpawnFailed { diagnostic } => {
                assert!(diagnostic.contains("missing"), "{diagnostic}");
            }
            _ => panic!("expected SpawnFailed"),
        }
    }

    #[test]
    fn open_is_idempotent_and_conflicts_on_mode_change() {
        let Some(python) = python_on_path() else {
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let script = write_fake_ui(dir.path(), REPORTING_UI);
        let host = UiHost::new(
            Some(python),
            script,
            DEFAULT_REPORT_TIMEOUT,
            test_logger(&dir.path().join("log")),
        );
        let OpenOutcome::Ready {
            url,
            token,
            created,
        } = host.open("global", None)
        else {
            panic!("expected Ready");
        };
        assert!(created);
        assert_eq!(url, "http://127.0.0.1:45678");
        assert_eq!(token, "fake-ui-token");

        let OpenOutcome::Ready { created, .. } = host.open("global", None) else {
            panic!("expected idempotent Ready");
        };
        assert!(!created);

        match host.open("project", Some("/repo")) {
            OpenOutcome::Conflict { mode, port, .. } => {
                assert_eq!(mode, "global");
                assert_eq!(port, 45678);
            }
            _ => panic!("expected Conflict"),
        }
        let status = host.status().expect("live instance");
        assert_eq!(status.port, 45678);
        assert_eq!(status.mode, "global");
        host.shutdown();
        assert!(host.status().is_none());
    }

    #[test]
    fn report_timeout_kills_the_child() {
        let Some(python) = python_on_path() else {
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let pid_file = dir.path().join("child.pid");
        let body = format!(
            "import os, pathlib, time\npathlib.Path({pid_file:?}).write_text(str(os.getpid()))\nwhile True:\n    time.sleep(0.1)\n",
            pid_file = pid_file.to_str().unwrap()
        );
        let script = write_fake_ui(dir.path(), &body);
        // 1s keeps the timeout short while leaving the interpreter enough
        // startup room to write the pid file before the kill fires.
        let host = UiHost::new(
            Some(python),
            script,
            Duration::from_secs(1),
            test_logger(&dir.path().join("log")),
        );
        match host.open("global", None) {
            OpenOutcome::SpawnFailed { diagnostic } => {
                assert!(diagnostic.contains("did not report"), "{diagnostic}");
            }
            _ => panic!("expected SpawnFailed"),
        }
        assert!(host.status().is_none());
        let pid: u32 = std::fs::read_to_string(&pid_file)
            .expect("child wrote its pid before the timeout")
            .trim()
            .parse()
            .unwrap();
        assert!(!pid_is_alive(pid), "child {pid} survived the timeout kill");
    }

    fn pid_is_alive(pid: u32) -> bool {
        #[cfg(windows)]
        {
            let output = std::process::Command::new("tasklist")
                .args(["/FI", &format!("PID eq {pid}"), "/NH"])
                .output()
                .expect("tasklist");
            String::from_utf8_lossy(&output.stdout).contains(&format!(" {pid} "))
        }
        #[cfg(unix)]
        {
            std::process::Command::new("kill")
                .args(["-0", &pid.to_string()])
                .status()
                .map(|status| status.success())
                .unwrap_or(false)
        }
    }

    #[test]
    fn exit_without_report_carries_stderr_tail() {
        let Some(python) = python_on_path() else {
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let script = write_fake_ui(
            dir.path(),
            "import sys\nprint('lock held by pid 123', file=sys.stderr)\nsys.exit(1)\n",
        );
        let host = UiHost::new(
            Some(python),
            script,
            DEFAULT_REPORT_TIMEOUT,
            test_logger(&dir.path().join("log")),
        );
        match host.open("global", None) {
            OpenOutcome::SpawnFailed { diagnostic } => {
                assert!(diagnostic.contains("without a report line"), "{diagnostic}");
                assert!(diagnostic.contains("lock held by pid 123"), "{diagnostic}");
            }
            _ => panic!("expected SpawnFailed"),
        }
    }

    #[test]
    fn exited_child_is_reaped_and_reopen_spawns_fresh() {
        let Some(python) = python_on_path() else {
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let script = write_fake_ui(
            dir.path(),
            "import json, os\nprint(json.dumps({\"port\": 45678, \"token\": \"t\", \"pid\": os.getpid()}), flush=True)\n",
        );
        let host = UiHost::new(
            Some(python),
            script,
            DEFAULT_REPORT_TIMEOUT,
            test_logger(&dir.path().join("log")),
        );
        let OpenOutcome::Ready { created, .. } = host.open("global", None) else {
            panic!("expected Ready");
        };
        assert!(created);
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if host.status().is_none() {
                break;
            }
            assert!(Instant::now() < deadline, "exited child was not reaped");
            thread::sleep(Duration::from_millis(20));
        }
        let OpenOutcome::Ready { created, .. } = host.open("global", None) else {
            panic!("expected respawn Ready");
        };
        assert!(created);
        host.shutdown();
    }

    #[test]
    fn malformed_report_lines_are_rejected() {
        let Some(python) = python_on_path() else {
            return;
        };
        let dir = tempfile::tempdir().unwrap();
        let cases = [
            ("print('not json', flush=True)\nimport sys\nsys.stdin.buffer.read()\n", "not valid JSON"),
            (
                "import json\nprint(json.dumps({'port': 1, 'token': 't', 'pid': 2, 'extra': 3}), flush=True)\nimport sys\nsys.stdin.buffer.read()\n",
                "exactly",
            ),
            (
                "import json\nprint(json.dumps({'port': 99999999, 'token': 't', 'pid': 2}), flush=True)\nimport sys\nsys.stdin.buffer.read()\n",
                "out of range",
            ),
            (
                "import json\nprint(json.dumps({'port': 1, 'token': '', 'pid': 2}), flush=True)\nimport sys\nsys.stdin.buffer.read()\n",
                "token is empty",
            ),
        ];
        for (body, needle) in cases {
            let script = write_fake_ui(dir.path(), body);
            let host = UiHost::new(
                Some(python.clone()),
                script,
                DEFAULT_REPORT_TIMEOUT,
                test_logger(&dir.path().join("log")),
            );
            match host.open("global", None) {
                OpenOutcome::SpawnFailed { diagnostic } => {
                    assert!(diagnostic.contains(needle), "{diagnostic}");
                }
                _ => panic!("expected SpawnFailed for {needle}"),
            }
            assert!(host.status().is_none());
        }
    }
}
