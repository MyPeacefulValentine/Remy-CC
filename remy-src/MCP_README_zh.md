# remy-index MCP 服务器

基于 stdio 的 [Model Context Protocol](https://modelcontextprotocol.io/) 服务器，从 remy-index SQLite 数据库暴露代码智能查询。Claude Code 通过 JSON-RPC 与此服务器通信，查询符号定义、调用图和影响分析。

## 前置条件

| 依赖 | 说明 |
| :--- | :--- |
| Python 3.10+ | `mcp` SDK 要求 |
| `mcp` 包 | `pip install mcp`（FastMCP stdio 传输） |
| `logic_index.db` | 由 `/remy-index` 或 `struct_scan.py` 生成 |

若 `mcp` 未安装或 `REMY_MCP_SERVER_ENABLED=false`，服务器以 exit code 0 退出。

## 架构

```
┌─────────────────────┐       JSON-RPC (stdio)       ┌──────────────────────────┐
│   Claude Code       │ ◄──────────────────────────► │  index_mcp_server.py     │
│   (MCP 客户端)      │                              │  ├─ _init_freshness()    │
└─────────────────────┘                              │  ├─ 12 个tool handler    │
                                                     │  └─ _with_freshness()    │
                                                     └──────────┬───────────────┘
                                                                │ import
                                                     ┌──────────▼───────────────┐
                                                     │  index_mcp_queries.py    │
                                                     │  ├─ SQL 查询             │
                                                     │  ├─ BFS 遍历             │
                                                     │  └─ 结果格式化           │
                                                     └──────────┬───────────────┘
                                                                │ import
                                                     ┌──────────▼───────────────┐
                                                     │  impact.py +             │
                                                     │  struct_scan.py           │
                                                     │  （稳定辅助入口）         │
                                                     └──────────┬───────────────┘
                                                                │ 只读
                                                     ┌──────────▼───────────────┐
                                                     │  .claude/logic_index.db  │
                                                     │  (SQLite, WAL 模式)      │
                                                     └──────────────────────────┘
```

**数据流向**：`struct_scan.py`继续作为SessionStart和脏文件消费器的稳定入口，并将结构扫描委托给`scanner.py`；`schema.py`、`symbol_names.py`和`migrations.py`分别保存schema、名称拆词和数据库迁移契约。扫描器向`logic_index.db`写入符号、边和模式。MCP服务器以WAL只读模式打开数据库并提供查询服务。全量、增量和手动索引写入者共用项目扫描锁；MCP读取可与当前写入者并发。

## 启动与注册

安装时注册到 `~/.claude.json`：

```json
{
  "mcpServers": {
    "remy-index": {
      "type": "stdio",
      "command": "python",
      "args": ["-u", "~/.claude/remy-src/index_mcp_server.py"]
    }
  }
}
```

- Claude Code 在会话启动时自动拉起服务器。
- `-u` 标志强制 stdout 无缓冲，确保 JSON-RPC 响应立即刷新到管道。
- 服务器在 Claude Code 会话期间持续运行。

### 启动流程

1. 导入检查：若 `mcp` 包缺失，输出错误到 stderr 并 `sys.exit(0)`。
2. 环境检查：若 `REMY_MCP_SERVER_ENABLED=false`，静默退出。
3. **`_init_freshness()`**：通过 subprocess 探测索引新鲜度（git commit 比对或 hash 抽样）。必须在事件循环启动前执行（见[故障排查](#故障排查)）。
4. **`mcp.run(transport="stdio")`**：进入 asyncio 事件循环，开始接收 JSON-RPC 请求。

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
```

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
| `language` | `str` | `""` | `python`、`c_cpp`或`typescript`解析器家族 |
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

## 配置

Python运行时参数保存在`~/.claude/remy-config.json`，项目覆盖保存在
`<project>/.claude/remy-config.json`。`remy-cc config`界面编辑这些文件。
同名`REMY_*`进程环境变量只覆盖当前进程树中的文件值。

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `REMY_REMY_MCP_SERVER_ENABLED` | `true` | 下次启动时禁用MCP服务器 |
| `REMY_REMY_MCP_BFS_MAX_DEPTH` | `5` | callers/callees/impact的BFS深度上限 |
| `REMY_REMY_MCP_RESULT_LIMIT` | `50` | BFS层与query_search共享结果上限 |
| `REMY_MCP_STATIC_ONLY_DEFAULT` | `false` | 查询实现收到`static_only=None`时使用的内部默认值；公开MCP工具保持`false` |
| `REMY_FLOW_MAX_DEPTH` | `15` | query_flow深度硬上限 |
| `REMY_FLOW_MAX_VISITED` | `2000` | query_flow访问节点硬上限 |
| `REMY_NAVIGATE_CANDIDATE_CLUSTERS` | `5` | query_navigate每次意图查询的cluster候选上限 |
| `REMY_NAVIGATE_CANDIDATE_FILES` | `10` | query_navigate每次意图查询的file候选上限 |
| `REMY_NAVIGATE_CANDIDATE_SYMBOLS` | `10` | query_navigate每次意图查询的symbol候选上限 |
| `REMY_REMY_LOGIC_INDEX_DB_PATH` | `.claude/logic_index.db` | 相对项目根的数据库路径 |

## 索引新鲜度检测

启动时，`_init_freshness()` 检查索引是否最新：

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

### 首次 tool call 永久挂起（Windows）

**症状**：首次 MCP tool call 永不返回。

**根因**：`subprocess.run(capture_output=True)` 在 asyncio tool handler 内部死锁。Windows 的 ProactorEventLoop 使用 I/O completion ports 管理 MCP 服务器的 stdin/stdout 管道。在同一进程内通过 `capture_output=True` 创建新管道时两者冲突，导致 subprocess 的管道读取永远不完成。

**修复**（v1.4.3）：所有 subprocess 调用已移至 `_init_freshness()`，在 `mcp.run()` 启动事件循环前执行。

**若修改后再次出现类似症状**：检查 tool handler 代码路径中是否存在 `subprocess.run`、`subprocess.Popen` 或 `os.popen` 调用。

### "Error: logic_index.db not found"

数据库不存在于预期路径。运行 `/remy-index` 生成，或检查 `REMY_LOGIC_INDEX_DB_PATH` 是否指向正确位置。

### 修改代码后结果过时

索引基于快照构建。若上次扫描后修改了源文件：
1. 新鲜度警告会出现（若 >20% 文件不同）。
2. 运行 `/remy-index` 重建，或等待下次 SessionStart 自动扫描。

### MCP 服务器未启动

检查：
1. `python --version` >= 3.10
2. `pip show mcp` 确认包已安装
3. `~/.claude.json` 的 `mcpServers` 中包含 `remy-index` 条目
4. `REMY_MCP_SERVER_ENABLED` 未设为 `false`

诊断：手动运行并检查 stderr：
```bash
python -u ~/.claude/remy-src/index_mcp_server.py
```

## 开发者指南

### 源文件

| 文件 | 行数 | 职责 |
| :--- | :--- | :--- |
| `index_mcp_server.py` | ~200 | FastMCP 服务器定义、tool handler（薄封装）、新鲜度初始化 |
| `index_mcp_queries.py` | ~830 | 全部查询实现：SQL、BFS 遍历、符号解析、结果格式化 |

### 架构约束

**INV-1**：tool handler 代码路径中禁止调用 `subprocess.run`、`subprocess.Popen` 或任何创建 OS 管道的 API。此不变量防止 Windows ProactorEventLoop 死锁。违反将导致服务器在 Windows 上首次 tool call 时挂起。

**INV-2**：`_freshness_warning` 由 `_init_freshness()` 在事件循环启动前写入一次，之后不再修改。tool handler 仅读取此值。

**INV-3**：当 `result.startswith("Error:")` 时，`_with_freshness(result)` 跳过警告前缀——错误消息原样返回。

**INV-4**：所有 tool handler 为同步 `def`（非 `async def`）。FastMCP 在线程池中运行它们。SQLite 操作不与 asyncio 冲突。

### 新增 Tool

1. 在 `index_mcp_queries.py` 中实现 `query_xxx_impl(...)`。
2. 在 `index_mcp_server.py` 中添加 handler：
   ```python
   @mcp.tool()
   def query_xxx(param: str, ...) -> str:
       """MCP 工具列表中的单行描述。"""
       return _with_freshness(query_xxx_impl(param, ...))
   ```
3. 在 `settings.example.json` 中注册权限（已被 `mcp__remy-index__*` 通配符覆盖）。
4. 更新本 README 和主项目 README。
5. 更新 `CLAUDE.md` 指令文本（`FastMCP()` 构造函数的 `instructions` 参数）。

### 边置信度层级

`edges` 表的 `provenance` 列有 4 个置信度级别：

| 级别 | 含义 | `static_only=True` |
| :--- | :--- | :--- |
| `definite` | 直接 AST 解析的调用 | 包含 |
| `probable` | 跨文件名称匹配的调用 | 包含 |
| `inferred` | 通过接口派发、观察者模式或信号连接合成 | **排除** |
| `speculative` | 歧义解析候选（多个目标） | **排除** |

当 `static_only=True` 时，仅遍历 `definite` 和 `probable` 边。产生更紧凑、更可靠的结果，代价是可能遗漏多态派发路径。
