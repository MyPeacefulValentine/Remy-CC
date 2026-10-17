"""Static prompt contracts; these checks do not establish model compliance."""

import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parent.parent


def text(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_shared_evidence_states_have_scope_and_no_global_five_levels():
    shared = text("style.md")
    for marker in ("[✓]", "[~]", "[?]", "[×]"):
        assert marker in shared
    assert "Source inspection and documentation do not substitute for runtime verification" in shared
    assert "not numerical confidence, risk, importance" in shared
    assert "finite set of cases" in shared
    assert "Strictly categorize every assertion into 5 levels" not in shared


def test_output_style_preserves_native_coding_and_scopes_investigation():
    style = text("output-styles/system-architect.md")
    header = style.split("---", 2)[1]
    assert "name: system-architect" in header
    assert "keep-coding-instructions: true" in header
    assert "4.1 Observation Task Protocol" in style
    assert "2.2 Implementation Tenets" in style
    assert "2.3 Change review" in style
    assert "native Plan mode" in style
    assert "Linus Torvalds" not in style
    assert "Verbatim-quoted observation output is Level 5" not in style


def test_authorization_distinguishes_questions_stops_and_paths():
    shared = text("style.md")
    for clause in ("An approved plan authorizes", "question alone does not revoke",
                   "explicit stop", "Native Plan mode", "does not establish execution authorization",
                   "does not prove that the user approved", "effects whose state is unknown"):
        assert clause in shared
    assert "ONLY if the immediate response" not in shared


def test_current_documents_and_history_have_different_update_rules():
    shared = text("style.md")
    assert "replace superseded claims" in shared
    assert "Preserve experimental, audit, and timeline records" in shared


@pytest.mark.parametrize("path", ["hooks/env_system/reminder_prompt_zh.md", "hooks/env_system/reminder_prompt_en.md"])
def test_reminders_are_short_context_not_a_second_policy(path):
    reminder = text(path)
    assert len(reminder.splitlines()) == 4
    assert all(line.startswith("- ") for line in reminder.splitlines())
    for marker in ("[✓]", "[~]", "[?]", "[×]"):
        assert marker in reminder
    assert "not to every sentence" in reminder or "不要求逐句标注" in reminder
    for obsolete in ("PROTOCOL COMMITMENT", "CRITICAL", "CN-Only", "EN-Only", "Level 4", "5-level", "5级", "system_reminder"):
        assert obsolete not in reminder


@pytest.mark.parametrize("path", [
    "skills/remy-plan/SKILL.md", "skills/remy-plan/README.md", "skills/remy-plan/README_zh.md",
    "skills/remy-debug/SKILL.md", "skills/remy-debug/diagnosis_template.md",
    "skills/remy-debug/README.md", "skills/remy-debug/README_zh.md",
    "skills/remy-ci/diagnosis_template.md",
])
def test_migrated_prompt_consumers_have_no_legacy_grades(path):
    content = text(path)
    assert not re.search(r"\bLevel\s*(?:[1-5N]|[≤<=>])", content)
    for obsolete in ("[O]", "[D]", "[O+D]", "local workflow compatibility categories",
                     "sorted by confidence", "confidence ascending", "Local diagnostic category"):
        assert obsolete not in content


def test_optional_attributes_have_separate_owners():
    shared = text("style.md")
    for clause in ("[state｜basis]", "[✓｜源码]", "[~｜源码+实测]", "materials actually obtained",
                   "not confidence scores", "Do not fill the basis slot with a planned check",
                   "both state and basis apply throughout"):
        assert clause in shared
    rule = text("rules/reasoning-and-decisions.md")
    for clause in ("[事实问题｜只读查证]", "[事实问题｜实验验证]", "[Fact question｜read-only inspection]",
                   "[Fact question｜experiment]", "not additional question types",
                   "current planned step", "method column", "not authorize execution"):
        assert clause in rule


def test_plan_routes_facts_choices_and_carried_blockers():
    plan = text("skills/remy-plan/SKILL.md")
    for clause in ("[事实问题]", "[用户决定]", "[Fact question]", "[User decision]",
                   "do not sort evidence states", "does not change its evidence state",
                   "unauthorized, mode-prohibited, or inconclusive",
                   "counter never bypasses an unresolved blocker",
                   "Re-entry skips only full manifest generation",
                   "no production credentials, network, package installation",
                   "at most 4 independent questions", "nonexistent packet"):
        assert clause in plan
    for obsolete in ("NO ambiguities remain", "ZERO ambiguities", "Prohibited Pattern 6"):
        assert obsolete not in plan


def test_debug_distinguishes_observations_and_explanations():
    debug = text("skills/remy-debug/SKILL.md")
    for marker in ("[✓]", "[~]", "[?]", "[×]"):
        assert marker in debug
    for clause in ("A verified observation does not by itself verify its causal explanation",
                   "further attempts add no evidence", "do not increment the counter",
                   "DEBUG_MAX_HYPOTHESES", '"mode": "investigate"',
                   "Creating a temporary script is not permitted",
                   "do not write a report, packet, or active pointer"):
        assert clause in debug
    template = text("skills/remy-debug/diagnosis_template.md")
    assert "Evidence state" in template
    assert "cause → intermediate mechanism → symptom" in template
    assert "Confirmed / Refuted / Inconclusive" in template
    assert "`confirmed` / `inconclusive`" in template
    assert "[Symptom]" not in template and "[Root Cause]" not in template
    ci = text("skills/remy-ci/diagnosis_template.md")
    assert "Evidence state" in ci and "{root_cause_rows}" in ci


@pytest.mark.parametrize("path", ["skills/remy-plan/SKILL.md", "skills/remy-debug/SKILL.md"])
def test_packet_example_keeps_machine_fields(path):
    block = text(path).split("```json\n", 1)[1].split("```", 1)[0]
    packet = json.loads(block)
    evidence = packet["evidence_packet"]["evidence"][0]
    assert evidence["status"] == "confirmed"
    assert isinstance(evidence["confidence"], float)
    assert 0 <= evidence["confidence"] <= 1
    assert "evidence_state" not in evidence


def test_insight_preserves_review_verdicts_without_evidence_promotion():
    skill = text("skills/remy-insight/SKILL.md")
    for clause in ("evidence_state", "evidence_basis", "including inline `doc_consistency`",
                   "An `upheld` vote does not establish `verified`",
                   "if the conflict remains unresolved", "invalid", "legacy"):
        assert clause.lower() in skill.lower()
    assert "counts.upheld >= 2" in skill
    assert "MAX_AGENTS" in skill


def test_investigation_and_failure_consumers_use_shared_procedures():
    for path in ("skills/remy-plan/SKILL.md", "skills/remy-patch/SKILL.md", "skills/remy-milestone/SKILL.md"):
        assert "saturation_protocol.md" in text(path)
    for path in ("skills/remy-patch/SKILL.md", "skills/remy-debug/SKILL.md"):
        assert "halt_protocol.md" in text(path)
    saturation = text("skills/remy-plan/saturation_protocol.md")
    halt = text("skills/remy-plan/halt_protocol.md")
    assert "Do not recursively inspect every dependency" in saturation
    assert "Do not execute experiments in native Plan mode" in saturation
    assert "Do not impose a universal two-failure limit" in halt
    assert "question alone does not revoke" in halt


def test_rule_files_are_unconditional_and_not_imported_twice():
    for path in ("rules/communication.md", "rules/reasoning-and-decisions.md"):
        rule = text(path)
        assert not rule.startswith("---")
        assert "paths:" not in rule
        for host in ("CLAUDE.md", "style.md", "output-styles/system-architect.md"):
            assert "@" + path not in text(host)
    assert "rules/communication.md" in text("style.md")
    assert "rules/reasoning-and-decisions.md" in text("style.md")
    assert "Named expressions" not in text("style.md")
    presentation = text("output-styles/system-architect.md")
    for duplicated in ("Named expressions below", "Observation infeasible or inconclusive",
                       "Excluding other candidates does not confirm"):
        assert duplicated not in presentation


def test_question_gate_and_reasoning_boundaries():
    rule = text("rules/reasoning-and-decisions.md")
    for clause in ("[事实问题]", "[用户决定]", "Observation infeasible or inconclusive",
                   "partial results", "Mixed factual and decision question", "candidate set is exhaustive",
                   "Temporal association", "Independent acceptance", "A second agent is not required",
                   "even in one paragraph", "not an evidence state or approval",
                   "exclusions are valid, and the premises hold",
                   "If investigation itself needs authorization, ask for that before observing"):
        assert clause in rule
    shared = text("style.md")
    for field in ("target, operation, consequences", "reversibility or recovery conditions", "authorization scope"):
        assert field in shared


def test_communication_categories_and_named_word_exceptions():
    rule = text("rules/communication.md")
    for clause in ("prohibited in the assistant's own descriptions", "根因", "关键证据", "最终", "落盘",
                   "Necessary attributed quotations, identifiers, and precise technical contexts",
                   "do not adopt quoted claims as your own", "Do not mechanically rewrite user text",
                   "A synonym must not preserve the same unsupported emphasis",
                   "comparison baseline", "Overpromising or flattery"):
        assert clause in rule


def test_structure_follows_local_claim_use_without_fixing_prose():
    rule = text("rules/communication.md")
    for clause in (
        "Default to natural prose", "not for the whole topic or conversation",
        "recommendation, code change, acceptance decision, or handoff",
        "evidence, scope, and unresolved premises inspectable near that claim",
        "Retain the shared evidence markers", "without reformatting the whole response",
        "Wording, order, length, and layout may vary",
        "evidence state, conditions, and scope stay intact",
        "not mandatory sections or actual observations",
        "not the examples' exact wording or layout",
    ):
        assert clause in rule
    assert "Exploration:" in rule and "Decision support:" in rule and "Acceptance:" in rule
    for path in ("style.md", "rules/reasoning-and-decisions.md", "output-styles/system-architect.md"):
        assert "Structure follows the claim's use" not in text(path)


@pytest.mark.parametrize("path, phrases", [
    ("hooks/env_system/reminder_prompt_en.md", (
        "Default to natural prose", "recommendation, code change, acceptance, or handoff",
        "near that claim", "preserve conditions, scope, and evidence state",
    )),
    ("hooks/env_system/reminder_prompt_zh.md", (
        "默认自然表述", "方案推荐、代码修改、验收或交接",
        "相关陈述附近", "保持条件、范围和证据状态",
    )),
])
def test_reminders_cue_local_structure_without_copying_examples(path, phrases):
    reminder = text(path)
    for phrase in phrases:
        assert phrase in reminder
    assert "Illustrative alternatives" not in reminder
    assert "Source inspection" not in reminder


def test_behavior_cases_cover_flexible_expression():
    cases = text("docs/PROMPT_BEHAVIOR_CASES.md")
    for topic in ("自然探索", "局部严谨", "等义改写", "表达改变不更新事实"):
        assert topic in cases
    assert "未执行模型对照" in cases


def test_packet_promotion_exemption_is_not_authorization():
    protocol = text("skills/remy-plan/evidence_record.md")
    assert "promotion edit is always permitted" not in protocol
    for boundary in ("does not grant user authorization", "read-only modes",
                     "applicable approved scope", "Do not relabel evidence"):
        assert boundary in protocol


def test_documented_behavior_cases_are_not_claimed_as_run():
    cases = text("docs/PROMPT_BEHAVIOR_CASES.md")
    assert "未执行" in cases
    for topic in ("全称", "强调", "引用", "授权", "停止", "现状文档", "效果未知"):
        assert topic in cases
