"""Integration tests for remy-cc via remy-cc CLI."""

import errno
import json
import shutil
import subprocess
import sys
import time

import pytest

from daemon_test_support import (
    daemon_bin,
    daemon_env,
    is_running,
    run_daemon,
    skip_reason,
    started_daemon,
    wait_for_state,
)

pytestmark = pytest.mark.skipif(skip_reason() is not None, reason=skip_reason() or "")


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


def _spawn_from_copy(tmp_path, home):
    exe = tmp_path / "deployed" / daemon_bin().name
    exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(daemon_bin(), exe)
    proc = subprocess.Popen(
        [str(exe), "start", "--foreground"],
        env={**daemon_env(home), "REMY_CC_HOME": str(home)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return exe, proc


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX unlink semantics")
def test_posix_unlink_of_running_binary_succeeds(tmp_path):
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ETXTBSY semantics")
def test_posix_overwrite_of_running_binary_raises_etxtbsy(tmp_path):
    home = tmp_path / "home"
    exe, proc = _spawn_from_copy(tmp_path, home)
    try:
        assert wait_for_state(home, True)
        with pytest.raises(OSError) as captured:
            shutil.copy2(daemon_bin(), exe)
        assert captured.value.errno == errno.ETXTBSY
    finally:
        proc.kill()
        proc.wait(timeout=10)
