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


def test_run_struct_scan_uses_custom_db_path(tmp_path, monkeypatch):
    project = tmp_path / "project"
    custom = project / "state" / "index.db"
    _write_config(project, "state/index.db")
    _write_db(custom)
    script = tmp_path / "struct_scan.py"
    script.write_text("", encoding="utf-8")
    observed = {}

    def run(args, **kwargs):
        observed["args"] = args
        observed["cwd"] = kwargs["cwd"]
        return type("Result", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(lifecycle, "STRUCT_SCAN_SCRIPT", str(script))
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    assert lifecycle.run_struct_scan(str(project)) == 0
    assert observed["cwd"] == str(project)
    assert "--consume-dirty" in observed["args"]


def test_scope_ui_checks_custom_db_path(tmp_path, monkeypatch):
    project = tmp_path / "project"
    custom = project / "state" / "index.db"
    _write_config(project, "state/index.db")
    _write_db(custom)
    script = tmp_path / "scope.py"
    script.write_text("", encoding="utf-8")
    calls = []
    monkeypatch.setattr(lifecycle, "SCOPE_UI_SCRIPT", str(script))
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    lifecycle.maybe_launch_scope_ui(str(project), mcp_minimal=False)
    assert len(calls) == 1
