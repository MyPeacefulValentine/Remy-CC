<p align="center">
  <img src="logo.svg" width="200" alt="Remy">
</p>

<h1 align="center">Remy</h1>

<p align="center">
  一套为 <a href="https://code.claude.com">Claude Code</a> 设计的工程化配置方案<br>
  通过 Hooks 拦截、提示词注入与结构化协议，约束 AI 在开发任务中的行为边界与合规性
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

## 环境限制

| 维度 | 要求 | 影响行为 |
| :--- | :--- | :--- |
| **软件版本** | Claude Code CLI >= 2.1.10 | 支持事件钩子机制和主动启动 skill |
| **LLM 模型** | OpenAI-compatible (e.g., GLM, Kimi, Qwen) | 按次计费，上下文上限高，支持复杂推理 |
| **操作系统** | Windows 优先 | 兼容路径处理与 Shell 语法 |
| **开发语言** | Python 3.7+ | 运行 Hooks 脚本 |
| **运行时环境** | Mamba / Conda | 自动注入 Shell 环境 |
| **命令行工具** | gh (GitHub CLI) | 仓库审计 (repo-audit) 依赖 |
| **交互语言** | 可配置 (`REMY_LANG`) | 协议头与输出语言由 `REMY_LANG` 环境变量控制（`en` / `zh-CN`） |
| **字符编码** | UTF-8 | 强制标准输入输出编码 |
| **Shell 语法** | POSIX Bash | 限制非标准语法使用 |
| **命名规范** | snake_case | 限制文件名格式 |
| **路径习惯** | 相对路径优先 | 自动转换绝对路径 |

## 核心机制

### 1. 交互约束

- **协议注入**: 通过 `hooks/env_system/enforcer_hook.py` 实现强制 **System Prompt Refreshing**，对抗长上下文带来的指令衰减。
- **中断驱动工作流**: 任何用户提问、条件句或错误报告均被视为 **STOP** 信号。严禁在报错后自动执行"打地鼠"式修复。
- **反俚语过滤器**: 在提示词中注入词汇表，从源头抑制"痛点/赋能"等非工程化词汇。

### 2. 动态文件树 &ensp;[📖 Doc](skills/update-tree/README.md)

- **自动维护**: 基于 `hooks/tree_system/` 自动维护 `.claude/project_tree.md` 快照。
- **生命周期集成**: 在会话启动 (`SessionStart`) 和压缩前 (`PreCompact`) 自动更新，确保 AI 掌握最新结构。
- **自动注入**: 将项目树注入 `CLAUDE.md`，为 AI 提供结构化导航。
- **按需精简**: 支持通过 `.claude/tree_config` 控制目录深度和文件可见性，并节省上下文 Token。
    ```text
    # Example .claude/tree_config
    src/core -depth -1 -if_file true  # 深入索引核心代码，包含文件 （-1 表示无限深度）
    tests/   -depth 1                 # 测试目录仅保留浅层结构
    !legacy/                          # 排除遗留代码目录
    ```

### 3. 环境与路径防护

- **路径归一化**: 通过 `hooks/pre_tool_guard.py` 拦截绝对路径，自动转换为项目内相对路径。
- **Shell 环境增强**: 针对 `Bash` 工具自动注入 `PYTHONIOENCODING` 及 Conda/Mamba 激活脚本。
- **Agent 拦截**: 拦截高耗时 Agent（如 `Explore`）并请求用户确认。

### 4. 上下文持久化 &ensp;[📖 Doc](skills/milestone/README.md)

- **历史索引 (Milestone System)**:
    - **架构**: 采用 "Timeline Index + Report Details" 的双层存储结构。
    - **持久化**: 通过 `/milestone` 命令生成结构化历史报告，并更新 `.claude/history/timeline.md` 索引。
    - **渐进披露**: `CLAUDE.md` 仅引用 Timeline 索引，AI 根据需要按需读取具体的历史报告，从而在保持长期记忆的同时节省 Token 上下文。

### 5. 逻辑索引 &ensp;[📖 Doc](skills/update-logic-index/README.md)

