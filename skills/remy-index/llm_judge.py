"""LLM-based propagation judgment for hierarchical summary updates.

Reads cached judgments from ``judge_cache`` keyed by payload hash; on miss,
calls the LLM and validates the response against the closed dimension set.
Falls back to conservative bias (``propagate=true``, ``confidence='low'``,
``matched_dimension='ambiguous'``) when the response is invalid or absent.
"""
import hashlib
import json
import os
from datetime import datetime


JUDGE_DIMENSIONS = frozenset({
    "signature",
    "error_contract",
    "side_effects",
    "concurrency",
    "resource_lifecycle",
    "complexity_tier",
    "security",
    "data_contract",
    "internal_refactor",
    "rename",
    "extract_inline",
    "comment_only",
    "test_only",
    "log_only",
    "ambiguous",
})

JUDGE_CONFIDENCE = frozenset({"high", "medium", "low"})

CONSERVATIVE_DEFAULT = {
    "propagate": True,
    "rationale": "LLM unavailable or response invalid; conservative bias applied.",
    "matched_dimension": "ambiguous",
    "confidence": "low",
}


def _canonical_payload(parent_kind, parent_ref, parent_summary_prev, child_changes):
    sorted_children = sorted(child_changes, key=lambda c: c.get("child_ref", ""))
    return json.dumps(
        {
            "parent_kind": parent_kind,
            "parent_ref": parent_ref,
            "parent_summary": parent_summary_prev,
            "children": sorted_children,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def payload_hash(parent_kind, parent_ref, parent_summary_prev, child_changes):
    canonical = _canonical_payload(parent_kind, parent_ref, parent_summary_prev, child_changes)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_response(raw_text):
    if not isinstance(raw_text, str):
        return None
    try:
        obj = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("propagate"), bool):
        return None
    if obj.get("matched_dimension") not in JUDGE_DIMENSIONS:
        return None
    if obj.get("confidence") not in JUDGE_CONFIDENCE:
        return None
    if not isinstance(obj.get("rationale"), str):
        return None
    return {
        "propagate": obj["propagate"],
        "rationale": obj["rationale"],
        "matched_dimension": obj["matched_dimension"],
        "confidence": obj["confidence"],
    }


def _load_prompt_template():
    prompts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
    path = os.path.join(prompts_dir, "judge_propagation.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return (
            "Judge whether the parent summary must be rewritten given child changes.\n"
            "Output JSON {propagate: bool, rationale: str, matched_dimension: str, "
            "confidence: 'high'|'medium'|'low'}.\n\n{{payload}}"
        )


def build_prompt(parent_kind, parent_ref, parent_summary_prev, child_changes):
    template = _load_prompt_template()
    payload = {
        "parent_kind": parent_kind,
        "parent_ref": parent_ref,
        "parent_summary": parent_summary_prev,
        "children": child_changes,
    }
    return template.replace("{{payload}}", json.dumps(payload, ensure_ascii=False, indent=2))


def judge_propagation(db, parent_kind, parent_ref, parent_summary_prev, child_changes, llm_call):
    cache_key = payload_hash(parent_kind, parent_ref, parent_summary_prev, child_changes)
    row = db.execute(
        "SELECT result FROM judge_cache WHERE payload_hash = ?", (cache_key,)
    ).fetchone()
    if row:
        try:
            cached = json.loads(row[0])
            if validate_response(json.dumps(cached, ensure_ascii=False)):
                return cached
        except json.JSONDecodeError:
            pass

    prompt = build_prompt(parent_kind, parent_ref, parent_summary_prev, child_changes)
    raw = llm_call(prompt)
    validated = validate_response(raw)
    result = validated if validated is not None else dict(CONSERVATIVE_DEFAULT)

    db.execute(
        "INSERT OR REPLACE INTO judge_cache (payload_hash, result, created_at) VALUES (?,?,?)",
        (
            cache_key,
            json.dumps(result, ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    db.commit()
    return result
