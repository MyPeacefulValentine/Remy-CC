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


def _first_json_object(text):
    """Extract the first balanced JSON object from rendered prompt text."""
    start = text.index("{")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:index + 1])
    raise AssertionError("no balanced JSON object in rendered prompt")


class TestPromptExampleFieldContract:
    """judge_propagation.md example payloads match the keys build_prompt emits."""

    PROMPT_PATH = os.path.join(
        os.path.dirname(__file__), "..", "skills", "remy-index", "prompts",
        "judge_propagation.md",
    )

    def _rendered_payload(self):
        prompt = llm_judge.build_prompt(
            "file", "f.py", {"short": "parent text", "full": None},
            [{"child_ref": "f.py::g", "old_summary": None,
              "new_summary": {"short": "child text", "full": None}}],
        )
        return _first_json_object(prompt)

    def _example_inputs(self):
        with open(self.PROMPT_PATH, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        payloads = []
        for index, line in enumerate(lines):
            if not line.startswith("Input"):
                continue
            for candidate in lines[index + 1:]:
                if not candidate.strip():
                    continue
                if candidate.startswith("{") and not candidate.startswith("{{"):
                    payloads.append(json.loads(candidate))
                break
        return payloads

    def test_examples_are_present(self):
        assert len(self._example_inputs()) == 4

    def test_example_top_level_keys_match_build_prompt(self):
        actual = set(self._rendered_payload())
        payloads = self._example_inputs()
        assert payloads
        for payload in payloads:
            assert set(payload) == actual

    def test_example_child_entry_keys_match_build_prompt(self):
        rendered = self._rendered_payload()
        assert rendered["children"], "rendered payload must carry a child entry"
        actual = set(rendered["children"][0])
        checked = 0
        for payload in self._example_inputs():
            for entry in payload["children"]:
                assert set(entry) == actual
                checked += 1
        assert checked >= 4

    def test_examples_cover_both_parent_kinds(self):
        kinds = {payload["parent_kind"] for payload in self._example_inputs()}
        assert kinds == {"file", "cluster"}

    def test_example_dimensions_are_within_the_closed_set(self):
        with open(self.PROMPT_PATH, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        outputs = []
        for index, line in enumerate(lines):
            if line == "Output:" and index + 1 < len(lines):
                candidate = lines[index + 1]
                if candidate.startswith("{"):
                    outputs.append(json.loads(candidate))
        assert len(outputs) == 4
        for payload in outputs:
            assert payload["matched_dimension"] in llm_judge.JUDGE_DIMENSIONS
            assert payload["confidence"] in llm_judge.JUDGE_CONFIDENCE
            assert isinstance(payload["propagate"], bool)
