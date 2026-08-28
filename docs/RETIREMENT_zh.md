# 退役登记册（R3.6 审计记录）

审计日期：2026-08-23。锚点提交：`c84e90e`。权威性：本文件记录 R3.6 对 Python 侧
scanner 组件与旧入口的退役审计裁定。每个条目记录 owner、生产消费者、裁定结论，
以及触发复审的可证伪条件。

## 1. 裁定摘要

| 组件 | 裁定 | 复审点 |
| :--- | :--- | :--- |
| Python scanner 生产臂（`struct_scan.py --result-json` 全量路径） | **保留** | R4.3（Python 退场阶段） |
| provider 回切能力（rust→python） | **保留** | R4.3，与生产臂同批 |
| Python hook fallback（`run_python_fallback` → hook 脚本 → `struct_scan.py`） | **保留** | R4.3，与回切同批 |
| `--worker-config-json` 明文 secret 探针通道（G4） | **保留**（与 Python worker 臂同生命周期） | R4.3，与回切同批 |
| `index_mcp_queries.py` 兼容壳 | **保留** | v1.7.1 后首个实质 release |
| `struct_scan.py` 兼容入口 | **保留** | R4.3（消费者集合须先清空） |
| Migration ladder 6→12（Python owner） | **保留，冻结**（R4.2 裁定：Rust owner 仅支持当前版本，阶梯不复刻） | R4.3（随 Python scanner 退场） |
| state.db v1→v2 迁移 + legacy manifest 翻译层 | **保留** | v2.0.0 发布审计（H8-B2/B6） |
| `install.py` v2 死臂（`write_manifest`/`do_install`/`do_uninstall`/`do_verify`） | **删除**（本批） | — |

## 2. 保留组件：证据与条件

### 2.1 Python scanner 生产臂与回切能力

审计首个 evidence 项固化的删除候选依赖链：

```
oracle 重生成工具 ← Python scanner ← Python full_scan 臂 ← provider 回切能力
```

- 稳定窗口经用户 2026-08-22 裁定提前关闭；日历判据（~1.9/7 天）为已登记残余
  风险。回切是该残余风险的唯一对冲手段。在风险仍开放时删除 Python worker 臂
  等于移除对冲。
- `struct_scan.py --result-json` 无 `--files` 是 daemon 执行 rust→python 回切
  全量扫描的机器契约（R3.5b）。
- oracle 重生成（pin venv）作为开发期 owner 永久依赖 Python scanner；它被排除
  在删除核算之外，不计为阻塞项。

**复审判据（R4.3——裁定 A 修订，2026-08-28）**：原文"一个完整 release 周期"无
操作定义，且以"下一 release 事件"为门槛存在循环依赖（下一 release 为 v2.0.0，
归 R4.4，晚于 R4.3）。修订后判据：7 天时间窗——2026-08-26（R4.1 关闭）至
2026-09-02——期间 rust provider 作为唯一生产 provider 运行且 `diagnostic`
恒空。时间窗**仅门控删除提交**；非删除工作（预研、审计、INV-R1 修订、样本源
替代）即刻启动。操作定义：删除提交仅在 2026-09-02 之后落地，且落地前须复核
窗口尾部 diagnostic 取证（`daemon.log` 全量 `provider_sync` 行 +
`remy-cc daemon status --json`）。开窗取证（2026-08-28）：切换事件后所有
`provider_sync` 均 `published=rust` / `diagnostic=null`。回切退役解锁——但
不自动决定——Python worker 臂与 G4 探针通道（§2.4）的删除；该删除由 §8
审计记录结清。

### 2.2 Python hook fallback 与 INV-R1

`hook_client.rs`（`run()`）：daemon IPC 路径失败时回退 `run_python_fallback`，
spawn 已部署的 Python hook 脚本，后者再子进程调用 `struct_scan.py`。删除
fallback 意味着：daemon 不可用 ⇒ 增量索引停摆。