- **更新机制 (`/update-logic-index`)**:
    - **核心功能**: 基于源码解析与 LLM API 推理，生成按架构分层组织的语义摘要，附带函数调用图数据。
    - **架构分层**: 按目录路径模式将文件分组为架构层（API、Service、Data、Utility 等）。用户可通过 `.claude/logic_index_config` 中的 `@layer:` 指令自定义层定义。
    - **调用图**: 提取函数级 caller→callee 关系（Python AST、C/C++/TypeScript tree-sitter），并通过 import map 将 callee 解析为跨文件的 qualified 引用。
    - **多语言支持**: Python（AST）、C/C++（正则回退 + tree-sitter 可选增强）、TypeScript/TSX（正则回退 + tree-sitter 可选增强）。
    - **上下文注入**: 将生成的 `.claude/logic_tree.md` 自动注入到 `CLAUDE.md`，输出按层分组并附带每文件的 imports 注释。
    - **被动富化**: 独立的 PreToolUse Hook (`logic_enrichment_hook.py`) 在 Claude Code 读取项目文件时自动追加 caller/callee/layer 上下文信息。
    - **精准增量**: 支持依赖感知哈希与 **Usage-Aware** 过滤，仅重新分析受实质影响的文件。
    - **版本感知**: 自动记录 Git Commit Hash 与时间戳，确保上下文与代码版本严格对应。
- **手动触发**: 通过 `/update-logic-index` 命令主动刷新逻辑索引，确保其与代码变更同步。
- **触发时机**: 建议在调用时保持项目处于"干净"状态（无未提交变更），以确保索引准确反映当前代码库。

### 6. 开发工作流

本项目定义了严格的 **Plan-Act-Verify** 闭环，以下 Skills 或 Commands 需由用户**主动调用**：

1. **架构预审 (`/deep-plan`)** &ensp;[📖 Doc](skills/deep-plan/README.md)
    - **阶段**: 计划阶段 (Plan)，在编写任何代码之前。
    - **流程**:
        1. **前置依赖检查**: 检测 `.claude/logic_index.json` 是否存在；不存在时询问用户是否执行 `/update-logic-index`，或回退到手动探索。
        2. **Context Saturation**: 若逻辑索引可用，对目标文件执行 `impact.py` 进行 BFS 影响半径分析（按深度输出受影响的文件/函数及跨层警告），强制阅读 Depth 0-1 层全部文件。否则使用 grep/glob 手动探索。
        3. **Ambiguity Elimination**: 识别决策点 -> 批量提问 (`AskUserQuestion`) -> 针对新信息再次搜索 (Loop)。
        4. **Finalize**: 按顺序输出四张核心表格，约定技术细节：
            - `歧义消除矩阵` (决策锁定)
            - `PBT 属性规约` (数学不变量)
            - `逻辑契约审计` (数据流与风险)
            - `物理变更预演` (文件级操作)
        5. **Evidence Packet**: 四张表格生成完毕后，强制将证据链、Git Commit、变更范围写入 `.claude/temp_task/task_{TIMESTAMP}.json`（`AgentTaskPacketLite` 格式），并更新 `.active_packet` 指针。停止提示中附带执行入口：`/code-modification task_{TIMESTAMP}.json`。
    - **功能**: 执行 "零决策" 架构审计，强制识别歧义与副作用；审计完成后输出可被 `/code-modification` 和 `/auditor` 直接消费的持久化任务包。

2. **代码修改 (`/code-modification`)**
    - **阶段**: 执行阶段 (Act)，获得架构批准后。
    - **输入**: 可选参数 `task_packet_file`（由 `/deep-plan` 生成）。
    - **流程**:
        - 若提供 `task_packet_file`：读取 `.claude/temp_task/{task_packet_file}`，以 `evidence_packet.proposed_changes[]` 作为权威变更范围，禁止超出该范围修改；`status: "suspected"` 的证据条目须重新读取确认后方可引用。
        - 若未提供：清除 `.active_packet` 后直接进入发现阶段。
        - **依赖发现**: 若 `.claude/logic_index.json` 存在，执行 `impact.py` 进行确定性依赖追踪；否则回退到 grep/glob 手动追踪。
    - **功能**: 遵循 "Forked Context" 模式，强制执行数据流下游适配、框架完整性检查及防御性编程。

