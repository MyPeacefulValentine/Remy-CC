"""Tests for propagation.py: force recompute, counters, candidate collection,
payload build, parent rewrite, and the end-to-end propagation pass."""

import json
import os
import sqlite3
import sys
from typing import Optional

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
import propagation


class _StubLlm:
    def __init__(self, response="", api_key: Optional[str] = "fake-key"):
        self.response = response
        self.api_key: Optional[str] = api_key
        self.circuit_open = False
        self.api_calls = 0
        self.lang = "English"

    def call(self, prompt):
        self.api_calls += 1
        return self.response


@pytest.fixture
def db(tmp_path):
    from struct_scan import SCHEMA_SQL, VERSION
    db_path = tmp_path / "logic_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    conn.commit()
    yield conn
    conn.close()


def _seed_counter(db, kind, ref, child=0, leaf=0, last_force=None):
    db.execute(
        "INSERT INTO node_change_counters "
        "(node_kind, node_ref, child_change_count, leaf_descendant_count, last_force_recompute_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, ref, child, leaf, last_force),
    )
    db.commit()


def _seed_ok_summary(db, kind, ref, short="ok", version=1):
    payload = json.dumps({"short": short, "full": None})
    db.execute(
        "INSERT INTO summary_versions "
        "(node_kind, node_ref, version, summary, status, created_at) "
        "VALUES (?, ?, ?, ?, 'ok', '2025-01-01T00:00:00')",
        (kind, ref, version, payload),
    )
    db.commit()


def _seed_summary(db, kind, ref, short, version, status):
    payload = json.dumps({"short": short, "full": None}) if short is not None else None
    db.execute(
        "INSERT INTO summary_versions "
        "(node_kind, node_ref, version, summary, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2025-01-01T00:00:00')",
        (kind, ref, version, payload, status),
    )
    db.commit()


def _seed_file_with_symbol(db):
    db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
    db.execute(
        "INSERT INTO symbols (file_path, name, type, name_tokens) "
        "VALUES ('a.py', 'foo', 'function', 'foo')"
    )
    db.commit()


class TestForceRecomputeCheck:
    def test_threshold_primary_fires(self, db, monkeypatch):
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "3")
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP", "-1")
        _seed_counter(db, "file", "a.py", child=3)
        assert propagation.force_recompute_check(db, "file", "a.py") is True

    def test_below_threshold_does_not_fire(self, db, monkeypatch):
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "3")
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP", "-1")
        _seed_counter(db, "file", "a.py", child=2)
        assert propagation.force_recompute_check(db, "file", "a.py") is False

    def test_backup_disabled_with_negative_one(self, db, monkeypatch):
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "1000")
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP", "-1")
        _seed_counter(db, "file", "a.py", child=0, leaf=99999)
        assert propagation.force_recompute_check(db, "file", "a.py") is False

    def test_backup_threshold_fires(self, db, monkeypatch):
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "1000")
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP", "5")
        _seed_counter(db, "file", "a.py", child=0, leaf=5)
        assert propagation.force_recompute_check(db, "file", "a.py") is True

    def test_interval_days_elapsed_fires(self, db, monkeypatch):
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "1000")
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP", "-1")
        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_INTERVAL_DAYS", "30")
        _seed_counter(db, "file", "a.py", child=1, last_force="2020-01-01T00:00:00")
        assert propagation.force_recompute_check(db, "file", "a.py") is True

    def test_no_counter_row_returns_false(self, db):
        assert propagation.force_recompute_check(db, "file", "missing.py") is False


