# Log Change（变更日志）

Log Change 生成结构化变更日志，记录修改内容、Q&A 决策和系统影响。变更日志作为 `/remy-audit` 的审计来源，同时为 `/rewind` 提供上下文保存。

## 何时使用

- 每次代码修改后，记录变更内容和原因
- 运行 `/remy-audit` 之前（审计需要变更日志作为输入）

## 工作流

### Phase 1: 输入分析

读取 `git diff --staged` 获取当前变更集（摘要和详情）。

### Phase 2: 构建上下文

按照 `output_schema.json` 构建上下文字典，包含：

- 任务 ID 和状态
- Q&A 对（会话中提出的问题和做出的决策）
- 每文件修改详情（摘要、原因、数据流角色、涟漪效应、行级逻辑说明）
- 系统影响（数据流、功能层级、框架、API 一致性、性能）
- 验证状态（通过的测试、手动检查）

### Phase 3: 渲染

使用 `render.save_changelog(project_root, context)` 通过 Jinja2 模板（或字符串格式化回退）生成变更日志，保存到 `.claude/temp_log/`。

## 输出格式

变更日志保存为 `.claude/temp_log/_temp_{task_id}_{timestamp}.md`。语言跟随 `REMY_LANG`。

## 内容标准

1. **完整性**：不得概括或省略技术细节。
2. **否定知识**：记录被否定的假设和失败的尝试。
3. **客观风格**：正式的陈述句。禁止形容词、副词。
4. **认知谦逊**：未验证的变更使用"已实现"或"已尝试"，而非"已修复"或"已解决"。

## 相关文件

| 文件 | 用途 |
| :--- | :--- |
| `SKILL.md` | 完整协议定义（由 Claude Code 加载） |
| `output_schema.json` | 上下文字典结构定义 |
| `render.py` | 模板渲染工具（Jinja2 + 回退） |
| `templates/changelog.md.j2` | 变更日志的 Jinja2 模板 |
