<p align="center">
  <img src="remy-assets/logo.svg" width="200" alt="Remy">
</p>

<h1 align="center">Remy</h1>

<p align="center">
  <b>为 <a href="https://docs.anthropic.com/en/docs/claude-code">Claude Code</a> 打造的工程约束层——</b><br>
  规则注入、工具拦截、依赖追踪、上下文持久化与结构化工作流，让长会话不再失控。
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>&nbsp;
  <img src="https://img.shields.io/badge/Claude_Code-≥2.1.139-blueviolet" alt="Claude Code ≥2.1.139">&nbsp;
  <img src="https://img.shields.io/badge/Python-3.10+-green.svg" alt="Python 3.10+">
</p>

<p align="center">
  <b>中文</b>&nbsp;|&nbsp;<a href="README.md">English</a>
</p>

---

## ❓ Remy 是什么？

在大型项目中，尤其是在接入能力较弱的模型时，Claude Code 可能面临**模型幻觉**或**上下文腐败**问题。尽管 Claude Code 提供了 `/compact` 等命令，一定程度平衡任务的连续性和上下文窗口的限制，但它们往往会丢失函数签名/接口等结构性细节，也无法持久化保留重要的开发记录和项目架构。

Remy 针对这些局限，在 Claude Code 之上添加了一层**自动化执行机制**和**结构化工作流**。此外，它还能够提取项目的**文件结构、语义索引和调用关系**，持久化地**记录开发历史**，并将它们注入到 Claude Code 的上下文中，以实现持续的上下文感知和依赖追踪。**具体而言，Remy 提供以下能力：**

- **行为规则回顾** — 行为规则在每条用户消息时重新注入，在长对话中持续生效，而非逐渐衰减直至失效。
- **依赖感知的代码修改** — 语义逻辑索引记录了函数级调用图数据（Python AST、C/C++/TypeScript tree-sitter），并提取类继承关系、合成动态分派边。系统在修改代码前能够追踪上游调用者和下游依赖。摘要按 symbol/file/cluster 三层组织，子节点变更通过 LLM 判定是否级联到父层。自适应输出预算根据项目规模自动调整注入上下文的密度（4 级：full → compact → core_only → top_n）。
- **自动化上下文维护** — 项目文件树、语义代码索引和会话历史通过生命周期钩子自动更新。文档注入器同步维护 `CLAUDE.md` 中的引用。
- **可组合的验证流水线** — 架构预审 → 代码修改 → 测试验证 → 变更日志 → 上下文回退 → 三方一致性审计，通过 `.claude/temp_task/` 中的 JSON 任务包串联。每个步骤相互独立，按任务复杂度选用。
- **跨会话记忆** — 里程碑系统将结构化历史报告写入时间线索引。新会话加载过滤视图，在不占满上下文窗口的前提下提供连续性。
- **环境一致性** — Shell 编码、路径格式、Conda/Mamba 激活和文件命名规范在每次工具调用时统一执行，不受运行平台影响。

<p align="center">
  <img src="remy-assets/banner_preview.png" width="700" alt="Remy 启动横幅">
</p>

---

## ✨ 核心功能

### 设计原则

Remy 不追求全自动化或多智能体协作；非只读类技能需要用户手动调用，并在关键决策点阻塞等待确认。这一设计的出发点是：在 agent 间传递概要时，函数签名、类型约束等结构性细节容易丢失；而让人始终参与开发回路，能够在每个阶段保持对变更意图和范围的控制。

### 整体架构

整个系统由四个协作层构成：

