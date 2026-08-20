"""Integration tests for the remy-daemon protocol v5 and Hook clients."""

import json
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from daemon_test_support import (
    PROTOCOL_VERSION,
    STATE_SCHEMA_VERSION,
    DaemonClient,
    daemon_env,
    force_cleanup,
    run_daemon,
    skip_reason,
    wait_for_terminal,
)

pytestmark = pytest.mark.skipif(skip_reason() is not None, reason=skip_reason() or "")

HOOK_DIR = Path(__file__).resolve().parent.parent / "hooks"
INDEX_DIR = Path(__file__).resolve().parent.parent / "skills" / "remy-index"
REMY_SRC_DIR = Path(__file__).resolve().parent.parent / "remy-src"
STRUCT_SCAN = INDEX_DIR / "struct_scan.py"


@pytest.fixture
def daemon_home(tmp_path):
    home = tmp_path / "home"
    result = run_daemon(home, ["start"])
    try:
        assert result.returncode == 0, result.stderr
        yield home
    finally:
        force_cleanup(home)


def connected_client(home):
    return DaemonClient(Path(home) / "run").discover()


def submit(client, project, file_path="src/main.py", priority="background"):
    return client.job_request(
        "submit_job",
        project_path=str(project),
        db_path=str(project / ".claude" / "logic_index.db"),
        file_path=file_path,
        priority=priority,
    )


def _hook_payload(tool_name, project, file_path):
    return json.dumps(
        {
            "tool_name": tool_name,
            "tool_input": {"file_path": str(file_path)},
            "cwd": str(project),
        }
    )


def _run_python_hook(home, script, payload, project, extra_env=None):
    environment = daemon_env(home)
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK_DIR / script)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=project,
        env=environment,
        timeout=40,
    )


def _seed_enrichment_db(project, home):
    result = subprocess.run(
        [
            sys.executable,
            str(STRUCT_SCAN),
            "--cwd",
            str(project),
            "--files",
            "main.py",
            "utils.py",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=daemon_env(home),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _install_index_skill(home):
    claude_home = Path(home) / ".claude"
    shutil.copytree(INDEX_DIR, claude_home / "skills" / "remy-index")
    shutil.copytree(REMY_SRC_DIR, claude_home / "remy-src")


def _write_config(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": "1.0.0", "values": values}), encoding="utf-8"
    )


class FakeEndpoint:
    def __init__(self, home, responder, expected_connections=1):
        self.home = Path(home)
        self.responder = responder
        self.expected_connections = expected_connections
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.token = "test-token"
        run_dir = self.home / "run"
        run_dir.mkdir(parents=True)
        (run_dir / "daemon.port").write_text(
            str(self.listener.getsockname()[1]), encoding="ascii"
        )
        (run_dir / "daemon.token").write_text(self.token, encoding="ascii")
        self.thread = threading.Thread(target=self._serve)

    def _serve(self):
        for index in range(self.expected_connections):
            connection, _ = self.listener.accept()
            with connection:
                with connection.makefile("r", encoding="utf-8") as reader:
                    payload = json.loads(reader.readline())
                self.responder(connection, payload, index)
        self.listener.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        del _args
        self.thread.join(timeout=5)
        self.listener.close()
        assert not self.thread.is_alive()


def _percentile(samples, quantile):
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * quantile + 0.999999) - 1))
    return ordered[index]


def _distribution(samples):
    return {
        "n": len(samples),
        "p50_ms": statistics.median(samples),
        "p90_ms": _percentile(samples, 0.90),
        "p99_ms": _percentile(samples, 0.99),
        "max_ms": max(samples),
    }


def test_hello_handshake_golden_sample(daemon_home):
    compatible, response = connected_client(daemon_home).hello()
    assert compatible is True
    assert response["type"] == "hello"
    assert response["protocol_version"] == PROTOCOL_VERSION
    assert response["state_schema_version"] == STATE_SCHEMA_VERSION
    assert response["daemon_version"]


def test_bad_token_rejected(daemon_home):
    client = connected_client(daemon_home)
    response = client.request({"cmd": "ping", "token": "not-the-token"})
    assert response["type"] == "error"
    assert response["code"] == "bad_token"


