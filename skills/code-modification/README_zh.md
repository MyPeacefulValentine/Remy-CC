# Code Modification（工程化代码修改）

Code Modification 通过依赖追踪、框架完整性检查和增量变更约束来执行代码修改。它以"隔离上下文"方式运行——AI 独立发现调用链和依赖关系，而非依赖对话记忆。

## 何时使用

- `/deep-plan` 审批后，执行有边界约束的计划变更
- 修改、重构或优化现有代码
- 存在来自 `/deep-plan` 的任务包时（可选，复杂变更推荐使用）

## 工作流

### Phase 0: 任务包加载（条件执行）

如果提供了 `task_packet_file` 参数，Skill 读取 `.claude/temp_task/{task_packet_file}`，以 `proposed_changes[]` 作为权威变更范围。禁止超出该范围的修改。未提供任务包时，进入自由发现模式。

### Phase 1: 依赖发现

1. 检查 `.claude/logic_index.json` 是否存在。存在时对目标文件运行 `impact.py`，生成双向依赖报告（上游调用者和下游被调用者）。
2. 逻辑索引不可用时，回退到基于 grep/glob 的手动追踪。
3. 读取上游深度 1 和下游深度 1 的全部文件。
4. 验证待使用的外部函数签名。

### Phase 2: 框架合规检查

检查修改代码的 JIT/Numba 兼容性和 numpy/JAX 数组操作安全性。

### Phase 3: 执行

对每个文件执行：预读取 → 编辑 → 后读取验证。

### Phase 4: 验证

运行计划中指定的测试。

## 流水线集成

此 Skill 是 计划 → 修改 → 审计 流水线的第二阶段：

```
/deep-plan → 任务包 → /code-modification → /auditor
```

有任务包时，Skill 执行严格的变更边界约束。无任务包时，无约束运行。

## 相关文件

| 文件 | 用途 |
| :--- | :--- |
| `SKILL.md` | 完整协议定义（由 Claude Code 加载） |
| `../update-logic-index/impact.py` | 依赖追踪脚本（Phase 1 调用） |