- **系统提示词**（`CLAUDE.md`、`style.md`、输出风格定义）规定了工程原则、沟通约束和禁止行为，构成会话启动时加载的静态行为基线。
- **运行时钩子**（hooks）在 Claude Code 事件上自动触发——每次工具调用前、每条用户消息发送时、以及会话生命周期的关键节点。它们负责重新注入行为规则以对抗指令衰减、规范路径和 Shell 环境、在文件读取时追加调用者/被调用者上下文，以及保持项目文件树快照的时效性。钩子是持续执行的约束层，无需用户介入。
- **MCP 服务器**（`remy-src/index_mcp_server.py`）是基于 stdio 的 Model Context Protocol 服务器，会话启动时自动拉起。暴露 12 个代码智能查询 tool（`query_symbol`、`query_symbol_summary`、`query_file_summary`、`query_callers`、`query_callees`、`query_impact`、`query_patterns`、`query_search`、`query_flow`、`query_cluster_summary`、`query_cluster_files`、`query_navigate`），使 Claude 可直接访问语义代码图，无需启动子进程。可用时，注入系统自动切换为 MCP Minimal 模式（约 1 KB），取代完整符号树（约 40 KB）。
- **技能**（skills）是需要手动调用的斜杠命令（`/remy-plan`、`/remy-patch`、`/remy-audit` 等），用于执行结构化的多步骤开发任务。每个技能都定义了明确的输入、输出和停止条件。

这四层之间存在设计上的耦合。钩子负责维护技能所依赖的上下文——文件树、语义代码索引、会话历史都通过生命周期事件自动更新。MCP 服务器与钩子共享 SQLite 数据库（`logic_index.db`，WAL 模式并发读）。反过来，技能产出的工件（任务包、变更日志、审计报告）也会被钩子在工具调用时校验。例如，`/remy-plan` 写入的任务包会约束 `/remy-patch` 允许编辑的文件范围，而 `pre_tool_guard` 钩子在每次 `Edit` 调用时执行这一边界检查。

### Prompts（静态规则）

| 文件 | 内容 |
| :--- | :--- |
| `CLAUDE.md` | 协议入口。引用其它提示词文件，声明反幻觉规则，列出核心 Skills 清单，注入动态上下文（项目树、逻辑索引、时间线） |
| `style.md` | 行为基线。定义角色定位、5 级置信度分层、沟通协议，统一工具调用策略 |
| `tools_ref.md` | 技术执行参考。文件操作流程、Git 工作流、文档同步规则、GitHub CLI 约束 |
| `output-styles/system-architect.md` | 输出风格定义。设定系统架构师角色、工程哲学（SOLID/KISS/DRY/YAGNI）、禁用词汇表、结构化输出模板 |

### Hooks（自动执行）

| Hook | 触发时机 | 功能 |
| :--- | :--- | :--- |
| 协议注入 | 每次用户消息 | 注入简要规则，对抗长对话中的指令衰减 |
| 工具前置防护 | 每次工具调用前 | 将绝对路径转换为相对路径；为 Shell 命令注入 Conda/Mamba 激活和 UTF-8 编码；检查 snake_case 文件命名 |
| 逻辑富化 | Read/Grep/Glob 执行前 | 消费脏文件条目进行增量重解析；追加目标文件的调用者/被调用者关系和架构层信息（需要逻辑索引） |
| 脏文件追踪 | Edit/Write 执行后 | 记录被修改的文件路径，供下次 Read 时增量更新逻辑索引 |
| 生命周期管理 | 会话启动/结束、上下文压缩前 | 重新生成项目树快照和语言指令文件；触发全量结构扫描以刷新符号行号和调用图；可选启动范围选择器 UI 过滤逻辑索引注入内容 |
| 文档注入 | 按需触发 | 将项目树、逻辑索引（经范围选择过滤）和时间线引用注入 `CLAUDE.md` |

### MCP 服务器 [📖](remy-src/MCP_README_zh.md)

`remy-index` MCP 服务器通过 Model Context Protocol 暴露 12 个查询 tool，使 Claude 可直接访问代码智能图，无需启动子进程：

