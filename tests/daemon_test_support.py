"""Shared daemon process and IPC helpers for tests only."""

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

DAEMON_SOURCE = Path(__file__).resolve().parent.parent / "remy-cc"
PROTOCOL_VERSION = 5
STATE_SCHEMA_VERSION = 2


def has_rust_toolchain():
    return shutil.which("cargo") is not None and shutil.which("rustc") is not None


def daemon_bin():
    name = "remy-cc.exe" if sys.platform == "win32" else "remy-cc"
    return DAEMON_SOURCE / "target" / "debug" / name


def daemon_binary_exists():
    return daemon_bin().exists()


def skip_reason():
    if not has_rust_toolchain():
        return "cargo/rustc not found in PATH"
    if not daemon_binary_exists():
        return "remy-cc binary not built; run cargo build first"
    return None


def daemon_env(home):
    home = Path(home)
    return {
        **os.environ,
        "REMY_CC_HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / "claude"),
        "HOME": str(home),
        "USERPROFILE": str(home),
    }


def run_daemon(home, args, timeout=10, *, input_data=None, extra_env=None):
    environment = daemon_env(home)
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [str(daemon_bin())] + list(args),
        env=environment,
        input=input_data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def is_running(home):
    return run_daemon(home, ["status"]).returncode == 0


def wait_for_state(home, expected_running, timeout_secs=5):
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if is_running(home) == expected_running:
            return True
        time.sleep(0.05)
    return is_running(home) == expected_running


def _path_is_released(path):
    if not path.exists():
        return True
    probe = path.with_name(path.name + ".release-probe")
    try:
        os.replace(path, probe)
        os.replace(probe, path)
        return True
    except OSError:
        if probe.exists() and not path.exists():
            try:
                os.replace(probe, path)
            except OSError:
                pass
        return False


def _wait_for_sqlite_release(home, timeout_secs=5):
    paths = [Path(home) / name for name in ("state.db", "state.db-wal", "state.db-shm")]
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if all(_path_is_released(path) for path in paths):
            return True
        time.sleep(0.01)
    return all(_path_is_released(path) for path in paths)


def force_cleanup(home):
    run_daemon(home, ["stop"], timeout=15)
    if is_running(home):
        try:
            pid = (Path(home) / "run" / "daemon.pid").read_text(encoding="ascii").strip()
        except OSError:
            pid = ""
        if pid.isdigit():
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
            else:
                subprocess.run(["kill", "-KILL", pid], capture_output=True)
        wait_for_state(home, False, timeout_secs=5)
    if not _wait_for_sqlite_release(home):
        raise RuntimeError("daemon SQLite handles were not released during cleanup")


@contextlib.contextmanager
def started_daemon(home):
    result = run_daemon(home, ["start"])
    try:
        yield result
    finally:
        force_cleanup(home)


class DaemonClient:
    def __init__(self, run_dir, timeout=2.0):
        self.run_dir = Path(run_dir)
        self.timeout = timeout
        self.port = None
        self.token = None

    def discover(self, wait_secs=5.0):
        deadline = time.time() + wait_secs
        port_path = self.run_dir / "daemon.port"
        token_path = self.run_dir / "daemon.token"
        while time.time() < deadline:
            if port_path.exists() and token_path.exists():
                port_text = port_path.read_text(encoding="ascii").strip()
                token_text = token_path.read_text(encoding="ascii").strip()
                if port_text and token_text:
                    self.port = int(port_text)
                    self.token = token_text
                    return self
            time.sleep(0.01)
        raise TimeoutError("daemon endpoint was not published within {}s".format(wait_secs))

    def request(self, payload):
        if self.port is None:
            raise RuntimeError("client discovery has not completed")
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


def wait_for_terminal(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.job_request("get_job", job_id=job_id)
        if response["job"]["status"] in {
            "succeeded", "failed", "cancelled", "superseded"
        }:
            return response["job"]
        time.sleep(0.02)
    raise TimeoutError("job {} did not reach a terminal state".format(job_id))
