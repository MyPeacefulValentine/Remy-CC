import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "hooks" / "tree_system" / "lifecycle_hook.py"
spec = importlib.util.spec_from_file_location("lifecycle_hook_tested", MODULE_PATH)
assert spec and spec.loader
lifecycle = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lifecycle
spec.loader.exec_module(lifecycle)


def _write_config(project, relative_db):
    path = project / ".claude" / "remy-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0.0",
        "values": {"REMY_LOGIC_INDEX_DB_PATH": relative_db},
    }), encoding="utf-8")


def _write_db(path, file_count=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE files (path TEXT PRIMARY KEY)")
    db.executemany("INSERT INTO files VALUES (?)", [(f"f{i}.py",) for i in range(file_count)])
    db.commit()
    db.close()


def test_run_struct_scan_spawns_daemon_scan_with_custom_db_path(tmp_path, monkeypatch):
    project = tmp_path / "project"
    custom = project / "state" / "index.db"
    _write_config(project, "state/index.db")
    _write_db(custom)
    legacy_queue = project / ".claude" / "logic_index_dirty"
    legacy_queue.write_text("main.py\n", encoding="utf-8")
    legacy_pending = project / ".claude" / "logic_index_dirty.pending.123"
    legacy_pending.write_text("a.py\n", encoding="utf-8")
    binary = tmp_path / "remy-daemon.exe"
    observed = {}

    def run(args, **kwargs):
        observed["args"] = args
        observed["cwd"] = kwargs["cwd"]
        return type("Result", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(lifecycle, "find_daemon_binary", lambda: str(binary))
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    assert lifecycle.run_struct_scan(str(project)) == 0
    assert observed["cwd"] == str(project)
    args = observed["args"]
    assert args[0] == str(binary)
    assert args[1] == "scan"
    assert args[args.index("--root") + 1] == str(project)
    assert args[args.index("--db") + 1] == str(custom)
    assert "--result-json" in args
    assert "--lock-timeout" in args
    assert "--consume-dirty" not in args
    assert not legacy_queue.exists()
    assert not legacy_pending.exists()


def test_run_struct_scan_skips_when_daemon_binary_is_missing(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    custom = project / "state" / "index.db"
    _write_config(project, "state/index.db")
    _write_db(custom)
    monkeypatch.setattr(lifecycle, "find_daemon_binary", lambda: None)
    assert lifecycle.run_struct_scan(str(project)) is None
    assert "remy-daemon binary not found" in capsys.readouterr().err