class TestZeroCounter:
    def test_resets_both_fields(self, db):
        _seed_counter(db, "file", "a.py", child=10, leaf=20)
        propagation.zero_counter(db, "file", "a.py")
        row = db.execute(
            "SELECT child_change_count, leaf_descendant_count, last_force_recompute_at "
            "FROM node_change_counters WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert row == (0, 0, None)

    def test_mark_force_stamps_timestamp(self, db):
        _seed_counter(db, "file", "a.py", child=10, leaf=20)
        propagation.zero_counter(db, "file", "a.py", mark_force=True)
        row = db.execute(
            "SELECT child_change_count, leaf_descendant_count, last_force_recompute_at "
            "FROM node_change_counters WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert row[0] == 0
        assert row[1] == 0
        assert row[2] is not None


class TestCollectCandidates:
    def test_includes_parent_with_ok_and_counter(self, db):
        _seed_ok_summary(db, "file", "a.py")
        _seed_counter(db, "file", "a.py", child=2)
        candidates = propagation.collect_propagation_candidates(db, "file")
        assert ("a.py", 2) in candidates

    def test_excludes_parent_without_ok_summary(self, db):
        _seed_counter(db, "file", "a.py", child=2)
        candidates = propagation.collect_propagation_candidates(db, "file")
        assert candidates == []

    def test_excludes_parent_with_zero_counter(self, db):
        _seed_ok_summary(db, "file", "a.py")
        _seed_counter(db, "file", "a.py", child=0)
        candidates = propagation.collect_propagation_candidates(db, "file")
        assert candidates == []


class TestBuildChildChanges:
    def test_file_parent(self, db):
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        db.commit()
        _seed_ok_summary(db, "symbol", "a.py::foo", short="v1", version=1)
        _seed_ok_summary(db, "symbol", "a.py::foo", short="v2", version=2)
        changes = propagation.build_child_changes_payload(db, "file", "a.py")
        assert len(changes) == 1
        c = changes[0]
        assert c["child_ref"] == "a.py::foo"
        assert c["new_summary"]["short"] == "v2"
        assert c["old_summary"]["short"] == "v1"

    def test_skips_when_new_equals_old(self, db):
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        db.commit()
        _seed_ok_summary(db, "symbol", "a.py::foo", short="same", version=1)
        _seed_ok_summary(db, "symbol", "a.py::foo", short="same", version=2)
        changes = propagation.build_child_changes_payload(db, "file", "a.py")
        assert changes == []

    def test_cluster_parent(self, db):
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.execute(
            "INSERT INTO clusters (name, label, entry_symbols, file_count) "
            "VALUES ('C', NULL, '[]', 1)"
        )
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'a.py')", (cid,)
        )
        db.commit()
        _seed_ok_summary(db, "file", "a.py", short="fv1", version=1)
        _seed_ok_summary(db, "file", "a.py", short="fv2", version=2)
        changes = propagation.build_child_changes_payload(db, "cluster", "C")
        assert len(changes) == 1
        assert changes[0]["new_summary"]["short"] == "fv2"

    def test_stale_predecessor_is_used_as_baseline(self, db):
        _seed_file_with_symbol(db)
        _seed_summary(db, "symbol", "a.py::foo", "before", 1, "stale")
        _seed_summary(db, "symbol", "a.py::foo", "after", 2, "ok")
        changes = propagation.build_child_changes_payload(db, "file", "a.py")
        assert len(changes) == 1
        assert changes[0]["old_summary"]["short"] == "before"
        assert changes[0]["new_summary"]["short"] == "after"

    def test_stale_predecessor_with_identical_text_returns_empty(self, db):
        _seed_file_with_symbol(db)
        _seed_summary(db, "symbol", "a.py::foo", "same", 1, "stale")
        _seed_summary(db, "symbol", "a.py::foo", "same", 2, "ok")
        assert propagation.build_child_changes_payload(db, "file", "a.py") == []

    def test_pending_predecessor_yields_null_old_summary(self, db):
        _seed_file_with_symbol(db)
        _seed_summary(db, "symbol", "a.py::foo", "first", 1, "ok")
        _seed_summary(db, "symbol", "a.py::foo", None, 2, "pending")
        _seed_summary(db, "symbol", "a.py::foo", "first", 3, "ok")
        changes = propagation.build_child_changes_payload(db, "file", "a.py")
        assert len(changes) == 1
        assert changes[0]["old_summary"] is None
        assert changes[0]["new_summary"]["short"] == "first"


