"""Evidence schemas and report rendering, without model calls."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from jsonschema import Draft7Validator, ValidationError
import pytest


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "remy-insight"
DIMENSIONS = ("architecture", "improvement", "robustness", "innovation", "custom")
STATES = {"verified": "✓", "inference": "~", "unknown": "?", "refuted": "×"}


@pytest.fixture(scope="module")
def renderer():
    spec = importlib.util.spec_from_file_location("insight_render_tested", SKILL / "render.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.JINJA2_AVAILABLE, "Jinja2 is required to verify both renderer paths"
    return module


def finding(state="inference", dimension="architecture"):
    return {
        "id": "F-001",
        "dimension": dimension,
        "severity": "concern",
        "target": {"file": "example.py", "symbol": "example", "layer": "Core"},
        "claim": "Synthetic finding for renderer tests",
        "evidence": "Synthetic example.py:1, with an unchecked runtime premise",
        "evidence_state": state,
    }


def report_context(item):
    return {
        "mode": "global", "depth": "light", "timestamp": "2026-10-15T00:00:00Z",
        "scope_description": "synthetic fixture", "active_section_names": [item["dimension"]],
        "agent_count": {"analysis": 1, "adversarial": 0, "total": 1},
        "adversarial_enabled": False,
        "sections": [{"name": item["dimension"], "findings": [item], "summary": "Fixture"}],
    }


@pytest.fixture(params=["agent", "report"])
def contract(request):
    if request.param == "agent":
        schema = json.loads((SKILL / "schemas" / "agent_finding.json").read_text(encoding="utf-8"))
        wrap = lambda item: {"findings": [item], "summary": "Fixture"}
    else:
        schema = json.loads((SKILL / "output_schema.json").read_text(encoding="utf-8"))
        wrap = report_context
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema), wrap


@pytest.mark.parametrize("state", STATES)
def test_new_states_validate(contract, state):
    validator, wrap = contract
    item = finding(state)
    validator.validate(wrap(item))
    item["evidence_basis"] = ["源码", "实测"]
    validator.validate(wrap(item))


@pytest.mark.parametrize("state", ["certain", "", None, 5, True, {}, []])
def test_invalid_new_states_rejected(contract, state):
    validator, wrap = contract
    with pytest.raises(ValidationError):
        validator.validate(wrap(finding(state)))


@pytest.mark.parametrize("with_new_state", [False, True])
def test_new_contract_rejects_old_confidence(contract, with_new_state):
    validator, wrap = contract
    item = finding()
    item["confidence"] = 5
    if not with_new_state:
        del item["evidence_state"]
    with pytest.raises(ValidationError):
        validator.validate(wrap(item))


def test_new_contract_requires_state(contract):
    validator, wrap = contract
    item = finding()
    del item["evidence_state"]
    with pytest.raises(ValidationError):
        validator.validate(wrap(item))


@pytest.mark.parametrize("basis", [None, "runtime", [3], [""], ["[runtime]"],
                                          ["code｜test"], ["a\nb"], ["a\n"], ["a\r"], [" "], ["code", "code"]])
def test_invalid_basis_rejected(contract, basis):
    validator, wrap = contract
    item = finding()
    item["evidence_basis"] = basis
    with pytest.raises(ValidationError):
        validator.validate(wrap(item))


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_prompt_example_matches_agent_schema(dimension):
    schema = json.loads((SKILL / "schemas" / "agent_finding.json").read_text(encoding="utf-8"))
    text = (SKILL / "prompts" / f"angle_{dimension}.md").read_text(encoding="utf-8")
    example = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
    Draft7Validator(schema).validate(example)
    assert '"confidence"' not in text
    assert "acquired material types" in text
    assert "Source inspection does not establish runtime behavior" in text


@pytest.mark.parametrize("state, marker", STATES.items())
def test_label_state_and_basis(renderer, state, marker):
    item = finding(state)
    assert renderer.evidence_label(item) == f"[{marker}]"
    item["evidence_basis"] = ["源码", "实测"]
    assert renderer.evidence_label(item) == f"[{marker}｜源码+实测]"
    item["evidence_basis"] = []
    assert renderer.evidence_label(item) == f"[{marker}]"


@pytest.mark.parametrize("score", [2, 3, 4, 5])
def test_legacy_score_never_upgrades_evidence(renderer, score):
    item = finding()
    del item["evidence_state"]
    item["confidence"] = score
    label = renderer.evidence_label(item)
    assert label.startswith("[?]")
    assert f"legacy confidence={score}" in label
    assert "not rechecked" in label
    assert "[✓]" not in label


def test_explicit_state_wins_over_legacy_and_votes(renderer):
    item = finding()
    item.update(confidence=5, verified_status="upheld", vote_detail="3 upheld")
    assert renderer.evidence_label(item) == "[~]"
    item["verified_status"] = "refuted"
    assert renderer.evidence_label(item) == "[~]"
    item["evidence_state"] = "invalid"
    with pytest.raises(ValueError, match="evidence_state"):
        renderer.evidence_label(item)


@pytest.mark.parametrize("state", [None, {}, [], True, ""])
def test_renderer_diagnoses_invalid_state(renderer, state):
    with pytest.raises(ValueError, match="evidence_state"):
        renderer.evidence_label(finding(state))


@pytest.mark.parametrize("basis", [None, "runtime", [None], [""], [" "],
                                          ["[test]"], ["a|b"], ["a\nb"], ["a", "a"]])
def test_renderer_diagnoses_invalid_basis(renderer, basis):
    item = finding()
    item["evidence_basis"] = basis
    with pytest.raises(ValueError, match="evidence_basis"):
        renderer.evidence_label(item)


def test_missing_state_is_explicit(renderer):
    item = finding()
    del item["evidence_state"]
    assert renderer.evidence_label(item) == "[?] (evidence state missing)"


@pytest.mark.parametrize("score", [0, 6, True, "5", None])
def test_invalid_legacy_score_diagnosed(renderer, score):
    item = finding()
    del item["evidence_state"]
    item["confidence"] = score
    with pytest.raises(ValueError, match="legacy confidence"):
        renderer.evidence_label(item)


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("jinja", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_every_section_uses_shared_formatter(renderer, monkeypatch, dimension, jinja, legacy):
    monkeypatch.setattr(renderer, "JINJA2_AVAILABLE", jinja)
    item = finding(dimension=dimension)
    item.update(verified_status="upheld", vote_detail="3 upheld", mechanism="Fixture mechanism",
                significance="Fixture significance", cross_refs=["§custom F-002"])
    if legacy:
        del item["evidence_state"]
        item["confidence"] = 5
    else:
        item["evidence_basis"] = ["源码", "实测"]
    output = renderer.render_template(f"section_{dimension}.md.j2", {
        "findings": [item], "summary": "Fixture summary", "metadata": {},
    })
    assert renderer.evidence_label(item) in output
    assert "**Evidence state**:" in output
    assert "**Adversarial review**: upheld" in output
    assert "**Confidence**:" not in output
    assert "3 upheld" in output
    assert "§custom F-002" in output
    if dimension == "innovation":
        assert "Fixture mechanism" in output
        assert "Fixture significance" in output
    if legacy:
        assert "[✓]" not in output


@pytest.mark.parametrize("jinja", [False, True])
def test_assemble_preserves_grouping_crossrefs_and_states(renderer, monkeypatch, jinja):
    monkeypatch.setattr(renderer, "JINJA2_AVAILABLE", jinja)
    first = finding()
    second = finding("unknown", "custom")
    second["id"] = "F-002"
    flat = {"dimensions": ["architecture", "custom"], "findings": [first, second]}
    mode, depth, sections, grouped, metadata = renderer._group_flat_findings(flat)
    assert sections == ["architecture", "custom"]
    output = renderer.assemble_report(mode, depth, sections, grouped, metadata)
    assert "[~]" in output and "[?]" in output
    assert "§custom F-002" in output and "§architecture F-001" in output
    assert first["evidence_state"] == "inference"
    assert second["evidence_state"] == "unknown"


@pytest.mark.parametrize("legacy", [False, True])
def test_cli_renders_new_and_legacy_json(tmp_path, legacy):
    item = finding()
    if legacy:
        del item["evidence_state"]
        item["confidence"] = 5
    else:
        item["evidence_basis"] = ["源码"]
    source = tmp_path / "findings.json"
    target = tmp_path / "report.md"
    source.write_text(json.dumps({"dimensions": ["architecture"], "findings": [item]},
                                 ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-B", str(SKILL / "render.py"),
                             "--input", str(source), "--output", str(target)],
                            capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    output = target.read_text(encoding="utf-8")
    assert ("legacy confidence=5" if legacy else "[~｜源码]") in output
    assert "[✓]" not in output
    assert json.loads(source.read_text(encoding="utf-8"))["findings"][0] == item


def test_cli_invalid_state_does_not_replace_report(tmp_path):
    source = tmp_path / "invalid.json"
    target = tmp_path / "report.md"
    source.write_text(json.dumps({"dimensions": ["architecture"],
                                  "findings": [finding("invalid")]}), encoding="utf-8")
    target.write_text("previous report", encoding="utf-8")
    result = subprocess.run([sys.executable, "-B", str(SKILL / "render.py"),
                             "--input", str(source), "--output", str(target)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "Invalid evidence_state" in result.stderr
    assert target.read_text(encoding="utf-8") == "previous report"
