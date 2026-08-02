# remy-index（语义代码索引）

remy-index 是一个基于多语言源码解析和 OpenAI 兼容 API 的语义索引工具。它解析 Python、C、C++ 和 TypeScript/TSX 代码，生成按架构分层组织的语义摘要和调用图数据，使 Claude Code 无需阅读完整源码即可理解项目结构和函数关系。

## 何时使用

- 项目初始化后，建立代码库的结构认知
- 重大重构后，刷新函数关系和架构层信息
- 新增语言/模块后，将其纳入索引

## 架构概览

系统分两个 Stage、四层运作：

```
Stage 1: 结构扫描（无 LLM，Hook 驱动）
┌──────────────────────────────────────────────────────────┐
│  struct_scan.py（稳定CLI和导入入口）                     │
│  ├── schema.py：当前SQLite schema契约                    │
│  ├── symbol_names.py：共享名称拆词                       │
│  ├── migrations.py：数据库初始化与migration阶梯          │
│  └── scanner.py：事实提取、图后处理、全量与增量扫描      │
│  触发：SessionStart、PreCompact（全量扫描）              │
│        PreToolUse 脏文件消费（增量扫描）                 │
└──────────────────────────────────────────────────────────┘

Stage 2: LLM 摘要生成（依赖 API，手动调用）
┌──────────────────────────────────────────────────────────┐
│  run.py（LLM 索引器）                                    │
│  ├── 将 Stage 1 委托给 struct_scan.py                    │
│  ├── 为脏符号生成语义摘要                                │
│  └── 将结构事实和摘要保存到logic_index.db                 │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  logic_index.db（注入 CLAUDE.md）                         │
│  ├── 架构层分组（文件按层归类）                          │
│  ├── 文件级摘要 + imports 注释                           │
│  └── 符号级签名 + 摘要                                   │
│  用途：基线认知（结构、角色、依赖）                      │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  logic_index.db（磁盘缓存，不注入）                    │
│  ├── 符号哈希 + 摘要缓存                                 │
│  ├── struct_hash（文件级原始源码指纹）                   │
│  ├── end_lineno（符号结束行号，用于精准 Read）           │
│  ├── 文件级 imports 列表                                 │
│  ├── 文件层分配                                          │
│  └── 函数级 CALLS 边（含 callee 解析）                   │
│  用途：Hook 查询源 + 增量构建缓存                        │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  Hooks（自动化管道）                                     │
│  ├── PostToolUse：脏文件追踪器记录 Edit/Write 目标       │
│  ├── PreToolUse：富化 hook 消费脏文件，触发增量          │
│  │   struct_scan，追加 callers/callees/layer +           │
│  │   [L{start}-L{end}] 行号范围                          │
│  └── Lifecycle：SessionStart/PreCompact 全量 struct_scan │
│  用途：无需手动调用即可持续维护结构准确性                │
└──────────────────────────────────────────────────────────┘
```

结构扫描和语义摘要写入者共用项目级进程锁。`struct_scan.py`保留既有CLI和Python导入，`schema.py`、`symbol_names.py`、`migrations.py`和`scanner.py`提供内部实现。安装器递归部署整个`skills/remy-index/`目录，因此这些模块会一同安装。结构扫描返回`success`、`partial`或`failed`，对应退出码`0`、`2`和`1`。脏路径通过可恢复的processing快照处理；只有完成结构扫描和全局后处理的路径才会被确认。

## 支持的语言

| 语言 | 扩展名 | 解析方式 | 调用图 |
| :--- | :--- | :--- | :--- |
| Python | `.py` | 标准库 `ast` 模块（内置） | 支持（AST） |
| C | `.c`, `.h` | 正则（内置）/ tree-sitter（可选） | 仅 tree-sitter |
| C++ | `.cpp`, `.hpp`, `.cc`, `.cxx`, `.hh`, `.hxx` | 正则（内置）/ tree-sitter（可选） | 仅 tree-sitter |
| TypeScript | `.ts`, `.tsx` | 正则（内置）/ tree-sitter（可选） | 仅 tree-sitter |

`.h` 文件自动检测：若包含 C++ 关键字（`class`、`namespace`、`template` 等），使用 C++ 解析。

## 功能说明

### 架构分层

文件按目录路径模式分组为架构层。默认层定义：

