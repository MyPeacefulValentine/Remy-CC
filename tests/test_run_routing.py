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


class _SemanticConfig:
    def __init__(self, overrides=None):
        self.values = {
            "REMY_LLM_API_KEY": "test-key",
            "REMY_LLM_MODEL": "test-model",
            "REMY_LLM_BASE_URL": "https://example.invalid/v1",
            "REMY_LLM_MAX_TOKENS": 1024,
            "REMY_LLM_RETRY_LIMIT": 0,
            "REMY_LLM_TIMEOUT": 30,
            "REMY_LLM_TLS_INSECURE": False,
            "REMY_LLM_MAX_WORKERS": 1,
            "REMY_SYMBOL_SUMMARY_MODE": "auto",
            "REMY_SYMBOL_AUTO_SIZE_GUARD": 300,
            "REMY_LOGIC_INDEX_DB_PATH": "",
        }
        self.values.update(overrides or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def get_int(self, key):
        return int(self.values[key])

    def get_float(self, key):
        return float(self.values[key])

    def get_bool(self, key):
        return bool(self.values[key])


class _FakeLlm:
    def __init__(self, response=None):
        self.api_key = "test-key"
        self.circuit_open = False
        self.api_calls = 0
        self.lang = "English"
        self._response = response

    def call(self, prompt):
        del prompt
        self.api_calls += 1
        return self._response


class _NullLock:
    def acquire(self):
        return None

    def release(self):
        return None


def _seed_semantic_db(db_path, symbols):
    from schema import SCHEMA_SQL, VERSION

    db = sqlite3.connect(str(db_path))
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    for file_path, name in symbols:
        db.execute("INSERT OR IGNORE INTO files (path, struct_hash) VALUES (?, '')", (file_path,))
        db.execute("INSERT INTO symbols (file_path, name, type) VALUES (?, ?, 'function')", (file_path, name))
    db.commit()
    db.close()


def _make_indexer(tmp_path, monkeypatch, overrides=None):
    db_path = tmp_path / "logic_index.db"
    _seed_semantic_db(db_path, [("mod.py", "f")])
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    config = _SemanticConfig({"REMY_LOGIC_INDEX_DB_PATH": str(db_path), **(overrides or {})})
    monkeypatch.setattr(run_module.remy_config, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(
        run_module,
        "run_daemon_scan",
        lambda *args, **kwargs: run_module.ScanResult(status=RunStatus.SUCCESS, postprocess_complete=True),
    )
    monkeypatch.setattr(run_module, "project_scan_lock", lambda root: _NullLock())
    indexer = run_module.LogicIndexer(str(tmp_path))
    indexer.llm_client = _FakeLlm(json.dumps([{"name": "f", "summary": "test summary"}]))
    calls = {"bootstrap": 0, "propagation": 0}
    monkeypatch.setattr(
        indexer,
        "_run_hierarchical_bootstrap",
        lambda mode_override=None: calls.__setitem__("bootstrap", calls["bootstrap"] + 1),
    )

    def fake_propagation(db, llm_client):
        del db, llm_client
        calls["propagation"] += 1
        return {"errors": 0}

    monkeypatch.setattr(run_module.propagation, "run_propagation_pass", fake_propagation)
    return indexer, calls


def test_resolve_symbol_mode_state_machine():
    config = _SemanticConfig()
    assert run_module.resolve_symbol_mode(config, 300) == "auto"
    assert run_module.resolve_symbol_mode(config, 301) == "ask"
    assert run_module.resolve_symbol_mode(_SemanticConfig({"REMY_SYMBOL_SUMMARY_MODE": "ask"}), 1) == "ask"
    never_config = _SemanticConfig({"REMY_SYMBOL_SUMMARY_MODE": "never"})
    assert run_module.resolve_symbol_mode(never_config, 1) == "never"
    assert run_module.resolve_symbol_mode(_SemanticConfig({"REMY_SYMBOL_SUMMARY_MODE": "bogus"}), 1) == "auto"
    assert run_module.resolve_symbol_mode(never_config, 1, override="auto") == "auto"


def test_symbol_mode_never_skips_llm_and_cascades(tmp_path, monkeypatch, capsys):
    indexer, calls = _make_indexer(tmp_path, monkeypatch, {"REMY_SYMBOL_SUMMARY_MODE": "never"})

    result = indexer.run()

    out = capsys.readouterr().out
    assert result.status == RunStatus.SUCCESS
    assert indexer.llm_client.api_calls == 0
    assert calls == {"bootstrap": 0, "propagation": 0}
    assert "SYMBOL_PENDING_CONFIRMATION" not in out
    assert "mode=never" in out


def test_symbol_mode_ask_emits_marker_and_skips_downstream(tmp_path, monkeypatch, capsys):
    indexer, calls = _make_indexer(tmp_path, monkeypatch, {"REMY_SYMBOL_AUTO_SIZE_GUARD": 0})

    result = indexer.run()

    out = capsys.readouterr().out
    assert result.status == RunStatus.SUCCESS
    assert "SYMBOL_PENDING_CONFIRMATION pending_symbols=1" in out
    assert indexer.llm_client.api_calls == 0
    assert calls == {"bootstrap": 0, "propagation": 0}


def test_symbol_auto_below_guard_persists_and_resumes(tmp_path, monkeypatch):
    indexer, calls = _make_indexer(tmp_path, monkeypatch)
    indexer.persisted_count = 7

    result = indexer.run()

    assert result.status == RunStatus.SUCCESS
    assert result.symbol_completed == 1
    assert indexer.llm_client.api_calls == 1
    assert calls == {"bootstrap": 1, "propagation": 1}
    db = sqlite3.connect(str(tmp_path / "logic_index.db"))
    try:
        rows = db.execute(
            "SELECT status FROM summary_versions WHERE node_kind='symbol' AND node_ref='mod.py::f'"
        ).fetchall()
        assert rows == [("ok",)]
        checker = run_module.LogicIndexer.__new__(run_module.LogicIndexer)
        checker.db = db
        assert checker._select_dirty_symbols() == []
    finally:
        db.close()


def test_per_batch_persistence_survives_fatal_error(tmp_path, monkeypatch):
    db_path = tmp_path / "logic_index.db"
    _seed_semantic_db(db_path, [("a.py", "fa"), ("b.py", "fb")])
    indexer = run_module.LogicIndexer.__new__(run_module.LogicIndexer)
    indexer.db = sqlite3.connect(str(db_path))
    indexer.root_dir = str(tmp_path)
    indexer.max_workers = 1
    indexer.summary_errors = []
    indexer.persisted_count = 0
    indexer.llm_client = _FakeLlm()
    indexer.dirty_nodes = [
        ("a.py", {"name": "fa", "summary": None}, "segment", None),
        ("b.py", {"name": "fb", "summary": None}, "segment", None),
    ]

    def fake_worker(file_path, items, context_summaries, parser):
        del context_summaries, parser
        if file_path == "a.py":
            items[0][0]["summary"] = "summary a"
        else:
            raise run_module.FatalError("fatal")

    monkeypatch.setattr(indexer, "_worker_task", fake_worker)

    indexer.process_llm_queue()

    try:
        rows = indexer.db.execute(
            "SELECT node_ref, status FROM summary_versions WHERE node_kind='symbol' ORDER BY node_ref"
        ).fetchall()
    finally:
        indexer.db.close()
    assert rows == [("a.py::fa", "ok")]
    assert indexer.persisted_count == 1
    assert any(error.stage == "symbol_summary" for error in indexer.summary_errors)


def _bare_indexer(tmp_path, symbols):
    db_path = tmp_path / "logic_index.db"
    _seed_semantic_db(db_path, symbols)
    indexer = run_module.LogicIndexer.__new__(run_module.LogicIndexer)
    indexer.db = sqlite3.connect(str(db_path))
    indexer.root_dir = str(tmp_path)
    indexer.max_workers = 1
    indexer.summary_errors = []
    indexer.persisted_count = 0
    indexer.llm_client = _FakeLlm()
    return indexer


def test_circuit_open_break_drains_completed_batches(tmp_path, monkeypatch):
    indexer = _bare_indexer(tmp_path, [("a.py", "fa"), ("b.py", "fb")])
    indexer.dirty_nodes = [
        ("a.py", {"name": "fa", "summary": None}, "segment", None),
        ("b.py", {"name": "fb", "summary": None}, "segment", None),
    ]

    def fake_worker(file_path, items, context_summaries, parser):
        del context_summaries, parser
        items[0][0]["summary"] = f"summary {file_path}"
        indexer.llm_client.circuit_open = True

    monkeypatch.setattr(indexer, "_worker_task", fake_worker)

    indexer.process_llm_queue()

    try:
        rows = indexer.db.execute(
            "SELECT node_ref FROM summary_versions WHERE node_kind='symbol' AND status='ok' ORDER BY node_ref"
        ).fetchall()
    finally:
        indexer.db.close()
    assert ("a.py::fa",) in rows
    assert indexer.persisted_count == len(rows)


def test_persist_symbol_summaries_partial_failure_keeps_batch(tmp_path, monkeypatch):
    import summarizer

    indexer = _bare_indexer(tmp_path, [("a.py", "good"), ("a.py", "bad")])
    real_write = summarizer.write_summary_version

    def flaky_write(db, node_kind, node_ref, payload, status):
        if node_ref.endswith("::bad"):
            raise sqlite3.OperationalError("write failed")
        return real_write(db, node_kind, node_ref, payload, status)

    monkeypatch.setattr(summarizer, "write_summary_version", flaky_write)

    written = indexer._persist_symbol_summaries(
        [("s1", "a.py", "good"), ("s2", "a.py", "bad")]
    )

    try:
        rows = indexer.db.execute(
            "SELECT node_ref FROM summary_versions WHERE node_kind='symbol' AND status='ok'"
        ).fetchall()
    finally:
        indexer.db.close()
    assert written == 1
    assert rows == [("a.py::good",)]
    assert any(error.path == "a.py::bad" for error in indexer.summary_errors)


def test_persistence_infrastructure_failure_stops_queue(tmp_path, monkeypatch):
    indexer = _bare_indexer(tmp_path, [("a.py", "fa"), ("b.py", "fb")])
    indexer.dirty_nodes = [
        ("a.py", {"name": "fa", "summary": None}, "segment", None),
        ("b.py", {"name": "fb", "summary": None}, "segment", None),
    ]

    def fake_worker(file_path, items, context_summaries, parser):
        del context_summaries, parser
        items[0][0]["summary"] = f"summary {file_path}"

    def broken_persist(updates):
        del updates
        raise RuntimeError("summarizer unavailable")

    monkeypatch.setattr(indexer, "_worker_task", fake_worker)
    monkeypatch.setattr(indexer, "_persist_symbol_summaries", broken_persist)

    indexer.process_llm_queue()

    indexer.db.close()
    assert indexer.persisted_count == 0
    assert any("summarizer unavailable" in error.message for error in indexer.summary_errors)