- INV-R1（daemon-optional）仍是主计划跨阶段硬不变量。收窄它需要修订主计划，
  而非一条退役登记。
- 冗余评估（2026-08-23）：该 fallback 是单一回退臂、两个消费 hook，复用 worker
  臂同一个 `struct_scan.py` 入口——无重复 owner、无独立 parity 义务。在回切臂
  与 oracle 使 Python scanner 存活期间，其边际维护成本趋零。

**复审判据（R4.3，"最后生产消费者"规则）**：回切臂退役后，hook fallback 将成为
Python scanner 的最后一个生产消费者。为一条异常路径供养整个 scanner 即过度冗余
阈值：届时 fallback 须与 scanner 一并退役，或对照改写后的 INV-R1 重新论证。

**已结清（R4.3 审计，2026-08-28——§8）**：INV-R1 先行修订（纯收窄，父计划 §2；
journal 与 spawn 两种 Rust 承接形态均否决），hook fallback 与 Python **生产臂**
同批退役。"与 scanner 一并退役"裁读为生产臂而非仓内模块删除：删除面严格限于
臂/旗标/路由。scanner 模块集（`scanner.py`、`parsers/`、`schema.py` 与
`struct_scan.py` 入口）整体存活为开发期 owner——`oracle/manifest.py` 经
`sys.path` 进程内导入模块集，`oracle/bench.py` 消费人类输出 CLI 臂。

### 2.3 兼容壳

- `index_mcp_queries.py` 为纯再导出壳（A1.2），是 `index_mcp_server.py:28` 的
  活 import 路径；eval（`arms.py`、`build_tasks.py`、`retrieval_baseline.py`）
  与两个测试模块消费它。A1.2 承诺"至少一个发布周期"，裁定为：v1.7.1 后首个
  实质 release 解锁退役。退役顺序：先改 `index_mcp_server.py:28` import 到
  owner 模块，再迁移 eval/测试消费者，最后删壳。
- `struct_scan.py` 仍是稳定 CLI 入口，消费者为 enrichment hook、lifecycle
  hook、daemon Python worker 臂与 `run.py`（进程内 import）。其退役前置是该
  消费者集合清空（R4.3 范围），不挂发布周期时钟。

### 2.4 G4：明文 secret 探针通道

`--worker-config-json` 按契约在 stdout 明文输出 `secret_values`（消费者：
`worker.rs:261`、`provider.rs:258`）。该通道与 Python worker 臂同生命周期：
R4.3 同批复审退役。在此之前的现行缓解为操作性约束（不在留痕终端手动调用）。

**已结清（R4.3 审计，2026-08-28——§8）**：通道随 worker 臂整体退役——Rust 侧
探针消费端（`provider.rs::validate_python`）归 daemon 侧退役提交删除，
`--worker-config-json` 臂与 `_worker_config` 归 Python 侧删除提交。不保留
去 secret 的探针变体；删除提交受 §2.1 时间窗门控。

### 2.5 兼容底线

- 最低支持起点：**v1.4.0**（logic index schema 6.0.0；无损升级在 R4.3 前经
  冻结的 Python ladder 执行，其后转为备份重建形态）。
- 阶梯归属裁定（R4.2，2026-08-25——取代此前"整体随迁"形态）：Rust owner 仅
  支持当前 schema 版本。打开时，低于当前版本的库——或含表但无版本行的库——
  先以 SQLite backup API 备份为 `.bak` 再按当前 schema 重建；增量入口随后在
  同一持锁调用内升级为全量文件集。高于当前版本或版本串不可解析的库拒绝并保留。
  六段 ladder 不在 Rust 侧复刻。裁定接受的数据代价：`summary_versions` 内容
  不随迁（保留在 `.bak` 中；重生成走既有 bootstrap 管线）。对外兼容底线的正式
  抬升声明归 v2.0.0 发布审计（H8-B6）；父计划 R4.2 行预留的"最低兼容版本截断
  裁定"槽位由本条结清。