| Tool | 用途 |
| :--- | :--- |
| `query_symbol` | 按名称查找符号定义 — 位置、类型、签名、层 |
| `query_symbol_summary` | 获取符号级摘要和文档字符串 |
| `query_file_summary` | 获取文件级语义摘要（角色、关键符号、所属层） |
| `query_callers` | BFS 上游调用者（支持 `include_ambiguous` 和 `static_only`） |
| `query_callees` | BFS 下游被调用者 |
| `query_impact` | 完整影响分析（等价于 `impact.py` CLI） |
| `query_patterns` | 查询事件/回调注册关系 |
| `query_search` | 模糊符号搜索 — FTS5 前缀 → LIKE 子串 → 编辑距离回退 |
| `query_flow` | 通过双向 BFS 查找命名符号间的调用路径 |
| `query_cluster_summary` | 单/全集群的语义摘要（short / full）与元数据 |
| `query_cluster_files` | 列出指定 cluster 的成员文件（含 layer），可选附短摘要 |
| `query_navigate` | 按自然语言意图跨 cluster → file → symbol 排序，含相关性评分 |

服务器包含**索引新鲜度检测**：每个会话的首次 tool 调用时，比较存储的 `source_commit` 与当前 git HEAD（非 git 项目回退到 struct_hash 抽样）。若 >20% 的文件不一致，在 tool 响应中附加警告。

安装时自动注册到 `~/.claude.json`，Claude Code 会话启动时自动拉起。以只读模式访问 SQLite 逻辑索引。通过 `remy-cc config` 的"MCP 服务器"分组配置参数。

当 MCP 服务器可用时，上下文注入系统自动切换为 **MCP Minimal 模式**——仅注入集群概览和 MCP 工具使用指引（约 1 KB），取代完整符号树（约 40 KB）。Claude 通过 `query_symbol` / `query_callers` / `query_impact` 按需查询详情。通过 `NAV_MCP_MINIMAL_ENABLED`（项目级参数，"上下文注入"分组）控制此行为。

### Skills（手动调用）

标记为 `disable-model-invocation: true` 的 Skills 必须手动调用。每个 Skill 定义了输入、输出和停止条件。

| 命令 | 功能 | 文档（链接） |
| :--- | :--- | :--- |
| `/remy-plan` | 在编写代码前深度分析并制定方案——五表审计含假设清单、场景探测和验证计划 | [📖](skills/remy-plan/README_zh.md) |
| `/remy-patch` | 带依赖追踪、发现检查点和决策日志的代码修改 | [📖](skills/remy-patch/README_zh.md) |
| `/remy-inspect` | 多角度缺陷预判 + 测试执行 + 语义质量审计（支持 low/medium/high 级别） | [📖](skills/remy-inspect/README_zh.md) |
| `/remy-testgen` | 生成持久化单元测试——后补测试（默认）或 TDD 模式，支持多角度 Agent 分析 | [📖](skills/remy-testgen/README_zh.md) |
| `/remy-secure` | 面向分支变更的安全审查——正则预扫描 + 并行分类 Agent + 误报过滤 | [📖](skills/remy-secure/README_zh.md) |
| `/remy-changelog` | 生成结构化变更日志，记录修改内容和影响 | [📖](skills/remy-changelog/README_zh.md) |
| `/remy-audit` | 验证计划、变更日志与实际代码之间的一致性 | [📖](skills/remy-audit/README_zh.md) |
| `/remy-milestone` | 生成历史报告并更新项目时间线 | [📖](skills/remy-milestone/README_zh.md) |
| `/remy-index` | 解析源代码，生成语义摘要和调用图数据 | [📖](skills/remy-index/README_zh.md) |
| `/remy-lookup` | 显示当前逻辑索引 | [📖](skills/remy-lookup/README_zh.md) |
| `/remy-tree` | 重新生成项目目录快照 | [📖](skills/remy-tree/README_zh.md) |
| `/remy-debug` | 仅诊断的调试技能 | [📖](skills/remy-debug/README_zh.md) |
| `/remy-reposcout` | 在沙盒临时目录中检查 GitHub 仓库 | [📖](skills/remy-reposcout/README_zh.md) |
| `/remy-insight` | 多智能体仓库深度分析——支持全局、聚焦和对照模式，可配置分析深度 | [📖](skills/remy-insight/README_zh.md) |
| `/remy-ci` | CI/CD 失败日志分析——编译、链接、测试、sanitizer、QEMU、风格、静态分析、构建配置 | [📖](skills/remy-ci/README_zh.md) |

