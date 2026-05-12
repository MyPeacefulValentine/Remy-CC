# Logic Indexer v3（语义代码索引）

Logic Indexer 是一个基于多语言源码解析和 OpenAI 兼容 API 的语义索引工具。它解析 Python、C、C++ 和 TypeScript/TSX 代码，生成按架构分层组织的语义摘要和调用图数据，使 Claude Code 无需阅读完整源码即可理解项目结构和函数关系。

## 何时使用

- 项目初始化后，建立代码库的结构认知
- 重大重构后，刷新函数关系和架构层信息
- 新增语言/模块后，将其纳入索引

## 架构概览

系统分三层运作：

```
┌──────────────────────────────────────────────────────────┐
│  logic_tree.md（注入 CLAUDE.md）                          │
│  ├── 架构层分组（文件按层归类）                              │
│  ├── 文件级摘要 + imports 注释                              │
│  └── 符号级签名 + 摘要                                     │
│  用途：基线认知（结构、角色、依赖）                           │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  logic_index.json（磁盘缓存，不注入）                       │
│  ├── 符号哈希 + 摘要缓存                                   │
│  ├── 文件级 imports 列表                                   │
│  ├── 文件层分配                                            │
│  └── 函数级 CALLS 边（含 callee 解析）                      │
│  用途：Hook 查询源 + 增量构建缓存                           │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  PreToolUse Hook（被动富化）                                │
│  触发：Claude Code 执行 Read/Grep/Glob                     │
│  行为：查询 logic_index.json，追加目标文件的                  │
│        callers/callees/layer 信息到 hook 输出              │
│  用途：按需获取关系信息，无需 MCP                            │
└──────────────────────────────────────────────────────────┘
```

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

`hooks/logic_enrichment_hook.py` 是一个 PreToolUse Hook，在 Read/Glob/Grep 操作时触发。它查询 `logic_index.json` 并输出：

```
[Logic Context] services/auth.py (Service Layer)
  Calls into: models/user.py::User.verify_password, utils/token.py::generate_jwt
  Called by: routes/login.py::handle_login, routes/register.py::handle_register
```

无需 Claude Code 主动调用 MCP 工具即可获取关系上下文。

### 多语言解析

- **AST 解析（Python）**：识别 Class、Function 和 Method 结构。
- **正则 + tree-sitter 双路径（C/C++/TypeScript）**：默认零依赖正则模式；安装 tree-sitter 后自动切换到高精度模式。

### 跨文件上下文

- 解析 Python `import`、C/C++ `#include "..."`、TypeScript 相对 `import` 依赖。
- 将上游模块摘要注入 LLM 提示词，实现上下文感知的摘要生成。
- 在 `logic_tree.md` 输出中显示每文件的 import 列表。

### 增量更新

- **文件级哈希**：基于 MD5 的源码内容哈希。
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

## 工作流（3 步）

### 步骤 1: 检查配置

Skill 检查 `.claude/logic_index_config` 是否存在。不存在时从默认模板创建（包含层定义和排除规则），并提示用户在继续前审查。

### 步骤 2: 执行扫描

运行 Python 索引器：

```bash
python "~/.claude/skills/update-logic-index/run.py"
```

首次运行（无 `.claude/logic_tree.md`）时执行全量扫描。索引器：
1. 遍历项目树，逐文件解析符号和调用图
2. 通过 import 映射将 callee 名称解析为 qualified 引用
3. 为无文档的符号生成 LLM 摘要
4. 将结果保存到 `.claude/logic_index.json`（缓存）和 `.claude/logic_tree.md`（输出）

### 步骤 3: 注入策略

根据 `LOGIC_INDEX_AUTO_INJECT` 策略：

| 策略 | 行为 |
| :--- | :--- |
| `ALWAYS`（默认） | 自动将 `logic_tree.md` 注入 `CLAUDE.md` |
| `ASK` | 注入前提示用户确认 |
| `NEVER` | 仅生成文件，不注入 |

## 输出格式

`logic_tree.md` 结构如下：

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

### 环境变量（`settings.json`）

在 `settings.local.json`（项目级）或 `~/.claude/settings.json`（全局）中配置：

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `OPENAI_API_KEY` | — | API 密钥 |
| `OPENAI_MODEL` | `glm-5` | 模型名称 |
| `OPENAI_BASE_URL` | `https://coding.dashscope.aliyuncs.com/v1/chat/completions` | API 端点 |
| `OPENAI_MAX_WORKERS` | `3` | 并发线程数 |
| `OPENAI_RETRY_LIMIT` | `3` | 重试次数 |
| `OPENAI_TIMEOUT` | `300` | 超时秒数 |
| `OPENAI_MAX_TOKENS` | `8192` | 响应 Token 限制 |
| `LOGIC_INDEX_AUTO_INJECT` | `ALWAYS` | `ALWAYS` / `ASK` / `NEVER` |
| `LOGIC_INDEX_FILTER_SMALL` | `false` | 跳过无 docstring 的短函数的 LLM 摘要 |
| `REMY_LANG` | `en` | 摘要输出语言（`en` / `zh-CN`） |
| `IMPACT_DEPTH_UP` | `2` | `impact.py` 默认上游（调用者）BFS 深度 |
| `IMPACT_DEPTH_DOWN` | `2` | `impact.py` 默认下游（被调用者）BFS 深度 |

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
将 `OPENAI_MAX_WORKERS` 设为 `1`（串行模式），或申请更高配额。

### Q: `Fatal API Error 403: Forbidden`？
检查 `OPENAI_API_KEY` 是否正确，`OPENAI_MODEL` 在服务端是否可用。

### Q: 中断后会丢失进度吗？
不会。`try...finally` 保护机制确保已生成的摘要保存到 `.claude/logic_index.json`。

### Q: C/C++/TypeScript 调用图未提取？
安装 `tree-sitter` 包。调用图提取需要 AST 精度，正则模式无法提供。Python 调用图使用标准库 `ast`，无需 tree-sitter。

### Q: 层分配不正确？
编辑 `.claude/logic_index_config` 自定义层模式。删除不需要的行并添加自定义规则。未匹配的文件默认归入 "Core"。

### Q: Hook 富化信息未出现？
确认 `logic_enrichment_hook.py` 已在 `~/.claude/settings.json` 的 `hooks.PreToolUse` 中注册，matcher 为 `Read|Glob|Grep`。运行 `python install.py --verify` 检查。