def test_version_mismatch_triggers_client_fallback(daemon_home):
    compatible, response = connected_client(daemon_home).hello(protocol_version=999)
    assert response["type"] == "hello"
    assert response["protocol_version"] == PROTOCOL_VERSION
    assert compatible is False


def test_business_version_mismatches_are_rejected(daemon_home, tmp_path):
    client = connected_client(daemon_home)
    project = tmp_path / "project"
    project.mkdir()
    payload = {
        "cmd": "submit_job",
        "protocol_version": 999,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "token": client.token,
        "project_path": str(project),
        "db_path": str(project / "index.db"),
        "file_path": "a.py",
        "priority": "interactive",
    }
    assert client.request(payload)["code"] == "incompatible_protocol"
    payload["protocol_version"] = PROTOCOL_VERSION
    payload["state_schema_version"] = 999
    assert client.request(payload)["code"] == "incompatible_state_schema"


def test_submit_get_cancel_and_pending_deduplication(daemon_home, tmp_path):
    client = connected_client(daemon_home)
    project = tmp_path / "project"
    project.mkdir()
    first = submit(client, project)
    assert first["type"] == "submitted"
    assert first["created"] is True
    assert first["job"]["status"] == "pending"
    job_id = first["job"]["id"]
    duplicate = submit(client, project, priority="interactive")
    assert duplicate["job"]["id"] in {job_id, job_id + 1}
    if duplicate["created"]:
        assert duplicate["job"]["status"] == "pending"
    else:
        assert duplicate["job"]["priority"] == "interactive"
    queried = client.job_request("get_job", job_id=job_id)
    assert queried["type"] == "job"
    assert queried["job"]["id"] == job_id
    assert queried["job"]["result"] is None
    assert queried["job"]["error"] is None
    cancelled = client.job_request("cancel_job", job_id=job_id)
    assert cancelled["type"] == "cancelled"
    assert cancelled["changed"] is True
    assert cancelled["job"]["status"] in {"cancelled", "cancel_requested"}
    repeated = client.job_request("cancel_job", job_id=job_id)
    assert repeated["changed"] is False


def test_promote_job_never_creates_a_successor(daemon_home, tmp_path):
    client = connected_client(daemon_home)
    project = tmp_path / "promote_project"
    project.mkdir()
    (project / "main.py").write_text("x = 1\n", encoding="utf-8")
    submitted = submit(client, project, file_path="main.py", priority="background")
    promoted = client.job_request(
        "promote_job", job_id=submitted["job"]["id"], priority="interactive"
    )
    assert promoted["type"] == "promoted"
    assert promoted["job"]["id"] == submitted["job"]["id"]
    jobs = client.job_request(
        "list_jobs", project_path=str(project), file_path="main.py", limit=10
    )["jobs"]
    assert [job["id"] for job in jobs] == [submitted["job"]["id"]]


