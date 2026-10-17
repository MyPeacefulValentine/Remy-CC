"""Test daemon startup budgets and failure diagnostics without spawning a daemon."""

from inspect import unwrap
import json
from pathlib import Path
import subprocess

import pytest

import daemon_test_support as support


@pytest.mark.parametrize("args, explicit_timeout, expected", [
    (["start"], None, 30),
    (["status"], None, 10),
    (["hook", "dirty"], None, 10),
    (["start"], 3, 3),
    (["start"], 60, 60),
    ([], None, 10),
])
def test_start_has_launcher_budget_and_explicit_limits_are_preserved(tmp_path, monkeypatch, args, explicit_timeout, expected):
    observed = {}
    completed = subprocess.CompletedProcess(args, 0, "ok", "")

    def fake_run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return completed

    monkeypatch.setattr(support.subprocess, "run", fake_run)
    assert support.run_daemon(tmp_path, args, timeout=explicit_timeout) is completed
    assert observed["timeout"] == expected
    assert observed["command"][1:] == args
    assert observed["env"]["REMY_CC_HOME"] == str(tmp_path)


def test_timeout_keeps_exception_and_reports_bounded_logs_without_token(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "daemon.pid").write_text("123", encoding="ascii")
    (run_dir / "daemon.port").write_text("456", encoding="ascii")
    (run_dir / "daemon.token").write_text("private-token-must-not-be-read", encoding="ascii")
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    (log_dir / "daemon.log").write_bytes(b"x" * 8192 + b"provider_sync")
    timeout = subprocess.TimeoutExpired(["remy-cc", "start"], 30)

    def fail(*args, **kwargs):
        raise timeout

    monkeypatch.setattr(support.subprocess, "run", fail)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        support.run_daemon(tmp_path, ["start"])
    assert raised.value is timeout
    output = capsys.readouterr().err
    assert "private-token-must-not-be-read" not in output
    diagnostics = json.loads(output.split("diagnostics: ", 1)[1])
    assert diagnostics["token_present"] is True
    assert diagnostics["run/daemon.pid"]["tail"] == "123"
    assert diagnostics["run/daemon.port"]["tail"] == "456"
    assert diagnostics["log/daemon.log"]["truncated"] is True
    assert len(diagnostics["log/daemon.log"]["tail"]) == 8192
    assert diagnostics["log/daemon.log"]["tail"].endswith("provider_sync")


def test_production_start_failure_is_not_changed_to_success(tmp_path, monkeypatch, capsys):
    completed = subprocess.CompletedProcess(["remy-cc", "start"], 2, "", "start timed out")
    monkeypatch.setattr(support.subprocess, "run", lambda *args, **kwargs: completed)
    result = support.run_daemon(tmp_path, ["start"])
    assert result is completed and result.returncode == 2
    assert "startup failure diagnostics" in capsys.readouterr().err


def test_context_manager_attempts_cleanup_when_start_raises(tmp_path, monkeypatch):
    cleaned = []

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(["remy-cc", "start"], 30)

    monkeypatch.setattr(support, "run_daemon", fail)
    monkeypatch.setattr(support, "force_cleanup", cleaned.append)
    with pytest.raises(subprocess.TimeoutExpired):
        with support.started_daemon(tmp_path):
            pytest.fail("a failed start must not enter the body")
    assert cleaned == [tmp_path]


def test_ipc_fixture_attempts_cleanup_when_start_raises(tmp_path, monkeypatch):
    import test_daemon_ipc as ipc

    cleaned = []

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired(["remy-cc", "start"], 30)

    monkeypatch.setattr(ipc, "run_daemon", fail)
    monkeypatch.setattr(ipc, "force_cleanup", cleaned.append)
    fixture = unwrap(ipc.daemon_home)(tmp_path)
    with pytest.raises(subprocess.TimeoutExpired):
        next(fixture)
    assert cleaned == [tmp_path / "home"]


def test_ci_keeps_daemon_logs_without_uploading_endpoint_tokens():
    workflow = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    section = workflow.split("- name: preserve daemon logs on failure", 1)[1].split("- name:", 1)[0]
    assert "if: failure()" in section
    assert "**/log/daemon.log*" in section
    assert "daemon.token" not in section
    assert "retention-days: 7" in section
