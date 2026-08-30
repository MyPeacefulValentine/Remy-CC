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
    binary = tmp_path / "remy-cc.exe"
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


def test_run_struct_scan_sweeps_legacy_queue_even_without_index(tmp_path):
    project = tmp_path / "project"
    _write_config(project, "state/index.db")
    legacy_queue = project / ".claude" / "logic_index_dirty"
    legacy_queue.write_text("main.py\n", encoding="utf-8")
    legacy_lock = project / ".claude" / "logic_index_dirty.lock"
    legacy_lock.write_text("", encoding="utf-8")
    assert lifecycle.run_struct_scan(str(project)) is None
    assert not legacy_queue.exists()
    assert not legacy_lock.exists()


def test_run_struct_scan_skips_when_daemon_binary_is_missing(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    custom = project / "state" / "index.db"
    _write_config(project, "state/index.db")
    _write_db(custom)
    monkeypatch.setattr(lifecycle, "find_daemon_binary", lambda: None)
    assert lifecycle.run_struct_scan(str(project)) is None
    assert "remy-cc binary not found" in capsys.readouterr().err


def _load_config(project, values=None):
    if values:
        path = project / ".claude" / "remy-config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": "1.0.0",
            "values": values,
        }), encoding="utf-8")
    else:
        project.mkdir(parents=True, exist_ok=True)
    return lifecycle.remy_config.load_config(str(project), strict=False)


def test_start_daemon_invokes_binary_start(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    config = _load_config(project)
    binary = tmp_path / "remy-cc.exe"
    observed = {}

    def run(args, **kwargs):
        observed["args"] = args
        observed["timeout"] = kwargs["timeout"]
        return type("Result", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(lifecycle, "find_daemon_binary", lambda: str(binary))
    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    assert lifecycle.start_daemon(config) == 0
    assert observed["args"] == [str(binary), "start"]
    assert observed["timeout"] == lifecycle._DAEMON_START_TIMEOUT
    assert capsys.readouterr().err == ""


def test_start_daemon_tolerates_already_running(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    config = _load_config(project)
    monkeypatch.setattr(lifecycle, "find_daemon_binary", lambda: "remy-cc")
    monkeypatch.setattr(
        lifecycle.subprocess, "run",
        lambda *a, **k: type("Result", (), {"returncode": 1, "stderr": b"remy-cc: already running"})(),
    )
    assert lifecycle.start_daemon(config) == 1
    assert capsys.readouterr().err == ""


def test_start_daemon_disabled_skips_subprocess(tmp_path, monkeypatch):
    project = tmp_path / "project"
    config = _load_config(project, {"REMY_DAEMON_AUTOSTART": "false"})
    calls = []
    monkeypatch.setattr(lifecycle, "find_daemon_binary", lambda: "remy-cc")
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *a, **k: calls.append(a))
    assert lifecycle.start_daemon(config) is None
    assert calls == []


def test_start_daemon_skips_when_binary_missing(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    config = _load_config(project)
    monkeypatch.setattr(lifecycle, "find_daemon_binary", lambda: None)
    assert lifecycle.start_daemon(config) is None
    assert "binary not found" in capsys.readouterr().err


def test_start_daemon_reports_failure_exit_code(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    config = _load_config(project)
    monkeypatch.setattr(lifecycle, "find_daemon_binary", lambda: "remy-cc")
    monkeypatch.setattr(
        lifecycle.subprocess, "run",
        lambda *a, **k: type("Result", (), {"returncode": 2, "stderr": b"start timed out"})(),
    )
    assert lifecycle.start_daemon(config) == 2
    err = capsys.readouterr().err
    assert "[DaemonStart] Failed" in err
    assert "start timed out" in err


def test_start_daemon_survives_timeout(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    config = _load_config(project)
    monkeypatch.setattr(lifecycle, "find_daemon_binary", lambda: "remy-cc")

    def run(*args, **kwargs):
        raise lifecycle.subprocess.TimeoutExpired(cmd="remy-cc start", timeout=15.0)

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    assert lifecycle.start_daemon(config) is None
    assert "[DaemonStart] Unexpected error" in capsys.readouterr().err