3. **后验测试 (`/post-verify`)**
    - **阶段**: 执行阶段 (Act)，代码修改完成后、变更日志生成前。
    - **输入**: 可选参数 `target_files` 或 `changed_functions`。
    - **流程**:
        1. **Scope Identification**: 提取变更集（`git diff` 或用户指定）。
        2. **Test Discovery**: 通过 `frameworks.json` 探测测试框架，映射已有测试覆盖。
        3. **Test Creation**: 对无覆盖符号，使用 Jinja2 模板生成临时测试（验证后自动清理）。
        4. **Fix Loop**: 失败时执行故障定位（测试缺陷 vs 实现缺陷），须经 `AskUserQuestion` 确认后修改。
        5. **Coverage Assessment**: 要求被修改函数/类分支覆盖率 >= 80%。
        6. **Assertion Quality Audit**: 通过 `anti_patterns.json` 规则扫描断言反模式，Critical 级别阻塞通过。
    - **功能**: 实现阶段完成后的测试覆盖验证，临时测试于验证完成后清理，报告持久化至 `.claude/temp_test/`。

4. **变更固化 (`/log-change`)**
    - **阶段**: 单次修改完成后。
    - **功能**: 生成原子化的变更日志，记录 Q&A 与 Systemic Impact，作为审计阶段的信源之一。

5. **上下文回退 (`/rewind`)**
    - **阶段**: 生成标准化变更日志后。
    - **操作**: 使用 Claude Code 内置的 `/rewind` 命令将对话上下文回退（Restore conversation only）到计划审计完成后、修改执行前的检查点。
    - **功能**: 确保 AI 不持对修改过程的记忆，从而避免在后续交互中引入偏见或误导，保持独立性。

6. **三方审计 (`/auditor`)**
    - **阶段**: 验证阶段 (Verify)，代码合并前。
    - **输入**: 变更日志（必填）+ 可选参数 `task_packet_file`。
    - **验证模式**:
        - 若提供 `task_packet_file`：从 `sender_payload.plan` 和 `sender_payload.analysis` 提取初始计划，执行完整**三方校验**（初始计划 vs 变更日志 vs 实际代码）。
        - 若未提供：执行**两方校验**（变更日志 vs 实际代码），Table 1 的"初始计划"列标记为 `N/A`。
    - **功能**: 扮演 "对抗性审计员"，在无上下文前提下进行一致性校验，识别意图与实现的偏差。

7. **Git 提交**

8. **里程碑报告 (`/milestone`)**
    - **阶段**: 阶段性任务完成或 `/compact` 之前。
    - **功能**: 手动触发生成结构化历史报告，记录技术决策、实验结果与遗留问题，并更新 `.claude/history/timeline.md` 索引。构建长期记忆，实现渐进式历史回顾。

9. **项目树更新 (`/update-tree`)**
    - **阶段**: 文件结构变更后。
    - **功能**: 主动刷新 `.claude/project_tree.md` 快照，确保 AI 掌握最新文件结构。支持按需配置扫描深度。

#### 计划-修改-审计 循环

`/deep-plan`、`/code-modification`、`/auditor` 三个技能通过 `.claude/temp_task/` 目录下的 JSON 任务包形成可循环进行的数据流管道：

```
/deep-plan
  └─→ 写入 .claude/temp_task/task_{TIMESTAMP}.json  （证据链 + 变更范围）
  └─→ 更新 .claude/temp_task/.active_packet
         └─→ /code-modification task_{TIMESTAMP}.json
               （以 proposed_changes[] 作为权威变更约束）
                      └─→ /auditor [log_file] task_{TIMESTAMP}.json
                            （从 sender_payload.plan 提取初始计划，执行三方校验）
```

若跳过 `/deep-plan` 直接调用后续技能，`/code-modification` 将无约束边界运行，`/auditor` 将退化为两方校验（无初始计划列）。

