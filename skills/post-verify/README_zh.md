# Post-Verify（后验测试）

Post-Verify 发现已有测试、为未覆盖代码创建临时测试、运行测试、评估分支覆盖率并审计断言质量。它在代码修改之后运行——与 TDD（实现之前运行）互补。

## 何时使用

- `/code-modification` 之后、生成变更日志之前
- 验证代码变更是否被测试覆盖
- 评估被修改函数的测试质量

## 工作流

### Phase 1: 范围识别

通过 `git diff` 或用户指定的目标确定变更内容。构建包含修改/新增的函数、类和方法的变更集。

### Phase 2: 测试发现

加载 `frameworks.json` 中的检测规则，识别项目的测试框架（pytest、jest、go test 等）。通过 grep 将每个变更符号映射到已有的测试文件。

### Phase 3: 测试创建（条件执行）

对无已有测试覆盖的符号，使用 Jinja2 模板（`test_python.py.j2`、`test_javascript.js.j2`、`test_go.go.j2`）生成临时测试。根据 import 需求，临时测试放置在 `/tmp/` 或项目目录中，验证后删除。

测试要求：每个行为一个断言、仅测试公共接口、至少包含 1 个正常路径 + 1 个边界用例 + 1 个错误用例、非外部 I/O 不使用 mock。

### Phase 4: 执行与修复循环

运行测试并对失败进行分类。分类决策树在尝试修复前，先判定失败属于测试缺陷还是实现缺陷。每次修复需通过 `AskUserQuestion` 获得用户确认。循环遵守 `POST_VERIFY_MAX_RETRIES` 限制。

### Phase 5: 覆盖率评估

测量变更函数的分支覆盖率（通过覆盖率工具或静态分析）。阈值：≥ 80%。低于阈值的符号触发额外测试创建。

### Phase 6: 断言质量审计

使用 `anti_patterns.json` 中定义的规则扫描测试文件中的反模式（恒真断言、仅 mock 测试等）。Critical 级别发现阻塞通过。

### Phase 7: 清理

删除 Phase 3 中创建的全部临时测试文件。

### Phase 8: 报告

将结构化报告保存到 `.claude/temp_test/report_{timestamp}.md` 并打印摘要。

## 配置

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `POST_VERIFY_MAX_RETRIES` | `-1`（无限制） | 测试-修复最大迭代次数。`-1` = 无限制。 |

## 相关文件

| 文件 | 用途 |
| :--- | :--- |
| `SKILL.md` | 完整协议定义（由 Claude Code 加载） |
| `frameworks.json` | 测试框架检测规则（用户可扩展） |
| `anti_patterns.json` | 断言反模式规则（用户可扩展） |
| `templates/` | 临时测试生成的 Jinja2 模板 |
| `render.py` | 模板渲染工具（Jinja2 + 回退） |