### 开发与测试

固定版本的开发依赖、本地验证命令、解析器后端覆盖范围和持续集成范围见 [docs/TESTING_zh.md](docs/TESTING_zh.md)。

### 开发循环

一次完整的开发循环按以下顺序进行。并非每次修改都需要全部步骤——根据任务复杂度选择。

0. **`/remy-index`**（**初始化**）：为项目生成语义代码索引（需要安装时配置的 LLM API）。在第一次全量扫描后，后续调用此指令会增量更新索引。（[文档](skills/remy-index/README_zh.md)）
1. **`/remy-plan`** — 审查架构风险，消除歧义，五表审计含验证计划，输出任务包。（[文档](skills/remy-plan/README_zh.md)）
2. **`/remy-patch [任务包]`** — 带依赖追踪的代码修改。可选使用任务包作为变更约束。（[文档](skills/remy-patch/README_zh.md)）
3. **`/remy-testgen`** — 生成单元测试。默认后补测试模式，也可选 TDD 模式（从计划任务包生成红色测试骨架）。（[文档](skills/remy-testgen/README_zh.md)）
4. **`/remy-inspect`** — 多角度缺陷预判、测试执行、分支覆盖率、语义质量审计。支持努力级别。（[文档](skills/remy-inspect/README_zh.md)）
5. **`/remy-changelog`** — 生成结构化变更日志，记录变更内容和原因。（[文档](skills/remy-changelog/README_zh.md)）
6. **`/rewind`** — （Claude Code 内置命令）将对话上下文回退到修改前的检查点，消除实现偏见。
7. **`/remy-audit [日志] [任务包]`** — 校验计划、变更日志与代码之间的一致性。（[文档](skills/remy-audit/README_zh.md)）
8. **`bash (git commit)`** — 提交已验证的变更。
9. **`/remy-milestone`** — 记录历史报告并更新项目时间线。（[文档](skills/remy-milestone/README_zh.md)）
10. **`/remy-tree`**（可选） — 如文件结构发生变化，刷新项目树快照。一般情况下，hooks 会自动更新和注入，无需手动调用。（[文档](skills/remy-tree/README_zh.md)）

对于小型、低风险的变更，可跳过步骤 3–6。

> [!NOTE]
> **计划 → 修改 → 审计 与 三方校验**
>
> 三个 Skills 通过 `.claude/temp_task/` 目录下的 JSON 任务包串联：
>```
>/remy-plan                          → 写入任务包
>  └→ /remy-patch <任务包>           → 以任务包作为变更边界
>        └→ /remy-audit <日志> <任务包> → 三方校验（计划 vs. 日志 vs. 代码）
>```
> 每个步骤相互独立。跳过 `/remy-plan` 会移除对 `/remy-patch` 的边界约束，并使 `/remy-audit` 退化为两方校验（仅日志 vs. 代码）。

> [!NOTE]
> **TDD（红绿重构）vs. 后补测试**
>
> `/remy-testgen` 支持两种工作流，取决于测试相对于实现代码的编写时机：
>```
> TDD 工作流 (--tdd)：
>   /remy-plan → /remy-testgen --tdd <任务包> → /remy-patch <任务包> → /remy-inspect
>   （规划 → 红色测试 → 绿色实现 → 验证）
>
> 后补测试工作流（默认）：
>   /remy-plan → /remy-patch <任务包> → /remy-testgen → /remy-inspect
>   （规划 → 实现 → 生成测试 → 验证）
>```
> TDD 模式从接口规约（计划任务包或桩函数）生成预期失败的测试骨架，并输出任务包供 `/remy-patch` 消费。后补模式读取已有实现代码，生成验证当前行为的测试。两种模式均支持 effort 分级（low/medium/high）和可配置的覆盖率阈值。

