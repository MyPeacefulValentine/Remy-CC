# remy-milestone（历史报告）

remy-milestone 在开发周期中创建标准化的阶段报告，并维护项目时间线索引。它执行"先审计、后记录"的工作流，确保技术决策和实验结果被完整记录。

## 何时使用

- 阶段性任务完成后，需要记录技术决策和实验结果
- 执行 `/compact` 之前，保存当前上下文到持久化存储
- 需要回顾项目历史时

## 工作流

### Phase 1: 上下文审计（必需）

生成任何文件前，AI 必须执行审计：

1. 查看 `git log` 确认自上次里程碑以来的全部提交。
2. 对每个修改文件，追踪其上游调用者和下游依赖。
3. 使用 `Read` 验证变更函数的源定义。
4. 在能解释每个变更的"涟漪效应"和"系统影响"之前不得继续。

### Phase 2: 生成草稿

运行 `/remy-milestone`。系统将：

- 执行 `generate_draft.py`，在 `.claude/history/reports/` 中创建带时间戳的报告文件（如 `20260130_103000.md`）。
- 在 `.claude/history/timeline.md` 中新增一行记录。

### Phase 3: 填充内容

AI 按照 `report_schema.json` 中的结构填充报告。必需章节：

| 章节 | 内容 |
| :--- | :--- |
| 1. 摘要 | 工作概述（1-3 句） |
| 2. 技术决策 | 关键架构选择及理由 |
| 3. 实现细节 | 每文件修改详情、数据流角色和涟漪效应 |
| 4. 系统影响分析 | 对数据流、框架、API、性能、并发的影响 |
| 5. 实验与调试 | 测试结果、日志或根因分析 |
| 6. 不变量与 PBT 规约 | 基于属性的测试不变量 |
| 7. 技术债务与后续计划 | 遗留任务和已知风险 |

语言跟随 `REMY_LANG` 环境变量（`en` 或 `zh-CN`）。

### Phase 4: 摘要同步

报告撰写完成后，再次运行 `/remy-milestone`（或 `python "~/.claude/skills/remy-milestone/sync_timeline.py"`）。系统将：

- 读取报告的摘要章节。
- 将摘要回填到 `timeline.md`。
- 根据当前过滤配置重新生成 `.claude/history/timeline_view.md`。

## 内容标准

1. **完整性**：不得为节省 Token 而省略或概括技术细节。
2. **否定知识**：必须记录被否定的假设和失败的尝试。
3. **客观风格**：使用正式的陈述句。禁止不必要的形容词、副词。
4. **认知谦逊**：无经验数据（日志/测试）支持时，不得声明"已修复"或"已解决"。使用"已实现"或"已尝试"。

## 时间线过滤配置

注入 `CLAUDE.md` 的时间线来源于 `timeline_view.md`（`timeline.md` 的过滤视图）。通过 `settings.json` 或 `settings.local.json` 配置：

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `TIMELINE_INJECT_MODE` | `"all"` | 过滤模式 |
| `TIMELINE_INJECT_VALUE` | `""` | 模式参数（见下表） |

| 模式 | VALUE 含义 | 示例 |
| :--- | :--- | :--- |
| `all` | 忽略；注入全部记录 | — |
| `last_n` | 整数；保留最新 N 条 | `"10"` |
| `since_date` | `YYYY-MM-DD`；保留此日期之后的记录 | `"2026-03-01"` |
| `within_days` | 整数；保留最近 N 天内的记录 | `"30"` |

模式非 `all` 时，`timeline_view.md` 会在头部添加元信息行，说明总记录数和可见范围。完整历史始终保留在 `timeline.md` 中。`VALUE` 无效时，脚本回退到 `mode=all` 并输出 stderr 警告。

## 目录结构

```text
.claude/history/
├── timeline.md          # 完整索引表（日期 | ID | 链接 | 摘要）
├── timeline_view.md     # 过滤视图（注入 CLAUDE.md）
└── reports/             # 详细报告存储
    ├── 20260124_xxxx.md
    └── 20260130_xxxx.md
```

## 注意事项

- **无占位符**：`sync_timeline.py` 检测并拒绝包含 `[AI TODO: ...]` 占位符的报告。
- **幂等性**：多次运行生成脚本不会覆盖已有文件（分钟级时间戳去重）。

## 相关文件

| 文件 | 用途 |
| :--- | :--- |
| `SKILL.md` | 完整协议定义（由 Claude Code 加载） |
| `report_schema.json` | 报告章节 Schema |
| `report_template_en.md` | 英文报告模板 |
| `report_template_zh.md` | 中文报告模板 |
| `generate_draft.py` | 草稿生成脚本 |
| `sync_timeline.py` | 时间线同步脚本 |
