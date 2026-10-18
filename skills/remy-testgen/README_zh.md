# remy-testgen — 自动化测试生成技能

为现有代码或桩代码生成持久化单元测试。支持后补测试（默认）和 TDD 模式（`--tdd`）。medium/high 级别使用多角度 Agent 并行分析。

## 使用方法

```
/remy-testgen [low|medium|high] [--tdd [packet_file]] [target_files...]
```

### 模式

- **后补测试**（默认）：区分当前行为的现状刻画与明确要求的符合性；失败须区分实现、生成测试和环境，不能默认实现正确或为通过而削弱断言。
- **TDD**（`--tdd`）：使用接口要求或原 `/remy-plan` 包；只有证明缺失行为的失败才是对应 RED。已通过回归项可并存，语法、导入和环境失败不算业务 RED。

原任务包、报告字段、effort 和覆盖率流程保持。要求来源、责任边界和未知通过文本传给三个代理模板；TDD 生产者仍在获准产包后更新 `.active_packet`，包内容本身不授予实施权限。

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
- **报告**：`.claude/temp_testgen/testgen_{timestamp}.md`；在实际保存的报告中补充测试用途、要求来源、失败分类和未验证前提的正文说明，不改变报告 JSON。
- **覆盖率报告**（如拒绝补充）：`.claude/temp_testgen/coverage_{timestamp}.md`
- **TDD 证据包**（仅 TDD 模式）：`.claude/temp_task/testgen_{timestamp}.json` — 传递给 `/remy-patch`。
