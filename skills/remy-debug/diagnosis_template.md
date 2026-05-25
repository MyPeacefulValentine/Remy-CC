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

<Paste impact.py output here, or "N/A — logic_index.json unavailable">

## 3. Hypothesis Log (假设日志)

| # | Hypothesis | Confidence | Probe | Result | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | <suspected cause> | Level N | <what was checked> | <observed result> | Confirmed / Refuted / Inconclusive |

## 4. Root Cause (主因)

**Status**: `confirmed` / `inconclusive`

**Statement**: <One-paragraph description of the identified root cause, citing evidence IDs>

**Evidence Chain**:
```
[Symptom] <what was observed>
  -> [Mechanism] <how the failure propagates>
    -> [Root Cause] <where the defect originates>
```

## 5. Proposed Fix (建议修复)

| File | Location | Change description |
| :--- | :--- | :--- |
| `path/to/file` | `function_name` L42 | <what to modify and why> |

**Constraints**: <any constraints the fix must respect, derived from dependency map or code review>

## 6. Suggestions (后续建议)

- <If inconclusive: what additional investigation steps would help>
- <If confirmed: recommended verification approach after fix>
- <Related areas to check for similar issues>
