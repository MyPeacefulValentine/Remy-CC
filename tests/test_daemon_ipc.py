"""Integration tests for remy-daemon IPC contract (R1.2): hello/ping/shutdown,
bad token rejection, version-mismatch fallback, latency measurement.

Skip conditions match test_daemon_integration.py: no cargo/rustc OR no compiled
binary. These tests drive the daemon through its IPC endpoints to validate INV-R4
(client-side version adjudication and fallback) and measure roundtrip latency.
"""
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

PROTOCOL_VERSION = 1


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


class DaemonClient:
    """Probe-level IPC client: reads endpoint files, sends one JSON line per
    connection, reads one JSON line back. Candidate for R2 hook reuse
    evaluation (plan section 4.2 R1.2)."""

    def __init__(self, run_dir, timeout=2.0):
        self.run_dir = Path(run_dir)
        self.timeout = timeout
        self.port = None
        self.token = None

    def discover(self, wait_secs=5.0):
        """Wait for daemon.port, then read both endpoint files. The daemon
        writes daemon.token before daemon.port, so seeing the port file
        guarantees the token file is readable."""
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
        """Handshake with client-side version adjudication (INV-R4). Returns
        (compatible, response); the caller falls back to non-IPC paths when
        compatible is False."""
        response = self.request(
            {"cmd": "hello", "protocol_version": protocol_version, "token": self.token}
        )
        compatible = (
            response.get("ok") is True
            and response.get("protocol_version") == protocol_version
        )
        return compatible, response


@pytest.fixture
def daemon_home(tmp_path):
    home = tmp_path / "home"
    result = run_daemon(home, ["start"])
    assert result.returncode == 0, result.stderr
    yield home
    run_daemon(home, ["stop"], timeout=15)


def connected_client(home):
    return DaemonClient(home / "run").discover()


def test_hello_handshake_golden_sample(daemon_home):
    client = connected_client(daemon_home)
    compatible, response = client.hello()

    assert compatible is True
    assert response["ok"] is True
    assert response["protocol_version"] == PROTOCOL_VERSION
    assert isinstance(response["daemon_version"], str)
    assert response["daemon_version"] != ""


def test_bad_token_rejected(daemon_home):
    client = connected_client(daemon_home)
    response = client.request({"cmd": "ping", "token": "not-the-token"})

    assert response == {"ok": False, "error": "bad_token"}


def test_version_mismatch_triggers_client_fallback(daemon_home):
    client = connected_client(daemon_home)
    compatible, response = client.hello(protocol_version=999)

    assert response["ok"] is True
    assert response["protocol_version"] == PROTOCOL_VERSION
    assert compatible is False


def test_ping_roundtrip(daemon_home):
    client = connected_client(daemon_home)
    response = client.request({"cmd": "ping", "token": client.token})

    assert response == {"ok": True}


def test_shutdown_stops_daemon(daemon_home):
    client = connected_client(daemon_home)
    response = client.request({"cmd": "shutdown", "token": client.token})
    assert response == {"ok": True}

    deadline = time.time() + 10
    while time.time() < deadline:
        if run_daemon(daemon_home, ["status"]).returncode == 1:
            break
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

    assert response == {"ok": False, "error": "invalid_json"}


def test_latency_measurement_recorded(daemon_home):
    client = connected_client(daemon_home)

    samples = []
    for _ in range(50):
        start = time.perf_counter()
        response = client.request({"cmd": "ping", "token": client.token})
        samples.append((time.perf_counter() - start) * 1000.0)
        assert response == {"ok": True}

    median = statistics.median(samples)
    p99 = sorted(samples)[int(len(samples) * 0.99) - 1]
    print(
        "\nIPC roundtrip (connect+ping, 50 samples): median={:.3f} ms, p99={:.3f} ms"
        " [conditions: platform={}, python={}.{}, debug binary]".format(
            median, p99, sys.platform, sys.version_info.major, sys.version_info.minor
        )
    )
