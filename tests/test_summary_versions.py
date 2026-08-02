"""Tests for summary_versions schema and helpers."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import (
    SCHEMA_SQL,
    SUMMARY_STATUS_ENUM,
    VERSION,
    _transition_status,
)
import summarizer


@pytest.fixture(autouse=True)
def isolated_remy_user_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(summarizer.remy_config.Path, "home", classmethod(lambda _cls: home))
    with summarizer.remy_config._CACHE_LOCK:
        summarizer.remy_config._FILE_CACHE.clear()
    yield
    with summarizer.remy_config._CACHE_LOCK:
        summarizer.remy_config._FILE_CACHE.clear()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "logic_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    conn.commit()
    yield conn
    conn.close()


class TestStatusEnum:
    def test_enum_contains_expected_values(self):
        assert "ok" in SUMMARY_STATUS_ENUM
        assert "pending" in SUMMARY_STATUS_ENUM
        assert "stale" in SUMMARY_STATUS_ENUM
        assert "oversized_warn" in SUMMARY_STATUS_ENUM
        assert "oversized_hard" in SUMMARY_STATUS_ENUM
        assert "corrupt" in SUMMARY_STATUS_ENUM

    def test_transition_pending_to_ok(self):
        assert _transition_status("pending", "llm_success") == "ok"

    def test_transition_ok_to_stale(self):
        assert _transition_status("ok", "mark_stale") == "stale"

    def test_transition_stale_to_ok(self):
        assert _transition_status("stale", "rewrite_success") == "ok"

    def test_transition_to_corrupt(self):
        assert _transition_status("ok", "parse_failure") == "corrupt"

    def test_unknown_status_returns_corrupt(self):
        assert _transition_status("invalid_value", "any_event") == "corrupt"

    def test_unmapped_event_returns_old_status(self):
        assert _transition_status("ok", "unknown_event") == "ok"


class TestSummaryVersions:
    def test_version_monotonic_per_node(self, db):
        for i, text in enumerate(["a", "b", "c"], start=1):
            v = summarizer.write_summary_version(
                db, "symbol", "f.py::x", {"short": text, "full": None}, "ok"
            )
            assert v == i

    def test_version_independent_across_nodes(self, db):
        v1 = summarizer.write_summary_version(db, "symbol", "f.py::a", {"short": "x", "full": None}, "ok")
        v2 = summarizer.write_summary_version(db, "symbol", "f.py::b", {"short": "y", "full": None}, "ok")
        assert v1 == 1
        assert v2 == 1

    def test_unique_constraint_on_kind_ref_version(self, db):
        db.execute(
            "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'f::x', 1, '{}', 'ok', '2025-01-01T00:00:00')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
                "VALUES ('symbol', 'f::x', 1, '{}', 'ok', '2025-01-01T00:00:00')"
            )

    def test_status_required(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO summary_versions (node_kind, node_ref, version, summary, status, created_at) "
                "VALUES ('symbol', 'f::x', 1, '{}', NULL, '2025-01-01T00:00:00')"
            )

    def test_write_rolls_back_summary_when_projection_refresh_fails(self, db, monkeypatch):
        def fail_refresh(*_args, **_kwargs):
            raise sqlite3.OperationalError("projection failure")

        monkeypatch.setattr(summarizer, "refresh_node", fail_refresh)
        with pytest.raises(sqlite3.OperationalError, match="projection failure"):
            summarizer.write_summary_version(
                db, "cluster", "c1", {"short": "x", "full": None}, "ok"
            )
        assert db.execute(
            "SELECT COUNT(*) FROM summary_versions "
            "WHERE node_kind='cluster' AND node_ref='c1'"
        ).fetchone()[0] == 0

    def test_write_rolls_back_summary_when_parent_counter_fails(self, db, monkeypatch):
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
        summarizer.write_summary_version(
            db, "file", "a.py", {"short": "parent", "full": None}, "ok"
        )

        def fail_counter(*_args, **_kwargs):
            raise sqlite3.OperationalError("counter failure")

        monkeypatch.setattr(
            summarizer, "_bump_parent_counter_if_applicable", fail_counter
        )
        with pytest.raises(sqlite3.OperationalError, match="counter failure"):
            summarizer.write_summary_version(
                db, "symbol", "a.py::foo", {"short": "child", "full": None}, "ok"
            )
        assert db.execute(
            "SELECT COUNT(*) FROM summary_versions "
            "WHERE node_kind='symbol' AND node_ref='a.py::foo'"
        ).fetchone()[0] == 0

    def test_json_facet_parseable(self, db):
        payload = {"short": "hello world", "full": "[定位] ..."}
        summarizer.write_summary_version(db, "cluster", "c1", payload, "ok")
        row = db.execute(
            "SELECT summary FROM summary_versions WHERE node_kind='cluster' AND node_ref='c1'"
        ).fetchone()
        decoded = json.loads(row[0])
        assert decoded["short"] == "hello world"
        assert decoded["full"] == "[定位] ..."


class TestLengthVerdict:
    def test_within_soft(self):
        v = summarizer._length_verdict(100, 100)
        assert v == "ok"

    def test_within_warn(self):
        v = summarizer._length_verdict(115, 100)
        assert v == "ok"

    def test_above_warn_below_retry(self):
        v = summarizer._length_verdict(140, 100)
        assert v == "oversized_warn"

    def test_above_retry(self):
        v = summarizer._length_verdict(200, 100)
        assert v == "over_retry"


class TestCharLimit:
    def test_en_default(self, monkeypatch):
        monkeypatch.setenv("REMY_LANG", "en")
        monkeypatch.delenv("REMY_SUMMARY_CHAR_LIMIT_SYMBOL", raising=False)
        assert summarizer.get_char_limit("symbol") == 100

    def test_zh_factor(self, monkeypatch):
        monkeypatch.setenv("REMY_LANG", "zh-CN")
        monkeypatch.setenv("REMY_SUMMARY_ZH_LENGTH_FACTOR", "0.5")
        assert summarizer.get_char_limit("symbol") == 50

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("REMY_LANG", "en")
        monkeypatch.setenv("REMY_SUMMARY_CHAR_LIMIT_CLUSTER", "300")
        assert summarizer.get_char_limit("cluster") == 300


class TestGenerateWithLimit:
    """End-to-end state machine for summarizer.generate_with_limit (P0-1)."""

    @staticmethod
    def _payload(text):
        return json.dumps({"short": text, "full": None})

    def test_ok_path_returns_first_payload(self, monkeypatch):
        monkeypatch.delenv("REMY_LANG", raising=False)
        calls = []

        def llm(prompt):
            calls.append(prompt)
            return self._payload("a" * 50)

        payload, status = summarizer.generate_with_limit(
            "symbol", llm,
            render_prompt=lambda lim: f"render({lim})",
            retry_strict_prompt=lambda lim: f"retry({lim})",
        )
        assert status == "ok"
        assert payload == {"short": "a" * 50, "full": None}
        assert len(calls) == 1

    def test_oversized_warn_accepted_without_retry(self, monkeypatch):
        monkeypatch.delenv("REMY_LANG", raising=False)
        calls = []

        def llm(prompt):
            calls.append(prompt)
            return self._payload("a" * 130)

        payload, status = summarizer.generate_with_limit(
            "symbol", llm,
            render_prompt=lambda lim: f"render({lim})",
            retry_strict_prompt=lambda lim: f"retry({lim})",
        )
        assert status == "oversized_warn"
        assert len(calls) == 1

    def test_oversized_hard_after_retry_still_over(self, monkeypatch):
        monkeypatch.delenv("REMY_LANG", raising=False)

        def llm(prompt):
            return self._payload("a" * 200)

        payload, status = summarizer.generate_with_limit(
            "symbol", llm,
            render_prompt=lambda lim: f"render({lim})",
            retry_strict_prompt=lambda lim: f"retry({lim})",
        )
        assert status == "oversized_hard"
        assert payload is not None

    def test_retry_succeeds_after_first_over_retry(self, monkeypatch):
        monkeypatch.delenv("REMY_LANG", raising=False)
        attempts = []

        def llm(prompt):
            attempts.append(prompt)
            if len(attempts) == 1:
                return self._payload("a" * 200)
            return self._payload("a" * 50)

        payload, status = summarizer.generate_with_limit(
            "symbol", llm,
            render_prompt=lambda lim: f"render({lim})",
            retry_strict_prompt=lambda lim: f"strict({lim})",
        )
        assert status == "ok"
        assert payload == {"short": "a" * 50, "full": None}
        assert len(attempts) == 2
        assert "strict" in attempts[1]

    def test_pending_on_error_response(self):
        def llm(prompt):
            return "Error: API timeout"

        payload, status = summarizer.generate_with_limit(
            "symbol", llm,
            render_prompt=lambda lim: "x",
            retry_strict_prompt=lambda lim: "y",
        )
        assert status == "pending"
        assert payload is None

    def test_corrupt_on_malformed_json(self):
        def llm(prompt):
            return "{not valid json"

        payload, status = summarizer.generate_with_limit(
            "symbol", llm,
            render_prompt=lambda lim: "x",
            retry_strict_prompt=lambda lim: "y",
        )
        assert status == "corrupt"
        assert payload is None


class TestKindHintConditionals:
    """{% if kind_hint == 'X' %} branching in _resolve_kind_conditionals (P0-2)."""

    def test_cohesive_branch_kept(self):
        text = (
            "head\n"
            "{% if kind_hint == 'cohesive' %}COHESIVE{% endif %}\n"
            "{% if kind_hint == 'utility' %}UTIL{% endif %}\n"
            "tail"
        )
        result = summarizer._resolve_kind_conditionals(text, "cohesive")
        assert "COHESIVE" in result
        assert "UTIL" not in result

    def test_utility_branch_kept(self):
        text = "{% if kind_hint == 'utility' %}U{% endif %}{% if kind_hint == 'cohesive' %}C{% endif %}"
        assert summarizer._resolve_kind_conditionals(text, "utility") == "U"

    def test_abstract_branch_kept(self):
        text = "{% if kind_hint == 'abstract' %}ABS{% endif %}{% if kind_hint == 'schema' %}SCH{% endif %}"
        assert summarizer._resolve_kind_conditionals(text, "abstract") == "ABS"

    def test_schema_branch_kept(self):
        text = "{% if kind_hint == 'schema' %}SCH{% endif %}{% if kind_hint == 'entry' %}ENT{% endif %}"
        assert summarizer._resolve_kind_conditionals(text, "schema") == "SCH"

    def test_entry_branch_kept(self):
        text = "{% if kind_hint == 'entry' %}ENT{% endif %}{% if kind_hint == 'cohesive' %}C{% endif %}"
        assert summarizer._resolve_kind_conditionals(text, "entry") == "ENT"

    def test_unknown_kind_strips_all_branches(self):
        text = "{% if kind_hint == 'cohesive' %}C{% endif %}{% if kind_hint == 'utility' %}U{% endif %}"
        assert summarizer._resolve_kind_conditionals(text, "unknown_kind") == ""

    def test_multiline_block_preserved(self):
        text = "{% if kind_hint == 'cohesive' %}line1\nline2\nline3{% endif %}"
        result = summarizer._resolve_kind_conditionals(text, "cohesive")
        assert result == "line1\nline2\nline3"


class TestRenderTemplate:
    """summarizer._render_template substitutes occupiers; fallback works (P0-2)."""

    def test_fallback_includes_char_limit_when_template_missing(self):
        result = summarizer._render_template("nonexistent_template.md", {"x": 1}, 250)
        assert "max 250 chars" in result

    def test_real_summarize_file_branches_on_kind_hint(self):
        cohesive = summarizer._render_template("summarize_file.md", {"kind_hint": "cohesive"}, 250)
        utility = summarizer._render_template("summarize_file.md", {"kind_hint": "utility"}, 800)
        assert cohesive != utility

    def test_char_limit_replaced_in_real_template(self):
        result = summarizer._render_template("summarize_file.md", {"kind_hint": "cohesive"}, 250)
        assert "{{char_limit}}" not in result
        assert "{{kind_hint}}" not in result


class TestClusterInput:
    """summarizer._cluster_input aggregates file summaries, entry symbols, inbound edges (P0-3)."""

    @pytest.fixture
    def populated(self, db):
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('b.py', 'h2')")
        db.execute(
            "INSERT INTO clusters (name, label, entry_symbols, file_count) "
            "VALUES ('A', NULL, '[\"a.py::foo\"]', 1)"
        )
        cid_a = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'a.py')", (cid_a,))
        db.execute(
            "INSERT INTO clusters (name, label, entry_symbols, file_count) "
            "VALUES ('B', NULL, '[]', 1)"
        )
        cid_b = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'b.py')", (cid_b,))
        db.execute(
            "INSERT INTO edges (source_file, caller, callee, callee_file, callee_qualified) "
            "VALUES ('b.py', 'main', 'foo', 'a.py', 'a.py::foo')"
        )
        db.commit()
        return db

    def test_file_summaries_aggregated(self, populated):
        summarizer.write_summary_version(populated, "file", "a.py",
                                          {"short": "Hosts foo", "full": None}, "ok")
        out = summarizer._cluster_input(populated, "A")
        assert any(s["file"] == "a.py" and s["short"] == "Hosts foo"
                   for s in out["file_summaries"])

    def test_file_without_summary_excluded(self, populated):
        out = summarizer._cluster_input(populated, "A")
        assert out["file_summaries"] == []

    def test_entry_symbols_extracted(self, populated):
        out = summarizer._cluster_input(populated, "A")
        assert out["entry_symbols"] == ["a.py::foo"]

    def test_inbound_clusters_resolved(self, populated):
        out = summarizer._cluster_input(populated, "A")
        assert "B" in out["inbound_clusters"]
        assert "A" not in out["inbound_clusters"]

    def test_no_inbound_when_isolated(self, populated):
        out = summarizer._cluster_input(populated, "B")
        assert out["inbound_clusters"] == []

    def test_unknown_cluster_returns_empty_fields(self, populated):
        out = summarizer._cluster_input(populated, "NOT_EXISTS")
        assert out["file_summaries"] == []
        assert out["entry_symbols"] == []
        assert out["inbound_clusters"] == []


class TestParentCounterBumpOnWrite:
    """write_summary_version triggers parent child_change_count bump only when parent has ok summary (P4-A)."""

    @pytest.fixture
    def populated(self, db):
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('a.py', 'h1')")
        db.execute(
            "INSERT INTO symbols (file_path, name, type, name_tokens) "
            "VALUES ('a.py', 'foo', 'function', 'foo')"
        )
        db.execute(
            "INSERT INTO clusters (name, label, entry_symbols, file_count) "
            "VALUES ('C', NULL, '[]', 1)"
        )
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO cluster_members (cluster_id, file_path) VALUES (?, 'a.py')", (cid,))
        db.commit()
        return db

    def _counter(self, db, kind, ref):
        row = db.execute(
            "SELECT child_change_count FROM node_change_counters "
            "WHERE node_kind = ? AND node_ref = ?",
            (kind, ref),
        ).fetchone()
        return row[0] if row else None

    def test_symbol_write_bumps_file_counter_when_file_has_ok(self, populated):
        summarizer.write_summary_version(
            populated, "file", "a.py", {"short": "file summary", "full": None}, "ok"
        )
        summarizer.write_summary_version(
            populated, "symbol", "a.py::foo", {"short": "sym summary", "full": None}, "ok"
        )
        assert self._counter(populated, "file", "a.py") == 1

    def test_symbol_write_skips_bump_when_file_lacks_ok(self, populated):
        summarizer.write_summary_version(
            populated, "symbol", "a.py::foo", {"short": "sym summary", "full": None}, "ok"
        )
        assert self._counter(populated, "file", "a.py") is None

    def test_file_write_bumps_cluster_counter_when_cluster_has_ok(self, populated):
        summarizer.write_summary_version(
            populated, "cluster", "C", {"short": "cluster summary", "full": None}, "ok"
        )
        summarizer.write_summary_version(
            populated, "file", "a.py", {"short": "file summary", "full": None}, "ok"
        )
        assert self._counter(populated, "cluster", "C") == 1

    def test_pending_status_does_not_bump(self, populated):
        summarizer.write_summary_version(
            populated, "file", "a.py", {"short": "file summary", "full": None}, "ok"
        )
        summarizer.write_summary_version(
            populated, "symbol", "a.py::foo", None, "pending"
        )
        assert self._counter(populated, "file", "a.py") is None

    def test_cluster_write_has_no_parent_to_bump(self, populated):
        summarizer.write_summary_version(
            populated, "cluster", "C", {"short": "cluster summary", "full": None}, "ok"
        )
        row = populated.execute(
            "SELECT COUNT(*) FROM node_change_counters WHERE node_kind != 'cluster'"
        ).fetchone()
        assert row[0] == 0

    def test_multiple_writes_increment_counter(self, populated):
        summarizer.write_summary_version(
            populated, "file", "a.py", {"short": "file summary", "full": None}, "ok"
        )
        for i in range(3):
            summarizer.write_summary_version(
                populated, "symbol", "a.py::foo", {"short": f"v{i}", "full": None}, "ok"
            )
        assert self._counter(populated, "file", "a.py") == 3

    def test_orphan_file_without_cluster_does_not_error(self, db):
        db.execute("INSERT INTO files (path, struct_hash) VALUES ('orphan.py', 'h')")
        db.commit()
        summarizer.write_summary_version(
            db, "file", "orphan.py", {"short": "x", "full": None}, "ok"
        )
        rows = db.execute("SELECT COUNT(*) FROM node_change_counters").fetchone()[0]
        assert rows == 0


class TestClusterTagsI18n:
    """Verify summarize_cluster.md tag placeholders resolve per REMY_LANG."""

    _PAYLOAD = {"name": "auth/gateway", "files": [], "entry_symbols": [], "inbound_clusters": []}

    def test_zh_cn_renders_chinese_tags(self, monkeypatch):
        monkeypatch.setenv("REMY_LANG", "zh-CN")
        text = summarizer._render_template("summarize_cluster.md", self._PAYLOAD, 200)
        assert "[定位]" in text
        assert "[API]" in text
        assert "[依赖]" in text
        assert "无外部调用方" in text
        assert "{{tag_position}}" not in text
        assert "{{empty_inbound_phrase}}" not in text

    def test_en_renders_english_tags(self, monkeypatch):
        monkeypatch.setenv("REMY_LANG", "en")
        text = summarizer._render_template("summarize_cluster.md", self._PAYLOAD, 200)
        assert "[Role]" in text
        assert "[API]" in text
        assert "[Inbound]" in text
        assert "No external callers." in text
        assert "[定位]" not in text
        assert "[依赖]" not in text

    def test_unknown_lang_is_rejected(self, monkeypatch):
        monkeypatch.setenv("REMY_LANG", "fr")
        with pytest.raises(ValueError, match="REMY_LANG"):
            summarizer._render_template("summarize_cluster.md", self._PAYLOAD, 200)


class TestShortFieldGuard:
    """generate_with_limit enforces short field non-empty after each _try_parse_payload."""

    @staticmethod
    def _violation_payload(value):
        if value == "__MISSING__":
            return json.dumps({"full": "x"})
        return json.dumps({"short": value, "full": None})

    @staticmethod
    def _valid_payload(text):
        return json.dumps({"short": text, "full": None})

    @pytest.mark.parametrize("bad_value", ["__MISSING__", None, "", "   "])
    def test_short_violation_retried_then_corrupt(self, monkeypatch, bad_value):
        monkeypatch.delenv("REMY_LANG", raising=False)
        attempts = []

        def llm(prompt):
            attempts.append(prompt)
            return self._violation_payload(bad_value)

        payload, status = summarizer.generate_with_limit(
            "symbol", llm,
            render_prompt=lambda lim: f"render({lim})",
            retry_strict_prompt=lambda lim: f"strict({lim})",
        )
        assert status == "corrupt"
        assert payload is None
        assert len(attempts) == 2
        assert "strict" in attempts[1]

    def test_short_violation_retry_succeeds_returns_ok(self, monkeypatch):
        monkeypatch.delenv("REMY_LANG", raising=False)
        attempts = []

        def llm(prompt):
            attempts.append(prompt)
            if len(attempts) == 1:
                return self._violation_payload("")
            return self._valid_payload("a" * 30)

        payload, status = summarizer.generate_with_limit(
            "symbol", llm,
            render_prompt=lambda lim: f"render({lim})",
            retry_strict_prompt=lambda lim: f"strict({lim})",
        )
        assert status == "ok"
        assert payload == {"short": "a" * 30, "full": None}
        assert len(attempts) == 2
        assert "strict" in attempts[1]

    def test_length_retry_with_schema_violation_returns_corrupt(self, monkeypatch):
        monkeypatch.delenv("REMY_LANG", raising=False)
        attempts = []

        def llm(prompt):
            attempts.append(prompt)
            if len(attempts) == 1:
                return self._valid_payload("a" * 200)
            return self._violation_payload("__MISSING__")

        payload, status = summarizer.generate_with_limit(
            "symbol", llm,
            render_prompt=lambda lim: f"render({lim})",
            retry_strict_prompt=lambda lim: f"strict({lim})",
        )
        assert status == "corrupt"
        assert payload is None
        assert len(attempts) == 2
