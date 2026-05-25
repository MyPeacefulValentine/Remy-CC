# remy-testgen — 自动化测试生成技能

为现有代码或桩代码生成持久化单元测试。支持后补测试（默认）和 TDD 模式（`--tdd`）。medium/high 级别使用多角度 Agent 并行分析。

## 使用方法

```
/remy-testgen [low|medium|high] [--tdd [packet_file]] [target_files...]
```

### 模式

- **后补测试**（默认）：读取已有实现代码，生成验证当前行为的测试。测试预期 PASS。
- **TDD**（`--tdd`）：从接口签名或 `/remy-plan` 证据包生成失败的测试骨架。测试预期 FAIL（RED 状态）。

### 级别

| 级别 | 策略 | Agent 数量 |
| :--- | :--- | :--- |
| low | 启发式：签名 + 文档字符串 | 0 |
| medium | 行为契约分析 (A) + 边界探测 (B) | 2 |
| high | A + B + 属性测试 (C) | 3 |

### 示例

```bash
/remy-testgen                           # 自动检测变更文件，medium 级别
/remy-testgen high src/auth.py          # 对指定文件使用 high 级别
/remy-testgen --tdd                     # TDD 模式，检测变更文件中的桩函数
/remy-testgen --tdd task_20260525.json  # TDD 模式，使用 remy-plan 证据包
/remy-testgen low src/utils.py          # 仅启发式生成
```

## 工作流链

```
/remy-plan → /remy-testgen --tdd {packet} → /remy-patch {packet} → /remy-inspect
```

## 配置

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `TEST_GEN_EFFORT` | `medium` | 默认级别 |
| `TEST_COVERAGE_THRESHOLD` | `80` | 分支覆盖率目标（与 `/remy-inspect` 共享） |
| `TEST_COVERAGE_MAX_SUPPLEMENT_ROUNDS` | `3` | 覆盖率补充最大轮数 |

## 输出

- **测试文件**：写入项目测试目录（自动检测或用户指定）。
- **报告**：`.claude/temp_testgen/testgen_{timestamp}.md`
- **覆盖率报告**（如拒绝补充）：`.claude/temp_testgen/coverage_{timestamp}.md`
- **TDD 证据包**（仅 TDD 模式）：`.claude/temp_task/testgen_{timestamp}.json` — 传递给 `/remy-patch`。
