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

**复审判据（R4.3）**：rust provider 作为唯一生产 provider 完整运行一个 release
周期且 `diagnostic` 恒空、残余风险登记册无针对性开放条目时，回切能力方可退役。
回切退役解锁——但不自动决定——Python worker 臂与 G4 探针通道（§2.4）的删除。

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
| 回切 + hook fallback + G4 通道联合复审 | R4.3 |
| `struct_scan.py` 消费者集合清空（hooks、worker、run.py） | R4.3 |
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