- Python ladder（`migrations.py`）冻结至 R4.3：不改任何段；唯一例外是窗口期
  内发生 schema bump 时的同步增段（Rust 重建下界自动跟随 `SCHEMA_VERSION`，
  无需改动）。
- **已结清（R4.3 审计，2026-08-28——§8）**：六段 ladder、`MIGRATION_HANDLERS`、
  `_resolve_migration_path` 与 `migrate_json` 归 Python 侧删除提交删除；
  `initialize_database` 改为 fail-closed（任何非当前版本报错退出并保留数据库
  原样），无损升级通道的关闭为有意行为，以显式拒绝测试确认。重建测试套的
  样本源自 ladder 工厂改为冻结 DDL 快照（`tests/schema_snapshots.py`，
  v6/v7/v10），在窗口内、任何删除之前落地；窗口内不改写 `ladder_samples.py`。
- daemon state schema v1→v2 迁移与安装器 legacy manifest 翻译层
  （`facade._parse_legacy_manifest`）保留至 v2.0.0 发布审计对存量 v2 安装
  人口做出裁定（H8-B2/B6）。

## 3. 删除组件：证据

### 3.1 install.py v2 死臂（B3-1）

`main()` 三分支全部路由 v3 facade（`do_install_v3` / `do_uninstall_v3` /
`do_verify_v3`）；v2 函数体（`write_manifest` L646、`do_install` L1080、
`do_uninstall` L1275、`do_verify` L1355，约 500 行）无任何调用者。对
`install.py` 的静态调用图分析证实 v2 专属 helper 集合为空——v2 臂调用的全部
helper 均与 v3 共用并保留。卸载 legacy（v2 manifest）安装由 facade 读取时
翻译层承担，非 v2 臂；删除死臂不影响该能力。直接调用 v2 臂的测试随之删除；
编码 legacy manifest 语义的断言由翻译层测试（`test_cli_manifest.py`）覆盖。

## 4. C2 结算记录

跨语言 symbol hash 裁定（C2）随本批结算：Python 侧 docstring 在双实现中剥出
symbol hash 输入（parser span 定位剥除；`CACHE_CONTRACT_VERSION` 2→3），关闭
oracle manifest 中的 `python-docstring-in-hash` 已知缺口。顺序约束已履行：
F.1（差分基线变更）先行，C2（oracle 身份变更）在后，两者未交叉。

## 5. R4 移交项

| 事项 | 归属 |
| :--- | :--- |
| 回切 + hook fallback + G4 通道联合复审 | R4.3——已审计 2026-08-28（§8）；删除提交门控至 2026-09-02 后 |
| `struct_scan.py` 消费者集合清空（hooks、worker、run.py） | R4.3——已审计 2026-08-28（§8）；路由改指 `remy-daemon scan` |
| `index_mcp_queries.py` 壳退役（先改 import 接线） | v1.7.1 后首个实质 release |
| Migration ladder 归属（当前版本重建语义；阶梯不复刻） | R4.2——已结清 2026-08-25（§2.5） |
| Legacy manifest 翻译窗口关闭 | v2.0.0 发布审计（H8-B2/B6） |
| Python scanner 退场后 rconfig 双 owner 单源化 | R4.3 |
| 探针语料 / parser 支持矩阵一致性检查 | 常设（§9 矩阵） |

## 6. Python 退场边界（H.6，R4.0 审计记录）

审计日期：2026-08-23（R4.0）。「Python 退场」（R4.3）定义为**生产路径退场**。
每个 Python 侧组件恰归属下表一类；R4.3 删除清单、H8-B2（settings 合并宿主语言）
与 H8-D3（诊断归属）以本表为边界权威。

