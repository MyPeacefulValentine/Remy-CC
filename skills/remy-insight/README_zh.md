# remy-insight

多智能体并行视角的仓库深度分析。

## 前置条件

1. 运行 `/init` 生成 `CLAUDE.md`
2. 运行 `/remy-index` 生成 `logic_index.json`
3. 运行 `/clear` 刷新注入的上下文

## 用法

```
/remy-insight [模式] [选项]
```

### 模式

| 模式 | 语法 | 说明 |
| :--- | :--- | :--- |
| 全局 | `/remy-insight global` | 跨所有维度的完整仓库分析 |
| 聚焦 | `/remy-insight focus <主题>` | 针对特定模块或子系统的聚焦分析 |
| 对照 | `/remy-insight compare <文档路径>` | 文档与代码的一致性检查 |

### 选项

| 选项 | 取值 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `--depth` | `light`、`standard`、`deep` | `standard` | 分析深度级别 |
| `--with` | section 名称 | — | 追加额外的分析维度（仅 focus 模式） |

### 深度级别

- **light**：2 个 Agent（仅 architecture + improvement），无对抗性验证
- **standard**：按活跃 section 数 2-5 个 Agent，对 issue 级 finding 单 Agent 反驳
- **deep**：每个视角 2-3 个实例（含偏置多样性），对 concern/issue 级 finding 3 Agent 投票

## 示例

```
/remy-insight global
/remy-insight global --depth deep
/remy-insight focus authentication
/remy-insight focus video-pipeline --with robustness
/remy-insight compare docs/design.md
/remy-insight README.md
```

## 输出

报告保存至 `.claude/temp_insight/insight_{timestamp}.md`。

## 配置

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `INSIGHT_DEFAULT_DEPTH` | `standard` | 未指定 `--depth` 时的默认深度 |
| `INSIGHT_MAX_CUSTOM_ANGLES` | `2` | 用户可定义的自定义分析视角上限 |
| `INSIGHT_MAX_AGENTS` | `30` | 单次运行的 Agent 总数上限 |

## 与其他技能的关系

| 技能 | 关系 |
| :--- | :--- |
| `/remy-index` | 数据源 — insight 消费 `logic_index.json` |
| `/remy-reposcout` | 上游 — reposcout 做浅层侦察，insight 做深度研究 |
| `/remy-secure` | 健壮性维度部分重叠；insight 给出高层面观察 |

## 批次状态

**当前：Batch 1** — 含交叉引用标注的多维度分析。

Batch 2（计划中）：共识检测、完整文档-代码审计（含 claim 提取）、`.pdf` 支持、多文件 `.tex` 支持。
