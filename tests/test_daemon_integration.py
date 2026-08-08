"""Integration tests for remy-daemon via remy-cc CLI.

Skip conditions: no cargo/rustc (reports reason) OR no compiled binary exists.
These tests drive the daemon through the Python CLI indirection to validate
the end-to-end contract specified in the R1.1 packet.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

DAEMON_SOURCE = Path(__file__).resolve().parent.parent / "remy-daemon"


def has_rust_toolchain():
    return shutil.which("cargo") is not None and shutil.which("rustc") is not None


def daemon_binary_exists():
    if sys.platform == "win32":
        return (DAEMON_SOURCE / "target" / "debug" / "remy-daemon.exe").exists()
    return (DAEMON_SOURCE / "target" / "debug" / "remy-daemon").exists()


skip_reason = None
if not has_rust_toolchain():
    skip_reason = "cargo/rustc not found in PATH"
elif not daemon_binary_exists():
    skip_reason = "remy-daemon binary not built; run: cargo build --manifest-path remy-daemon/Cargo.toml"

pytestmark = pytest.mark.skipif(skip_reason is not None, reason=skip_reason or "")


def daemon_bin():
    if sys.platform == "win32":
        return DAEMON_SOURCE / "target" / "debug" / "remy-daemon.exe"
    return DAEMON_SOURCE / "target" / "debug" / "remy-daemon"


def run_daemon(home, args, timeout=10):
    return subprocess.run(
        [str(daemon_bin())] + args,
        env={**os.environ, "REMY_CC_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def is_running(home):
    result = run_daemon(home, ["status"])
    return result.returncode == 0


def wait_for_state(home, expected_running, timeout_secs=5):
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if is_running(home) == expected_running:
            return True
        time.sleep(0.05)
    return is_running(home) == expected_running


def test_single_instance_mutual_exclusion(tmp_path):
    home = tmp_path / "home"
    result = run_daemon(home, ["start"])
    assert result.returncode == 0, result.stderr
    assert "started" in result.stdout
    try:
        assert wait_for_state(home, True)
        duplicate = run_daemon(home, ["start"])
        assert duplicate.returncode == 1
        assert "already running" in duplicate.stderr
    finally:
        run_daemon(home, ["stop"], timeout=15)


def test_stop_idempotent_when_not_running(tmp_path):
    home = tmp_path / "home"
    for _ in range(2):
        result = run_daemon(home, ["stop"])
        assert result.returncode == 0
        assert "not running" in result.stdout


def test_status_tracks_daemon_lifecycle(tmp_path):
    home = tmp_path / "home"
    before = run_daemon(home, ["status"])
    assert before.returncode == 1
    assert "not running" in before.stdout

    run_daemon(home, ["start"])
    assert wait_for_state(home, True)
    try:
        during = run_daemon(home, ["status"])
        assert during.returncode == 0
        assert "running" in during.stdout
    finally:
        run_daemon(home, ["stop"], timeout=15)

    assert wait_for_state(home, False, timeout_secs=10)
    after = run_daemon(home, ["status"])
    assert after.returncode == 1


def test_start_stop_roundtrip(tmp_path):
    home = tmp_path / "home"
    started = run_daemon(home, ["start"])
    assert started.returncode == 0, started.stderr
    assert wait_for_state(home, True)

    stopped = run_daemon(home, ["stop"], timeout=15)
    assert stopped.returncode == 0, stopped.stderr
    assert "stopped" in stopped.stdout
    assert wait_for_state(home, False, timeout_secs=10)


def test_json_log_created_with_daemon_started_event(tmp_path):
    home = tmp_path / "home"
    run_daemon(home, ["start"])
    assert wait_for_state(home, True)
    try:
        log_path = home / "log" / "daemon.log"
        assert wait_for_state(home, True, timeout_secs=3)
        content = log_path.read_text(encoding="utf-8")
        lines = [line for line in content.splitlines() if line.strip()]
        assert len(lines) >= 1
        import json
        first = json.loads(lines[0])
        assert first["event"] == "daemon_started"
        assert first["level"] == "info"
        assert "ts" in first
        assert "pid" in first
    finally:
        run_daemon(home, ["stop"], timeout=15)
