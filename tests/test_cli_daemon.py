"""Unit tests for the `remy-cc daemon` passthrough (cmd_daemon / _remy_cc_home).

No Rust toolchain or built binary is required: filesystem state is faked under
tmp_path and subprocess execution is stubbed at the boundary.
"""
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))

import cli


def test_remy_cc_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("REMY_CC_HOME", str(tmp_path / "custom"))
    assert cli._remy_cc_home() == tmp_path / "custom"


def test_remy_cc_home_empty_env_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.setenv("REMY_CC_HOME", "")
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    assert cli._remy_cc_home() == tmp_path / ".remy-cc"


def test_remy_cc_home_env_fallback_when_path_home_raises(monkeypatch, tmp_path):
    def _raise():
        raise RuntimeError("no home")

    monkeypatch.delenv("REMY_CC_HOME", raising=False)
    monkeypatch.setattr(cli.Path, "home", _raise)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli._remy_cc_home() == tmp_path / ".remy-cc"


def test_remy_cc_home_exits_1_when_no_home_determinable(monkeypatch, capsys):
    def _raise():
        raise RuntimeError("no home")

    monkeypatch.delenv("REMY_CC_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(cli.Path, "home", _raise)

    with pytest.raises(SystemExit) as exc:
        cli._remy_cc_home()

    assert exc.value.code == 1
    assert "Cannot determine home directory" in capsys.readouterr().err


def test_cmd_daemon_missing_binary_exits_1_with_guidance(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("REMY_CC_HOME", str(tmp_path))
    args = types.SimpleNamespace(daemon_args=["status"])

    with pytest.raises(SystemExit) as exc:
        cli.cmd_daemon(args)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "remy-daemon binary not found" in err
    assert "cargo build --release" in err
    assert str(tmp_path) in err


def test_cmd_daemon_forwards_args_and_exit_code(monkeypatch, tmp_path):
    exe_name = "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"
    exe = tmp_path / "bin" / exe_name
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"placeholder")
    monkeypatch.setenv("REMY_CC_HOME", str(tmp_path))

    captured = {}

    def fake_run(argv):
        captured["argv"] = argv
        return types.SimpleNamespace(returncode=7)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    args = types.SimpleNamespace(daemon_args=["start", "--foreground"])

    with pytest.raises(SystemExit) as exc:
        cli.cmd_daemon(args)

    assert exc.value.code == 7
    assert captured["argv"] == [str(exe), "start", "--foreground"]