| 分类 | 组件 | 裁定 |
| :--- | :--- | :--- |
| 1. 生产 worker 臂 | Python scanner 生产臂（`struct_scan.py --result-json` 全量扫描路径）、daemon Python worker 臂、rust→python 回切、hook fallback、G4 探针通道 | R4.3 退场，受 §2.1/§2.2 判据门控 |
| 2. hooks 本体 | `hooks/*.py` 运行时钩子（R2 仅迁记账，不迁 hook 本体） | 长存 Python（非退场） |
| 3. 配置与 CLI 面 | `config_ui.py`、`remy_config.py` registry、`cli.py` 全部子命令族（含 `summary-*`）、`remy-cc` shim | 长存 Python（非退场）；shim 在 I3 后的指向由 R4.4 裁定（H8-B5） |
| 4. 开发期工具 | `oracle/`、`eval/` | 永久保留；不计退场阻塞 |

H.5 裁定（R4.0，2026-08-23）：**summary runtime**
（`summarizer` / `propagation` / `llm_judge` / `bootstrap` + `llm_client`）
长存 Python，生命周期计入分类 3。MCP `query_navigate` 的 LLM 通道在 R4.1
以 Rust 重写（reqwest，OpenAI 线协议，单 POST；TLS 键语义自
`REMY_LLM_TLS_INSECURE` 移植）。navigate prompt 为内嵌字符串，D2（prompt
资产根）不被 R4.1 触及；仅当未来裁定重写 summary runtime 本体时才需处理。

`remy_config.py` 在任何裁定下均存活（G2 已核实：hooks / skills / MCP / cli /
install / config UI 共 29 处生产 import）；R4.3 的 rconfig 单源化命题因此是
契约同步义务归属问题，而非存活侧选择问题。

## 7. Python MCP server：部署面退役（R4.1 审计记录）

审计日期：2026-08-26（R4.1）。MCP 读路径迁入 Rust 宿主（`remy-daemon mcp`，
rmcp 3.1.4，per-session stdio；INV-R2 拓扑不变）。退役范围为**仅部署面**，
与 §6 边界一致：

- `remy_mcp.json` 改注册 `~/.remy-cc/bin/remy-daemon` + `["mcp"]`
  （由 `install.py::register_mcp_server` 展开为二进制绝对路径）。
- 六条 `index_mcp_*.py` 条目移出 `DEPLOY_FILES_MAP`；已部署副本由下次
  install 事务的 delete 语义清除。
- `mcp` SDK 不再是 install 必装依赖（`_prepare_dependencies` 撤销必装检查）；
  保留在 `requirements-dev.txt` 供存留消费者使用。
- **仓内保留**（§6 第 4 类）：`remy-src/index_mcp_server.py` 与五个 owner
  模块作为差分 oracle 及 `eval/arms.py`（FastMCP `list_tools()` schema 来源）
  与测试模块（`test_freshness.py`、`test_mcp_server_invariants.py`、
  `test_retrieval_baseline.py`、`test_mcp_rust_parity.py`）的存活消费面。
- 差分证据：`tests/test_mcp_rust_parity.py`（H.4 矩阵；10 tool 剥警告前缀后
  字节级，search/navigate 比较有序 node_ref 序列）。复审点：若 Python oracle
  模块失去最后一个开发期消费者，按第 4 类常规清理退役，无需新审计。

## 8. R4.3 审计记录（2026-08-28）

审计日期：2026-08-28（packet `task_20260828_020823`，锚点 `a474e5f`）。
17 项裁定经三轮 `AskUserQuestion` 加场景确认门全部锁定。提交序列：
0（INV-R1 修订，父计划）→ 1（本记录与裁定文本）→ 2（样本源）窗口内落地；
3a（Rust 侧退役）→ 3b（消费者路由改造）→ 3c（Python 侧删除）→ 4（doc-sync）
于 2026-09-02 后落地，每次落地前执行 §2.1 窗口尾部 diagnostic 复核。
3a 先于 3c：Rust 侧停用 fallback 路由后，Python hook 脚本方成孤儿。

