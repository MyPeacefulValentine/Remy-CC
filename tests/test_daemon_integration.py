"""Integration tests for remy-daemon via remy-cc CLI.

Skip conditions: no cargo/rustc (reports reason) OR no compiled binary exists.
These tests drive the daemon through the Python CLI indirection to validate
the end-to-end contract specified in the R1.1 packet.
"""
import contextlib
import errno
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


def _force_cleanup(home):
    """Teardown mirror of the Rust DetachedCleanup: try `stop`, then fall back
    to a direct pid kill if some daemon still holds the lock. Covers the case
    where `start` timed out (exit 2) but the detached process came up later."""
    run_daemon(home, ["stop"], timeout=15)
    if not is_running(home):
        return
    try:
        pid = (Path(home) / "run" / "daemon.pid").read_text(encoding="ascii").strip()
    except OSError:
        return
    if not pid.isdigit():
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    else:
        subprocess.run(["kill", "-KILL", pid], capture_output=True)


@contextlib.contextmanager
def started_daemon(home):
    """Issue `start` and guarantee cleanup regardless of assertion failures."""
    result = run_daemon(home, ["start"])
    try:
        yield result
    finally:
        _force_cleanup(home)


def test_single_instance_mutual_exclusion(tmp_path):
    home = tmp_path / "home"
    with started_daemon(home) as result:
        assert result.returncode == 0, result.stderr
        assert "started" in result.stdout
        assert wait_for_state(home, True)
        duplicate = run_daemon(home, ["start"])
        assert duplicate.returncode == 1
        assert "already running" in duplicate.stderr


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

    with started_daemon(home):
        assert wait_for_state(home, True)
        during = run_daemon(home, ["status"])
        assert during.returncode == 0
        assert "running" in during.stdout

    assert wait_for_state(home, False, timeout_secs=10)
    after = run_daemon(home, ["status"])
    assert after.returncode == 1


def test_start_stop_roundtrip(tmp_path):
    home = tmp_path / "home"
    with started_daemon(home) as started:
        assert started.returncode == 0, started.stderr
        assert wait_for_state(home, True)

        stopped = run_daemon(home, ["stop"], timeout=15)
        assert stopped.returncode == 0, stopped.stderr
        assert "stopped" in stopped.stdout
        assert wait_for_state(home, False, timeout_secs=10)


def test_json_log_created_with_daemon_started_event(tmp_path):
    home = tmp_path / "home"
    with started_daemon(home):
        assert wait_for_state(home, True)
        log_path = home / "log" / "daemon.log"
        deadline = time.time() + 5
        lines = []
        while time.time() < deadline:
            if log_path.exists():
                content = log_path.read_text(encoding="utf-8")
                lines = [line for line in content.splitlines() if line.strip()]
                if lines:
                    break
            time.sleep(0.05)
        assert len(lines) >= 1
        first = json.loads(lines[0])
        assert first["event"] == "daemon_started"
        assert first["level"] == "info"
        assert "ts" in first
        assert "pid" in first


# ── POSIX file semantics behind the reinstall-scoping decision ──


def _spawn_from_copy(tmp_path, home):
    """Start the daemon from a copy of the binary, so the test can act on that
    copy the way install.py acts on the deployed one."""
    exe = tmp_path / "deployed" / daemon_bin().name
    exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(daemon_bin(), exe)
    proc = subprocess.Popen(
        [str(exe), "start", "--foreground"],
        env={**os.environ, "REMY_CC_HOME": str(home)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return exe, proc


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX unlink semantics")
def test_posix_unlink_of_running_binary_succeeds(tmp_path):
    """A running daemon does not protect its own file on POSIX. Manifest cleanup
    would therefore delete a live deployment outright, which is why
    install.cleanup_from_manifest skips records outside claude_home."""
    home = tmp_path / "home"
    exe, proc = _spawn_from_copy(tmp_path, home)
    try:
        assert wait_for_state(home, True)
        exe.unlink()
        assert not exe.exists()
        assert is_running(home)
    finally:
        proc.kill()
        proc.wait(timeout=10)
        _force_cleanup(home)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ETXTBSY semantics")
def test_posix_overwrite_of_running_binary_raises_etxtbsy(tmp_path):
    """Counterpart to the Windows sharing violation: writing over a running
    executable fails on POSIX too, with ETXTBSY instead of PermissionError.
    deploy_daemon_binary catches OSError on both platforms."""
    home = tmp_path / "home"
    exe, proc = _spawn_from_copy(tmp_path, home)
    try:
        assert wait_for_state(home, True)
        with pytest.raises(OSError) as exc:
            shutil.copy2(daemon_bin(), exe)
        assert exc.value.errno == errno.ETXTBSY
    finally:
        proc.kill()
        proc.wait(timeout=10)
        _force_cleanup(home)
