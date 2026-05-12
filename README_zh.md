<p align="center">
  <img src="remy-assets/logo.svg" width="200" alt="Remy">
</p>

<h1 align="center">Remy</h1>

<p align="center">
  一套为 <a href="https://docs.anthropic.com/en/docs/claude-code">Claude Code</a> 设计的配置方案<br>
  为 AI 编码会话添加结构化工作流、自动上下文维护和行为规则
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>&nbsp;
  <img src="https://img.shields.io/badge/Claude_Code-≥2.1.10-blueviolet" alt="Claude Code ≥2.1.10">&nbsp;
  <img src="https://img.shields.io/badge/Python-3.7+-green.svg" alt="Python 3.7+">
</p>

<p align="center">
  <b>中文</b>&nbsp;|&nbsp;<a href="README.md">English</a>
</p>

---

## Remy-CC 是什么？

在大型项目中，尤其是在接入能力较弱的模型时，Claude Code 可能面临**模型幻觉**（AI Hallucination）或**上下文腐败**（Context Rot）问题。
不良影响包含在未检查依赖和函数签名的情况下修改代码，跳过验证步骤，忽视风险项，或在重构后继续引用已不存在的内容和文件路径。
在跨会话场景中，问题同样存在。尽管 Claude Code 提供了 `/compact` 等命令来压缩上下文，一定程度上保持任务的连续性，
但它们往往会丢失函数签名/接口等结构性细节，也无法持久化保留重要的开发记录和项目架构。
这并不是 Claude Code 的 bug，而是基于上下文窗口的 AI 交互的固有局限。

