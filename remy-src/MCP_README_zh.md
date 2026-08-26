# remy-index MCP 服务器

基于 stdio 的 [Model Context Protocol](https://modelcontextprotocol.io/) 服务器，从 remy-index SQLite 数据库暴露代码智能查询。自 R4.1 起，生产宿主为 Rust 二进制——`remy-daemon mcp`（rmcp 3.1.4），由安装器部署到 `~/.remy-cc/bin/`。Claude Code 通过 JSON-RPC 与之通信，查询符号定义、调用图和影响分析，无需子进程。

Python 服务器（`index_mcp_server.py`，FastMCP）作为 H.4 差分套（`tests/test_mcp_rust_parity.py`）的开发期渲染 oracle 保留在仓内，不再部署。

## 前置条件

| 依赖 | 说明 |
| :--- | :--- |
| `remy-daemon` 二进制 | 由 `install.py` 部署到 `~/.remy-cc/bin/` |
| `logic_index.db` | 由 `/remy-index` 或 `struct_scan.py` 生成 |

若 `REMY_MCP_SERVER_ENABLED=false`，服务器向 stderr 输出提示并以 exit code 0 退出。

## 架构

```
┌─────────────────────┐       JSON-RPC (stdio)       ┌──────────────────────────┐
│   Claude Code       │ ◄──────────────────────────► │  remy-daemon mcp         │
│   (MCP 客户端)      │                              │  (rmcp，每会话一进程)    │
└─────────────────────┘                              │  ├─ init_freshness()     │
                                                     │  ├─ 13 个tool handler    │
                                                     │  └─ with_freshness()     │
                                                     └──────────┬───────────────┘
                                                                │ mod
                                                     ┌──────────▼───────────────┐
                                                     │  域模块                  │
                                                     │  ├─ config / common      │
                                                     │  ├─ facts / graph        │
                                                     │  ├─ search / navigate    │
                                                     │  └─ deps / freshness     │
                                                     └──────────┬───────────────┘
                                                                │ rusqlite (WAL)
                                                     ┌──────────▼───────────────┐
                                                     │  .claude/logic_index.db  │
                                                     │  (SQLite, WAL 模式)      │
                                                     └──────────────────────────┘
```

**数据流向**：扫描器（生产为 Rust `remy-daemon scan`；Python `struct_scan.py` 为保留回退臂）在项目扫描锁保护下向 `logic_index.db` 写入符号、边和模式。MCP 服务器为每次查询打开短生命周期只读连接（WAL + `busy_timeout=3000`，无写路径——INV-R2）；MCP 读取可与当前写入者并发。查询语义按 H.4 基线（`docs/MCP_RUST_PARITY_BASELINE_zh.md`）自 Python owner 模块逐字节迁移。

## 启动与注册

安装时注册到 `~/.claude.json`（模板：`remy_mcp.json`）：

```json
{
  "mcpServers": {
    "remy-index": {
      "type": "stdio",
      "command": "~/.remy-cc/bin/remy-daemon",
      "args": ["mcp"]
    }
  }
}
```

- Claude Code 在会话启动时自动拉起服务器。
- 服务器在 Claude Code 会话期间持续运行（每会话一个进程）。

### 启动流程

1. **`config::load()`**：读取 `remy-config.json`（用户级，项目级覆盖）与 `REMY_*` 进程环境变量；非法值诊断输出到 stderr。
2. 环境检查：若 `REMY_MCP_SERVER_ENABLED=false`，向 stderr 输出提示并以 exit 0 退出。
3. **`init_freshness()`**：探测索引新鲜度（git commit 比对或 hash 抽样），仅执行一次且先于 tokio runtime 启动；警告串此后不可变。
4. **`serve(stdio())`**：启动 tokio runtime，开始接收 JSON-RPC 请求。

## 工具参考

### query_symbol

按名称查找符号定义。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `name` | `str` | （必需） | 符号名、短名或限定名（`file::name`） |
| `file` | `str` | `""` | 按文件路径过滤结果 |

**输出示例：**
```
symbols matching 'parse_file' (2 results)

  [function] src/parser.py::parse_file(path, encoding='utf-8')  src/parser.py:L42-L87 (Core)
        解析源文件并返回 AST 节点。
  [method] src/loader.py::Loader.parse_file(self, path)  src/loader.py:L120-L145 (IO)
        使用 loader 专属选项委托给解析器。
```

---

### query_symbol_summary

获取符号级摘要和文档注释，无需读取源文件。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `name` | `str` | （必需） | 符号名或限定名 |
| `file` | `str` | `""` | 按文件过滤 |

**输出示例：**
```
summary for 'bfs_callers'

  [function] skills/remy-index/impact.py::bfs_callers(db, target_set, max_depth, static_only=False)  L55
  summary: 从目标符号向上游 BFS 遍历。返回 dict[depth] -> list[qualified_name]。
```

---

### query_file_summary

获取文件级语义摘要（角色、关键符号、所属层），无需读取整个源文件。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `file` | `str` | （必需） | 仓库相对路径（自动将 `\` 归一化为 `/`） |

**输出示例：**
```
## skills/remy-index/impact.py (8 symbols, layer=Core)
  short: 基于调用图的 BFS 影响分析。
  full: 提供 bfs_callers / bfs_callees / format_output 工具函数，供 /remy-plan 与 /remy-patch 调用。
  key symbols:
    - [function] bfs_callees  L91-L120
    - [function] bfs_callers  L60-L88
    ... (+6 more)
```

key symbols 按 casefold 名称排序，条数受 `REMY_MCP_RESULT_LIMIT` 限制；截断时以 `... (+N more)` 报告剩余数，无符号文件输出 `key symbols: (none)`。

错误情况：路径未索引时返回 `No file '<path>' in index. Run /remy-index to scan.`

---

### query_callers

通过 BFS 查找符号的上游调用者，按深度分组。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `symbol` | `str` | （必需） | 符号名或限定名 |
| `depth` | `int` | `2` | 最大 BFS 深度（受 `REMY_MCP_BFS_MAX_DEPTH` 限制） |
| `include_ambiguous` | `bool` | `False` | 包含通过 `edge_candidates` 表解析的边 |
| `static_only` | `bool` | `False` | 排除合成边（provenance: inferred/speculative） |

**输出示例：**
```
callers of parse_file (2 levels, 5 results)

[depth 1] direct:
  src/loader.py::Loader.load [L30-L55] (IO)
  src/cli.py::main [L10-L25] (Entry)

[depth 2]
  tests/test_loader.py::test_load_valid [L15-L30] (Test)
  tests/test_cli.py::test_main_happy [L8-L20] (Test)
  src/app.py::Application.start [L100-L130] (Entry)
```

---

### query_callees

通过 BFS 查找符号的下游被调用者。参数和输出格式与 `query_callers` 相同。

---

### query_impact

分析修改一个或多个文件的影响半径。展示目标文件中所有符号的上游调用者和下游被调用者。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `files` | `list[str]` | （必需） | 文件路径（正斜杠，相对于项目根目录） |
| `depth_up` | `int` | `3` | 上游 BFS 深度 |
| `depth_down` | `int` | `3` | 下游 BFS 深度 |
| `include_ambiguous` | `bool` | `False` | 包含歧义边 |
| `static_only` | `bool` | `False` | 排除合成边 |

**输出示例：**
```
impact analysis for: src/parser.py

upstream (callers into these files):
  [depth 1] 4 file(s), 7 symbol(s): src/loader.py, src/cli.py, src/app.py, tests/test_loader.py
  [depth 2] 6 file(s), 9 symbol(s): src/app.py, src/server.py, src/worker.py, tests/test_cli.py, tests/test_app.py ... +1 more file(s)

downstream (called by these files):
  [depth 1] 2 file(s), 3 symbol(s): src/ast_nodes.py, src/utils.py
  [depth 2] 1 file(s), 1 symbol(s): src/tokenizer.py

summary: 12 files affected, 16 upstream + 4 downstream symbols
```

每个深度行列出去重后的文件，因此含多个匹配符号的文件只出现一次。行内的 `file(s)`、`symbol(s)` 计数与 `files affected` 总数覆盖该层全部结果。每层最多打印 5 个文件标签，其余以 `+N more file(s)` 说明；`REMY_MCP_RESULT_LIMIT` 不作用于该工具。

---

### query_patterns

查询事件/回调注册模式（Django signals、PyQt signals、observer 模式）。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `pattern_type` | `str` | `""` | 按类型过滤：`django_signal`、`pyqt_signal`、`observer` |
| `signal_name` | `str` | `""` | 按信号名过滤 |
| `file` | `str` | `""` | 按文件路径过滤 |

**输出示例：**
```
event/callback patterns (3 results)

  [django_signal] post_save -> handle_user_created  src/signals.py:L15
  [pyqt_signal] clicked -> on_button_click  src/ui/main_window.py:L42
  [observer] on_data_changed -> DataView.refresh  src/views.py:L88
```

---

### query_search

通过确定性候选并集搜索符号：精确名称、词前缀和BM25摘要三个通道独立执行，按
节点身份合并去重，每个结果保留全部匹配来源与来源内名次。编辑距离fuzzy回退仅
在三个确定性通道均为空时执行。无效输入或通道执行错误返回`Error:`结果，并且
不继续执行后续通道。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `text` | `str` | （必需） | 查询文本；操作符和标点按分隔符处理 |
| `limit` | `int` | `10` | 结果上限，范围为`1..REMY_MCP_RESULT_LIMIT` |
| `file_hint` | `str` | `""` | `path_hint`的兼容别名 |
| `match` | `str` | `"all"` | `all`、`any`或精确连续`phrase`语义 |
| `language` | `str` | `""` | `python`、`c_cpp`、`typescript`或`rust`解析器家族 |
| `symbol_type` | `str` | `""` | 精确符号类型过滤 |
| `path_hint` | `str` | `""` | 不区分大小写的字面路径子串过滤 |

只有当`file_hint`和`path_hint`规范化后相同时，才可同时提供。路径分隔符会被统一。
`%`和`_`是普通字符，不是通配符。符号类型、文件、名称和行号所在的定位行保持不变；
每个结果追加缩进的`sources`/`priority`行与可选的`sig`/`summary`行。精确通道按
Unicode casefold比较完整查询文本，忽略`match`模式。

**输出示例：**
```
search results for 'parse_file' (2 results, matched via union)

  [function] src/parser.py::parse_file  src/parser.py:L42 (Core)
        sources: exact#1, prefix#1 | priority=0
        sig: (path) | summary: parses one source file
  [method] src/loader.py::Loader.parse_file  src/loader.py:L120 (IO)
        sources: prefix#2 | priority=1
```

---

### query_flow

通过双向 BFS 查找两个或多个符号间的调用路径。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `symbols` | `list[str]` | （必需，至少 2 个） | 按遍历顺序排列的符号名 |
| `max_depth` | `int` | `15` | 每对符号的最大 BFS 深度 |
| `max_visited` | `int` | `2000` | 每对符号的最大访问节点数（防止组合爆炸） |
| `static_only` | `bool` | `False` | 排除合成边 |

**符号语法：**
- 裸名：`parse_file`
- 限定名：`src/parser.py::parse_file`
- 类.方法：`Loader.parse_file`
- 文件提示：`parser.py:parse_file`

**输出示例：**
```
## Flow (call path among queried symbols)

1. main (src/cli.py:10)
   ↓ call
2. load (src/loader.py:30)
   ↓ call
3. parse_file (src/parser.py:42)
   ↓ call
4. tokenize (src/tokenizer.py:15)
```

符号不连通时：
```
## Flow (partial — 2/3 symbols connected)

1. main (src/cli.py:10)
   ↓ call
2. parse_file (src/parser.py:42)

[Break: pair (parse_file, unrelated_func) not connected within depth=15]

3. unrelated_func (src/other.py:5)
```

---

### query_cluster_summary

返回单个或全部集群的子系统级摘要。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `name` | `str` | `""` | 集群名（精确匹配）；空值返回全部集群，按 `file_count DESC, name` 排序 |

每个集群渲染 `## <name> (<N> files)` 头部（存在不同 label 时附 `[alias: <label>]`），
有可用摘要版本时渲染 `short:` / `full:` 行，`entry_symbols` 至多 5 个，仅当
当前摘要 status 非 `ok` 时渲染 `status:` 行。

**输出示例**（实机探针；该集群尚无生成摘要，故无 `short:`/`full:` 行并显示 status）：
```
## Remy-CC/hooks (11 files)
  entry_symbols: Remy-CC/hooks/session_anchor.py::read, Remy-CC/hooks/permission_gate.py::decide, Remy-CC/hooks/pre_tool_guard.py::validate_packet
  status: stale
```

**错误：**
- 未知集群：`No clusters found matching '<name>'`
- 空表：`No clusters found`

---

### query_cluster_files

列出指定 cluster 的成员文件，可选附短摘要。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `cluster` | `str` | （必需） | cluster 名称（精确匹配；可从 `query_cluster_summary` 获取） |
| `with_summary` | `bool` | `False` | 为 `True` 时在每个文件下追加 `short:` 行 |

**输出示例 (`with_summary=False`)：**
```
## Remy-CC/skills (23 files)
  - skills/remy-index/impact.py  (layer=Core)
  - skills/remy-index/run.py  (layer=Core)
  - skills/remy-index/struct_scan.py  (layer=Core)
```

**输出示例 (`with_summary=True`)：**
```
## Remy-CC/skills (23 files)
  - skills/remy-index/impact.py  (layer=Core)
      short: 基于调用图的 BFS 影响分析。
  - skills/remy-index/run.py  (layer=Core)
      short: (no summary available)
```

**错误情况：**
- 空 `cluster` 参数：`Error: cluster name is required`
- 未知 cluster：`No cluster '<name>' found. Use query_cluster_summary() to list all clusters.`
- cluster 存在但无成员：`Cluster '<name>' has no member files.`

---

### query_navigate

在有界的 cluster/file/symbol 候选上按自然语言意图定位工作区域。意图先拆词并
以 `any` 语义经 P1.3 确定性通道执行：symbol 候选来自 exact/prefix/BM25 并集
（单词意图在确定性通道全空时追加 fuzzy 通道），file/cluster 候选来自投影行
的加权 BM25 查询（名称、路径词、路径与摘要列）。每层候选受配额参数约束；
只为入选候选读取摘要并发送给 LLM。symbol 候选携带所属 file 与 cluster，
file 候选携带所属 cluster，逐层下钻不出现孤儿。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `intent` | `str` | （必需） | 自然语言意图；索引摘要为英文存储，建议使用英文以获得词法召回 |
| `top_k` | `int` | `5` | 结果上限，收敛到 `1..20` |

输出头部的 `source=` 标签报告实际路径：`llm`（候选 prompt 经 LLM 排序）、
`llm-cluster-only`（词法候选为空——例如非英文意图——降级为全部 cluster 短摘要
排序）、`heuristic`（未配置 LLM；按候选确定性顺序输出）、`heuristic-fallback`
（LLM 响应不可解析）、`cache`。排序结果缓存在 `judge_cache`，键由规范化意图、
`top_k`、候选 `(node_ref, content_hash)` 序列与 prompt 模板版本派生——与候选
无关的摘要写入不再使缓存失效。

**输出示例：**
```
## Navigate results for 'locate authentication token parser' (top 2, source=llm)

1. [0.92] security / auth/token_parser.py :: parse_token
   - parses and validates raw tokens
2. [0.55] security / auth/session.py
   - session lifecycle around token use
```

### query_dependencies

分析文件级导入/包含关系——调用图不表达的纯导入依赖。依赖图由已存储的解析
导入（`files.imports`）与查询期从 `files.import_bindings` 做的唯一后缀派生
合并而成（与 scanner 边解析共用同一 `derive_import_bindings` 规则，扫描期与
查询期语义不会漂移）。多重命中绑定不产生边（unique-only）；stdlib 模块短路。
`imports` 中不在 files 表的条目渲染时附加 `(not indexed)` 标记。

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `files` | `list[str]` | （必需） | 项目相对路径；反斜杠自动归一化 |
| `direction` | `str` | `both` | `up` = 导入目标的文件，`down` = 目标导入的文件，`both` = 双段 |
| `depth` | `int` | `2` | BFS 深度，钳制至 `REMY_MCP_BFS_MAX_DEPTH` |

层内路径按字典序；每个文件仅出现在首达层（visited 集合使导入环终止）。

**输出示例：**
```
dependency analysis for: app/main.py

imported by (upstream importers):
  (none)

imports (downstream dependencies):
  [depth 1] 2 file(s): app/util.py, libs/helpers.py
  [depth 2] 1 file(s): vendor/missing.py (not indexed)

summary: 0 upstream file(s), 3 downstream file(s)
```

**错误：**
- 非法 direction：`Error: direction must be one of up/down/both.`
- 目标均不在索引：`No indexed files found matching: <inputs>`

该 tool 为 Rust 单实现（无 Python oracle 臂）；验收面为
`tests/test_mcp_dependencies.py`，不入 H.4 差分矩阵
（见 `docs/MCP_RUST_PARITY_BASELINE_zh.md` §4.2）。

## 配置

Python运行时参数保存在`~/.claude/remy-config.json`，项目覆盖保存在
`<project>/.claude/remy-config.json`。`remy-cc config`界面编辑这些文件。
同名`REMY_*`进程环境变量只覆盖当前进程树中的文件值。

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `REMY_MCP_SERVER_ENABLED` | `true` | 下次启动时禁用MCP服务器 |
| `REMY_MCP_BFS_MAX_DEPTH` | `5` | callers/callees/impact/dependencies的BFS深度上限 |
| `REMY_MCP_RESULT_LIMIT` | `50` | BFS层与query_search共享结果上限 |
| `REMY_MCP_STATIC_ONLY_DEFAULT` | `false` | 查询实现收到`static_only=None`时使用的内部默认值；公开MCP工具保持`false` |
| `REMY_FLOW_MAX_DEPTH` | `15` | query_flow深度硬上限 |
| `REMY_FLOW_MAX_VISITED` | `2000` | query_flow访问节点硬上限 |
| `REMY_NAVIGATE_CANDIDATE_CLUSTERS` | `5` | query_navigate每次意图查询的cluster候选上限 |
| `REMY_NAVIGATE_CANDIDATE_FILES` | `10` | query_navigate每次意图查询的file候选上限 |
| `REMY_NAVIGATE_CANDIDATE_SYMBOLS` | `10` | query_navigate每次意图查询的symbol候选上限 |
| `REMY_LOGIC_INDEX_DB_PATH` | `.claude/logic_index.db` | 相对项目根的数据库路径 |

## 索引新鲜度检测

启动时，`init_freshness()`（`remy-daemon/src/mcp/freshness.rs`）检查索引是否最新：

```
┌─────────────────────────────┐
│ 从 meta 表读取 source_commit│
└──────────┬──────────────────┘
           │
     ┌─────▼─────┐     是     ┌──────────────────┐
     │ git 可用？├───────────►│ 比较 HEAD 与     │
     └─────┬─────┘            │ source_commit    │
           │ 否               └────────┬─────────┘
           │                           │
           │                     ┌─────▼──────┐
  ┌────────▼───────────┐         │  匹配？    │
  │ Hash 抽样          │         └──┬──────┬──┘
  │ (10% 文件,         │          是│      │否
  │  最多 10 个样本)   │            │      │
  └────────┬───────────┘            │  ┌───▼──────────────────┐
           │                        │  │ 警告: commit 漂移    │
           │                        │  └──────────────────────┘
  ┌────────▼───────────┐            │
  │ >20% 不匹配？      │   ┌────────▼─────────────┐
  │ >50% 不匹配？      │   │ 检查 git status      │
  └────────┬───────────┘   │ (porcelain)          │
           │               └────────┬─────────────┘
           │                        │
  ┌────────▼───────┐          ┌─────▼──────┐
  │ 发出警告       │          │ >20% 脏？  │──► 警告
  └────────────────┘          └────────────┘
```

当生成警告时，前置到每个 tool 响应中：

```
[Warning: index built at commit a1b2c3d4, current HEAD is e5f6g7h8. Run /remy-index to rebuild.]

symbols matching 'parse_file' (1 results)
  ...
```

警告不阻止 tool 使用——结果仍然返回，但可能不完整或过时。

## MCP Minimal 模式

当 MCP 服务器运行时，上下文注入系统（`injector.py`）从注入完整逻辑树（~40 KB 的符号签名和摘要）切换为最小载荷（~1 KB）：
- 集群概览表（集群名、文件数、入口文件）
- MCP 工具使用指引（何时使用哪个工具）

## 故障排查

### "Error: logic_index.db not found"

数据库不存在于预期路径。运行 `/remy-index` 生成，或检查 `REMY_LOGIC_INDEX_DB_PATH` 是否指向正确位置。

### 修改代码后结果过时

索引基于快照构建。若上次扫描后修改了源文件：
1. 新鲜度警告会出现（若 >20% 文件不同）。
2. 运行 `/remy-index` 重建，或等待下次 SessionStart 自动扫描。

### MCP 服务器未启动

检查：
1. `~/.remy-cc/bin/remy-daemon --version` 可运行并报告部署版本
2. `~/.claude.json` 的 `mcpServers` 中包含 `remy-index` 条目（command = daemon 二进制，args `["mcp"]`）
3. `REMY_MCP_SERVER_ENABLED` 未设为 `false`

诊断：手动运行并检查 stderr（配置诊断与禁用提示均输出到 stderr）：
```bash
~/.remy-cc/bin/remy-daemon mcp
```

### 首次 tool call 挂起（历史——仅适用开发期 Python oracle）

该故障模式仅适用于开发期 Python oracle（`index_mcp_server.py`），不适用于 Rust 生产宿主。Windows 上 `subprocess.run(capture_output=True)` 在 asyncio tool handler 内部死锁：ProactorEventLoop 的 I/O completion ports 与同进程新建管道冲突，subprocess 读取永不完成。修复（v1.4.3）将全部 subprocess 调用移至 oracle 的 `_init_freshness()`，先于 `mcp.run()` 启动事件循环。修改 oracle 时，tool handler 代码路径中不得出现 `subprocess.run`/`subprocess.Popen`/`os.popen`。

## 开发者指南

### 源文件（Rust 宿主，`remy-daemon/src/mcp/`）

| 模块 | 职责 |
| :--- | :--- |
| `mod.rs` | rmcp 服务器定义、tool 注册（`#[tool]`）、INSTRUCTIONS 文本、tokio 入口 |
| `config.rs` | `REMY_*` 字段注册表、remy-config.json 作用域、环境变量覆盖、诊断 |
| `common.rs` | 共享数据库访问（`open_db`）、摘要查找、新鲜度前缀辅助 |
| `facts.rs` | symbol/file/cluster/pattern 事实查询 |
| `graph.rs` | ambiguous BFS、callers/callees/impact、flow 遍历与格式化 |
| `search.rs` | 查询校验、exact/prefix/BM25/fuzzy 四通道、候选合并 |
| `navigate.rs` | 意图导航：候选收集、judge_cache 键、prompt、LLM 排序（reqwest） |
| `deps.rs` | 文件级导入关系（复用 `scanner_core::postprocess::derive_import_bindings`） |
| `freshness.rs` | 启动期新鲜度探测（git 比对、hash 抽样） |

Python oracle 对应模块（`index_mcp_server.py` + `index_mcp_common/facts/graph/search/navigate.py`）保留在 `remy-src/`，仅供差分套与 eval 基准使用。

### 架构约束（Rust 宿主）

**INV-1**：外部进程调用（git 探测）仅发生在 `init_freshness()` 内，且先于 tokio runtime 启动（`mod.rs::run` 顺序）。tool handler 代码路径不派生进程；navigate 的 LLM 调用使用进程内 `reqwest`。

**INV-2**：新鲜度警告由 `init_freshness()` 计算一次，经 `Arc<String>` 只读共享；tool handler 不修改它。

**INV-3**：当结果以 `Error:` 开头时，`with_freshness(result)` 跳过警告前缀——错误消息原样返回（`common.rs`）。

**INV-4**：每次查询打开短生命周期 rusqlite 连接（WAL + `busy_timeout=3000`），从不写入——读路径不进 daemon 写拓扑（INV-R2）。

历史 oracle 约束：Python FastMCP oracle 额外禁止 tool handler 内的 subprocess 调用（Windows ProactorEventLoop 死锁——见故障排查）。

### 新增 Tool

1. 在 `remy-daemon/src/mcp/` 下对应职责域模块中实现 `query_xxx_impl(...)`（新域则新建模块，如 `deps.rs`）。
2. 在 `mod.rs` 注册：`Params` 结构体（serde 默认值 + schemars）+ 以 `self.wrap(...)` 包装 impl 的 `#[tool]` 方法 + INSTRUCTIONS 增一行。
3. 按 `docs/MCP_RUST_PARITY_BASELINE_zh.md` §4.2 裁定验证面：有 Python oracle 臂的 tool 入差分矩阵；Rust 单实现 tool 建专项套（参照 `tests/test_mcp_dependencies.py`）并加入 parity 的 `RUST_ONLY_TOOLS` 白名单。
4. 权限已被 `settings.example.json` 的 `mcp__remy-index__*` 通配符覆盖。
5. 同步注册面：本 README（+英文版）、两个项目 README、工作区 `CLAUDE.md` 工具计数、`hooks/doc_manager/mcp_minimal_template.json`、`docs/TESTING.md`（+_zh）。

### 边置信度层级

`edges` 表的 `provenance` 列有 4 个置信度级别：

| 级别 | 含义 | `static_only=True` |
| :--- | :--- | :--- |
| `definite` | 直接 AST 解析的调用 | 包含 |
| `probable` | 跨文件名称匹配的调用 | 包含 |
| `inferred` | 通过接口派发、观察者模式或信号连接合成 | **排除** |
| `speculative` | 歧义解析候选（多个目标） | **排除** |

当 `static_only=True` 时，仅遍历 `definite` 和 `probable` 边。产生更紧凑、更可靠的结果，代价是可能遗漏多态派发路径。