> [!NOTE]
> **仓库分析工作流**
>
> 使用 `/remy-insight` 分析外部仓库：
>```
> 1. 克隆仓库（手动克隆，或先用 /remy-reposcout 评估）
> 2. cd 进入仓库目录，启动 Claude Code
> 3. /init                → 生成 CLAUDE.md
> 4. /remy-index          → 生成 logic_index.db（需要 LLM API）
> 5. /clear               → 刷新注入的上下文
> 6. /remy-insight [模式] → 执行分析（global | focus <主题> | compare <文档>）
>```
> `/remy-reposcout` 在克隆前进行浅层元数据评估。`/remy-insight` 在索引完成后进行深度语义分析。

---

## 🚀 快速开始

### 环境要求

| 要求 | 用途 |
| :--- | :--- |
| Claude Code CLI ≥ 2.1.139 | 事件 Hooks 和 Skill 调用 |
| Python 3.10+ | Hook 脚本、安装器、MCP 服务器 |
| OpenAI 兼容的 LLM API | `/remy-index` 的语义摘要生成 |
| Conda 或 Mamba（可选） | 存在时自动注入到 Shell 环境 |
| `gh` CLI（可选） | `/remy-reposcout` 和 `/remy-ci` GitHub Actions 模式依赖 |
| tree-sitter Python 包（可选） | C/C++/TypeScript 的高精度解析和调用图提取 |
| `mcp` Python 包（可选） | remy-index MCP 服务器所需（`pip install mcp`） |

语言可通过 `REMY_LANG` 环境变量配置（`en` 或 `zh-CN`）。

### 安装插件

Remy 支持一键安装脚本：

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/MyPeacefulValentine/Remy-CC/main/install.sh | sh

# Windows (PowerShell)
irm https://raw.githubusercontent.com/MyPeacefulValentine/Remy-CC/main/install.ps1 | iex
```

或者从源码安装：

```bash
git clone https://github.com/MyPeacefulValentine/Remy-CC.git
cd Remy-CC
python install.py                # 默认英文
python install.py --lang zh-CN   # 简体中文
```

安装脚本执行以下操作：
- 将 Hooks、Skills、输出风格和配置文件复制到 `~/.claude/`
- 将 Hook 注册和环境变量合并到 `~/.claude/settings.json`（不覆盖已有值）
- 将 remy-index MCP 服务器注册到 `~/.claude.json`
- 将 Hook 和 MCP 服务器路径展开为当前机器的绝对路径
- 交互式配置 `/remy-index` 使用的 LLM API（URL、模型、API Key）
- 创建 `remy-cc` CLI 命令，可选将其加入系统 PATH

### 命令与配置

安装完成后，`remy-cc` 命令在系统全局可用：

| 命令 | 说明 |
| :--- | :--- |
| `remy-cc ui` | 打开浏览器设置编辑器，编辑 `~/.claude/settings.json` |
| `remy-cc project <路径>` | 打开项目级设置编辑器，编辑 `<路径>/.claude/settings.local.json` |
| `remy-cc logic-scope [--path <目录>]` | 配置会话启动时注入哪些逻辑索引文件 |
| `remy-cc update` | 获取并安装最新版本 |
| `remy-cc uninstall` | 移除所有 Remy 文件和配置 |
| `remy-cc verify` | 检查安装完整性 |
| `remy-cc version` | 显示版本号 |

设置编辑器提供双语界面（English / 中文），管理 13 组环境变量（语义索引、影响分析、上下文注入、时间线、后验测试、安全审计、调试、测试生成、CI/CD、仓库洞察、MCP 服务器、系统、Claude Code）。项目级设置默认继承全局配置，可逐参数覆盖。

---

## 🔗 友情链接

感谢 **[LINUX DO](https://linux.do/)** 社区朋友们的支持与反馈。
