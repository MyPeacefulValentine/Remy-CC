"""run.py production routing: daemon-scan spawn and the semantic gate."""

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "remy-index"))

import run as run_module
from index_state import RunStatus


class _Config:
    def get_float(self, key):
        del key
        return 1.0

    def get_int(self, key):
        del key
        return 5


def test_run_daemon_scan_maps_the_terminal_scan_result(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        del kwargs
        captured["command"] = command
        payload = {
            "type": "scan_result",
            "schema_version": 1,
            "outcome": "partial",
            "successful_paths": ["a.py"],
            "failed_paths": ["b.py"],
            "deleted_paths": [],
            "postprocess_complete": True,
            "errors": [{"stage": "file_scan", "path": "b.py", "message": "bad"}],
        }
        return types.SimpleNamespace(
            returncode=2, stdout=json.dumps(payload) + "\n", stderr=""
        )

    binary = tmp_path / "remy-cc.exe"
    monkeypatch.setattr(run_module, "find_daemon_binary", lambda: str(binary))
    monkeypatch.setattr(run_module.subprocess, "run", fake_run)

    result = run_module.run_daemon_scan(str(tmp_path), str(tmp_path / "db.sqlite"), _Config())

    command = captured["command"]
    assert command[0] == str(binary)
    assert command[1] == "scan"
    assert command[command.index("--root") + 1] == str(tmp_path)
    assert command[command.index("--db") + 1] == str(tmp_path / "db.sqlite")
    assert "--result-json" in command
    assert "--lock-timeout" in command
    assert result.status == RunStatus.PARTIAL
    assert result.successful_paths == ("a.py",)
    assert result.failed_paths == ("b.py",)
    assert result.postprocess_complete is True
    assert result.errors[0].stage == "file_scan"
    assert result.errors[0].path == "b.py"


def test_run_daemon_scan_missing_binary_fails_with_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(run_module, "find_daemon_binary", lambda: None)

    result = run_module.run_daemon_scan(str(tmp_path), str(tmp_path / "db.sqlite"), _Config())

    assert result.status == RunStatus.FAILED
    assert result.postprocess_complete is False
    assert "remy-cc binary not found" in result.errors[0].message


def test_run_daemon_scan_without_terminal_json_fails(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        del command, kwargs
        return types.SimpleNamespace(returncode=1, stdout="", stderr="scan crashed")

    monkeypatch.setattr(run_module, "find_daemon_binary", lambda: str(tmp_path / "bin"))
    monkeypatch.setattr(run_module.subprocess, "run", fake_run)

    result = run_module.run_daemon_scan(str(tmp_path), str(tmp_path / "db.sqlite"), _Config())

    assert result.status == RunStatus.FAILED
    assert "scan crashed" in result.errors[0].message


def test_open_semantic_connection_rejects_version_mismatch(tmp_path):
    db_path = tmp_path / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO meta VALUES ('version', '11.0.0')")
    db.commit()
    db.close()

    with pytest.raises(RuntimeError, match="11.0.0"):
        run_module.open_semantic_connection(str(db_path))


def test_open_semantic_connection_accepts_current_version(tmp_path):
    from schema import SCHEMA_SQL, VERSION

    db_path = tmp_path / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    db.commit()
    db.close()

    connection = run_module.open_semantic_connection(str(db_path))
    try:
        assert connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    finally:
        connection.close()