def test_incremental_scan_worker_writes_structured_result(daemon_home, tmp_path):
    client = connected_client(daemon_home)
    project = tmp_path / "worker_project"
    project.mkdir()
    (project / "main.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    response = submit(client, project, file_path="main.py", priority="interactive")
    job = wait_for_terminal(client, response["job"]["id"])
    assert job["status"] == "succeeded"
    assert job["progress_current"] == job["progress_total"] == 3
    assert job["result"]["schema_version"] == 1
    assert job["result"]["outcome"] == "success"
    assert job["result"]["successful_paths"] == ["main.py"]
    assert job["result"]["failed_paths"] == []
    assert job["result"]["postprocess_complete"] is True
    assert (project / ".claude" / "logic_index.db").exists()


def test_list_jobs_file_filter_and_status_json_contract(daemon_home, tmp_path):
    client = connected_client(daemon_home)
    project = tmp_path / "list_project"
    project.mkdir()
    for name in ("main.py", "other.py"):
        (project / name).write_text("x = 1\n", encoding="utf-8")
    first = submit(client, project, file_path="main.py")
    second = submit(client, project, file_path="other.py")
    wait_for_terminal(client, first["job"]["id"])
    wait_for_terminal(client, second["job"]["id"])
    response = client.job_request(
        "list_jobs",
        project_path=str(project),
        file_path="main.py",
        status="succeeded",
        job_type="incremental_scan",
        limit=50,
    )
    assert response["type"] == "job_list"
    assert response["filters"] == {
        "project_path": str(project),
        "file_path": "main.py",
        "status": "succeeded",
        "job_type": "incremental_scan",
    }
    assert [job["id"] for job in response["jobs"]] == [first["job"]["id"]]
    status = run_daemon(daemon_home, ["status", "--json"])
    payload = json.loads(status.stdout)
    assert payload["running"] is True
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["state_schema_version"] == STATE_SCHEMA_VERSION
    assert payload["scanner"]["desired"] == "python"
    assert payload["scanner"]["published"]["provider"] == "python"
    assert payload["scanner"]["diagnostic"] is None
    assert response["jobs"][0]["provider"] == "python"


def test_invalid_paths_and_unknown_jobs_have_stable_error_codes(daemon_home, tmp_path):
    client = connected_client(daemon_home)
    project = tmp_path / "project"
    project.mkdir()
    assert submit(client, project, file_path="../outside.py")["code"] == "invalid_request"
    assert client.job_request("get_job", job_id=9_999_999)["code"] == "not_found"
    invalid_filter = client.job_request(
        "list_jobs", project_path=str(project), file_path="../outside.py", limit=1
    )
    assert invalid_filter["code"] == "invalid_request"


def test_ping_roundtrip(daemon_home):
    client = connected_client(daemon_home)
    assert client.request({"cmd": "ping", "token": client.token}) == {"type": "ack"}


def test_shutdown_stops_daemon(daemon_home):
    client = connected_client(daemon_home)
    assert client.request({"cmd": "shutdown", "token": client.token}) == {"type": "ack"}
    assert run_daemon(daemon_home, ["status"]).returncode == 1


def test_invalid_json_reports_error(daemon_home):
    client = connected_client(daemon_home)
    assert client.port is not None
    with socket.create_connection(("127.0.0.1", client.port), timeout=2.0) as sock:
        sock.sendall(b"this is not json\n")
        with sock.makefile("r", encoding="utf-8") as reader:
            response = json.loads(reader.readline())
    assert response["type"] == "error"
    assert response["code"] == "invalid_request"


def test_hook_dirty_submits_without_writing_dirty_queue(daemon_home, tmp_path):
    project = tmp_path / "hook_project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("x = 1\n", encoding="utf-8")
    result = run_daemon(
        daemon_home, ["hook", "dirty"], input_data=_hook_payload("Write", project, source)
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert not (project / ".claude" / "logic_index_dirty").exists()
    response = connected_client(daemon_home).job_request(
        "list_jobs", project_path=str(project), file_path="main.py", limit=1
    )
    assert len(response["jobs"]) == 1


def test_hook_dirty_falls_back_when_daemon_is_stopped(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("x = 1\n", encoding="utf-8")
    result = run_daemon(
        home, ["hook", "dirty"], input_data=_hook_payload("Write", project, source)
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert (project / ".claude" / "logic_index_dirty").read_text(
        encoding="utf-8"
    ).split() == ["main.py"]


def test_hook_fallback_prefers_managed_python_descriptor(tmp_path):
    home = tmp_path / "home with 空格"
    runtime_dir = home / "runtime"
    runtime_dir.mkdir(parents=True)
    runtime_dir.joinpath("python.json").write_text(
        json.dumps({
            "schema_version": 1,
            "executable": sys.executable,
            "version": list(sys.version_info[:3]),
            "implementation": "CPython",
            "platform": sys.platform,
            "probed_at": "2026-08-13T00:00:00Z",
        }),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("x = 1\n", encoding="utf-8")

    result = run_daemon(
        home,
        ["hook", "dirty"],
        input_data=_hook_payload("Write", project, source),
        extra_env={"REMY_PYTHON": str(tmp_path / "missing-python")},
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert (project / ".claude" / "logic_index_dirty").read_text(
        encoding="utf-8"
    ).split() == ["main.py"]


def test_hook_ignores_missing_path_without_creating_queue(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    payload = json.dumps({"tool_name": "Write", "tool_input": {}, "cwd": str(project)})
    result = run_daemon(home, ["hook", "dirty"], input_data=payload)
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert not (project / ".claude" / "logic_index_dirty").exists()


def test_hook_dirty_falls_back_during_partial_endpoint_publication(tmp_path):
    home = tmp_path / "home"
    run_dir = home / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "daemon.port").write_text("45678", encoding="ascii")
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("x = 1\n", encoding="utf-8")
    result = run_daemon(
        home, ["hook", "dirty"], input_data=_hook_payload("Write", project, source)
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert (project / ".claude" / "logic_index_dirty").exists()


@pytest.mark.parametrize("mode", ["version", "invalid_json", "disconnect", "timeout"])
def test_hook_dirty_falls_back_for_unconfirmed_response(tmp_path, mode):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("x = 1\n", encoding="utf-8")

    def respond(connection, payload, *_ignored):
        del _ignored
        assert payload["cmd"] == "submit_job"
        if mode == "version":
            connection.sendall(
                (json.dumps({
                    "type": "error",
                    "code": "incompatible_protocol",
                    "message": "do not expose this path",
                }) + "\n").encode("utf-8")
            )
        elif mode == "invalid_json":
            connection.sendall(b"{not-json}\n")
        elif mode == "timeout":
            time.sleep(0.1)

    with FakeEndpoint(home, respond):
        result = run_daemon(
            home, ["hook", "dirty"], input_data=_hook_payload("Write", project, source)
        )
    assert result.returncode == 0
    assert result.stdout == ""
    assert "do not expose" not in result.stderr
    assert (project / ".claude" / "logic_index_dirty").exists()


def test_response_loss_after_submit_preserves_state_and_dirty_queue(daemon_home, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("x = 1\n", encoding="utf-8")
    run_dir = Path(daemon_home) / "run"
    original_port = int((run_dir / "daemon.port").read_text(encoding="ascii"))
    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen()
    proxy_port = proxy.getsockname()[1]
    observed = []

    def forward_then_drop():
        downstream, _ = proxy.accept()
        with downstream, socket.create_connection(("127.0.0.1", original_port), timeout=2) as upstream:
            request = downstream.makefile("rb").readline()
            upstream.sendall(request)
            response = upstream.makefile("rb").readline()
            observed.append(json.loads(response))
        proxy.close()

    thread = threading.Thread(target=forward_then_drop)
    thread.start()
    (run_dir / "daemon.port").write_text(str(proxy_port), encoding="ascii")
    try:
        result = run_daemon(
            daemon_home,
            ["hook", "dirty"],
            input_data=_hook_payload("Write", project, source),
        )
    finally:
        (run_dir / "daemon.port").write_text(str(original_port), encoding="ascii")
        thread.join(timeout=5)
        proxy.close()
    assert not thread.is_alive()
    assert observed[0]["type"] == "submitted"
    assert result.returncode == 0
    assert (project / ".claude" / "logic_index_dirty").exists()
    jobs = connected_client(daemon_home).job_request(
        "list_jobs", project_path=str(project), file_path="main.py", limit=10
    )["jobs"]
    assert len(jobs) == 1


def test_rust_and_python_enrichment_outputs_match(daemon_home, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text(
        "import os\nfrom utils import helper\n\ndef run():\n    return helper(1)\n",
        encoding="utf-8",
    )
    (project / "utils.py").write_text("def helper(x):\n    return x\n", encoding="utf-8")
    _seed_enrichment_db(project, daemon_home)
    payload = _hook_payload("Read", project, project / "main.py")
    python_result = _run_python_hook(
        daemon_home, "logic_enrichment_hook.py", payload, project
    )
    rust_result = run_daemon(daemon_home, ["hook", "enrich"], input_data=payload)
    assert python_result.returncode == rust_result.returncode == 0
    assert json.loads(rust_result.stdout) == json.loads(python_result.stdout)


def test_rust_and_python_config_precedence_match(daemon_home, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text(
        "import os\nfrom utils import helper\n\ndef run():\n    return helper(1)\n",
        encoding="utf-8",
    )
    (project / "utils.py").write_text("def helper(argument):\n    return argument\n", encoding="utf-8")
    _seed_enrichment_db(project, daemon_home)
    _write_config(
        daemon_home / ".claude" / "remy-config.json",
        {"REMY_ENRICHMENT_SIG_MAX_CHARS": "6"},
    )
    _write_config(
        project / ".claude" / "remy-config.json",
        {"REMY_ENRICHMENT_SIG_MAX_CHARS": "4"},
    )
    payload = _hook_payload("Read", project, project / "main.py")
    for environment_value in ("2", "invalid"):
        extra_env = {"REMY_ENRICHMENT_SIG_MAX_CHARS": environment_value}
        python_result = _run_python_hook(
            daemon_home,
            "logic_enrichment_hook.py",
            payload,
            project,
            extra_env=extra_env,
        )
        rust_result = run_daemon(
            daemon_home,
            ["hook", "enrich"],
            input_data=payload,
            extra_env=extra_env,
        )
        assert python_result.returncode == rust_result.returncode == 0
        assert json.loads(rust_result.stdout) == json.loads(python_result.stdout)


def test_enrichment_uses_python_when_dirty_queue_exists(daemon_home, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("x = 1\n", encoding="utf-8")
    _seed_enrichment_db(project, daemon_home)
    dirty = project / ".claude" / "logic_index_dirty"
    dirty.write_text("main.py\n", encoding="utf-8")
    _install_index_skill(daemon_home)
    result = run_daemon(
        daemon_home, ["hook", "enrich"], input_data=_hook_payload("Read", project, source)
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert not dirty.exists()


def test_hook_timeout_distributions_are_recorded(tmp_path):
    samples = 20
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("x = 1\n", encoding="utf-8")
    payload = _hook_payload("Write", project, source)

    connect_home = tmp_path / "connect-home"
    connect_run = connect_home / "run"
    connect_run.mkdir(parents=True)
    reserve = socket.socket()
    reserve.bind(("127.0.0.1", 0))
    closed_port = reserve.getsockname()[1]
    reserve.close()
    (connect_run / "daemon.port").write_text(str(closed_port), encoding="ascii")
    (connect_run / "daemon.token").write_text("test-token", encoding="ascii")
    connect_samples = []
    for _ in range(samples):
        start = time.perf_counter()
        result = run_daemon(connect_home, ["hook", "dirty"], input_data=payload)
        connect_samples.append((time.perf_counter() - start) * 1000.0)
        assert result.returncode == 0

    read_home = tmp_path / "read-home"

    def hold_response(_connection, request, *_ignored):
        del _connection, _ignored
        assert request["cmd"] == "submit_job"
        time.sleep(0.1)

    read_samples = []
    with FakeEndpoint(read_home, hold_response, expected_connections=samples):
        for _ in range(samples):
            start = time.perf_counter()
            result = run_daemon(read_home, ["hook", "dirty"], input_data=payload)
            read_samples.append((time.perf_counter() - start) * 1000.0)
            assert result.returncode == 0

    record = {
        "platform": sys.platform,
        "binary": "debug",
        "connect_timeout_ms": 35,
        "read_timeout_ms": 50,
        "connect_fallback": _distribution(connect_samples),
        "read_fallback": _distribution(read_samples),
    }
    print("\nHook timeout paths: " + json.dumps(record, sort_keys=True))
    assert record["connect_fallback"]["p50_ms"] <= record["connect_fallback"]["p99_ms"]
    assert record["read_fallback"]["p50_ms"] <= record["read_fallback"]["p99_ms"]


def test_latency_distributions_are_recorded(daemon_home):
    client = connected_client(daemon_home)
    samples = []
    for _ in range(50):
        start = time.perf_counter()
        response = client.request({"cmd": "ping", "token": client.token})
        samples.append((time.perf_counter() - start) * 1000.0)
        assert response == {"type": "ack"}
    distribution = _distribution(samples)
    print(
        "\nIPC fresh-connection ping: "
        + json.dumps(
            {
                **distribution,
                "platform": sys.platform,
                "python": "{}.{}".format(sys.version_info.major, sys.version_info.minor),
                "binary": "debug",
            },
            sort_keys=True,
        )
    )
    assert distribution["p50_ms"] <= distribution["p90_ms"] <= distribution["p99_ms"]
