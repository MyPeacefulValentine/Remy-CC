# Diagnosis Report Template

You MUST output the diagnosis using the following structure. Fill each section with evidence collected during the investigation.

---

## 1. Symptom (症状)

| Field | Value |
| :--- | :--- |
| Entry type | `test_failure` / `error_description` / `location_reference` |
| Raw input | <user-provided argument or captured error output> |
| Timestamp | <when this diagnosis started> |

## 2. Localization (定位)

### Suspect Files

| File | Reason for suspicion | Git recent changes |
| :--- | :--- | :--- |
| `path/to/file` | Stack trace line 42 / grep match / impact upstream | `abc1234 — commit msg (2d ago)` |

### Dependency Map (if available)

<Paste impact.py output here, or "N/A — logic_index.db unavailable">

## 3. Hypothesis Log (假设日志)

Use `style.md`'s shared evidence state and optional acquired-basis attribute for each hypothesis. Keep concrete citations, scope, and unresolved premises in the Result; do not fill an attribute with a planned test. The verdict below is a diagnostic workflow value, not a replacement for the evidence state.

| # | Hypothesis | Evidence state | Probe | Result | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | <candidate explanation> | <shared state, with acquired basis if useful> | <permitted check or reason not run> | <observation, citation, scope and remaining gap> | Confirmed / Refuted / Inconclusive |

## 4. Failure Mechanism (故障机制)

**Status**: `confirmed` / `inconclusive`

**Statement**: <Supported explanation and scope, or the unresolved question; cite evidence IDs without inventing missing links>

**Observation and explanation**:
- Observation: <what was checked and observed; apply its own evidence state>.
- Explanation: <why it may explain the symptom; keep inference and assumptions explicit>.
- Missing evidence: <what would resolve the remaining uncertainty, or state that this bounded explanation is established>.

The hypothesis log records the diagnostic sequence: symptom, candidate explanation, check, result. If a causal chain is supported, describe cause → intermediate mechanism → symptom and bind evidence to each material link. Do not treat the order of investigation as a proven causal chain.

## 5. Proposed Fix (建议修复)

| File | Location | Change description |
| :--- | :--- | :--- |
| `path/to/file` | `function_name` L42 | <what to modify and why> |

**Constraints**: <any constraints the fix must respect, derived from dependency map or code review>

## 6. Suggestions (后续建议)

- <If inconclusive: what additional investigation steps would help>
- <If confirmed: recommended verification approach after fix>
- <Related areas to check for similar issues>
