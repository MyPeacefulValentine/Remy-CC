"""Integration tests for the remy-daemon protocol v2 job contract."""
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

DAEMON_SOURCE = Path(__file__).resolve().parent.parent / "remy-daemon"
PROTOCOL_VERSION = 2
STATE_SCHEMA_VERSION = 1


def has_rust_toolchain():
    return shutil.which("cargo") is not None and shutil.which("rustc") is not None


def daemon_binary_exists():
    name = "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"
    return (DAEMON_SOURCE / "target" / "debug" / name).exists()


skip_reason = None
if not has_rust_toolchain():
    skip_reason = "cargo/rustc not found in PATH"
elif not daemon_binary_exists():
    skip_reason = "remy-daemon binary not built; run cargo build first"

pytestmark = pytest.mark.skipif(skip_reason is not None, reason=skip_reason or "")


def daemon_bin():
    name = "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"
    return DAEMON_SOURCE / "target" / "debug" / name


def run_daemon(home, args, timeout=10):
    return subprocess.run(
        [str(daemon_bin())] + args,
        env={**os.environ, "REMY_CC_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class DaemonClient:
    def __init__(self, run_dir, timeout=2.0):
        self.run_dir = Path(run_dir)
        self.timeout = timeout
        self.port = None
        self.token = None

    def discover(self, wait_secs=5.0):
        deadline = time.time() + wait_secs
        port_path = self.run_dir / "daemon.port"
        while time.time() < deadline:
            if port_path.exists():
                text = port_path.read_text(encoding="ascii").strip()
                if text:
                    self.port = int(text)
                    break
            time.sleep(0.01)
        if self.port is None:
            raise TimeoutError("daemon.port not written within {}s".format(wait_secs))
        self.token = (self.run_dir / "daemon.token").read_text(encoding="ascii").strip()
        return self

    def request(self, payload):
        with socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout) as sock:
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            with sock.makefile("r", encoding="utf-8") as reader:
                line = reader.readline()
        if not line:
            raise ConnectionError("daemon closed connection without a response")
        return json.loads(line)

    def hello(self, protocol_version=PROTOCOL_VERSION):
        response = self.request(
            {"cmd": "hello", "protocol_version": protocol_version, "token": self.token}
        )
        compatible = (
            response.get("type") == "hello"
            and response.get("protocol_version") == protocol_version
            and response.get("state_schema_version") == STATE_SCHEMA_VERSION
        )
        return compatible, response

    def job_request(self, command, **fields):
        return self.request(
            {
                "cmd": command,
                "protocol_version": PROTOCOL_VERSION,
                "state_schema_version": STATE_SCHEMA_VERSION,
                "token": self.token,
                **fields,
            }
        )


def _force_cleanup(home):
    run_daemon(home, ["stop"], timeout=15)
    if run_daemon(home, ["status"]).returncode != 0:
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


@pytest.fixture
def daemon_home(tmp_path):
    home = tmp_path / "home"
    result = run_daemon(home, ["start"])
    try:
        assert result.returncode == 0, result.stderr
        yield home
    finally:
        _force_cleanup(home)


def connected_client(home):
    return DaemonClient(home / "run").discover()


def submit(client, project, file_path="src/main.py", priority="background"):
    return client.job_request(
        "submit_job",
        project_path=str(project),
        db_path=str(project / ".claude" / "logic_index.db"),
        file_path=file_path,
        priority=priority,
    )


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
    assert duplicate["created"] is False
    assert duplicate["job"]["id"] == job_id
    assert duplicate["job"]["priority"] == "interactive"

    queried = client.job_request("get_job", job_id=job_id)
    assert queried["type"] == "job"
    assert queried["job"]["id"] == job_id
    assert queried["job"]["result"] is None
    assert queried["job"]["error"] is None

    cancelled = client.job_request("cancel_job", job_id=job_id)
    assert cancelled["type"] == "cancelled"
    assert cancelled["changed"] is True
    assert cancelled["job"]["status"] == "cancelled"
    repeated = client.job_request("cancel_job", job_id=job_id)
    assert repeated["changed"] is False
    assert repeated["job"]["status"] == "cancelled"


def test_invalid_paths_and_unknown_jobs_have_stable_error_codes(daemon_home, tmp_path):
    client = connected_client(daemon_home)
    project = tmp_path / "project"
    project.mkdir()
    invalid = submit(client, project, file_path="../outside.py")
    assert invalid["code"] == "invalid_request"
    assert client.job_request("get_job", job_id=9_999_999)["code"] == "not_found"


def test_ping_roundtrip(daemon_home):
    client = connected_client(daemon_home)
    assert client.request({"cmd": "ping", "token": client.token}) == {"type": "ack"}


def test_shutdown_stops_daemon(daemon_home):
    client = connected_client(daemon_home)
    assert client.request({"cmd": "shutdown", "token": client.token}) == {"type": "ack"}
    deadline = time.time() + 10
    while time.time() < deadline and run_daemon(daemon_home, ["status"]).returncode != 1:
        time.sleep(0.05)
    status = run_daemon(daemon_home, ["status"])
    assert status.returncode == 1
    assert "not running" in status.stdout


def test_invalid_json_reports_error(daemon_home):
    client = connected_client(daemon_home)
    with socket.create_connection(("127.0.0.1", client.port), timeout=2.0) as sock:
        sock.sendall(b"this is not json\n")
        with sock.makefile("r", encoding="utf-8") as reader:
            response = json.loads(reader.readline())
    assert response["type"] == "error"
    assert response["code"] == "invalid_request"


def test_latency_measurement_recorded(daemon_home):
    client = connected_client(daemon_home)
    samples = []
    for _ in range(50):
        start = time.perf_counter()
        response = client.request({"cmd": "ping", "token": client.token})
        samples.append((time.perf_counter() - start) * 1000.0)
        assert response == {"type": "ack"}
    median = statistics.median(samples)
    p99 = sorted(samples)[int(len(samples) * 0.99) - 1]
    print(
        "\nIPC roundtrip (connect+ping, 50 samples): median={:.3f} ms, p99={:.3f} ms"
        " [conditions: platform={}, python={}.{}, debug binary]".format(
            median, p99, sys.platform, sys.version_info.major, sys.version_info.minor
        )
    )