| 层 | 匹配模式 |
| :--- | :--- |
| API Layer | routes, controller, handler, endpoint, api |
| Service Layer | service, usecase, use-case, business |
| Data Layer | model, entity, schema, database, db, migration, repository, repo |
| UI Layer | component, view, page, screen, layout, widget, ui |
| Middleware Layer | middleware, interceptor, guard, filter, pipe |
| External Services | client, integration, external, sdk, vendor, adapter |
| Background Tasks | worker, job, queue, cron, consumer, processor, scheduler, background |
| Utility Layer | util, helper, lib, common, shared |
| Test Layer | test, spec, \_\_test\_\_, \_\_spec\_\_, \_\_tests\_\_, \_\_specs\_\_ |
| Configuration Layer | config, setting, env |
| Core | （未匹配的文件） |

匹配规则：文件路径按 `/` 拆分为目录段，与模式大小写不敏感比较（复数形式自动匹配 `+s`）。首次匹配生效。

层定义可在 `.claude/logic_index_config` 中使用 `@layer:Name=pattern1,pattern2,...` 语法自定义。

### 调用图提取

提取每个文件内的 caller-to-callee 关系：

- **Python**：使用标准库 `ast` 模块和函数栈模式。处理 `ast.Name`（简单调用）和 `ast.Attribute`（方法调用）。
- **C/C++/TypeScript**：使用 tree-sitter（可用时），采用相同的函数栈模式。正则模式不提取调用图（需要 AST 精度）。

提取后，`_resolve_call_edges` 通过文件的 import 列表和目标文件的缓存符号数据，将 callee 名称解析为 qualified 引用（如 `models/user.py::User.verify_password`）。

### 被动富化 Hook

`hooks/logic_enrichment_hook.py` 是一个 PreToolUse Hook，在 Read/Glob/Grep 操作时触发。它首先消费脏文件条目（由 PostToolUse 脏文件追踪器在 Edit/Write 操作后写入），为受影响的文件触发增量 `struct_scan`，然后查询 `logic_index.db` 并输出：

```
[Logic Context] services/auth.py (Service Layer)
  Calls into: models/user.py::User.verify_password [L42-L68], utils/token.py::generate_jwt [L15-L30]
  Called by: routes/login.py::handle_login, routes/register.py::handle_register
```

`[L{start}-L{end}]` 行号范围使 `remy-plan` 和 `remy-patch` Skill 能对超过 `PRECISION_READ_THRESHOLD`（默认 500 行）的文件使用偏移 `Read()`，避免全量读取大文件。

无需 Claude Code 主动调用 MCP 工具即可获取关系上下文。

### 多语言解析

- **AST 解析（Python）**：识别 Class、Function 和 Method 结构。
- **正则 + tree-sitter 双路径（C/C++/TypeScript）**：默认零依赖正则模式；安装 tree-sitter 后自动切换到高精度模式。

### 跨文件上下文

- 解析 Python `import`、C/C++ `#include "..."`、TypeScript 相对 `import` 依赖。
- 将上游模块摘要注入 LLM 提示词，实现上下文感知的摘要生成。
- 在 `logic_index.db` 输出中显示每文件的 import 列表。

### 增量更新

- **文件级哈希**：基于 MD5 的源码内容哈希。
- **结构哈希（`struct_hash`）**：原始源码 MD5（与符号级 `hash` 独立）。任何字节变更（包括空白或注释编辑）都会触发结构重解析以刷新行号和调用边。未变更文件被跳过。
- **注释不敏感的符号哈希**：检查函数的 LLM 摘要是否需要重新生成时，从源码中剥离注释（`#`、`//`、`/* */`）后再计算哈希。修改注释或添加行内注释不会触发不必要的 API 调用。Docstring 和 Doxygen 注释保留在哈希中（它们影响摘要内容）。注释剥离失败时回退到完整源码哈希。
- **依赖感知哈希**：上游摘要变更触发下游重新分析。
- **使用感知过滤**：仅在引用的符号在当前文件中被实际使用时触发更新。

### 混合摘要策略

- **Docstring/Doxygen 优先**：自动提取 Python docstring 和 C/C++ Doxygen 注释（`[Doc]` 标签），零 API 开销。
- **短函数跳过**：3 行以下且无文档的函数自动标记（可配置）。
- **LLM 语义增强**：仅对复杂逻辑调用 LLM API。
- **数据流追踪**：要求 LLM 识别数据源 `[Source]` 和数据汇 `[Sink]`。

### 容错机制

