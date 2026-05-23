# Deep Plan Analysis Tables Template

You must output your analysis in the following **four** Markdown tables in this exact order. **Add 1 empty line before and after each table.**

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

### ✅ Table 5: Verification Plan (验证计划)

*   **Goal**: Define how to verify the implementation is correct after execution.
*   **Scope**: Each entry corresponds to one or more changes from Table 4.

| 验证步骤 | 方法 | 预期结果 | 回退条件 |
| :--- | :--- | :--- | :--- |
| *Example: Unit test* | `pytest tests/test_auth.py -v` | All tests pass | Revert commit |
| *Example: Integration* | Manual invocation of modified endpoint | Response matches Table 2 invariant | Mark as partial implementation |
| *Example: Regression* | Run full test suite | No new failures | Investigate before proceeding |