Remy-CC 针对这些局限，在 Claude Code 之上添加了一层**自动化执行机制**和**结构化工作流**。
它仅使用 Claude Code 原生的扩展接口——[hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) 
和 [CLAUDE.md](https://docs.anthropic.com/en/docs/claude-code/memory#claudemd-files)，不对 Claude Code 本体做任何修改。
此外，Remy-CC 还能够提取项目的文件结构、语义索引和调用关系，持久化地记录开发历史，并将它们注入到 Claude Code 的上下文中，以实现持续的上下文感知和依赖追踪。

整个系统由三个协作层构成：

- **系统提示词**（`CLAUDE.md`、`style.md`、输出风格定义）规定了工程原则、沟通约束和禁止行为，构成会话启动时加载的静态行为基线。
- **运行时钩子**（hooks）在 Claude Code 事件上自动触发——每次工具调用前、每条用户消息发送时、以及会话生命周期的关键节点。它们负责重新注入行为规则以对抗指令衰减、规范路径和 Shell 环境、在文件读取时追加调用者/被调用者上下文，以及保持项目文件树快照的时效性。钩子是持续执行的约束层，无需用户介入。
- **技能**（skills）是需要手动调用的斜杠命令（`/deep-plan`、`/code-modification`、`/auditor` 等），用于执行结构化的多步骤开发任务。每个技能都定义了明确的输入、输出和停止条件。

这三层之间存在设计上的耦合。钩子负责维护技能所依赖的上下文——文件树、语义代码索引、会话历史都通过生命周期事件自动更新。反过来，技能产出的工件（任务包、变更日志、审计报告）也会被钩子在工具调用时校验。例如，`/deep-plan` 写入的任务包会约束 `/code-modification` 允许编辑的文件范围，而 `pre_tool_guard` 钩子在每次 `Edit` 调用时执行这一边界检查。

Remy-CC 不追求全自动化或多智能体协作。所有代码修改类技能都需要用户手动调用，并在关键决策点阻塞等待确认。这一设计的出发点是：在 agent 间传递概要时，函数签名、类型约束等结构性细节容易丢失；而让人始终参与开发回路，能够在每个阶段保持对变更意图和范围的控制。

**具体而言，Remy-CC 提供以下能力：**

- **指令持久化** — 行为规则在每条用户消息时重新注入，在长对话中持续生效，而非逐渐衰减直至失效。
- **依赖感知的代码修改** — 语义逻辑索引记录了函数级调用图数据（Python AST、C/C++/TypeScript tree-sitter），使系统在修改代码前能够追踪上游调用者和下游依赖。
- **自动化上下文维护** — 项目文件树、语义代码索引和会话历史通过生命周期钩子自动更新。文档注入器同步维护 `CLAUDE.md` 中的引用。
- **可组合的验证流水线** — 架构预审 → 代码修改 → 测试验证 → 变更日志 → 上下文回退 → 三方一致性审计，通过 `.claude/temp_task/` 中的 JSON 任务包串联。每个步骤相互独立，按任务复杂度选用。
- **跨会话记忆** — 里程碑系统将结构化历史报告写入时间线索引。新会话加载过滤视图，在不占满上下文窗口的前提下提供连续性。
- **环境一致性** — Shell 编码、路径格式、Conda/Mamba 激活和文件命名规范在每次工具调用时统一执行，不受运行平台影响。

## 功能

### Hooks（自动执行）

| Hook | 触发时机 | 功能 |
| :--- | :--- | :--- |
| 协议注入 | 每次用户消息 | 重新注入行为规则，对抗长对话中的指令衰减 |
| 工具前置防护 | 每次工具调用前 | 将绝对路径转换为相对路径；为 Shell 命令注入 Conda/Mamba 激活和 UTF-8 编码；检查 snake_case 文件命名 |
| 逻辑富化 | Read/Grep/Glob 执行前 | 追加目标文件的调用者/被调用者关系和架构层信息（需要逻辑索引） |
| 生命周期管理 | 会话启动/结束、上下文压缩前 | 重新生成项目树快照和语言指令文件 |
| 文档注入 | 按需触发 | 将项目树、逻辑索引和时间线引用注入 `CLAUDE.md` |

### Skills（手动调用）

标记为 `disable-model-invocation: true` 的 Skills 必须手动调用。每个 Skill 定义了输入、输出和停止条件。

| 命令 | 功能 | 文档 |
| :--- | :--- | :--- |
| `/deep-plan` | 在编写代码前分析架构风险、消除歧义 | [📖](skills/deep-plan/README_zh.md) |
| `/code-modification` | 带依赖追踪和完整性检查的代码修改 | [📖](skills/code-modification/README_zh.md) |
| `/post-verify` | 发现/创建测试、运行测试、评估分支覆盖率和断言质量 | [📖](skills/post-verify/README_zh.md) |
| `/log-change` | 生成结构化变更日志，记录修改内容和影响 | [📖](skills/log-change/README_zh.md) |
| `/auditor` | 验证计划、变更日志与实际代码之间的一致性 | [📖](skills/auditor/README_zh.md) |
| `/milestone` | 生成历史报告并更新项目时间线 | [📖](skills/milestone/README_zh.md) |
| `/update-logic-index` | 解析源代码，生成语义摘要和调用图数据 | [📖](skills/update-logic-index/README_zh.md) |
| `/read-logic-index` | 显示当前逻辑索引 | [📖](skills/read-logic-index/README_zh.md) |
| `/update-tree` | 重新生成项目目录快照 | [📖](skills/update-tree/README_zh.md) |
| `/repo-audit` | 在沙盒临时目录中检查 GitHub 仓库 | [📖](skills/repo-audit/README_zh.md) |
| `/receiving-feedback` | 处理代码审查反馈，先验证再实现 | [📖](skills/receiving-feedback/README_zh.md) |

其他 Skills（调试、TDD、Git 工作流等）根据上下文自动加载，无需手动调用。

#### 计划 → 修改 → 审计 流水线

三个 Skills 通过 `.claude/temp_task/` 目录下的 JSON 任务包串联：

```
/deep-plan                          → 写入任务包
  └→ /code-modification <任务包>    → 以任务包作为变更边界
        └→ /auditor <日志> <任务包> → 三方校验（计划 vs. 日志 vs. 代码）
```

每个步骤相互独立。跳过 `/deep-plan` 会移除对 `/code-modification` 的边界约束，并使 `/auditor` 退化为两方校验（仅日志 vs. 代码）。

### CLI 与配置

安装完成后，`remy-cc` 命令在系统全局可用：

| 命令 | 说明 |
| :--- | :--- |
| `remy-cc ui` | 打开浏览器设置编辑器，编辑 `~/.claude/settings.json` |
| `remy-cc project <路径>` | 打开项目级设置编辑器，编辑 `<路径>/.claude/settings.local.json` |
| `remy-cc update` | 获取并安装最新版本 |
| `remy-cc verify` | 检查安装完整性 |
| `remy-cc version` | 显示版本号 |

设置编辑器提供双语界面（English / 中文），管理 7 组环境变量（LLM API、影响分析、上下文注入、时间线、后验测试、系统、Claude Code）。项目级设置默认继承全局配置，可逐参数覆盖。

## 安装

### 一键安装

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/Till-Crazy-Tears-Us-Apart/Remy-CC/main/install.sh | sh

# Windows (PowerShell)
irm https://raw.githubusercontent.com/Till-Crazy-Tears-Us-Apart/Remy-CC/main/install.ps1 | iex
```

### 从源码安装

```bash
git clone https://github.com/Till-Crazy-Tears-Us-Apart/Remy-CC.git
cd Remy-CC
python install.py                # 默认英文
python install.py --lang zh-CN   # 简体中文
```

安装脚本执行以下操作：
- 将 Hooks、Skills、输出风格和配置文件复制到 `~/.claude/`
- 将 Hook 注册和环境变量合并到 `~/.claude/settings.json`（不覆盖已有值）
- 将 Hook 路径展开为当前机器的绝对路径
- 交互式配置 `/update-logic-index` 使用的 LLM API（URL、模型、API Key）
- 创建 `remy-cc` CLI 命令，可选将其加入系统 PATH

### 更新

```bash
# 一键更新
curl -fsSL https://raw.githubusercontent.com/Till-Crazy-Tears-Us-Apart/Remy-CC/main/install.sh | sh -s -- --update

# 从源码
python install.py
```

### 验证

```bash
python install.py --verify
# 或
remy-cc verify
```

### 卸载

```bash
# 一键卸载
curl -fsSL https://raw.githubusercontent.com/Till-Crazy-Tears-Us-Apart/Remy-CC/main/install.sh | sh -s -- --uninstall

# 从源码
python install.py --uninstall
```

## 快速开始

1. **启动 Claude Code 会话**：在任意项目目录中启动。Hooks 自动激活。
2. **运行 `/update-logic-index`**：为项目生成语义代码索引（需要安装时配置的 LLM API）。
3. **使用 `/deep-plan`**：在重大变更前审查架构风险。
4. **使用 `/milestone`**：定期记录进度到项目时间线。

## 推荐工作流

一次完整的开发循环按以下顺序进行。并非每次修改都需要全部步骤——根据任务复杂度选择。

1. **`/deep-plan`** — 审查架构风险，消除歧义，输出任务包。（[文档](skills/deep-plan/README_zh.md)）
2. **`/code-modification [任务包]`** — 带依赖追踪的代码修改。可选使用任务包作为变更约束。（[文档](skills/code-modification/README_zh.md)）
3. **`/post-verify`** — 运行测试，评估分支覆盖率（≥ 80%），审计断言质量。（[文档](skills/post-verify/README_zh.md)）
4. **`/log-change`** — 生成结构化变更日志，记录变更内容和原因。（[文档](skills/log-change/README_zh.md)）
5. **`/rewind`** — （Claude Code 内置命令）将对话上下文回退到修改前的检查点，消除实现偏见。
6. **`/auditor [日志] [任务包]`** — 校验计划、变更日志与代码之间的一致性。（[文档](skills/auditor/README_zh.md)）
7. **`git commit`** — 提交已验证的变更。
8. **`/milestone`** — 记录历史报告并更新项目时间线。（[文档](skills/milestone/README_zh.md)）
9. **`/update-tree`** — 如文件结构发生变化，刷新项目树快照。（[文档](skills/update-tree/README_zh.md)）

对于小型、低风险的变更，可跳过步骤 3–6。

## 环境要求

| 要求 | 用途 |
| :--- | :--- |
| Claude Code CLI ≥ 2.1.10 | 事件 Hooks 和 Skill 调用 |
| Python 3.7+ | Hook 和安装脚本 |
| OpenAI 兼容的 LLM API | `/update-logic-index` 的语义摘要生成 |
| Conda 或 Mamba（可选） | 存在时自动注入到 Shell 环境 |
| `gh` CLI（可选） | `/repo-audit` 依赖 |
| tree-sitter Python 包（可选） | C/C++/TypeScript 的高精度解析和调用图提取 |

语言可通过 `REMY_LANG` 环境变量配置（`en` 或 `zh-CN`）。

## 目录结构

```text
.
├── install.py                      # 安装脚本（部署、卸载、验证）
├── install.sh                      # macOS/Linux 一键安装
├── install.ps1                     # Windows 一键安装
├── remy-src/                       # CLI 源码
│   ├── cli.py                      # remy-cc 命令分发器
│   ├── config_ui.py                # 浏览器设置编辑器服务端
│   └── config_ui.html              # 设置编辑器前端
├── remy-assets/
│   └── logo.svg
├── CLAUDE.md                       # AI 角色与协议入口
├── language.md                     # 语言指令（安装时生成）
├── style.md                        # 沟通规则与工具约束
├── tools_ref.md                    # Skill 和 Hook 参考索引
├── settings.example.json           # 含 Hook 定义的配置模板
├── output-styles/
│   └── system-architect.md         # 输出风格定义（语气、词汇规则）
├── skills/                         # Skill 定义（按需加载）
│   ├── deep-plan/                  # 架构审查
│   ├── code-modification/          # 代码修改
│   ├── post-verify/                # 测试验证
│   ├── log-change/                 # 变更日志
│   ├── auditor/                    # 一致性审计
│   ├── milestone/                  # 历史报告
│   ├── update-logic-index/         # 语义代码索引
│   ├── read-logic-index/           # 索引查看
│   ├── update-tree/                # 项目树快照
│   ├── repo-audit/                 # 仓库检查
│   ├── receiving-feedback/         # 代码审查处理
│   └── ...                         # 其他 Skills（TDD、调试、Git 等）
└── hooks/                          # 自动事件处理器
    ├── pre_tool_guard.py           # 路径、命名、环境检查
    ├── logic_enrichment_hook.py    # 代码关系上下文注入
    ├── doc_manager/
    │   └── injector.py             # CLAUDE.md 引用注入
    ├── env_system/
    │   ├── enforcer_hook.py        # 行为规则注入
    │   ├── reminder_prompt_en.md   # 规则模板（英文）
    │   └── reminder_prompt_zh.md   # 规则模板（中文）
    └── tree_system/
        ├── generate_smart_tree.py  # 树生成逻辑
        └── lifecycle_hook.py       # 会话生命周期处理
```

## Git 配置

在项目 `.gitignore` 中添加：

```gitignore
.claude/
```

## 鸣谢

本项目中的部分 Skills 借鉴或移植自 **[superpowers](https://github.com/obra/superpowers)** 项目（作者 Jesse Vincent）。
