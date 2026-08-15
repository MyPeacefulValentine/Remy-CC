"""Tests for session_anchor.py and cwd_guard.py: anchor lifecycle and drift IO."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"

anchor_spec = importlib.util.spec_from_file_location("session_anchor_tested", HOOKS_DIR / "session_anchor.py")
assert anchor_spec and anchor_spec.loader
anchor = importlib.util.module_from_spec(anchor_spec)
sys.modules[anchor_spec.name] = anchor
anchor_spec.loader.exec_module(anchor)

CWD_GUARD_PATH = HOOKS_DIR / "cwd_guard.py"


@pytest.fixture(autouse=True)
def isolated_anchor_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("TEMP", str(tmp_path / "tmp"))
    monkeypatch.setenv("TMP", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    import tempfile
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None


class TestAnchor:
    def test_record_and_read_roundtrip(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        anchor.record("sess-1", str(root))
        assert anchor.read("sess-1") == os.path.realpath(str(root))

    def test_read_missing_session_returns_none(self):
        assert anchor.read("no-such-session") is None

    def test_empty_session_id_is_noop(self, tmp_path):
        anchor.record("", str(tmp_path))
        assert anchor.read("") is None

    def test_resolve_root_prefers_anchor(self, tmp_path):
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)
        anchor.record("sess-2", str(root))
        assert anchor.resolve_root("sess-2", str(sub)) == os.path.realpath(str(root))

    def test_resolve_root_without_anchor_keeps_cwd(self, tmp_path):
        assert anchor.resolve_root("no-such-session", str(tmp_path)) == str(tmp_path)

    def test_drift_detected(self, tmp_path):
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)
        anchor.record("sess-3", str(root))
        assert anchor.drift("sess-3", str(sub)) == os.path.realpath(str(root))

    def test_no_drift_at_anchor(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        anchor.record("sess-4", str(root))
        assert anchor.drift("sess-4", str(root)) is None

    def test_no_drift_without_anchor(self, tmp_path):
        assert anchor.drift("no-such-session", str(tmp_path)) is None

    def test_session_id_is_sanitized(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        anchor.record("a/b\\c:d", str(root))
        assert anchor.read("a/b\\c:d") == os.path.realpath(str(root))


class TestCwdGuardIO:
    def _run(self, stdin_bytes, tmp_dir):
        env = dict(os.environ)
        env["TMPDIR"] = str(tmp_dir)
        env["TEMP"] = str(tmp_dir)
        env["TMP"] = str(tmp_dir)
        proc = subprocess.run(
            [sys.executable, str(CWD_GUARD_PATH)],
            input=stdin_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=60,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()

    def test_drift_emits_system_message(self, tmp_path):
        tmp_dir = tmp_path / "tmp"
        root = tmp_path / "proj"
        sub = root / "sub"
        sub.mkdir(parents=True)
        anchor_dir = tmp_dir / anchor.ANCHOR_DIR_NAME
        anchor_dir.mkdir(parents=True)
        (anchor_dir / "sess-io").write_text(os.path.realpath(str(root)), encoding="utf-8")

        payload = json.dumps({
            "hook_event_name": "CwdChanged",
            "session_id": "sess-io",
            "old_cwd": str(root),
            "new_cwd": str(sub),
        }).encode("utf-8")
        rc, out = self._run(payload, tmp_dir)
        assert rc == 0
        assert "systemMessage" in json.loads(out)

    def test_no_anchor_emits_nothing(self, tmp_path):
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir(exist_ok=True)
        payload = json.dumps({
            "hook_event_name": "CwdChanged",
            "session_id": "sess-none",
            "old_cwd": str(tmp_path),
            "new_cwd": str(tmp_path / "sub"),
        }).encode("utf-8")
        rc, out = self._run(payload, tmp_dir)
        assert rc == 0
        assert out == ""

    @pytest.mark.parametrize("stdin_bytes", [b"", b"not json"])
    def test_malformed_stdin_fails_open(self, tmp_path, stdin_bytes):
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir(exist_ok=True)
        rc, out = self._run(stdin_bytes, tmp_dir)
        assert rc == 0
        assert out == ""