## 目录结构

```text
.
├── install.py                      # 安装脚本 (部署、卸载、验证)
├── CLAUDE.md                       # 系统入口，定义核心 Persona 和静态协议
├── language.md                     # 语言指令（安装时及 SessionStart 时动态生成）
├── style.md                        # 统一协议层 (定义 "Can/Cannot" 边界与 Agent 限制)
├── tools_ref.md                    # 工具参考 (技能与钩子索引)
├── settings.example.json           # 配置文件模板 (含 Hooks 配置)
├── output-styles/                  # 输出风格定义
│   └── system-architect.md         # 工程师角色卡 (定义语气、反模式与词汇表)
├── skills/                         # 动态技能库 (按需加载)
│   ├── deep-plan/                  # 深度计划: 架构预审协议
│   ├── code-modification/          # 代码修改: 工程化改动协议
│   ├── log-change/                 # 日志固化: 变更记录生成
│   ├── post-verify/                # 后验测试: 测试发现、覆盖率评估与断言质量审计
│   ├── auditor/                    # 审计代理: 三方一致性校验
│   ├── milestone/                  # 里程碑: 历史记录与阶段性总结
│   ├── update-tree/                # 树更新: 手动刷新快照 (Proactive 模式)
│   ├── update-logic-index/         # 逻辑索引: 语义摘要生成 (Python/C/C++/TypeScript)
│   │   └── impact.py               # 影响半径: BFS 反向遍历受影响文件/函数分析
│   ├── read-logic-index/           # 逻辑索引: 语义摘要读取
│   ├── repo-audit/                 # 仓库审计: 安全克隆与结构分析 (Sandboxed)
│   └── ...                         # 其他工程化技能 (TDD, Debugging, FileOps 等)
└── hooks/                          # 自动化钩子系统
    ├── doc_manager/                # 文档管理
    │   └── injector.py             # CLAUDE.md 引用注入器
    ├── pre_tool_guard.py           # 工具前置拦截 (路径、命名、环境)
    ├── logic_enrichment_hook.py    # 逻辑上下文富化 (callers/callees/layer)
    ├── env_system/                 # 约束增强系统
    │   ├── enforcer_hook.py        # 协议注入 (UserPromptSubmit)
    │   ├── reminder_prompt_en.md   # 约束提示词 (英文)
    │   └── reminder_prompt_zh.md   # 约束提示词 (中文)
    └── tree_system/                # 项目树自动化
        ├── generate_smart_tree.py  # 核心生成逻辑
        └── lifecycle_hook.py       # 生命周期集成
```

## 安装与配置

### 1. 安装

```bash
git clone https://github.com/Till-Crazy-Tears-Us-Apart/Remy-CC.git
cd Remy-CC
python install.py                # 默认：英文
python install.py --lang zh-CN   # 简体中文
```

安装脚本执行以下操作：
- 将 `hooks/`、`skills/`、`output-styles/` 及配置文件复制到 `~/.claude/`
- 将 `settings.example.json` 中的 hooks、permissions、env 合并到 `~/.claude/settings.json`（不覆盖已有值）
- 自动将 hook 路径展开为当前机器的绝对路径
- 根据 `--lang` 参数设置 `settings.json` 中的 `REMY_LANG` 值并生成 `language.md`（默认：`en`）
- 交互式配置 Logic Index 的 LLM API（URL、模型、API Key）；可选测试连通性
- 检测 tree-sitter 是否已安装，未安装时询问是否安装（C/C++/TypeScript 高精度解析，可选）

### 2. 验证

```bash
python install.py --verify
```

### 3. 卸载

```bash
python install.py --uninstall
```

### 4. Git 配置 (推荐)

为了保持项目整洁，建议将自动生成的元数据目录加入 `.gitignore`：

```gitignore
.claude/
```

## 鸣谢

本项目中的部分 Skills 借鉴或移植自 **[superpowers](https://github.com/obra/superpowers)** 项目。感谢 Jesse Vincent (obra) 的贡献。
