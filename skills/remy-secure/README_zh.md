# remy-secure

面向分支变更的安全代码审查技能。通过多阶段管道（确定性正则预扫描 → 并行分类 Agent → 独立误报过滤）识别高置信度可利用漏洞。

## 使用方式

```
/remy-secure [low|medium|high] [diff_range]
```

### 示例

```bash
/remy-secure                    # medium 级别, origin/HEAD...HEAD
/remy-secure high               # high 级别, origin/HEAD...HEAD
/remy-secure medium HEAD~5...HEAD  # medium 级别, 最近 5 次提交
```

## 分析级别

| 级别 | Phase 0 (正则) | 分类 Agent | 过滤 Agent | 适用场景 |
| :--- | :--- | :--- | :--- | :--- |
| low | 是 | 0 | 0 | 仅确定性模式匹配 |
| medium | 是 | 3 | 最多 15 | 标准 PR 审查 |
| high | 是 | 5 | 最多 15 | 发版前或高风险变更审查 |

## 架构

```
Phase 0: Git 仓库发现 → Diff 提取 → 正则预扫描
                                        ↓
Phase 1: 分类 Agent（并行）        [medium/high]
          ├── A: 注入类（SQL/命令/路径/模板/NoSQL/XXE）
          ├── B: 认证与授权
          ├── C: 数据泄露
          ├── D: 加密与密钥管理        [仅 high]
          └── E: 反序列化/动态执行     [仅 high]
                                        ↓
Phase 2: 误报过滤（并行）          [medium/high]
          └── 每个发现一个 Agent（最多 15 个）
                                        ↓
Phase 3: 置信度阈值（≥ 8/10）→ 报告生成
```

## logic_index 集成

当项目中存在 `.claude/logic_index.db` 时，技能运行 `impact.py` 获取调用关系。该数据注入分类 Agent 的 prompt，支持跨文件数据流追踪（source → sink 分析）。

若逻辑索引不可用，技能降级为纯 diff 分析。

## 配置

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `SECURITY_AUDIT_EFFORT` | `medium` | 默认分析级别 |
| `SECURITY_AUDIT_MAX_FILTER_AGENTS` | `15` | 最大并行过滤 Agent 数 |
| `SECURITY_AUDIT_CONFIDENCE_THRESHOLD` | `8` | 最终报告的最低置信度 |

## 自定义扩展

- `rules/exclusions.json`: 添加/禁用硬排除规则
- `rules/precedents.json`: 添加上下文先例判定
- `rules/patterns.json`: 添加 Phase 0 确定性正则模式

## 报告输出

报告保存至项目目录的 `.claude/temp_secure/security_audit_{timestamp}.md`。