- **原子回退**：批量处理失败自动降级为逐符号模式。
- **截断恢复**：检测 API 响应截断并自动重试。
- 内置指数退避、熔断器（429/401 自动停止）和检查点保护。

## 工作流（4 阶段）

### Phase 1: 检查配置

Skill 检查 `.claude/logic_index_config` 是否存在。不存在时从默认模板创建（包含层定义和排除规则），并提示用户在继续前审查。

### Phase 2: 执行扫描

运行 Python 索引器：

```bash
python "~/.claude/skills/remy-index/run.py"
```

首次运行（无 `.claude/logic_index.db`）时执行全量扫描。索引器：
1. 遍历项目树，逐文件解析符号和调用图
2. 通过 import 映射将 callee 名称解析为 qualified 引用
3. 为无文档的符号生成 LLM 摘要
4. 将结果保存到 `.claude/logic_index.db`（缓存）和 `.claude/logic_index.db`（输出）

### Phase 3: 层级摘要引导确认（条件触发）

若 `run.py` stdout 包含 `BOOTSTRAP_PENDING_CONFIRMATION`，Skill 通过 `--bootstrap-only --mode auto` 询问用户是否生成 file/cluster 摘要。仅当 `REMY_SUMMARY_BOOTSTRAP_MODE=ask` 时触发（显式设置或从 `auto` 降级）。该行缺失时跳过。

### Phase 4: 注入策略

根据 `REMY_LOGIC_INDEX_AUTO_INJECT` 策略：

| 策略 | 行为 |
| :--- | :--- |
| `ALWAYS`（默认） | 自动将 `logic_index.db` 注入 `CLAUDE.md` |
| `ASK` | 注入前提示用户确认 |
| `NEVER` | 仅生成文件，不注入 |

### 范围选择（注入过滤）

对于 `logic_index.db` 超出上下文窗口预算的大型项目，范围选择器可过滤注入的文件。文档注入器基于 `.claude/logic_inject_selection.json` 中的用户选择，生成 `logic_tree_view.md` —— `logic_index.db` 的过滤子集。

配置方式：
- **SessionStart UI**：当 `REMY_LOGIC_INDEX_INTERACTIVE` 为 `true` 时，会话启动（startup/clear/compact 事件）时弹出浏览器选择器 UI。用户可勾选/取消文件和层以控制注入范围。
- **CLI**：随时运行 `remy-cc logic-scope [--path <目录>]` 打开选择器。
- **存档**：选择器支持保存/加载命名配置存档（上限 20 个），方便在不同范围配置间切换。

若选择文件不存在，则注入完整的 `logic_index.db`（等同于选择所有文件）。

## 输出格式

`logic_index.db` 结构如下：

```markdown
## 🏗️ API Layer
### 📄 `routes/auth.py`
> Imports: models/user.py, services/auth_service.py
- **[f]** `login(request)`: 处理登录请求
- **[f]** `register(request)`: 处理注册请求

## 🏗️ Service Layer
### 📄 `services/auth_service.py`
> Imports: models/user.py, utils/token.py
- **[f]** `verify_credentials(email, password)`: 验证用户凭据
- **[f]** `create_session(user)`: 创建会话令牌

## 🏗️ Core
### 📄 `main.py`
> Imports: routes/auth.py, config.py
- **[f]** `main()`: 应用入口
```

## 安装 tree-sitter（可选）

C/C++ 和 TypeScript/TSX 解析默认使用正则模式（零依赖）。安装 tree-sitter 可获得更高精度和调用图提取：

```bash
pip install tree-sitter tree-sitter-c tree-sitter-cpp tree-sitter-typescript
```

**C/C++**：

| 功能 | 正则模式 | tree-sitter 模式 |
| :--- | :--- | :--- |
| 函数/结构体/枚举/宏 | 支持 | 支持 |
| 类方法 | 支持 | 支持 |
| 命名空间嵌套 | 仅外层 | 全部层级 |
| 模板类 | 不支持 | 支持 |
| 调用图提取 | 不支持 | 支持 |

**TypeScript/TSX**：

| 功能 | 正则模式 | tree-sitter 模式 |
| :--- | :--- | :--- |
| function/class/interface/enum/type/namespace | 支持 | 支持 |
| 箭头函数（`export const foo = () => {}`） | 不支持 | 支持 |
| 抽象类方法 | 不支持 | 支持 |
| 嵌套命名空间成员 | 不支持 | 支持 |
| 调用图提取 | 不支持 | 支持 |

