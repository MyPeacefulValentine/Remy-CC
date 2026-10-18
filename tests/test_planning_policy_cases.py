"""Offline fixture/rubric checks, not a model runner or semantic answer grader."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "planning_policy"
DIMENSIONS = {
    "修改边界", "契约与生命周期", "方案策略", "事实与未知", "权限", "推进能力",
}
CASE_IDS = {f"{number:02d}" for number in range(1, 13)}
PAIR_IDS = {f"{number:02d}" for number in range(1, 7)}
JUDGMENTS = {"满足", "部分满足", "不满足"}


def load_fixtures():
    inputs = json.loads((FIXTURES / "inputs.json").read_text(encoding="utf-8"))
    expectations = json.loads(
        (FIXTURES / "expectations.json").read_text(encoding="utf-8")
    )
    return inputs, expectations


def validate_fixtures(inputs, expectations):
    """Reject missing cases, label leakage, unmatched rubrics and paired drift."""
    assert set(inputs) == {"version", "cases"}
    assert inputs["version"] == expectations["version"] == "1.0.0"
    assert len(inputs["cases"]) == len(expectations["cases"]) == 12
    assert {case["id"] for case in inputs["cases"]} == CASE_IDS
    assert {case["id"] for case in expectations["cases"]} == CASE_IDS
    assert len(expectations["dimensions"]) == 6
    assert set(expectations["dimensions"]) == DIMENSIONS
    assert len(expectations["pairs"]) == 6
    assert {pair["id"] for pair in expectations["pairs"]} == PAIR_IDS

    by_id = {case["id"]: case for case in inputs["cases"]}
    expected_by_id = {case["id"]: case for case in expectations["cases"]}
    seen_case_ids = []
    all_requirement_ids = []
    for pair in expectations["pairs"]:
        assert len(pair["case_ids"]) == 2
        first, second = (by_id[case_id] for case_id in pair["case_ids"])
        assert first["pair_id"] == second["pair_id"] == pair["id"]
        assert [first["variant"], second["variant"]] == ["a", "b"]
        assert pair["changed_line"] == 1
        assert isinstance(pair["fact"], str) and pair["fact"].strip()
        first_lines = first["prompt"].splitlines()
        second_lines = second["prompt"].splitlines()
        assert len(first_lines) == len(second_lines)
        changed = [
            index for index, (left, right) in enumerate(zip(first_lines, second_lines))
            if left != right
        ]
        assert changed == [pair["changed_line"]]
        assert pair["values"] == [first_lines[1], second_lines[1]]
        assert first_lines[:1] + first_lines[2:] == second_lines[:1] + second_lines[2:]
        seen_case_ids.extend(pair["case_ids"])
    assert len(seen_case_ids) == len(set(seen_case_ids)) == 12

    for case in inputs["cases"]:
        assert set(case) == {"id", "pair_id", "variant", "prompt"}
        assert isinstance(case["prompt"], str) and case["prompt"].strip()
        assert case["prompt"].count("当前没有工具或源码可供读取") == 1
        assert "不声称已查证或执行" in case["prompt"]
        for leaked_label in (
            "评分", "必需判据", "applicable_dimensions", "acceptable_variations",
            "实现级", "架构级", "Decision Grade", "R01a", "严重错",
        ):
            assert leaked_label not in case["prompt"]

    for criterion in expectations["common_required"]:
        assert set(criterion) == {"id", "dimension", "text"}
        assert criterion["dimension"] in DIMENSIONS
        assert isinstance(criterion["text"], str) and criterion["text"].strip()
        all_requirement_ids.append(criterion["id"])
    assert expectations["common_required"]
    for case in expectations["cases"]:
        assert set(case) == {
            "id", "required", "forbidden", "acceptable_variations", "applicable_dimensions",
        }
        assert case["required"] and case["forbidden"] and case["acceptable_variations"]
        dimensions = case["applicable_dimensions"]
        assert len(dimensions) == len(set(dimensions))
        assert set(dimensions) <= DIMENSIONS
        for criterion in expectations["common_required"] + case["required"]:
            assert set(criterion) == {"id", "dimension", "text"}
            assert criterion["dimension"] in dimensions
            assert isinstance(criterion["text"], str) and criterion["text"].strip()
        all_requirement_ids.extend(criterion["id"] for criterion in case["required"])
        for field in ("forbidden", "acceptable_variations"):
            assert all(isinstance(item, str) and item.strip() for item in case[field])
    assert len(all_requirement_ids) == len(set(all_requirement_ids))

    severe_ids = {item["id"] for item in expectations["severe_errors"]}
    assert len(severe_ids) == len(expectations["severe_errors"]) == 4
    assert all(isinstance(item["text"], str) and item["text"].strip()
               for item in expectations["severe_errors"])
    common_ids = {item["id"] for item in expectations["common_required"]}
    for example in expectations["counterexamples"]:
        case = expected_by_id[example["case_id"]]
        allowed_ids = common_ids | {item["id"] for item in case["required"]}
        assert example["answer"].strip()
        assert example["violated_required"] or example["matched_forbidden"] or example["severe_error_ids"]
        assert set(example["violated_required"]) <= allowed_ids
        assert set(example["severe_error_ids"]) <= severe_ids
        assert all(type(index) is int and 0 <= index < len(case["forbidden"])
                   for index in example["matched_forbidden"])


def assessment_passes(expectations, case_id, assessment):
    """Apply adoption rules to explicit human judgments, never to answer keywords.

    Quoted answer evidence must be reviewed by a human. This helper only checks
    coverage and the adoption rule; it cannot establish semantic correctness.
    """
    case = next(item for item in expectations["cases"] if item["id"] == case_id)
    required_ids = {
        item["id"] for item in expectations["common_required"] + case["required"]
    }
    assert set(assessment) == {"required", "dimensions", "forbidden", "severe_errors"}
    assert set(assessment["required"]) == required_ids
    assert set(assessment["dimensions"]) == set(case["applicable_dimensions"])
    assert set(assessment["forbidden"]) == {str(index) for index in range(len(case["forbidden"]))}
    assert set(assessment["severe_errors"]) == {
        item["id"] for item in expectations["severe_errors"]
    }
    for section in ("required", "dimensions"):
        for judgment in assessment[section].values():
            assert set(judgment) == {"state", "evidence"}
            assert judgment["state"] in JUDGMENTS
            assert isinstance(judgment["evidence"], str) and judgment["evidence"].strip()
    for section in ("forbidden", "severe_errors"):
        for judgment in assessment[section].values():
            assert set(judgment) == {"present", "evidence"}
            assert type(judgment["present"]) is bool
            assert isinstance(judgment["evidence"], str) and judgment["evidence"].strip()
    return (
        all(item["state"] == "满足" for item in assessment["required"].values())
        and all(item["state"] == "满足" for item in assessment["dimensions"].values())
        and not any(item["present"] for item in assessment["forbidden"].values())
        and not any(item["present"] for item in assessment["severe_errors"].values())
    )


def synthetic_assessment(expectations, case_id):
    """Synthetic annotations for testing scoring plumbing, not observed outputs."""
    case = next(item for item in expectations["cases"] if item["id"] == case_id)
    return {
        "required": {
            item["id"]: {"state": "满足", "evidence": "合成满足标注，不是模型结果。"}
            for item in expectations["common_required"] + case["required"]
        },
        "dimensions": {
            dimension: {"state": "满足", "evidence": "合成满足标注，不是模型结果。"}
            for dimension in case["applicable_dimensions"]
        },
        "forbidden": {
            str(index): {"present": False, "evidence": "合成否定标注，不是模型结果。"}
            for index in range(len(case["forbidden"]))
        },
        "severe_errors": {
            item["id"]: {"present": False, "evidence": "合成否定标注，不是模型结果。"}
            for item in expectations["severe_errors"]
        },
    }


def test_fixture_integrity_and_single_fact_pairs():
    validate_fixtures(*load_fixtures())


@pytest.mark.parametrize("mutation", [
    "missing_case", "duplicate_id", "missing_expectation", "wrong_pair", "second_fact",
    "score_leak", "missing_dimension", "dangling_criterion", "invalid_forbidden_index",
])
def test_integrity_rejects_fixture_regressions(mutation):
    inputs, expectations = deepcopy(load_fixtures())
    if mutation == "missing_case":
        inputs["cases"].pop()
    elif mutation == "duplicate_id":
        inputs["cases"][1]["id"] = inputs["cases"][0]["id"]
    elif mutation == "missing_expectation":
        expectations["cases"].pop()
    elif mutation == "wrong_pair":
        inputs["cases"][1]["pair_id"] = "02"
    elif mutation == "second_fact":
        inputs["cases"][1]["prompt"] += "\n另一个条件也改变。"
    elif mutation == "score_leak":
        for case in inputs["cases"][:2]:
            case["prompt"] += "\n评分：请满足必需判据。"
    elif mutation == "missing_dimension":
        expectations["cases"][0]["applicable_dimensions"].remove("权限")
    elif mutation == "dangling_criterion":
        expectations["counterexamples"][0]["violated_required"] = ["missing"]
    else:
        expectations["counterexamples"][0]["matched_forbidden"] = [999]
    with pytest.raises(AssertionError):
        validate_fixtures(inputs, expectations)


def test_cache_pair_covers_shared_and_historical_entries():
    inputs, expectations = load_fixtures()
    validate_fixtures(inputs, expectations)
    by_id = {case["id"]: case for case in inputs["cases"]}
    for case_id in ("09", "10"):
        prompt = by_id[case_id]["prompt"]
        assert "整个相关缓存命名空间及条目有效期" in prompt
        assert "可能命中的历史条目" in prompt
        assert "不存在范围外写入者" in prompt
        assert "不含语言的查询参数" in prompt


def test_offline_pair_changes_only_server_additions():
    inputs, expectations = load_fixtures()
    validate_fixtures(inputs, expectations)
    by_id = {case["id"]: case for case in inputs["cases"]}
    for case_id in ("11", "12"):
        prompt = by_id[case_id]["prompt"]
        for condition in (
            "本地已在断网前同步全部旧订单", "已有订单不会在断网期间修改或删除",
            "完全断网", "不允许新增网络通道",
        ):
            assert condition in prompt
    assert "服务器可能新增订单" in by_id["11"]["prompt"]
    assert "服务器明确不会新增订单" in by_id["12"]["prompt"]
    by_expectation = {case["id"]: case for case in expectations["cases"]}
    assert "无法获知可能新增" in by_expectation["11"]["required"][0]["text"]
    assert "离线快照可覆盖" in by_expectation["12"]["required"][0]["text"]


def test_counterexample_annotations_cannot_pass():
    _, expectations = load_fixtures()
    assert {example["case_id"] for example in expectations["counterexamples"]} >= {
        "01", "04", "06", "08", "09", "10", "11", "12",
    }
    for example in expectations["counterexamples"]:
        assessment = synthetic_assessment(expectations, example["case_id"])
        for criterion_id in example["violated_required"]:
            assessment["required"][criterion_id] = {
                "state": "不满足", "evidence": example["answer"],
            }
        for index in example["matched_forbidden"]:
            assessment["forbidden"][str(index)] = {
                "present": True, "evidence": example["answer"],
            }
        for error_id in example["severe_error_ids"]:
            assessment["severe_errors"][error_id] = {
                "present": True, "evidence": example["answer"],
            }
        assert not assessment_passes(expectations, example["case_id"], assessment)


@pytest.mark.parametrize("error_id", ["S1", "S2", "S3", "S4"])
def test_severe_error_is_not_offset_by_other_satisfied_dimensions(error_id):
    _, expectations = load_fixtures()
    assessment = synthetic_assessment(expectations, "01")
    assert assessment_passes(expectations, "01", assessment)
    assessment["severe_errors"][error_id] = {
        "present": True, "evidence": "合成严重错标注，不是模型结果。",
    }
    assert not assessment_passes(expectations, "01", assessment)


@pytest.mark.parametrize("state", ["部分满足", "不满足"])
def test_each_required_criterion_must_be_satisfied(state):
    _, expectations = load_fixtures()
    assessment = synthetic_assessment(expectations, "01")
    assessment["required"]["R01a"]["state"] = state
    assert not assessment_passes(expectations, "01", assessment)


def test_missing_or_unsupported_judgment_is_not_a_pass():
    _, expectations = load_fixtures()
    assessment = synthetic_assessment(expectations, "01")
    del assessment["required"]["R01a"]
    with pytest.raises(AssertionError):
        assessment_passes(expectations, "01", assessment)
    assessment = synthetic_assessment(expectations, "01")
    assessment["dimensions"]["事实与未知"]["evidence"] = ""
    with pytest.raises(AssertionError):
        assessment_passes(expectations, "01", assessment)
    assessment = synthetic_assessment(expectations, "01")
    assessment["dimensions"]["事实与未知"]["state"] = "不适用"
    with pytest.raises(AssertionError):
        assessment_passes(expectations, "01", assessment)


def test_nonapplicable_dimension_is_not_required():
    _, expectations = deepcopy(load_fixtures())
    case = expectations["cases"][0]
    case["applicable_dimensions"].remove("契约与生命周期")
    case["required"] = [
        item for item in case["required"] if item["dimension"] != "契约与生命周期"
    ]
    assessment = synthetic_assessment(expectations, "01")
    assert "契约与生命周期" not in assessment["dimensions"]
    assert assessment_passes(expectations, "01", assessment)


def test_document_preserves_history_and_distinguishes_batch_outcome():
    document = (ROOT / "docs" / "PROMPT_BEHAVIOR_CASES.md").read_text(encoding="utf-8")
    historical, batch = document.split("## 任务属性驱动规划对照批次", 1)
    assert "状态：未执行模型对照" in historical
    assert "本批状态：已执行，未全部满足采用条件" in batch
    assert "R08b部分满足" in batch
    assert "未修改规则后重跑原样本" in batch
    assert "tests/fixtures/planning_policy/inputs.json" in document
    assert "tests/fixtures/planning_policy/expectations.json" in document
    assert "24" in document and "26" in document
    assert "单独判不通过" in document
    assert "不能自动判定回答的语义" in document
