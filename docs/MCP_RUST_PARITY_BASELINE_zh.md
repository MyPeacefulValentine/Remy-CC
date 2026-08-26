# MCP Rust 差分基线（H.4）

版本：v0.1（2026-08-24）。来源：2026-08-22 窗口期调研 §4，由 R4.1 迁移前准备批次
（packet `task_20260824_213739`）定稿入库。

本文档是 R4.1 的跨实现验证契约：Rust MCP server 必须在冻结语料上复现 Python MCP
server 的输出，Python server 方可退役。oracle 为**稳定化后**的 Python 输出——即准备
批次加入 Rust 语言过滤、确定性次级排序与 `query_file_summary` 有界 `key symbols`
段之后的行为。

## 1. 快照身份

每次差分运行须记录以下身份字段，任一不匹配时比较器拒绝比较：

- DB 快照 SHA-256（冻结的 `logic_index.db` 文件）；
- 生成快照的代码树 git commit；
- logic index schema 版本（锚 `12.0.0`）；
- 差分工具/比较器版本；
- 查询层读取的全部 `REMY_MCP_*`、`REMY_FLOW_*`、`REMY_NAVIGATE_*` 配置快照。

## 2. 冻结 DB 快照——结构要求（R1–R8）

快照必须内含以下结构，否则对应非确定性/歧义路径测不到：

- **R1** 多文件同短名符号对（query_symbol 歧义路径、search tie-break）；
- **R2** 无摘要节点 + `stale` 状态节点 + `oversized_warn` 节点（状态文案全枚举）；
- **R3** file_count 并列的集群对（次级排序键语料——该键随准备批次落地）；
- **R4** 合成边（interface/observer/rust_trait 各一）供 `static_only` 分臂；
- **R5** 至少一行 `rust_trait_impl` patterns；
- **R6** BM25 rank 并列/接近的检索对（浮点漂移敏感）；
- **R7** judge_cache 预热行（navigate 缓存命中路径）；
- **R8** stored `source_commit` 与快照 git HEAD 一致（使 `random.sample` 新鲜度分支
  不可达）。

## 3. 逐 tool 查询矩阵（每 tool ≥3 组：常规/空结果/歧义或参数变体）

| Tool | 组1 常规 | 组2 空结果 | 组3+ 歧义/变体 | 比较层 |
| :--- | :--- | :--- | :--- | :--- |
| query_symbol | 唯一全名 | 不存在名 | 多文件同短名；+file 过滤 | 逐字节 |
| query_symbol_summary | 有摘要符号 | 不存在名 | 无摘要符号；stale 符号（R2） | 逐字节 |
| query_file_summary | 有摘要文件 | 不存在文件 | 无摘要文件；0 符号文件；key symbols 截断（> `REMY_MCP_RESULT_LIMIT`） | 逐字节 |
| query_callers | depth=2 默认 | 无调用者符号 | static_only=True；include_ambiguous=True；depth=1 | 逐字节 |
| query_callees | depth=2 默认 | 叶子函数 | static_only=True；include_ambiguous=True | 逐字节 |
| query_impact | 单文件 | 不存在文件 | 多文件；跨层文件 | 逐字节 |
| query_patterns | 无参全量 | 不存在 signal | pattern_type=rust_trait_impl；file 过滤 | 逐字节 |
| query_search | match=all 常规 | 无命中文本 | match=any；phrase；typo 触发 fuzzy；language（含 `rust`）/path_hint 过滤；R6 并列对 | 语义层（node_ref 序列 + rank 容差或仅比序） |
| query_flow | 双符号有路径 | 双符号无路径 | 三符号；qualified 语法（file:name / Class.method）；static_only | 逐字节 |
| query_cluster_summary | name="" 全集群（含 R3 并列对） | 不存在集群 | 单集群 | 逐字节（次级键随准备批次落地） |
| query_cluster_files | 常规集群 | 不存在集群 | with_summary=True | 逐字节 |
| query_navigate | R7 缓存命中意图 | —（LLM 层不设空组） | top_k=1 | 语义层，仅 judge_cache **命中路径**入基线；miss 路径排除 |

## 4. 比较层与排除项

- **逐字节**比较在剥离新鲜度警告前缀后进行（`[Warning: index may be stale …]` 行依赖
  启动时状态，不属于契约）。
- **语义层**（search/navigate）：仅比较有序 node_ref 序列，BM25 rank 数值不入断言
  （R4.1 裁定，2026-08-26——Python 内置 SQLite 与 rusqlite bundled SQLite 版本不同，
  浮点 rank 相等无保障，数值容差在每次 SQLite 升级时需重新标定）。
- **排除项**：navigate LLM miss 路径行为；新鲜度抽样随机性（R8 下不可达；**N2** 种子
  接缝已随 R4.1 首提交落地——`REMY_FRESHNESS_SAMPLE_SEED` 使回退分支切换为按 path
  排序、按种子旋转的确定性子集，跨实现可复现；该 env 键为 test seam，不注册进配置面）；
  search/navigate 的非 ASCII 标识符行为（casefold/Unicode 分类/fuzzy ratio 等价仅对
  ASCII 标识符保证——即当前全部索引语料；对未来非 ASCII 语料为已声明边界）。

### 4.1 允许的 tool schema 差异（R4.1 裁定，2026-08-26）

rmcp/schemars 输出与 FastMCP oracle 的差异仅限下列装饰层条目；超出本清单即缺陷：

| 条目 | FastMCP | rmcp/schemars |
| :--- | :--- | :--- |
| per-property `title` | 有（"Max Depth" 等） | 无 |
| 顶层 `title` | `query_xxxArguments` | 无 |
| 顶层 `$schema` | 无 | `https://json-schema.org/draft/2020-12/schema` |
| 整型 `format` | 无 | `int64` |

关闭动作：一次真实 Claude Code 会话对 Rust server 调用全部 12 tool，验证参数解析与
可调用性。

### 4.2 单实现 tool（裁定，2026-08-26）

仅有 Rust 实现、无 Python oracle 臂的 tool **不入差分矩阵**：为其新写 Python 参照与
Rust 实现之间没有独立正确性锚点（互相比较是循环验证），且向退役中的 Python server
添加代码与退役方向矛盾。其验收面改为专项测试套，且必须包含 DB 不可变断言
（调用前后快照 hash 不变）。

当前单实现 tool：`query_dependencies`（专项套：`tests/test_mcp_dependencies.py`）。
`tests/test_mcp_rust_parity.py` 的 tool 名单断言经 `RUST_ONLY_TOOLS` 跟踪它们。

对 eval 基准的影响：`eval/arms.py` 从 Python oracle 的 `list_tools()` 枚举工具面，
单实现 tool 不会进入 eval 臂——eval 工具面是生产工具面的真子集。

## 5. 状态

- N5（集群列表次级排序键）：**已随准备批次落地**——`query_cluster_summary(name="")`
  具备逐字节比较条件。
- N2（新鲜度抽样种子）：**已随 R4.1 首提交落地**——`REMY_FRESHNESS_SAMPLE_SEED`
  确定性子集模式（按 path 排序、按种子旋转）。
- `query_search` 自准备批次起接受 `language="rust"`，`query_file_summary` 输出有界
  `key symbols` 段；Rust 实现以该行为为 oracle 复现。