## 配置

### Remy配置

用户默认值写入`~/.claude/remy-config.json`，项目覆盖写入
`<project>/.claude/remy-config.json`。`remy-cc config`编辑用户配置，
`remy-cc config --path <project>`编辑项目配置。同名`REMY_*`进程环境变量
只覆盖当前进程树中的文件配置。

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `REMY_LLM_API_KEY` | — | API密钥；只允许用户配置或进程环境 |
| `REMY_LLM_MODEL` | `deepseek-v4-flash` | 模型名称 |
| `REMY_LLM_BASE_URL` | `https://api.deepseek.com/v1/chat/completions` | API端点 |
| `REMY_LLM_MAX_WORKERS` | `5` | 并发线程数 |
| `REMY_LLM_RETRY_LIMIT` | `3` | 重试次数 |
| `REMY_LLM_TIMEOUT` | `300` | 超时秒数 |
| `REMY_LLM_MAX_TOKENS` | `32768` | 响应Token上限 |
| `REMY_REMY_LOGIC_INDEX_AUTO_INJECT` | `ALWAYS` | `ALWAYS` / `ASK` / `NEVER` |
| `REMY_LOGIC_INDEX_FILTER_SMALL` | `false` | 跳过无文档小函数的LLM摘要 |
| `REMY_REMY_LOGIC_INDEX_INTERACTIVE` | `true` | SessionStart时启动范围选择器 |
| `REMY_LOGIC_SCOPE_TIMEOUT` | `300` | 范围选择器超时秒数 |
| `REMY_LANG` | `en` | 摘要输出语言（`en` / `zh-CN`） |
| `REMY_STRUCT_SCAN_TIMEOUT` | `60` | 生命周期结构扫描超时秒数 |

`PRECISION_READ_THRESHOLD`继续作为Claude技能协议参数保留在`settings.json`，
不属于Python运行时Remy配置。

### 配置文件（`.claude/logic_index_config`）

两种指令类型：

**排除规则**（`!` 前缀）：语法类似 `.gitignore`，支持通配符。

```text
!tests/
!**/migrations/
!**/CMakeFiles/
!**/*.o
```

**层定义**（`@layer:` 前缀）：将文件分配到架构层。

```text
@layer:API Layer=routes,controller,handler,endpoint,api
@layer:Service Layer=service,usecase,use-case,business
@layer:Data Layer=model,entity,schema,database,db,migration,repository,repo
```

## 符号类型

| 图标 | 含义 | 语言 |
| :--- | :--- | :--- |
| `[C]` | 类 | Python, C++, TypeScript |
| `[f]` | 函数 | Python, C, C++, TypeScript |
| `[S]` | 结构体 | C, C++ |
| `[E]` | 枚举 | C, C++, TypeScript |
| `[T]` | Typedef / TypeAlias | C, C++, TypeScript |
| `[M]` | 宏 | C, C++ |
| `[N]` | 命名空间 | C++, TypeScript |
| `[I]` | 接口 | TypeScript |

## API 开销控制

- **Docstring/Doxygen 优先**：有文档的符号零 API 开销。
- **短函数跳过**：3 行以下且无文档的函数自动标记。
- **依赖感知增量更新**：仅在实际变更时重新生成。

## 常见问题

### Q: `Fatal API Error 429: Rate limit exceeded`？
将`REMY_LLM_MAX_WORKERS`设为`1`（串行模式），或申请更高配额。

### Q: `Fatal API Error 403: Forbidden`？
检查`REMY_LLM_API_KEY`是否正确，`REMY_LLM_MODEL`在服务端是否可用。

### Q: 中断后会丢失进度吗？
不会。`try...finally` 保护机制确保已生成的摘要保存到 `.claude/logic_index.db`。

### Q: C/C++/TypeScript 调用图未提取？
安装 `tree-sitter` 包。调用图提取需要 AST 精度，正则模式无法提供。Python 调用图使用标准库 `ast`，无需 tree-sitter。

### Q: 层分配不正确？
编辑 `.claude/logic_index_config` 自定义层模式。删除不需要的行并添加自定义规则。未匹配的文件默认归入 "Core"。

### Q: Hook 富化信息未出现？
确认 `logic_enrichment_hook.py` 已在 `~/.claude/settings.json` 的 `hooks.PreToolUse` 中注册，matcher 为 `Read|Glob|Grep`。运行 `python install.py --verify` 检查。
