"""Process probes used by the installer transaction preflight."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import InstallRuntimeError, RootPaths, RuntimeDescriptor

PROBE_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class DaemonProbe:
    state: str
    version: Optional[str] = None


def roots_from_environment() -> RootPaths:
    home = _user_home()
    claude_value = os.environ.get("CLAUDE_CONFIG_DIR")
    remy_value = os.environ.get("REMY_CC_HOME")
    claude = Path(claude_value) if claude_value else home / ".claude"
    remy = Path(remy_value) if remy_value else home / ".remy-cc"
    return RootPaths(claude=claude, remy=remy)


def probe_python(executable: Path | str) -> RuntimeDescriptor:
    command = [
        str(executable),
        "-I",
        "-c",
        (
            "import json,platform,sys;"
            "print(json.dumps({'executable':sys.executable,'version':list(sys.version_info[:3]),"
            "'implementation':platform.python_implementation(),'platform':sys.platform}))"
        ),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallRuntimeError("Python interpreter probe failed") from exc
    if result.returncode != 0:
        raise InstallRuntimeError("Python interpreter probe failed")
    try:
        payload = json.loads(result.stdout)
        version = payload["version"]
        resolved = Path(payload["executable"])
        implementation = payload["implementation"]
        platform_name = payload["platform"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InstallRuntimeError("Python interpreter returned an invalid probe") from exc
    if (
        not isinstance(version, list)
        or len(version) != 3
        or not all(isinstance(item, int) for item in version)
        or tuple(version) < (3, 10, 0)
        or not resolved.is_absolute()
        or not isinstance(implementation, str)
        or not isinstance(platform_name, str)
    ):
        raise InstallRuntimeError("Python 3.10 or newer is required")
    return RuntimeDescriptor(
        executable=str(resolved),
        version=(version[0], version[1], version[2]),
        implementation=implementation,
        platform=platform_name,
        probed_at=datetime.now(timezone.utc).isoformat(),
    )


def probe_daemon(executable: Path) -> DaemonProbe:
    if not executable.is_file():
        run_dir = executable.parent.parent / "run"
        endpoints = ("daemon.pid", "daemon.port", "daemon.token")
        if any((run_dir / name).exists() for name in endpoints):
            return DaemonProbe("unknown")
        lock_state = _lock_file_is_held(run_dir / "daemon.lock")
        return DaemonProbe("unknown" if lock_state is not False else "stopped")
    try:
        result = subprocess.run(
            [str(executable), "status", "--json"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DaemonProbe("unknown")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        if result.returncode == 2:
            # Daemons predating `status --json` reject the flag with exit 2
            # and usage text; fall back to the plain-text tri-state probe.
            return _probe_daemon_plain_status(executable)
        return DaemonProbe("unknown")
    if not isinstance(payload, dict) or not isinstance(payload.get("running"), bool):
        return DaemonProbe("unknown")
    if payload["running"]:
        version = payload.get("daemon_version")
        return DaemonProbe("running", version if isinstance(version, str) else None)
    if result.returncode == 1:
        return DaemonProbe("stopped")
    return DaemonProbe("unknown")


def _probe_daemon_plain_status(executable: Path) -> DaemonProbe:
    try:
        result = subprocess.run(
            [str(executable), "status"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DaemonProbe("unknown")
    if result.returncode == 0:
        return DaemonProbe("running", probe_daemon_version(executable))
    if result.returncode == 1:
        return DaemonProbe("stopped")
    return DaemonProbe("unknown")


def probe_daemon_version(executable: Path) -> Optional[str]:
    if not executable.is_file():
        return None
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split()
    return parts[-1] if len(parts) >= 2 else None


def _lock_file_is_held(path: Path) -> Optional[bool]:
    if not path.exists():
        return False
    try:
        with path.open("a+b") as stream:
            if os.name == "nt":
                if path.stat().st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt = importlib.import_module("msvcrt")
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        return None
    return False


def default_daemon_name() -> str:
    return "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"


def _user_home() -> Path:
    try:
        return Path.home()
    except RuntimeError:
        value = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        if not value:
            raise InstallRuntimeError("cannot determine user home")
        return Path(value)
