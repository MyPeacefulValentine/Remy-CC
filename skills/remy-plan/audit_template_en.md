# Deep Plan Analysis Tables Template

You must output your analysis in the following **five** Markdown tables in this exact order. **Add 1 empty line before and after each table.**

### 🧩 Table 1: Ambiguity Resolution Matrix

*   **Goal**: Eliminate ALL "TBD" (To Be Determined). Convert options to hard constraints.
*   **Strict Rule**: If technical details (timeouts, retries, specific libraries) are not locked, the plan is **REJECTED**.

| Decision Point | Grade | Options | Final Constraint | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| *Example: Timeout* | *[Implementation]* | *Default / 30s / 60s* | ***Fixed: 15s connect, 30s read*** | *Avoid resource exhaustion* |
| *Example: Library* | *[Architectural] criterion 1: consumers = API layer, CLI* | *Json / Orjson* | ***Fixed: Standard json*** | *Avoid new dependencies; correction cost: synchronized replacement at all serialization call sites* |

### 🧪 Table 2: Property-Based Testing Spec

*   **Goal**: Define mathematical invariants that must hold true for ALL inputs (not just examples).
*   **Categories**: Idempotency, Round-trip, Invariant Preservation, Commutativity.

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
| **Data Structure** | Hardcoded / parameterized | Pass/Locked | (Must prioritize args/config over hardcoding) |
| **System Risk** | Side effects / environment compatibility | Pass/Warn | (Check global mechanisms & OS differences) |
| **Complexity** | Time / space / OOM | Pass/Warn | (Assess loops & memory usage) |
| **Concurrency** | Read-write conflict / deadlock | Pass/Warn | (Check file IO & shared resources) |
| **Zero Decision** | Parameter locking / ambiguity resolution | Locked | (Must match Table 1) |

### 🛠️ Table 4: Physical Change Simulation

*   **Scope Position**: Open with one tag — `[Boundary-Wrap]` / `[Source-Modify]` / `[Contract-Change]` / `[Scope-Refactor]` — followed by a 1-sentence explanation of why the modification belongs at this level. See SKILL.md Phase 3 → Scope Tag Reference.
*   **Ripple Effect**: Confirm imports and dependencies do not create circular references.

| File Path | Location | Action | Summary | Scope Position | Ripple Effect |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `path/to/file` | `func_name` | Modify | Add retry logic | [Source-Modify] Add retry inside the function where the error state originates | None |

**Caller Refs Annotation** (required when `Action=Create`; see SKILL.md Phase 4.5)

| Create ID | caller_file (existing tree) | caller_function | evidence_ref |
| :--- | :--- | :--- | :--- |
| *Example: C-002* | *path/to/existing_file.py* | *EntryClass.run* | *E-003* |

If this packet contains no `Create` rows, this annotation table is empty. **No orphan creation**: `caller_file` must be a file already present in the current tree, not another `Create` entry from the same packet.

### ✅ Table 5: Verification Plan

*   **Goal**: Define how to verify the implementation is correct after execution.
*   **Scope**: Each entry corresponds to one or more changes from Table 4.

| Step | Method | Expected Result | Rollback Condition |
| :--- | :--- | :--- | :--- |
| *Example: Unit test* | `pytest tests/test_auth.py -v` | All tests pass | Revert commit |
| *Example: Integration* | Manual invocation of modified endpoint | Response matches Table 2 invariant | Mark as partial implementation |
| *Example: Regression* | Run full test suite | No new failures | Investigate before proceeding |

---

> **⚠ CHECKPOINT**: All 5 tables are complete. Do **NOT** emit the stop prompt yet. You **MUST** proceed to **Phase 6 (Evidence Packet Generation)** and execute all 5 steps (timestamp → git commit → ensure directory → write packet → update .active_packet) before outputting the final stop prompt.
