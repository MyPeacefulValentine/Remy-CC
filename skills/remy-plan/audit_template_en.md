# Deep Plan Analysis Tables Template

You must output your analysis in the following **five** Markdown tables in this exact order. **Add 1 empty line before and after each table.**

### 🧩 Table 1: Ambiguity Resolution Matrix

*   **Goal**: Settle user intent, contracts, scope and acceptance. Record implementation freedom with explicit boundaries rather than leaving user-owned choices unresolved.
*   **Strict Rule**: Timeouts, retries and library choices must be fixed when they affect promised behavior; otherwise state the allowed range, constraints and when to ask again. No binary grade controls the recommendation.

| Decision Point | Options | Settled Constraint / Implementation Bounds | Rationale and Source |
| :--- | :--- | :--- | :--- |
| *Example: Timeout* | *Default / 30s / 60s* | *Use the timeout required by the agreed contract* | *Cite the requirement and affected failure behavior* |
| *Example: Internal storage* | *Existing compatible representations* | *Implementation may choose within the agreed ownership and interface constraints* | *State bounds and the conditions requiring a new user decision* |

### 🧪 Table 2: Property-Based Testing Spec

*   **Goal**: Define properties only when supported by the task's contract; state the input domain and premises, not unsupported all-input claims.
*   **Categories**: Idempotency, Round-trip, Invariant Preservation, Commutativity. Keep this property table; if no property applies, show N/A with the reason and use `pbt_specs: []`. Put ordinary scenarios, compatibility and recovery checks in Table 5 instead.

| Module | PBT Property | Invariant | Falsification Strategy |
| :--- | :--- | :--- | :--- |
| *Example: Parser* | *Round-trip* | `decode(encode(x)) == x` | *Random Unicode strings* |
| *Example: Wallet* | *Invariant* | `balance >= 0` always | *Concurrent subtraction* |

### ⚖️ Table 3: Logic & Contract Audit

*   **Data Flow**: Verify upstream/downstream parameter compatibility.
*   **System Risk**: Check for global state modification or OS-specific assumptions.

| Dimension | Check Item | Status | Decision / Constraint |
| :--- | :--- | :--- | :--- |
| **Data Flow** | Upstream / downstream compatibility | Pass/Warn | (Must define specific data contract) |
| **Data Flow** | New mutable state entity (schema column / config / sentinel file) lists init / read / write paths | Pass/Warn | If write path is absent, declare the entity as init-once read-only in Table 1 |
| **Consistency** | Function signatures / library calls | Pass/Fail | (Check recursively against definitions) |
| **Data Structure** | Constants / parameters / configuration | Pass/Locked | Reuse suitable mechanisms; new configuration requires actual variability, not a default preference |
| **System Risk** | Side effects / environment compatibility | Pass/Warn | (Check global mechanisms & OS differences) |
| **Complexity** | Time / space / OOM | Pass/Warn | (Assess loops & memory usage) |
| **Concurrency** | Read-write conflict / deadlock | Pass/Warn | (Check file IO & shared resources) |
| **Decision readiness** | User decisions settled / implementation bounds stated | Locked | Must match Table 1; preserve the legacy JSON dimension value without reviving an all-details-fixed requirement |

### 🛠️ Table 4: Physical Change Simulation

*   **Scope Position**: Keep the existing `scope_position` string. Describe actual placement, responsibility, contract requirements and affected consumers; optional `{field: value｜field: value}` annotations do not create a grade. See SKILL.md Phase 3 → Task Attribute Reference.
*   **Ripple Effect**: Confirm imports and dependencies do not create circular references.

| File Path | Location | Action | Summary | Scope Position | Ripple Effect |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `path/to/file` | `func_name` | Modify | Add agreed retry behavior | Retry remains inside the responsible function and preserves the stated contract | Inspect affected callers |

**Caller Refs Annotation** (required when `Action=Create`; see SKILL.md Phase 4.5)

| Create ID | caller_file (existing tree) | caller_function | evidence_ref |
| :--- | :--- | :--- | :--- |
| *Example: C-002* | *path/to/existing_file.py* | *EntryClass.run* | *E-003* |

If this packet contains no `Create` rows, this annotation table is empty. **No orphan creation**: `caller_file` must be a file already present in the current tree, not another `Create` entry from the same packet.

### ✅ Table 5: Verification Plan

*   **Goal**: Define how to verify the implementation is correct after execution.
*   **Coverage**: Map every goal, preserved contract and applicable branch of the selected approach to a check with a method and observable expected result. Reverse-check each entry against its requirement, basis, applicable conditions and related Table 4 changes, including affected unchanged consumers. Reused approval or skipped confirmation does not remove acceptance obligations.
*   **Applicability**: One check may cover multiple obligations if their conditions and results remain distinguishable. Do not require checks for unselected alternatives or demand support for version/data combinations outside the agreed contract; retain checks for required rejection or isolation of such combinations. N/A requires genuine inapplicability and a reason; unknown or not yet executed does not mean N/A. Acceptance-blocking unknowns remain blockers for the affected scope, not nonblocking assumptions. Planned checks are not passing results.
*   **Handoff**: Keep the coverage and concrete check content in the existing Phase 6 text fields. Fill settled mappings directly; new facts or decisions use the existing routing and budgets, not a new confirmation loop.

| Step | Method | Expected Result | Rollback Condition |
| :--- | :--- | :--- | :--- |
| *Example: Unit test* | `pytest tests/test_auth.py -v` | All tests pass | Revert commit |
| *Example: Integration* | Invoke the endpoint under the selected scenario's applicable success and failure conditions | Response and preserved behavior match the stated contract; include a Table 2 property only when applicable | Stop the affected transition and investigate within approved recovery bounds |
| *Example: Regression* | Run full test suite | No new failures | Investigate before proceeding |

---

> **⚠ CHECKPOINT**: All 5 tables remain mandatory; show N/A with a reason where genuinely inapplicable and keep the existing JSON fields. Proceed to **Phase 6 (Evidence Packet Generation)** and its five steps only when authorization, mode and absence of blockers permit writing the packet. Native Plan writes only its designated plan file. Otherwise report why no packet was created and do not update the active pointer or advertise execution. Then follow the fixed Phase 7 stop protocol.