class TestRewriteParentSummary:
    def test_invokes_summarizer(self, db, monkeypatch):
        import summarizer
        calls = {"count": 0}

        def fake_summarize(db_, file_path, hint, llm_call):
            calls["count"] += 1
            return {"short": "rewritten", "full": None}, "ok"

        monkeypatch.setattr(summarizer, "summarize_file", fake_summarize)
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.commit()
        stub = _StubLlm()
        propagation.rewrite_parent_summary(db, "file", "a.py", stub.call)
        assert calls["count"] == 1
        row = db.execute(
            "SELECT summary FROM summary_versions "
            "WHERE node_kind='file' AND node_ref='a.py' AND status='ok'"
        ).fetchone()
        assert row is not None
        assert json.loads(row[0])["short"] == "rewritten"

    def test_skips_pending_status(self, db, monkeypatch):
        import summarizer

        def fake_summarize(db_, file_path, hint, llm_call):
            return None, "pending"

        monkeypatch.setattr(summarizer, "summarize_file", fake_summarize)
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.commit()
        stub = _StubLlm()
        propagation.rewrite_parent_summary(db, "file", "a.py", stub.call)
        row = db.execute(
            "SELECT COUNT(*) FROM summary_versions "
            "WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert row[0] == 0


class TestRunPropagationPass:
    def test_propagate_true_rewrites_and_zeros(self, db, monkeypatch):
        import llm_judge
        import summarizer

        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        db.commit()
        _seed_ok_summary(db, "file", "a.py", short="file_v1")
        _seed_ok_summary(db, "symbol", "a.py::foo", short="s_v1", version=1)
        _seed_ok_summary(db, "symbol", "a.py::foo", short="s_v2", version=2)
        _seed_counter(db, "file", "a.py", child=2)

        monkeypatch.setattr(llm_judge, "judge_propagation",
                            lambda *args, **kw: {"propagate": True, "rationale": "",
                                                 "matched_dimension": "signature",
                                                 "confidence": "high"})
        monkeypatch.setattr(summarizer, "summarize_file",
                            lambda db_, fp, hint, llm: ({"short": "file_v2", "full": None}, "ok"))

        stats = propagation.run_propagation_pass(db, _StubLlm())
        assert stats["file_propagate"] == 1
        counter = db.execute(
            "SELECT child_change_count FROM node_change_counters "
            "WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert counter[0] == 0

    def test_propagate_false_keeps_counter(self, db, monkeypatch):
        import llm_judge

        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        db.commit()
        _seed_ok_summary(db, "file", "a.py", short="file_v1")
        _seed_ok_summary(db, "symbol", "a.py::foo", short="s_v1", version=1)
        _seed_ok_summary(db, "symbol", "a.py::foo", short="s_v2", version=2)
        _seed_counter(db, "file", "a.py", child=2)

        monkeypatch.setattr(llm_judge, "judge_propagation",
                            lambda *a, **kw: {"propagate": False, "rationale": "",
                                              "matched_dimension": "internal_refactor",
                                              "confidence": "high"})
        stats = propagation.run_propagation_pass(db, _StubLlm())
        assert stats["file_skip"] == 1
        counter = db.execute(
            "SELECT child_change_count FROM node_change_counters "
            "WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert counter[0] == 2

    def test_force_recompute_overrides_verdict(self, db, monkeypatch):
        import llm_judge
        import summarizer

        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.commit()
        _seed_ok_summary(db, "file", "a.py", short="file_v1")
        _seed_counter(db, "file", "a.py", child=100)

        monkeypatch.setenv("REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY", "50")
        judge_called = {"count": 0}

        def fake_judge(*a, **kw):
            judge_called["count"] += 1
            return {"propagate": False}

        monkeypatch.setattr(llm_judge, "judge_propagation", fake_judge)
        monkeypatch.setattr(summarizer, "summarize_file",
                            lambda db_, fp, hint, llm: ({"short": "forced", "full": None}, "ok"))

        stats = propagation.run_propagation_pass(db, _StubLlm())
        assert stats["file_force"] == 1
        assert judge_called["count"] == 0
        row = db.execute(
            "SELECT child_change_count, last_force_recompute_at "
            "FROM node_change_counters WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert row[0] == 0
        assert row[1] is not None

    def test_judge_exception_counts_error_and_skips(self, db, monkeypatch):
        import llm_judge

        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        db.commit()
        _seed_ok_summary(db, "file", "a.py", short="file_v1")
        _seed_ok_summary(db, "symbol", "a.py::foo", short="s_v1", version=1)
        _seed_ok_summary(db, "symbol", "a.py::foo", short="s_v2", version=2)
        _seed_counter(db, "file", "a.py", child=2)

        def raise_judge(*_a, **_k):
            raise RuntimeError("judge backend down")

        monkeypatch.setattr(llm_judge, "judge_propagation", raise_judge)
        stats = propagation.run_propagation_pass(db, _StubLlm())
        assert stats["errors"] == 1
        assert stats["file_skip"] == 1

    def test_rewrite_failure_after_propagate_counts_error(self, db, monkeypatch):
        import llm_judge
        import summarizer

        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h')")
        db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        db.commit()
        _seed_ok_summary(db, "file", "a.py", short="file_v1")
        _seed_ok_summary(db, "symbol", "a.py::foo", short="s_v1", version=1)
        _seed_ok_summary(db, "symbol", "a.py::foo", short="s_v2", version=2)
        _seed_counter(db, "file", "a.py", child=2)

        monkeypatch.setattr(llm_judge, "judge_propagation",
                            lambda *a, **kw: {"propagate": True, "rationale": "",
                                              "matched_dimension": "signature",
                                              "confidence": "high"})
        monkeypatch.setattr(summarizer, "summarize_file",
                            lambda db_, fp, hint, llm: (None, "pending"))

        stats = propagation.run_propagation_pass(db, _StubLlm())
        assert stats["errors"] == 1
        assert stats["file_skip"] == 1
        counter = db.execute(
            "SELECT child_change_count FROM node_change_counters "
            "WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert counter[0] == 2

    def test_skipped_when_no_api_key(self, db):
        result = propagation.run_propagation_pass(db, _StubLlm(api_key=None))
        assert result is None

    def test_skipped_when_circuit_open(self, db):
        stub = _StubLlm()
        stub.circuit_open = True
        result = propagation.run_propagation_pass(db, stub)
        assert result is None

    def test_skipped_when_db_is_none(self):
        result = propagation.run_propagation_pass(None, _StubLlm())
        assert result is None

    def test_empty_child_changes_zeroes_counter(self, db, monkeypatch):
        import llm_judge

        _seed_file_with_symbol(db)
        _seed_ok_summary(db, "file", "a.py", short="file_v1")
        _seed_summary(db, "symbol", "a.py::foo", "same", 1, "stale")
        _seed_summary(db, "symbol", "a.py::foo", "same", 2, "ok")
        _seed_counter(db, "file", "a.py", child=3)

        judge_called = {"count": 0}

        def fake_judge(*_a, **_k):
            judge_called["count"] += 1
            return {"propagate": True}

        monkeypatch.setattr(llm_judge, "judge_propagation", fake_judge)
        stats = propagation.run_propagation_pass(db, _StubLlm())
        assert stats["file_skip"] == 1
        assert stats["errors"] == 0
        assert judge_called["count"] == 0
        counter = db.execute(
            "SELECT child_change_count FROM node_change_counters "
            "WHERE node_kind='file' AND node_ref='a.py'"
        ).fetchone()
        assert counter[0] == 0
