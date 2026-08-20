"""End-to-end tests for R3.5b scanner provider switching and publication."""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from daemon_test_support import (
    DaemonClient,
    force_cleanup,
    run_daemon,
    skip_reason,
    wait_for_state,
    wait_for_terminal,
)

pytestmark = pytest.mark.skipif(skip_reason() is not None, reason=skip_reason() or "")


def _client(home):
    return DaemonClient(Path(home) / "run").discover()


def _status_json(home):
    result = run_daemon(home, ["status", "--json"])
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _wait_for_published(home, provider, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = _status_json(home)
        published = payload["scanner"]["published"]
        if published and published["provider"] == provider:
            return payload
        time.sleep(0.2)
    raise TimeoutError("published provider did not become {}".format(provider))


def _submit(client, project, file_path):
    return client.job_request(
        "submit_job",
        project_path=str(project),
        db_path=str(project / ".claude" / "logic_index.db"),
        file_path=file_path,
        priority="interactive",
    )


def _full_scan_jobs(client, project):
    return client.job_request(
        "list_jobs", project_path=str(project), job_type="full_scan", limit=50
    )["jobs"]


def _hard_kill(home):
    pid = (Path(home) / "run" / "daemon.pid").read_text(encoding="ascii").strip()
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
    else:
        subprocess.run(["kill", "-KILL", pid], capture_output=True)
    assert wait_for_state(home, False, timeout_secs=10)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(
        "def answer():\n    return 42\n\n\ndef caller():\n    return answer()\n",
        encoding="utf-8",
    )
    return root


def test_switch_publishes_rust_full_scans_and_survives_hard_kill(tmp_path, project):
    home = tmp_path / "home"
    assert run_daemon(home, ["start"]).returncode == 0
    try:
        client = _client(home)
        first = _submit(client, project, "main.py")
        wait_for_terminal(client, first["job"]["id"])
        payload = _wait_for_published(home, "python")
        assert payload["scanner"]["desired"] == "python"

        assert run_daemon(home, ["stop"]).returncode == 0
        assert wait_for_state(home, False)
        start = run_daemon(
            home,
            ["start"],
            timeout=60,
            extra_env={"REMY_SCANNER_PROVIDER": "rust"},
        )
        assert start.returncode == 0, start.stderr
        payload = _wait_for_published(home, "rust")
        assert payload["scanner"]["desired"] == "rust"
        assert payload["scanner"]["diagnostic"] is None
        assert payload["scanner"]["published"]["probe_summary"]

        client = _client(home)
        full_scans = _full_scan_jobs(client, project)
        assert len(full_scans) == 1
        full_scan = wait_for_terminal(client, full_scans[0]["id"], timeout=60)
        assert full_scan["status"] == "succeeded", full_scan["error"]
        assert full_scan["provider"] == "rust"
        assert full_scan["result"]["outcome"] == "success"
        assert "main.py" in full_scan["result"]["successful_paths"]

        db = sqlite3.connect(str(project / ".claude" / "logic_index.db"))
        try:
            backends = dict(
                db.execute("SELECT path, parser_backend FROM files")
            )
        finally:
            db.close()
        assert backends.get("main.py") == "python-tree-sitter"

        (project / "extra.py").write_text("def extra():\n    return 1\n", encoding="utf-8")
        incremental = _submit(client, project, "extra.py")
        job = wait_for_terminal(client, incremental["job"]["id"], timeout=60)
        assert job["status"] == "succeeded", job["error"]
        assert job["provider"] == "rust"

        _hard_kill(home)
        restart = run_daemon(
            home,
            ["start"],
            timeout=60,
            extra_env={"REMY_SCANNER_PROVIDER": "rust"},
        )
        assert restart.returncode == 0, restart.stderr
        payload = _wait_for_published(home, "rust")
        assert payload["scanner"]["diagnostic"] is None
        client = _client(home)
        assert len(_full_scan_jobs(client, project)) == 1
    finally:
        force_cleanup(home)


def test_invalid_desired_value_keeps_python_and_reports_diagnostic(tmp_path):
    home = tmp_path / "home"
    start = run_daemon(
        home, ["start"], extra_env={"REMY_SCANNER_PROVIDER": "weird"}
    )
    assert start.returncode == 0, start.stderr
    try:
        payload = _status_json(home)
        assert payload["scanner"]["desired"] == "weird"
        assert payload["scanner"]["published"] is None
        assert "REMY_SCANNER_PROVIDER" in payload["scanner"]["diagnostic"]
    finally:
        force_cleanup(home)


def test_rust_job_cancel_kills_the_scan_subprocess(tmp_path, project):
    home = tmp_path / "home"
    hold_ms = 20_000
    start = run_daemon(
        home, ["start"], timeout=120, extra_env={"REMY_SCANNER_PROVIDER": "rust"}
    )
    assert start.returncode == 0, start.stderr
    try:
        _wait_for_published(home, "rust")
        assert run_daemon(home, ["stop"]).returncode == 0
        assert wait_for_state(home, False)
        restart = run_daemon(
            home,
            ["start"],
            timeout=120,
            extra_env={
                "REMY_SCANNER_PROVIDER": "rust",
                "REMY_SCAN_LOCK_HOLD_MS": str(hold_ms),
            },
        )
        assert restart.returncode == 0, restart.stderr

        client = _client(home)
        submitted = _submit(client, project, "main.py")
        job_id = submitted["job"]["id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            job = client.job_request("get_job", job_id=job_id)["job"]
            if job["status"] == "running" and job["progress_message"] == "scanning":
                break
            time.sleep(0.05)
        else:
            pytest.fail("job did not reach the scanning stage")
        assert job["provider"] == "rust"

        cancel_started = time.time()
        cancelled = client.job_request("cancel_job", job_id=job_id)
        assert cancelled["changed"] is True
        job = wait_for_terminal(client, job_id, timeout=15)
        elapsed = time.time() - cancel_started
        assert job["status"] == "cancelled"
        assert elapsed < hold_ms / 1000.0 / 2, elapsed
    finally:
        force_cleanup(home)
