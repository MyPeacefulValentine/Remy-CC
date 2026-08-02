"""Tests for bootstrap.py: NULL-as-pending resume, mode resolution, API key downgrade."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import SCHEMA_SQL, VERSION
import bootstrap
import summarizer


@pytest.fixture(autouse=True)
def isolated_remy_user_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(bootstrap.remy_config.Path, "home", classmethod(lambda _cls: home))
    with bootstrap.remy_config._CACHE_LOCK:
        bootstrap.remy_config._FILE_CACHE.clear()
    yield
    with bootstrap.remy_config._CACHE_LOCK:
        bootstrap.remy_config._FILE_CACHE.clear()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "logic_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    conn.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
    conn.execute("INSERT INTO files (path, struct_hash) VALUES ('b.py', 'h2')")
    conn.execute(
        "INSERT INTO clusters (name, label, entry_symbols, file_count) VALUES ('group', NULL, '[]', 2)"
    )
    cluster_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'a.py')", (cluster_id,))
    conn.execute("INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'b.py')", (cluster_id,))
    conn.commit()
    yield conn
    conn.close()


def _fake_file_llm(prompt):
    return json.dumps({"short": "fake file summary", "full": None})


def _fake_cluster_llm(prompt):
    return json.dumps({"short": "fake cluster summary", "full": "[定位] subsystem"})


def _routing_llm(prompt):
    if "cluster_name" in prompt:
        return _fake_cluster_llm(prompt)
    return _fake_file_llm(prompt)


class TestResolveMode:
    def test_explicit_never(self, db, monkeypatch):
        monkeypatch.setenv("REMY_SUMMARY_BOOTSTRAP_MODE", "never")
        assert bootstrap.resolve_mode(db) == "never"

    def test_explicit_ask(self, db, monkeypatch):
        monkeypatch.setenv("REMY_SUMMARY_BOOTSTRAP_MODE", "ask")
        assert bootstrap.resolve_mode(db) == "ask"

    def test_auto_downgrades_without_api_key(self, db, monkeypatch):
        monkeypatch.setenv("REMY_SUMMARY_BOOTSTRAP_MODE", "auto")
        monkeypatch.delenv("REMY_LLM_API_KEY", raising=False)
        assert bootstrap.resolve_mode(db) == "ask"

    def test_auto_with_api_key_stays_auto(self, db, monkeypatch):
        monkeypatch.setenv("REMY_SUMMARY_BOOTSTRAP_MODE", "auto")
        monkeypatch.setenv("REMY_LLM_API_KEY", "sk-fake")
        monkeypatch.setenv("REMY_BOOTSTRAP_AUTO_SIZE_GUARD", "10")
        assert bootstrap.resolve_mode(db) == "auto"

    def test_auto_downgrades_when_size_exceeds_guard(self, db, monkeypatch):
        monkeypatch.setenv("REMY_SUMMARY_BOOTSTRAP_MODE", "auto")
        monkeypatch.setenv("REMY_LLM_API_KEY", "sk-fake")
        monkeypatch.setenv("REMY_BOOTSTRAP_AUTO_SIZE_GUARD", "10")
        for index in range(9):
            db.execute(
                "INSERT INTO files (path, struct_hash) VALUES (?, ?)",
                (f"extra_{index}.py", f"extra-{index}"),
            )
        db.commit()
        assert bootstrap.resolve_mode(db) == "ask"

    def test_invalid_mode_is_rejected(self, db, monkeypatch):
        monkeypatch.setenv("REMY_SUMMARY_BOOTSTRAP_MODE", "garbage")
        monkeypatch.setenv("REMY_LLM_API_KEY", "sk-fake")
        monkeypatch.setenv("REMY_BOOTSTRAP_AUTO_SIZE_GUARD", "10")
        with pytest.raises(ValueError, match="REMY_SUMMARY_BOOTSTRAP_MODE"):
            bootstrap.resolve_mode(db)


class TestNeedsBootstrap:
    def test_empty_db_needs(self, db):
        assert bootstrap.needs_bootstrap(db) is True

    def test_fully_summarized_does_not_need(self, db):
        for path in ("a.py", "b.py"):
            summarizer.write_summary_version(db, "file", path, {"short": "ok", "full": None}, "ok")
        summarizer.write_summary_version(db, "cluster", "group", {"short": "g", "full": None}, "ok")
        assert bootstrap.needs_bootstrap(db) is False


class TestBootstrapSummaries:
    def test_never_mode_skips(self, db):
        result = bootstrap.bootstrap_summaries(db, lambda _: "", mode="never")
        assert result["skipped"] is True
        assert result["mode"] == "never"

    def test_ask_mode_skips_with_pending_count(self, db):
        result = bootstrap.bootstrap_summaries(db, lambda _: "", mode="ask")
        assert result["skipped"] is True
        assert result["needs_user_confirmation"] is True
        assert result["pending_files"] == 2
        assert result["pending_clusters"] == 1

    def test_auto_mode_populates_all(self, db, monkeypatch):
        monkeypatch.setenv("REMY_LLM_MAX_WORKERS", "1")
        result = bootstrap.bootstrap_summaries(db, _routing_llm, mode="auto")
        assert result["skipped"] is False
        assert result["file_done"] == 2
        assert result["cluster_done"] == 1

    def test_resume_only_pending(self, db, monkeypatch):
        summarizer.write_summary_version(db, "file", "a.py", {"short": "done", "full": None}, "ok")
        monkeypatch.setenv("REMY_LLM_MAX_WORKERS", "1")
        result = bootstrap.bootstrap_summaries(db, _routing_llm, mode="auto")
        assert result["file_done"] == 1

    def test_failed_llm_does_not_write(self, db, monkeypatch):
        monkeypatch.setenv("REMY_LLM_MAX_WORKERS", "1")

        def failing_llm(_prompt):
            return "Error: API down"

        result = bootstrap.bootstrap_summaries(db, failing_llm, mode="auto")
        assert result["file_done"] == 0
        rows = db.execute("SELECT COUNT(*) FROM summary_versions WHERE status='ok'").fetchone()[0]
        assert rows == 0


class TestConcurrentBootstrap:
    """ThreadPoolExecutor + WAL multi-connection path with REMY_LLM_MAX_WORKERS > 1 (P1-7)."""

    def test_concurrency_two_completes_all(self, db, monkeypatch):
        for i in range(4):
            db.execute(
                "INSERT INTO files (path, struct_hash) VALUES (?, ?)",
                (f"f{i}.py", f"h{i}"),
            )
        db.commit()
        db.execute("PRAGMA journal_mode=WAL")

        monkeypatch.setenv("REMY_LLM_MAX_WORKERS", "4")
        result = bootstrap.bootstrap_summaries(db, _routing_llm, mode="auto")
        assert result["skipped"] is False
        assert result["file_done"] == 6
        assert result["cluster_done"] == 1

    def test_concurrent_failure_isolated_from_success(self, db, monkeypatch):
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('extra.py', 'h_extra')")
        db.commit()
        db.execute("PRAGMA journal_mode=WAL")

        monkeypatch.setenv("REMY_LLM_MAX_WORKERS", "3")

        def selective_llm(prompt):
            if "extra.py" in prompt:
                return "Error: API down"
            if "cluster_name" in prompt:
                return _fake_cluster_llm(prompt)
            return _fake_file_llm(prompt)

        result = bootstrap.bootstrap_summaries(db, selective_llm, mode="auto")
        assert result["file_done"] == 2
        ok_files = db.execute(
            "SELECT COUNT(*) FROM summary_versions WHERE status='ok' AND node_kind='file'"
        ).fetchone()[0]
        assert ok_files == 2

    def test_concurrency_invokes_llm_for_each_pending_node(self, db, monkeypatch):
        db.execute("PRAGMA journal_mode=WAL")
        monkeypatch.setenv("REMY_LLM_MAX_WORKERS", "2")

        invocations = []

        def tracking_llm(prompt):
            invocations.append(prompt)
            if "cluster_name" in prompt:
                return _fake_cluster_llm(prompt)
            return _fake_file_llm(prompt)

        bootstrap.bootstrap_summaries(db, tracking_llm, mode="auto")
        assert len(invocations) >= 3

    def test_concurrent_oversized_warn_counts_as_completed(self, db, monkeypatch):
        db.execute("PRAGMA journal_mode=WAL")
        monkeypatch.setenv("REMY_LLM_MAX_WORKERS", "2")
        monkeypatch.setenv("REMY_LANG", "en")
        oversized = json.dumps({"short": "x" * 350, "full": None})

        result = bootstrap.bootstrap_summaries(
            db, lambda _prompt: oversized, mode="auto"
        )

        assert result["file_done"] == 2
        assert result["cluster_done"] == 1
        assert result["file_failed"] == 0
        assert result["cluster_failed"] == 0
        statuses = {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT status FROM summary_versions"
            ).fetchall()
        }
        assert statuses == {"oversized_warn", "ok"}

    def test_concurrent_resume_skips_completed(self, db, monkeypatch):
        summarizer.write_summary_version(
            db, "file", "a.py", {"short": "done", "full": None}, "ok"
        )
        db.execute("PRAGMA journal_mode=WAL")
        monkeypatch.setenv("REMY_LLM_MAX_WORKERS", "2")

        result = bootstrap.bootstrap_summaries(db, _routing_llm, mode="auto")
        assert result["file_done"] == 1
        ok_files = db.execute(
            "SELECT COUNT(*) FROM summary_versions WHERE status='ok' AND node_kind='file'"
        ).fetchone()[0]
        assert ok_files == 2
