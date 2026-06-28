# Deep Plan Analysis Tables Template

You must output your analysis in the following **five** Markdown tables in this exact order. **Add 1 empty line before and after each table.**

### 🧩 Table 1: Ambiguity Resolution Matrix (歧义消除矩阵)

*   **Goal**: Eliminate ALL "TBD" (To Be Determined). Convert options to hard constraints.
*   **Strict Rule**: If technical details (timeouts, retries, specific libraries) are not locked, the plan is **REJECTED**.

| 决策点 (Ambiguity) | 选项/可能性 | 最终约束 | 理由 |
| :--- | :--- | :--- | :--- |
| *Example: Timeout* | *Default / 30s / 60s* | ***Fixed: 15s connect, 30s read*** | *Avoid resource exhaustion* |
| *Example: Library* | *Json / Orjson* | ***Fixed: Standard json*** | *Avoid new dependencies* |

### 🧪 Table 2: Property-Based Testing Spec (PBT 属性规约)

*   **Goal**: Define mathematical invariants that must hold true for ALL inputs (not just examples).
*   **Categories**: Idempotency (幂等性), Round-trip (可逆性), Invariant Preservation (守恒性), Commutativity (交换律).

| 功能模块 | PBT 属性类型 | 不变量描述 | 证伪策略 |
| :--- | :--- | :--- | :--- |
| *Example: Parser* | *Round-trip* | `decode(encode(x)) == x` | *Random Unicode strings* |
| *Example: Wallet* | *Invariant* | `balance >= 0` always | *Concurrent subtraction* |

### ⚖️ Table 3: Logic & Contract Audit (逻辑与契约审计)

*   **Data Flow**: Verify upstream/downstream parameter compatibility.
*   **System Risk**: Check for global state modification or OS-specific assumptions.

| 维度 | 检查项 | 状态 | 决策/规约 |
| :--- | :--- | :--- | :--- |
| **数据流** | 上游依赖 / 下游兼容 | Pass/Warn | (Must define specific data contract) |
| **数据流** | 新增可变状态实体（schema 列 / 配置项 / sentinel 文件）是否同时列出 init / read / write 三路径 | Pass/Warn | Write 路径缺失时必须在 Table 1 显式声明该实体为 init-once read-only |
| **一致性** | 函数签名 / 库调用 | Pass/Fail | (Check recursively against definitions) |
| **数据结构** | 硬编码 / 参数化 | Pass/Locked | (Must prioritize args/config over hardcoding) |
| **系统风险** | 副作用 / 环境兼容 | Pass/Warn | (Check global mechanisms & OS differences) |
| **复杂度** | 时间 / 空间 / OOM | Pass/Warn | (Assess loops & memory usage) |
| **并发与锁** | 读写冲突 / 死锁 | Pass/Warn | (Check file IO & shared resources) |
| **零决策** | 参数锁定 / 歧义消除 | Locked | (Must match Table 1) |

### 🛠️ Table 4: Physical Change Simulation (物理变更预演)

*   **Minimalist Check**: Confirm no changes to unrelated whitespace or comments.
*   **Ripple Effect**: Confirm imports and dependencies do not create circular references.

| 文件路径 | 定位 | 操作 | 简述 | 最小化验证 | 涟漪效应 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `path/to/file` | `func_name` | Modify | 增加重试逻辑 | ✅ 仅修改目标函数 | 无 |

**Caller Refs Annotation** (`操作=Create` 行必填，参见 SKILL.md Step 2.9.2)

| Create 项 ID | caller_file (existing tree) | caller_function | evidence_ref |
| :--- | :--- | :--- | :--- |
| *Example: C-002* | *path/to/existing_file.py* | *EntryClass.run* | *E-003* |

如本 packet 不含任何 `Create` 行，本注解表为空。**禁止 orphan creation**：`caller_file` 必须是当前工作树已存在的文件，不能是同 packet 中其他 `Create` 项创建的新文件。

### ✅ Table 5: Verification Plan (验证计划)

*   **Goal**: Define how to verify the implementation is correct after execution.
*   **Scope**: Each entry corresponds to one or more changes from Table 4.

| 验证步骤 | 方法 | 预期结果 | 回退条件 |
| :--- | :--- | :--- | :--- |
| *Example: Unit test* | `pytest tests/test_auth.py -v` | All tests pass | Revert commit |
| *Example: Integration* | Manual invocation of modified endpoint | Response matches Table 2 invariant | Mark as partial implementation |
| *Example: Regression* | Run full test suite | No new failures | Investigate before proceeding |

---

> **⚠ CHECKPOINT**: All 5 tables are complete. Do **NOT** emit the stop prompt yet. You **MUST** proceed to **Section 5.5 (Evidence Packet Generation)** and execute all 5 steps (timestamp → git commit → ensure directory → write packet → update .active_packet) before outputting the final stop prompt.
