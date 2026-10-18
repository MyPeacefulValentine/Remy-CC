"""阶段重放的离线材料与标注检查，不运行模型或判断回答语义。"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests/fixtures/plan_acceptance_coverage"
LANGUAGES = {"zh", "en"}
CASE_IDS = {"A", "B", "C", "D"}
STATES = {"满足", "部分满足", "不满足"}


def load_fixtures():
    return tuple(json.loads((FIXTURES / name).read_text(encoding="utf-8"))
                 for name in ("inputs.json", "expectations.json"))


def sources_for(inputs, case_id):
    case = next(item for item in inputs["cases"] if item["id"] == case_id)
    common = inputs["tasks"][case["task"]]["sources"]
    assert not (set(common) & set(case["sources"]))
    return {**common, **case["sources"]}


def call_matrix(inputs):
    """固定交错顺序；不发起调用，也不据此声称实际发送材料正确。"""
    matrix = []
    for index, case in enumerate(inputs["cases"]):
        for language_index, language in enumerate(inputs["replay"]["languages"]):
            policies = inputs["replay"]["policies"]
            if (index + language_index) % 2:
                policies = list(reversed(policies))
            matrix.extend({"case_id": case["id"], "language": language, "policy": policy}
                          for policy in policies)
    return matrix


def validate_fixtures(inputs, expectations):
    assert inputs["version"] == expectations["version"] == "1.0.0"
    assert len(inputs["cases"]) == len(expectations["cases"]) == 4
    assert {case["id"] for case in inputs["cases"]} == CASE_IDS
    assert {case["id"] for case in expectations["cases"]} == CASE_IDS
    replay = inputs["replay"]
    assert set(replay["languages"]) == LANGUAGES and len(replay["languages"]) == 2
    assert replay["policies"] == ["before", "after"]
    assert replay["answer_limit"] == 16 and replay["review_limit"] == 2
    assert replay["tools_allowed"] is False
    assert set(replay["instructions"]) == LANGUAGES
    assert set(replay["required_materials"]) == {
        "skill", "template", "schema", "shared_rules", "protocol_dependencies", "scenario",
    }
    assert set(expectations["states"]) == STATES
    common_ids = {item["id"] for item in expectations["common_required"]}
    assert len(common_ids) == len(expectations["common_required"]) > 0
    forbidden = expectations["common_forbidden"]
    assert len({item["id"] for item in forbidden}) == len(forbidden) > 0
    assert all(item["text"].strip() for item in forbidden)
    for case in inputs["cases"]:
        assert set(case) == {"id", "task", "sources"}
        sources = sources_for(inputs, case["id"])
        assert {"goal", "scenarios", "approval"} <= set(sources)
        for translations in sources.values():
            assert set(translations) == LANGUAGES
            for value in translations.values():
                assert isinstance(value, str) and value.strip()
                for label in ("common_required", "acceptable_variations", "评分", "评分判据"):
                    assert label not in value
    for case in expectations["cases"]:
        sources = set(sources_for(inputs, case["id"])) | {"replay"}
        required = expectations["common_required"] + case["required"]
        assert len({item["id"] for item in required}) == len(required)
        assert case["required"] and case["acceptable_variations"]
        for item in required:
            assert set(item) == {"id", "source_refs", "text"}
            assert item["source_refs"] and set(item["source_refs"]) <= sources
            assert isinstance(item["text"], str) and item["text"].strip()
    pair = expectations["single_fact_pair"]
    assert pair == {"cases": ["C", "D"], "source": "replacement_guarantee"}
    first, second = (deepcopy(sources_for(inputs, case_id)) for case_id in pair["cases"])
    changed_first = first.pop(pair["source"])
    changed_second = second.pop(pair["source"])
    assert first == second
    assert all(changed_first[language] != changed_second[language] for language in LANGUAGES)
    matrix = call_matrix(inputs)
    assert len(matrix) == replay["answer_limit"]
    assert len({tuple(row.values()) for row in matrix}) == 16


def assessment_passes(expectations, case_id, assessment):
    """仅汇总显式标注；引文真实性与语义支持须另行核对。"""
    case = next(item for item in expectations["cases"] if item["id"] == case_id)
    required = expectations["common_required"] + case["required"]
    assert set(assessment) == {"required", "forbidden"}
    assert set(assessment["required"]) == {item["id"] for item in required}
    assert set(assessment["forbidden"]) == {item["id"] for item in expectations["common_forbidden"]}
    for item in assessment["required"].values():
        assert set(item) == {"state", "evidence"}
        assert item["state"] in STATES
        assert isinstance(item["evidence"], str) and item["evidence"].strip()
    for item in assessment["forbidden"].values():
        assert set(item) == {"present", "evidence"}
        assert type(item["present"]) is bool
        assert isinstance(item["evidence"], str) and item["evidence"].strip()
    return (all(item["state"] == "满足" for item in assessment["required"].values())
            and not any(item["present"] for item in assessment["forbidden"].values()))


def synthetic_assessment(expectations, case_id):
    case = next(item for item in expectations["cases"] if item["id"] == case_id)
    return {
        "required": {item["id"]: {"state": "满足", "evidence": "合成标注，不是模型回答。"}
                     for item in expectations["common_required"] + case["required"]},
        "forbidden": {item["id"]: {"present": False, "evidence": "合成标注，不是模型回答。"}
                      for item in expectations["common_forbidden"]},
    }


def test_replay_fixture_integrity():
    validate_fixtures(*load_fixtures())


@pytest.mark.parametrize("mutation", [
    "missing_case", "missing_language", "extra_fact", "bad_source", "leaked_rubric",
    "missing_template", "extra_call", "tools", "duplicate_case",
])
def test_fixture_mutations_are_rejected(mutation):
    inputs, expectations = deepcopy(load_fixtures())
    if mutation == "missing_case":
        inputs["cases"].pop()
    elif mutation == "missing_language":
        del inputs["tasks"]["docs"]["sources"]["goal"]["en"]
    elif mutation == "extra_fact":
        inputs["cases"][3]["sources"]["extra"] = {"zh": "额外事实", "en": "Extra fact"}
    elif mutation == "bad_source":
        expectations["cases"][0]["required"][0]["source_refs"] = ["missing"]
    elif mutation == "leaked_rubric":
        inputs["tasks"]["docs"]["sources"]["goal"]["zh"] += "评分判据"
    elif mutation == "missing_template":
        inputs["replay"]["required_materials"].remove("template")
    elif mutation == "extra_call":
        inputs["replay"]["answer_limit"] += 1
    elif mutation == "tools":
        inputs["replay"]["tools_allowed"] = True
    else:
        inputs["cases"][1]["id"] = "A"
    with pytest.raises(AssertionError):
        validate_fixtures(inputs, expectations)


@pytest.mark.parametrize("case_id", sorted(CASE_IDS))
@pytest.mark.parametrize("mutation", ["partial", "unmet", "missing", "forbidden", "no_evidence"])
def test_incomplete_or_forbidden_assessments_cannot_pass(case_id, mutation):
    _, expectations = load_fixtures()
    assessment = synthetic_assessment(expectations, case_id)
    assert assessment_passes(expectations, case_id, assessment)
    if mutation in ("partial", "unmet"):
        assessment["required"]["coverage"]["state"] = "部分满足" if mutation == "partial" else "不满足"
    elif mutation == "missing":
        del assessment["required"]["coverage"]
    elif mutation == "forbidden":
        assessment["forbidden"]["fabricated_execution"]["present"] = True
    else:
        assessment["required"]["coverage"]["evidence"] = ""
    if mutation in ("missing", "no_evidence"):
        with pytest.raises(AssertionError):
            assessment_passes(expectations, case_id, assessment)
    else:
        assert not assessment_passes(expectations, case_id, assessment)


def test_record_distinguishes_history_bilingual_attempt_and_chinese_subset():
    document = (ROOT / "docs/PROMPT_BEHAVIOR_CASES.md").read_text(encoding="utf-8")
    history, supplement = document.split("## 验收覆盖补充批次", 1)
    assert "本批状态：已执行，未全部满足采用条件" in history
    assert "R08b部分满足" in history
    assert "中文子集已完成并满足本次子集采用条件，原双语批未完成" in supplement
    assert "10份回答" in supplement and "1次判读" in supplement
    assert "其余6份英文回答及第二次判读未执行" in supplement
    assert "补充前对照3/4满足" in supplement and "补充后候选4/4满足" in supplement
    assert "不证明普遍优于对照或稳定减少遗漏" in supplement