| # | 裁定 |
| :--- | :--- |
| 1 | 判据修订=裁定 A：7 天时间窗至 2026-09-02，仅门控删除提交（§2.1） |
| 2 | INV-R1 处置=纯收窄；journal 与 spawn 两种 Rust 承接形态均否决。降级面仅为 Dirty 提交（enrichment 注入为 Rust 直读）；陈旧窗口由下次扫描经 struct_hash 最终一致闭合；父计划 §5.1 竞态叙述收窄为单写者。**执行注记（2026-08-28）**："Rust 直读"判读未覆盖 freshness 段的 IPC connect 依赖；`7a884a5` 把 connect 失败降级为空新鲜度信号，daemon 停止时 enrichment 照常输出 |
| 3 | §2.2 删除面措辞：严格限于臂/旗标/路由；scanner 模块集整体存活为开发期 owner（§2.2） |
| 4 | G4 随 worker 臂整体退役；不保留去 secret 探针变体（§2.4） |
| 5 | `struct_scan.py` CLI 入口存活；保留面=人类输出臂、`--result-json`、`--files` / `--cwd` / `--lock-timeout`（oracle `bench.py` 消费人类臂） |
| 6 | 重建测试套样本源=新建 `tests/schema_snapshots.py` 冻结 DDL 快照工厂（v6/v7/v10），删除前用现 handler 构造一次并经规范化 `iterdump` 固化，提交内含等价性一次验证；窗口内禁止改写 `ladder_samples.py` |
| 7 | ladder 删除：六段 handler + `MIGRATION_HANDLERS` + `_resolve_migration_path`；`initialize_database` 改 fail-closed 并以显式拒绝测试确认（§2.5） |
| 8 | `migrate_json` 删除；TESTING R4.2 窗口纪律条目在 doc-sync 提交中关闭，作为有意关闭的记录 |
| 9 | `run.py` 路由（两臂）：结构段改 subprocess `remy-daemon scan --result-json`；语义段自开连接（WAL、busy_timeout、`meta.version == 12.0.0` 断言），不承担建库或 schema 重放；两段之间锁窗口允许其他写者插入，无正确性影响 |
| 10 | `lifecycle_hook.run_struct_scan` 同构 spawn `remy-daemon scan`：二进制发现=`~/.remy-cc/bin/` + 开发树 `target/{release,debug}` 回退，超时=`REMY_STRUCT_SCAN_TIMEOUT` + `REMY_INDEX_SCAN_LOCK_TIMEOUT` + 5 s，`--lock-timeout` 透传，二进制缺失时 stderr 一行提示并跳过 |
| 11 | Python dirty queue 全链退役（`DirtyQueue` / `DirtyClaim` / `manage_dirty` / `--consume-dirty` / 两个 hook 文件）；lifecycle 增加 `.claude/logic_index_dirty*` 残留的一次性清扫 |
| 12 | `REMY_SCANNER_PROVIDER` 配置键删除；用户残值零噪声（Python 侧未注册键静默入 unknown 桶；Rust 侧读取链删除） |
| 13 | `REMY_MIGRATION_KEEP_JSON` 配置键随 `migrate_json` 删除 |
| 14 | install `hook_mode="python"` 臂删除；`facade._select_daemon` 降级分支改为报错中止并给出指引 |
| 15 | `state.db` 历史 `provider='python'` 行零迁移——provider 在 claim 时经 UPDATE 快照写入，读路径无校验；启动 sync 无条件以 rust 覆写 published。jobs 与 published 行的 DDL 字面 `CHECK (provider IN ('python', 'rust'))` 为容忍历史行而有意保留（state schema v2 不 bump） |
| 16 | `python.json` 运行时描述符：daemon 侧消费链本批删除；install 侧探测与部署保留至 R4.4（install.py 退役时统一处置） |
| 17 | 验收=三通道引用扫描（grep 符号清单 + `query_callers` + importlib 字符串扫描，`query_dependencies` 对动态导入不可见）、每提交独立全量 pytest + pyright、Rust 提交后 cargo fmt/clippy/test、oracle/eval/`.oracle-venv` 零波及显式核对、实机探针前先 `cargo build --release`、双平台 CI（用户确认制） |
