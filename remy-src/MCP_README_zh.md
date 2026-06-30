# remy-index MCP 服务器

基于 stdio 的 [Model Context Protocol](https://modelcontextprotocol.io/) 服务器，从 remy-index SQLite 数据库暴露代码智能查询。Claude Code 通过 JSON-RPC 与此服务器通信，查询符号定义、调用图和影响分析。

## 前置条件

| 依赖 | 说明 |
| :--- | :--- |
| Python 3.10+ | `mcp` SDK 要求 |
| `mcp` 包 | `pip install mcp`（FastMCP stdio 传输） |
| `logic_index.db` | 由 `/remy-index` 或 `struct_scan.py` 生成 |

若 `mcp` 未安装或 `MCP_SERVER_ENABLED=false`，服务器以 exit code 0 退出。

## 架构

```
┌─────────────────────┐       JSON-RPC (stdio)       ┌──────────────────────────┐
│   Claude Code       │ ◄──────────────────────────► │  index_mcp_server.py     │
│   (MCP 客户端)      │                              │  ├─ _init_freshness()    │
└─────────────────────┘                              │  ├─ 8 个 tool handler    │
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
                                                     │  impact.py + struct_scan │
                                                     │  (共享 BFS / 辅助函数)   │
                                                     └──────────┬───────────────┘
                                                                │ 只读
                                                     ┌──────────▼───────────────┐
                                                     │  .claude/logic_index.db  │
                                                     │  (SQLite, WAL 模式)      │
                                                     └──────────────────────────┘
```

**数据流向**：`struct_scan.py`（SessionStart 或脏文件消费器触发）向 `logic_index.db` 写入符号、边和模式。MCP 服务器以 WAL 只读模式打开数据库并提供查询服务。两个进程不会同时写入同一文件。

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
2. 环境检查：若 `MCP_SERVER_ENABLED=false`，静默退出。
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
| `depth` | `int` | `2` | 最大 BFS 深度（受 `MCP_BFS_MAX_DEPTH` 限制） |
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
  [depth 1] src/loader.py, src/cli.py ... +2
  [depth 2] src/app.py, tests/test_loader.py ... +3

downstream (called by these files):
  [depth 1] src/ast_nodes.py, src/utils.py
  [depth 2] src/tokenizer.py

summary: 8 files affected, 7 upstream + 3 downstream symbols
```

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

按名称模糊搜索符号。三级回退策略：
1. **FTS5 前缀匹配**（最快，利用全文索引）
2. **LIKE 子串匹配**（FTS 无结果时）
3. **编辑距离匹配**（LIKE 无结果时，捕获拼写错误）

| 参数 | 类型 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `text` | `str` | （必需） | 搜索查询（部分名称、前缀或近似值） |
| `limit` | `int` | `10` | 最大返回结果数 |
| `file_hint` | `str` | `""` | 文件路径子串过滤 |

**输出示例：**
```
search results for 'pars_fil' (2 results, matched via fuzzy)

  [function] src/parser.py::parse_file  src/parser.py:L42 (Core)
  [method] src/loader.py::Loader.parse_file  src/loader.py:L120 (IO)
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

## 配置

所有参数通过 `settings.local.json`（或全局 `settings.json`）的 `env` 块设置。可通过 `remy-cc config` UI 的"MCP 服务器"分组编辑。

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `MCP_SERVER_ENABLED` | `true` | 设为 `false` 禁用 MCP 服务器 |
| `MCP_BFS_MAX_DEPTH` | `5` | BFS 深度硬上限（callers/callees/impact） |
| `MCP_IMPACT_MAX_DEPTH_UP` | `3` | `query_impact` 默认上游深度 |
| `MCP_IMPACT_MAX_DEPTH_DOWN` | `3` | `query_impact` 默认下游深度 |
| `MCP_RESULT_LIMIT` | `50` | BFS 输出中每层最大结果数 |
| `MCP_STATIC_ONLY_DEFAULT` | `false` | 未指定 `static_only` 时的默认值 |
| `FLOW_MAX_DEPTH` | `15` | `query_flow` 默认最大 BFS 深度 |
| `FLOW_MAX_VISITED` | `2000` | `query_flow` 默认最大访问节点数 |
| `LOGIC_INDEX_DB_PATH` | `.claude/logic_index.db` | SQLite 数据库相对路径 |

相关变量（"上下文注入"分组）：

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `NAV_MCP_MINIMAL_ENABLED` | `true` | MCP 可用时，仅注入集群概览（~1 KB）而非完整符号树（~40 KB） |

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

通过 `NAV_MCP_MINIMAL_ENABLED`（项目级设置）控制。

## 故障排查

### 首次 tool call 永久挂起（Windows）

**症状**：首次 MCP tool call 永不返回。

**根因**：`subprocess.run(capture_output=True)` 在 asyncio tool handler 内部死锁。Windows 的 ProactorEventLoop 使用 I/O completion ports 管理 MCP 服务器的 stdin/stdout 管道。在同一进程内通过 `capture_output=True` 创建新管道时两者冲突，导致 subprocess 的管道读取永远不完成。

**修复**（v1.4.3）：所有 subprocess 调用已移至 `_init_freshness()`，在 `mcp.run()` 启动事件循环前执行。

**若修改后再次出现类似症状**：检查 tool handler 代码路径中是否存在 `subprocess.run`、`subprocess.Popen` 或 `os.popen` 调用。

### "Error: logic_index.db not found"

数据库不存在于预期路径。运行 `/remy-index` 生成，或检查 `LOGIC_INDEX_DB_PATH` 是否指向正确位置。

### 修改代码后结果过时

索引基于快照构建。若上次扫描后修改了源文件：
1. 新鲜度警告会出现（若 >20% 文件不同）。
2. 运行 `/remy-index` 重建，或等待下次 SessionStart 自动扫描。

### MCP 服务器未启动

检查：
1. `python --version` >= 3.10
2. `pip show mcp` 确认包已安装
3. `~/.claude.json` 的 `mcpServers` 中包含 `remy-index` 条目
4. `MCP_SERVER_ENABLED` 未设为 `false`

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
