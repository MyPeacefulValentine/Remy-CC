"""Tests for llm_judge.py: payload hashing, JSON schema validation, conservative bias, cache."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import SCHEMA_SQL, VERSION
import llm_judge


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "logic_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    conn.commit()
    yield conn
    conn.close()


class TestPayloadHash:
    def test_deterministic(self):
        h1 = llm_judge.payload_hash("file", "f.py", {"short": "x", "full": None}, [{"child_ref": "a"}])
        h2 = llm_judge.payload_hash("file", "f.py", {"short": "x", "full": None}, [{"child_ref": "a"}])
        assert h1 == h2

    def test_child_order_invariant(self):
        h1 = llm_judge.payload_hash("file", "f.py", None, [{"child_ref": "a"}, {"child_ref": "b"}])
        h2 = llm_judge.payload_hash("file", "f.py", None, [{"child_ref": "b"}, {"child_ref": "a"}])
        assert h1 == h2

    def test_different_parent_differs(self):
        h1 = llm_judge.payload_hash("file", "f.py", None, [])
        h2 = llm_judge.payload_hash("cluster", "f.py", None, [])
        assert h1 != h2


class TestValidate:
    def test_valid_response(self):
        raw = json.dumps({
            "propagate": True,
            "rationale": "signature changed",
            "matched_dimension": "signature",
            "confidence": "high",
        })
        result = llm_judge.validate_response(raw)
        assert result["propagate"] is True
        assert result["matched_dimension"] == "signature"

    def test_invalid_dimension_rejected(self):
        raw = json.dumps({
            "propagate": True,
            "rationale": "x",
            "matched_dimension": "fictional_dim",
            "confidence": "high",
        })
        assert llm_judge.validate_response(raw) is None

    def test_invalid_confidence_rejected(self):
        raw = json.dumps({
            "propagate": True,
            "rationale": "x",
            "matched_dimension": "signature",
            "confidence": "certain",
        })
        assert llm_judge.validate_response(raw) is None

    def test_missing_propagate_rejected(self):
        raw = json.dumps({
            "rationale": "x",
            "matched_dimension": "signature",
            "confidence": "high",
        })
        assert llm_judge.validate_response(raw) is None

    def test_non_string_rejected(self):
        assert llm_judge.validate_response(None) is None
        assert llm_judge.validate_response(42) is None

    def test_malformed_json_rejected(self):
        assert llm_judge.validate_response("{not json") is None


class TestJudgePropagation:
    def test_conservative_bias_on_invalid_response(self, db):
        result = llm_judge.judge_propagation(
            db, "file", "f.py", None, [{"child_ref": "a"}],
            llm_call=lambda _p: "{garbage}",
        )
        assert result["propagate"] is True
        assert result["matched_dimension"] == "ambiguous"
        assert result["confidence"] == "low"

    def test_conservative_bias_on_error(self, db):
        result = llm_judge.judge_propagation(
            db, "file", "f.py", None, [{"child_ref": "a"}],
            llm_call=lambda _p: "Error: API timeout",
        )
        assert result["propagate"] is True

    def test_valid_response_passed_through(self, db):
        ok_response = json.dumps({
            "propagate": False,
            "rationale": "internal refactor only",
            "matched_dimension": "internal_refactor",
            "confidence": "high",
        })
        result = llm_judge.judge_propagation(
            db, "file", "f.py", None, [{"child_ref": "a"}],
            llm_call=lambda _p: ok_response,
        )
        assert result["propagate"] is False
        assert result["matched_dimension"] == "internal_refactor"

    def test_cache_hit_avoids_llm_call(self, db):
        call_count = {"n": 0}

        def counting_call(_prompt):
            call_count["n"] += 1
            return json.dumps({
                "propagate": True,
                "rationale": "first",
                "matched_dimension": "signature",
                "confidence": "high",
            })

        payload = {"parent_kind": "file", "parent_ref": "f.py",
                   "parent_summary_prev": None, "child_changes": [{"child_ref": "a"}]}
        llm_judge.judge_propagation(db, payload["parent_kind"], payload["parent_ref"],
                                    payload["parent_summary_prev"], payload["child_changes"],
                                    llm_call=counting_call)
        llm_judge.judge_propagation(db, payload["parent_kind"], payload["parent_ref"],
                                    payload["parent_summary_prev"], payload["child_changes"],
                                    llm_call=counting_call)
        assert call_count["n"] == 1

    def test_cache_stored(self, db):
        llm_judge.judge_propagation(
            db, "file", "f.py", None, [{"child_ref": "a"}],
            llm_call=lambda _p: json.dumps({
                "propagate": True, "rationale": "x",
                "matched_dimension": "signature", "confidence": "low",
            }),
        )
        count = db.execute("SELECT COUNT(*) FROM judge_cache").fetchone()[0]
        assert count == 1
