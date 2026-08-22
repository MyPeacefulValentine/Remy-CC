# 测试

## 环境

Remy-CC 支持 Python 3.10 及以上版本。使用以下命令安装固定版本的开发工具：

```bash
python -m pip install -r requirements-dev.txt
```

该文件包含pytest、Pyright以及`tests/test_freshness.py`所需的MCP SDK。

使用以下命令安装可选的高精度解析器包：

```bash
python -m pip install -r requirements-tree-sitter.txt
```

## 验证基线

P0.1a/P0.1b实施前，仓库工作树可收集454项测试。2026-08-01的P0.6函数指针pattern修正验证收集并通过512项测试。

```bash
python -m pytest tests -q -p no:cacheprovider
pyright -p pyrightconfig.json
python -m pytest tests/test_struct_scan.py tests/test_enrichment_hook.py tests/test_index_state.py -q -p no:cacheprovider
python -m pytest tests/test_migration_ladder.py -q -p no:cacheprovider
python -m pytest tests/test_synthesizers.py tests/test_c_fnptr_dispatch.py -q -p no:cacheprovider
python -m pytest tests/test_struct_scan.py tests/test_fts_three_layer.py -q -p no:cacheprovider
python -m pytest tests/test_tee_canary.py -q -p no:cacheprovider
PYTHONPATH=. python -m eval.cli retrieval-baseline --help
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_1.json --save
python tests/tee_project_canary.py tests/fixtures/tee_canary --fixture --backend regex --scope product
# 已安装 requirements-tree-sitter.txt 时：
python tests/tee_project_canary.py tests/fixtures/tee_canary --fixture --backend tree-sitter --scope product
```

持续集成在不安装tree-sitter的Python 3.10环境和安装固定tree-sitter包的Python 3.12环境运行全部测试。两个作业分别使用可用解析后端执行固定公开TEE fixture canary。Windows Python 3.12作业安装固定tree-sitter包，运行进程锁、脏队列和Rust解析器测试（同时验证pin的grammar组合在Windows可安装）。Rust作业的Linux与Windows两臂均安装固定tree-sitter包并运行`tests/test_scanner_core_diff.py`跨实现差分测试（四语言fixture语料、混合项目、Python失败映射；R3.4起增加全量视图差分——含后处理产物edge_candidates/clusters/retrieval_documents与推断边、跨文件trait-impl一致性、旧schema版本fail-closed守卫——以及`tests/test_postprocess_parity.py`的摘要失效状态机双侧逐值断言、后处理失败注入与窄配置默认值契约；无cargo二进制或无tree-sitter时跳过）。

## P1.1确定性检索基线

`eval/tasks/retrieval_baseline/p1_1.json`声明合成schema 10.0.0 fixture和人工审查的候选级真值。基线记录FTS、LIKE、fuzzy各通道、公共回退输出、Recall@1/5/10、MRR、无结果指标、数据库/WAL大小和全部延迟样本。每项测量执行3次预热和30次记录。耗时只作观测，不作为通过阈值。

`eval/results/`中的带时间标识原始记录继续由Git忽略。审查后必须显式传入`--update-snapshot`才能更新`eval/baselines/p1_1.json`。命令记录Git提交、schema、Python版本、平台和合成解析配置。提供`--navigate-db`时，命令还记录cluster/file数量和prompt字符数，但不调用LLM，也不写入`judge_cache`。

## P1.2查询语义与过滤

运行格式1.1.0任务集和未修改P1.1任务的兼容比较：

```bash
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_2.json --update-snapshot eval/baselines/p1_2.json
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_1.json --compare-baseline eval/baselines/p1_1.json --comparison-output eval/baselines/p1_2_compat.json
```

P1.2记录all/any/phrase匹配、语言/类型/路径SQL过滤、输入和通道错误、
与插入顺序无关的LIKE/fuzzy结果，以及同名符号fuzzy最终limit。两次运行仍执行
3次预热和30次测量。P1.2 fixture继续使用schema 10.0.0。

## P1.3候选并集

`query_search`独立生成exact、prefix和BM25三类候选，按`node_ref`合并去重并
保留每个节点的全部匹配来源与来源内名次，按确定性优先级（exact、prefix、
BM25、来源内名次、名称、文件、行号）排序，截断只在合并之后执行。fuzzy仅在
三类确定性候选均为空时执行。任一通道的SQLite错误仍整体返回`Error:`结果，
不做部分降级。每个结果保留原有定位行不变，并追加缩进的`sources`/`priority`
与`sig`/`summary`行。精确等值使用注册的Python `casefold` SQL函数实现，因为
SQLite的`NOCASE`和`lower()`只处理ASCII。schema保持11.0.0，无需migration。

运行并集任务集和兼容比较：

```bash
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_3.json --update-snapshot eval/baselines/p1_3.json
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_1.json --compare-baseline eval/baselines/p1_1.json --comparison-output eval/baselines/p1_3_compat.json
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_2.json --compare-baseline eval/baselines/p1_2.json
```

`p1_3.json`复用P1.1 fixture和16个查询原文，期望值在实现前按新排序规则人工
推导。`summary_name_conflict`任务断言名称候选`encrypt_session_tokens`经
prefix通道进入公共结果并排第1，摘要候选`persist_blob`保留在并集中。运行记录
保存四通道候选列表、带来源与优先级的合并结果、Recall@1/5/10、MRR、延迟样本
和数据库大小。`p1_1.json`与`p1_2.json`中的`expected_channel`记录旧通道语义，
与其失配属于预期中的通道重组，不是缺陷。

## P1.4意图导航候选缩减

`query_navigate`不再把全部cluster与file摘要写入LLM prompt。意图拆词后以
`any`语义复用P1.3确定性通道生成symbol候选（单词意图在确定性通道全空时追加
fuzzy），file/cluster候选来自投影行的加权BM25查询；每层候选受
`REMY_NAVIGATE_CANDIDATE_CLUSTERS/FILES/SYMBOLS`（默认5/10/10）约束，只为
入选候选读取摘要并构造prompt。词法候选为空时降级为cluster-only prompt
（`source=llm-cluster-only`），无LLM时按候选确定性顺序输出
（`source=heuristic`）或在候选为空时返回`No matches`。缓存键由规范化意图、
`top_k`、候选`(node_ref, content_hash)`序列与prompt模板版本哈希派生，存入
现有`judge_cache`；与候选无关的摘要写入不再使缓存失效，`top_k`进入键。
schema保持11.0.0。同期索引内摘要统一英文存储：`run.py`摘要语言固定English、
cluster标签固定en集、`SUMMARY_ZH_LENGTH_FACTOR`注册字段随本阶段移除（存量
配置文件中的该键不再激活，按未知字段round-trip保留）；存量中文摘要仅在重新
生成或`remy-cc summary-rebuild`时替换。

运行P1.4任务集（tasks逐字复用p1_3以验证`query_search`契约不回归；
navigation区块扩展为英文/中文/混合三条意图）：

```bash
PYTHONPATH=. python -m eval.cli retrieval-baseline --tasks eval/tasks/retrieval_baseline/p1_4.json --navigate-db .claude/logic_index.db --update-snapshot eval/baselines/p1_4.json
```

navigation记录为同库双口径：corpus口径（cluster/file数、有摘要file数、
`corpus_chars`=全语料prompt等价字符数）与candidate口径（各层候选数、
`prompt_chars`、`fallback_reason`、候选内容身份缓存键）。验收断言
`prompt_chars < corpus_chars`且候选总数不超过配额和；中文意图记录
`fallback_reason=lexical_empty`（unicode61将连续CJK收为单一token，词法
通道对中英文语料均为空集，由审计探针证实）。p1_1基线的1346字符测量基于
已漂移的语料范围（当时0个file有摘要），不作为对比基准。

## A1.1 run.py职责拆分

`llm_client.py`（`LlmClient`类：HTTP传输、上界退避重试、401/403/429熔断、
截断检测、错误分类）与`propagation.py`（强制重算判定、计数器清零、候选
收集、子变更载荷、父摘要重写、传播主流程）自`run.py`拆出。`run.py`保留
`LogicIndexer`作为编排与CLI入口：参数、输出状态、退出码`0 / 2 / 1`、
`success / partial / failed`聚合规则与dirty确认均不变。
`index_mcp_queries.py`与`cli.py`的默认LLM通道改为直接构造`LlmClient`
（每次调用新建实例，熔断不跨调用，与拆分前行为一致）。

```
python -m pytest Remy-CC/tests/test_llm_client.py Remy-CC/tests/test_propagation.py -v
```

等价性由一次性探针验证（不设永久golden测试）：拆分前树
（`git archive HEAD`）与拆分后树在同一无API key fixture上各跑
`LogicIndexer.run()`，`files`、`symbols`、`summary_versions`、
`retrieval_documents`、`node_change_counters`五表dump与全部`RunResult`
字段逐字节一致（时间戳列除外）。导入`llm_client`无网络I/O、不创建文件；
拆分前`import run`基线为0.068秒（只记录，不设阈值）。

同一版本内注入系统收敛到MCP minimal视图：`generate_logic_tree_view`
无条件渲染该视图，范围选择器全链（`logic_scope_ui.py`、
`remy-cc logic-scope`、selection文件）删除，5个注入env字段移除（注册表
55字段，injection组8字段），存量配置中的旧键经strict加载round-trip保留
且不激活。安装器无条件安装`mcp`包，pip失败或Python低于3.10时中止。校验有两个
独立入口，二者必须遵守同一契约：源码目录的`install.py --verify`与已安装
shim背后的`cli.py::cmd_verify`（`remy-cc verify`）。两者都把缺失的`mcp`包
记为错误并以退出码1结束，也都要求Python 3.10。

## query_impact渲染与计数

`_format_impact_result`为每个深度层列出去重后的文件路径，含多个匹配符号的
文件只打印一次，不再按符号重复。层内`file(s)`、`symbol(s)`计数与
`files affected`总数在该层全集上计算。此前文件集合在
`REMY_MCP_RESULT_LIMIT`前缀上累加，而符号总数使用完整列表，summary行的两个
数字取自不同样本；`REMY_MCP_RESULT_LIMIT`不再作用于该工具。每层标签上限为
5个文件，其余以`+N more file(s)`说明。

```
python -m pytest Remy-CC/tests/test_mcp_graph.py -k Impact -v
```

`TestQueryImpactRendering`覆盖标签去重、层内计数、两个result limit下输出一致、
limit小于该层符号数时的文件总数、截断标记，以及全部文件都展示时不出现该标记。

## 摘要失效范围

摘要失效只在结构身份变化时发生。`scanner.scan_file`在符号hash变化时把该symbol
摘要置`stale`；只有文件符号集合变化（`old_symbol_refs != new_symbol_refs`）时
才把file摘要置`stale`；`_detect_clusters`只在集群成员集合变化时把cluster摘要置
`stale`。此前`scan_file`对每个被重扫的已存在文件无条件置file摘要为`stale`，并
经`mark_node_and_ancestors_stale`级联到cluster。由于
`summarizer._bump_parent_counter_if_applicable`在父摘要为`stale`或缺失时跳过
自增，父节点从不进入`collect_propagation_candidates`，`judge_propagation`在内容
变化路径上不被调用。该函数已删除，两处调用改为`mark_current_summary_stale`。

```
python -m pytest Remy-CC/tests/test_struct_scan.py -k SummaryInvalidationScope -v
python -m pytest Remy-CC/tests/test_summary_versions.py -k ParentCounterBump -v
```

`TestSummaryInvalidationScope`用真实扫描覆盖：只改函数体时file与cluster摘要保持
可用、新增符号与删除符号时file摘要失效、只改函数体时symbol摘要仍失效。断言直接
查询`summary_versions`最新版本状态，不经过被测模块的`select_current_summary`。
`TestParentCounterBumpOnWrite`新增两项：父摘要为`stale`时不自增计数器，以及
`stale`屏障阻断更早的`ok`版本——后者与"父节点无任何摘要行"在
`select_current_summary`中走不同分支。

本仓库索引上的一次性验证记录：修复前一次扫描输出`PROPAGATION_RESULT`六项全零，
20个file与4个cluster摘要全部无条件重建；修复后一次扫描输出
`file_skip=1 cluster_propagate=1 cluster_skip=1`，`judge_cache`新增3条LLM判定
记录，只有3个符号集合变化的file进入bootstrap，cluster层bootstrap为0 pending。
两次扫描的变更文件数不同（20与3），调用次数不作为成本对比依据。

## 摘要重写的成本门控

`summarizer.write_summary_version`把新payload与紧邻上一版本的payload比较——即被写
入版本之下version最大的那一条，不筛其status——两者相同时跳过父节点
`child_change_count`的自增。版本行仍然插入、`refresh_node`仍然执行，因此version保
持单调、投影保持当前。前驱缺失，或前驱的`summary`为NULL（`status='pending'`），都
记为发生变化。`propagation.build_child_changes_payload`采用同一个"上一版本"定义，
因此已被标记`stale`的前驱作为比较基准，不再产生`old_summary: null`。
`run_propagation_pass`在`child_changes`为空时清零计数器；`propagate=false`仍然保持
计数器向`REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY`累积。

```
python -m pytest Remy-CC/tests/test_summary_versions.py -k "ParentCounterBump or PromptExampleFieldContract" -v
python -m pytest Remy-CC/tests/test_propagation.py -k "BuildChildChanges or RunPropagationPass" -v
python -m pytest Remy-CC/tests/test_llm_judge.py -k PromptExampleFieldContract -v
```

`TestParentCounterBumpOnWrite`覆盖：相同payload不自增、不同payload仍自增、跳过自增
时version仍递增、键顺序不同但内容相同、前驱为`pending`时重新自增、前驱为`stale`且
文本相同时不自增，以及file到cluster一级的同一门控。`TestBuildChildChanges`覆盖：
`stale`前驱作为比较基准、跨`stale`前驱文本相同时返回空列表、`pending`前驱产生
`old_summary: null`。`TestPromptExampleFieldContract`（分别位于
`test_summary_versions.py`与`test_llm_judge.py`）从`summarize_file.md`、
`summarize_cluster.md`和`judge_propagation.md`解析示例payload，断言其键集是
`_file_input`、`_cluster_input`和`build_prompt`实际产出键集的子集。此前这三个模板
记录的字段名没有任何调用方传入。

本仓库索引上决定比较语义的测量：已存储的57次版本转换，其前驱status全部为`stale`
（symbol 34/34、file 18/18、cluster 4/4），因为`scan_file`在重写前把当前摘要置
`stale`。把查询限定为`status IN ('ok','oversized_warn')`因此不匹配任何一条——0/57。
改为与紧邻上一版本比较且不筛status，则symbol层匹配11/34，file层0/18、cluster层
0/5，因此节省只存在于symbol到file这一级。

符号hash的alpha归一化经测量后否决。最近40个提交构成的39个提交对中，5473次符号比较
里有426次被当前hash判定为变化。把两侧都经`ast.unparse`重新归一化后匹配0/426，再把
局部变量与参数重写为位置占位符后同样匹配0。对照实验确认探针有效：纯局部重命名、参
数重命名、引号风格变化、冗余括号变化各自都被识别，而`+1`改为`+2`的真实变化未被识
别。`_calculate_symbol_hash`已移除全部空白、`_strip_comments`（R3.0a 起由
`LanguageParser.symbol_hash_input`替代）已移除注释，因此不存
在只含格式差异的变化类别能到达hash。

## P1.2.1扫描范围与解析器缓存身份

schema 11.0.0为每个`files`行增加`parser_contract_version`、
`parser_backend`和`parser_environment`。10.0.0到11.0.0 migration保留源码哈希
和结构事实，为旧行写入空解析器身份。后续扫描只有在单文件解析成功后才替换该文件身份。

增量扫描测试验证配置排除会删除既有事实和检索文档、被排除的脏路径会被确认、解析器
契约或后端变化只重解析受影响文件、失败重解析保留旧事实和旧身份，并比较增量状态与
新数据库全量状态。TEE canary报告记录regex和tree-sitter运行实际保存到数据库的去重
解析器缓存身份。

```bash
python -m pytest tests/test_struct_scan.py tests/test_migration_ladder.py tests/test_tee_canary.py -q -p no:cacheprovider
```

## P1.2.2 Remy独立配置

Python运行时Remy参数使用`~/.claude/remy-config.json`，项目覆盖使用
`<project>/.claude/remy-config.json`。测试覆盖来源优先级、严格schema与类型校验、
密钥脱敏、项目密钥拒绝、伴随文件锁、原子替换、一次迁移备份、哨兵拒绝和模拟
CC Switch重建`settings.json`。

```bash
python -m pytest tests/test_remy_config.py tests/test_install_manifest.py tests/test_cli_manifest.py -q -p no:cacheprovider
```

Windows CI同时运行`test_remy_config.py`和`test_index_state.py`。P1.2.1的
schema 11、解析器身份、排除规则以及增量/全量状态比较继续由全量回归覆盖。

## P1.2.3 Config UI行为

UI-A阶段将后端配置与未保存草稿分离，并定义
`reset_mode=none/non_secret/all`。测试覆盖稀疏更新、项目覆盖、密钥保留与显式
清除、未知字段保留、非法与混合重置拒绝、保存后刷新结果、活动写请求期间的
显式关闭、活动请求清理和禁用控件保护。Node测试从`config_ui.html`提取并执行
payload、实际差异和保存结果状态函数。

```bash
python -m pytest tests/test_remy_config.py tests/test_config_ui.py -q -p no:cacheprovider
python -m pytest tests -q -p no:cacheprovider
pyright -p pyrightconfig.json
python -m compileall -q remy-src tests
# 可选浏览器验证：
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
python -m pytest browser_tests -q -p no:cacheprovider --browser chromium --tracing off --video off --screenshot off
```

UI-B1在全局模式增加内存内LLM端点测试。浏览器将当前API密钥动作、端点和模型
传给本地Python服务，不先保存配置。服务端发送一次最小chat-completions请求，使用
15秒超时、零重试、系统默认TLS校验、64 KiB本地请求限制和1 MiB上游响应限制。
连接测试不跟随重定向，因为urllib默认会在重定向请求中保留Authorization头。

所有POST入口要求精确Host、精确Origin、JSON Content-Type和进程级256位会话令牌。
每个HTML响应使用独立的128位脚本nonce。动态HTML和JSON响应发送`no-store`、
`no-referrer`与`nosniff`；HTML还使用nonce CSP，禁止frame嵌入、表单、外部脚本
和非同源连接。测试只使用唯一假密钥与本地假服务，拒绝真实外部请求，并断言密钥
不进入GET/测试响应、错误、日志或浏览器制品。Playwright在独立Ubuntu Chromium
作业运行，不生成trace、video或截图。浏览器可能暂停后台标签页，因此本地服务不再因
heartbeat停止而自动关闭。服务只在页面“退出”按钮调用`/api/shutdown`或终端收到
`Ctrl+C`时关闭；关闭或最小化浏览器本身不会结束进程。JavaScript与Python字符串不能被证明已从进程
内存清零；实现只避免持久化、限制副本并释放临时payload引用，同时保留未保存草稿。

UI-A验证基线为604项测试通过，Pyright没有错误或警告。本地Edge 151验证初始
保存按钮处于禁用状态、禁用样式可辨识，并且无修改点击保存后退出不会出现未保存
确认。浏览器自动化、API端点通信测试、响应式布局、动效和可访问性留在B1/B2阶段。

UI-B2-A重构配置信息结构。注册表为每个字段声明双语短名称、可选成对单位、
`advanced`标记和四值`restart_scope`（`immediate` / `next_index` /
`next_session` / `next_mcp_launch`）。7个显示组为
`llm_api / index_generation / injection / mcp / summary / timeline / system`，
字段计数7/12/13/6/12/2/6（共58项，配置键与schema 1.0.0不变）。页面分离
`#remy-host`宿主区（标题、模式、语言、退出）与`#config-page`配置区（搜索、
分组导航、字段、页内sticky操作区）。桌面使用分组侧栏，900px以下改为顶部原生
select。常用字段仅7项，其余按组折叠为高级设置；已修改或待恢复字段固定显示。
搜索在本地对键名、双语标签、双语说明和双语组名执行规范化Unicode小写子串匹配，
保持注册表顺序，匹配组锁定展开，Escape或清除后焦点返回搜索框。全局单字段恢复
使用新的严格`/api/save` `remove_keys`契约：重复、未知、密钥、项目模式或与重置
混合的请求返回HTTP 400且文件字节不变。项目恢复继续使用overrides差集路径。
B1连接测试移至LLM服务组级控件，请求、安全和生命周期契约不变。

B2-A验证基线为全量648项测试通过（新增6项：`test_remy_config.py`的注册表元数据
与FieldSpec校验；`test_config_ui.py`的remove_keys接受/拒绝、GET元数据和搜索/
状态/恢复Node纯函数），另有8项Playwright Chromium测试（新增4项：1280×800桌面
搜索/导航/高级折叠、390×844分组select、含“编辑取消恢复”的单字段恢复往返、
被搜索隐藏的已修改字段仍被保存）。注册表测试断言全部58项字段到组的精确归属
及代表性消费者的`restart_scope`取值。两次Pyright运行均为0错误0警告。

## 调用边解析：形态降级与import补链

schema 12.0.0为`edges`增加`call_form`（`name` / `attribute`，默认`name`），为`files`
增加`import_bindings`（parser无法映射到项目文件的导入绑定，JSON列表）。Python解析器
区分`ast.Name`与`ast.Attribute`调用并收集未解析导入绑定；C/C++与TypeScript解析器不
变，两列保持默认值。11.0.0到12.0.0 migration幂等加列；在`edges`表尚不存在的早期阶梯
起点上，handler跳过该ALTER，表随后由`SCHEMA_SQL`以含新列的定义创建。

`_resolve_call_edges`在每次postprocess从全库`import_bindings`内存派生两组数据，不落
盘、与文件扫描顺序无关：模块名对已索引Python文件的唯一路径后缀匹配（`pkg/mod.py`或
`pkg/mod/__init__.py`；多重命中视为不确定）成为import层补充；无命中（stdlib经
`sys.stdlib_module_names`短路）把绑定名标记为外部。裸名callee命中外部集合时跳过全局
回退。属性调用在import层或全局层命中时降级为`speculative`；同文件层豁免，
`self.method()`类内调用保持`definite`。降级的单候选边不写入`edge_candidates`。

```
python -m pytest Remy-CC/tests/test_struct_scan.py -k ResolveCallEdges -v
python -m pytest Remy-CC/tests/test_migration_ladder.py -k v11_to_v12 -v
```

验证记录（2026-08-08，双实现对同一97文件语料全量重扫）：

- provenance分布：definite 1366→1706、probable 1543→101、speculative 223→1322、
  未解析4854→4857。`test_mcp_minimal.py`对`patch_descriptions.py::patch`的同名假边
  从`probable`变为未绑定。
- pyright全图真值（复用`eval/gen_gt.py`的`LspClient`单会话收割93文件、708调用方、
  1521条项目内边，47.8秒、0错误；按A1.1先例为一次性探针，不设永久harness）：
  static-only口径（definite+probable）精确率0.712→0.919；全解析口径precision
  0.691→0.693、recall 0.951→0.952。109条属性形式的真实调用（如
  `remy_config.load_config`）按降级规则进入`speculative`，解析目标保持正确。残余
  `probable`层（40条GT可判定的裸名全局单候选）实测精确率0.025。
- 查询层零改动：flow输出的`call [name-match]`与`call [speculative resolution]`标签
  及其测试早于本轮存在（提交`ad687885`）；`static_only`过滤集合
  `IN ('definite','probable')`未变，降级边自动退出static-only输出。
- TEE canary：fixture双后端与固定断言零回归；C/C++边的`call_form`保持默认`name`。

## 公开TEE canary

已提交fixture来自`openharmony-sig/tee_tee_os_framework`的`b11ffb19d83da42047cc0b5cbfbbfb95ba3304f4`提交，许可证为MulanPSL-2.0。清单记录每个复制文件的Git blob SHA。fixture保留上游许可证和源码文件头。CI不访问网络。

tree-sitter配置断言已知符号、`handle_ns_cmd -> dispatch_ns_cmd`直接边，以及`dispatch_ns_global_cmd -> need_load_app`推断边。正则回退不提取C直接调用边，但仍断言符号、函数指针事实和推断边。

使用固定提交的Git检出目录执行本地完整项目验证：

```bash
python tests/tee_project_canary.py /path/to/tee_tee_os_framework --backend tree-sitter --scope product --output tee-product.json
python tests/tee_project_canary.py /path/to/tee_tee_os_framework --backend tree-sitter --scope full-tree --output tee-full-tree.json
python tests/tee_project_canary.py /path/to/tee_tee_os_framework --backend regex --scope product --output tee-regex.json
```

完整项目命令拒绝非Git目录和不等于固定提交的版本。脚本将已提交源码归档到系统临时目录后扫描，不会在输入检出目录中创建数据库。JSON报告包含解析后端、范围、源码版本、扫描状态、文件/符号/pattern/直接边/推断边/函数指针边数量、按类型统计的pattern、全部按文件与类型统计的pattern来源、耗时、数据库字节数和WAL字节数。清单可以为固定提交声明禁止出现的pattern事实；P0.6使用该规则拒绝`pic1080s`、`pic1440s`和`back_png`字节数组的`c_fnptr_register`。耗时和存储字段只作记录，不作为通过阈值。

## PreToolUse 门禁

`hooks/pre_tool_guard.py`决定每次Write / Edit / Bash / PowerShell / Agent调用是
放行、改写还是拒绝。在本组测试之前它没有任何行为覆盖：唯一的引用是
`test_install_manifest.py`断言其路径出现在安装清单中。

```
python -m pytest Remy-CC/tests/test_pre_tool_guard.py -v
```

纯函数通过`importlib`加载hook模块在进程内测试。`main()`决策矩阵通过`subprocess`
向脚本stdin写入JSON载荷并解析`hookSpecificOutput`来测试。子进程环境把
`HOME`/`USERPROFILE`改到临时目录并设`REMY_LANG=en`，因此没有测试读取开发者真实的
`remy-config.json`。断言只针对`permissionDecision`、`updatedInput`和
`additionalContext`，不针对消息文本，因此在两种语言下都成立。

覆盖的16条`main()`分支：Bash命令含与不含已有编码/miniforge标记；PowerShell含与不
含Python命令；Plan agent的语言注入；Explore与general-purpose的确认门；未知agent类
型静默落空；Write / Edit / NotebookEdit在证据未确认时拒绝；Write在证据确认后放行；
项目内绝对路径改写为相对路径；项目外绝对路径请求确认；Read / Glob / Grep不受该门约
束；kebab-case的三种结果（新文件拒绝、只有snake变体存在时改写、两者都存在时询问）；
Edit的软提醒；lock文件警告；无任何路径的载荷静默退出；stdin非法JSON时fails open并
写stderr。packet门另覆盖`.claude/temp_task/`豁免（相对路径、绝对路径、新建文件，
以及`temp_task`之外的`.claude/`路径仍被门禁约束）和结构不合规packet对项目文件写入
的拒绝。

此前四个缺陷以`xfail(strict=True)`用例记录。三个已修复，一个裁定为有意设计；四个
用例现均为正式回归测试：

| 用例 | 处置 |
| :--- | :--- |
| `test_does_not_reinject_when_encoding_already_set` | 已修复。`inject_bash_env`跳过任何携带注入preamble标记（`.env_setup.sh`）的命令，且仅在命令未设置编码时追加export。手写含编码的命令仍获得一次mamba preamble。 |
| `test_evidence_entry_missing_id_is_rejected` | 已修复。`validate_packet`显式校验结构并fail closed：非法JSON、根非对象、`evidence`/`proposed_changes`非list、条目非dict、`id`缺失或非字符串、`evidence_refs`非list、引用非字符串均拒绝并附整改提示。仅I/O错误仍放行。 |
| `test_writing_to_the_active_packet_is_permitted` | 已修复。`main()`将`.claude/temp_task/`下的写入目标豁免于证据门禁，因此门禁自己要求的整改——把证据提升为`confirmed`或修复损坏的packet——始终可用Edit/Write执行。该豁免与上述fail-closed改动在同一提交落地；单独落地任何一个都会死锁或保持静默绕过。 |
| `test_bash_bypasses_packet_validation_by_design`（由`test_bash_is_subject_to_packet_validation`改名） | 不修复——裁定为有意设计。shell命令无法静态判定读写，且技能协议依赖Bash向`.claude/temp_task/`写入。理由记录在该用例的docstring中，用例断言`allow`。 |

`test_validation_is_not_scoped_to_a_file_path`继续记录门禁的全局性：
`proposed_changes`中任何一处未确认引用会阻塞`.claude/temp_task/`之外全部文件的编
辑。按文件收窄需要packet schema不具备的change到file映射。

断言强度在树的临时副本上经变异检验：四个原始变异（删除suspected/stale检查、删除
`inject_bash_env`中的Python判断、把`has_kebab_case`扩大到整个路径、把
`path_is_contained`的`commonpath`比较换成字符串前缀）各自只破坏一条断言；四个修复
后变异（移除temp_task豁免、恢复吞掉结构错误、删除export前置条件、删除preamble标记
跳过）在72用例全绿基线上分别破坏3、2、1、2条对应断言。

### hook与安装器模块覆盖

此前记录为零行为覆盖的三个模块现已有测试：

```
python -m pytest Remy-CC/tests/test_logic_dirty_tracker.py Remy-CC/tests/test_enforcer_hook.py Remy-CC/tests/test_patch_descriptions.py -v
```

`tests/test_logic_dirty_tracker.py`通过subprocess stdin驱动PostToolUse hook：
Write与Edit把归一化的项目相对路径写入`.claude/logic_index_dirty`；只读工具、缺
`file_path`的载荷、非源码扩展名和项目外路径均不记录；stdin非法时静默退出0。
`HOME`/`USERPROFILE`指向临时目录，加载器因此回退到仓库的`skills/remy-index`副本。

`tests/test_enforcer_hook.py`把hook复制到临时目录以控制reminder文件集合：
`REMY_LANG=zh-CN`选择`reminder_prompt_zh.md`，`en`（或未设置语言）选择英文文件，
primary缺失时回退到另一语言，两个文件都缺失时返回文档化的默认串，读取文本被
strip。`remy-src`通过`PYTHONPATH`提供。

`tests/test_patch_descriptions.py`在进程内覆盖`patch()`：`description:`行只在前
`MAX_FRONTMATTER_LINES`行内被改写，请求语言回退到`en`，行未变化时不写文件（用只读
目标文件证明），`skill_descriptions.json`缺失或格式错误时向stderr告警且不触碰任何
SKILL.md。

`remy-src/patch_descriptions.py`在逻辑索引中显示被`test_mcp_minimal.py`调用；该边
是与`unittest.mock.patch`的同名冲突，早于上述真实覆盖存在。

## R2.4 安装事务测试矩阵

R2.4将安装所有权集中到`remy-src/install_runtime/`。测试在系统临时目录下显式设置
`CLAUDE_CONFIG_DIR`、`REMY_CC_HOME`、`HOME`和`USERPROFILE`。安装、升级、回滚和
卸载测试均不访问开发者真实用户目录。

矩阵覆盖manifest v1/v2到v3迁移、Python/Rust两种Hook模式、重复安装、来源不明目标、
受管理文件和settings认领被修改、manifest与事务元数据损坏、事务/runtime字段严格校验、
daemon运行/状态未知拒绝、未持有旧lock处理、相同版本不同hash的daemon拒绝、提交前
回滚、manifest发布崩溃窗口、提交后清理恢复、卸载manifest原子移除、默认保留状态、
显式状态清除、项目索引保护、含空格和非ASCII的用户根、部署时排除Python缓存文件、
保留安装前已有的settings permission、稳定JSON结果，以及0到4退出码。

```bash
python -m pytest Remy-CC/tests/test_install_manifest.py Remy-CC/tests/test_cli_manifest.py Remy-CC/tests/test_cli_daemon.py Remy-CC/tests/test_daemon_ipc.py -q -p no:cacheprovider
pyright -p Remy-CC/pyrightconfig.json
cargo fmt --check --manifest-path Remy-CC/remy-daemon/Cargo.toml
cargo clippy --workspace --manifest-path Remy-CC/remy-daemon/Cargo.toml --all-targets -- -D warnings
cargo test --workspace --manifest-path Remy-CC/remy-daemon/Cargo.toml
```

2026-08-13 Windows验证通过130项定向Python测试（1项skip）、957项全量Python测试（3项skip）、
60项Rust单元测试、13项Rust CLI集成测试，以及0 error / 0 warning的Pyright。
MCP依赖已有的`IncompleteFieldDefinitionWarning`保持不变。`crt-static` release构建输出
`remy-daemon 0.2.0`，二进制不含`VCRUNTIME140`字节串。系统临时目录中的端到端探针
验证了Rust模式安装、verify、默认卸载、重装、`--purge-state`和项目索引保留；未修改的浏览器UI套件通过8项Chromium测试。

## R3.5a scanner-core 生产化测试

R3.5a 修复双侧检索投影谓词（`(file_path, name)` 列等值 + concat 回退），为 Rust 批量
写入保留 `idx_edges_source_file`/`idx_patterns_file` 两个 DELETE 索引，补齐 Rust 增量
语义（排除清扫、identity-invalid 并入、请求排除剔除），并把项目扫描锁
（`.claude/logic_index_scan.lock`，std `File::try_lock`）移入 scanner-core。

- `tests/test_fts_three_layer.py::TestFactLookupPredicateEquivalence`：随机化 node_ref
  （含名称与路径中的`::`碰撞）下拆分查询与原表达式谓词的结果集等价。
- `scanner-core/src/projection.rs` 内联测试覆盖表达式回退路径；`writer.rs` 内联测试
  断言白名单索引在 drop 后存活；`lock.rs` 内联测试覆盖获取、超时与释放。
- `tests/test_index_state.py::TestScanLockInterop`：Python 字节锁与 Rust 整文件锁在同
  一锁文件上的双向互斥（Rust 持有方向经 `REMY_SCAN_LOCK_HOLD_MS` 测试缝隙与
  `lock_acquired` 进度行同步）；二进制未构建时 skip，CI rust 双臂执行。
- `tests/test_scanner_core_diff.py`：新增排除清扫对齐、identity-invalid 重扫对齐与
  `--progress-json` JSON Lines 契约（首条 progress 为 `lock_acquired`，末行仍为
  scan_result v1）三组差分用例。
- `REMY_INDEX_SCAN_LOCK_TIMEOUT`/`REMY_STRUCT_SCAN_TIMEOUT` 进入 rconfig 四级解析，
  默认值由 `tests/test_postprocess_parity.py::TestNarrowConfigContract` 双侧锁定。

```bash
python -m pytest Remy-CC/tests/test_scanner_core_diff.py Remy-CC/tests/test_index_state.py Remy-CC/tests/test_fts_three_layer.py Remy-CC/tests/test_postprocess_parity.py -q -p no:cacheprovider
cargo test --workspace --manifest-path Remy-CC/remy-daemon/Cargo.toml
```

## R3.5b daemon provider 切换测试

R3.5b 引入 state schema v2（`jobs.provider` claim 快照列、`full_scan` job_type、
`published_provider` 表）、`provider.rs` 两级候选探针（`--version` 握手 + 编译期
四语言微语料扫描临时 DB 校验 scan_result v1 与 schema 12.0.0）、worker 双 provider
分派（Rust 臂 `current_exe()` re-exec `scan`、rconfig 自读参数、kill 取消；Python 臂
增量路径逐字节不变）、发布后逐项目 background full_scan 与队列级 superseded 归并
（pending full_scan 吸收同项目增量作业），以及 IPC PROTOCOL_VERSION 4→5
（status 追加 `scanner.desired/published/diagnostic`，Job 序列化含 `provider`）。

- `src/state.rs` 内联测试：v1→v2 迁移保留作业并默认 provider=python、备份存在、
  外键零违例；claim 快照 provider；full_scan 归并/吸收/去重；published_provider
  单行 upsert；Pending→Superseded 转换合法性。
- `src/scheduler.rs` 内联测试（M3 固化）：迟到 Complete 对终态零覆写、
  CancelRequested 优先于 worker 结果、终态后 Progress 静默丢弃、单槽不重派后继。
- `src/provider.rs` 内联测试：bootstrap 不探针不扫全量、验证失败保持已发布
  provider、无效 desired 值只出诊断、缺失二进制探针报错。
- `tests/test_daemon_provider.py`：e2e python→rust 切换（发布、full_scan 成功、
  `parser_backend='python-tree-sitter'` 作为 Rust 写库指纹、增量按 rust 快照执行、
  taskkill 硬杀后重启发布状态存续且不重复提交 full_scan）与无效 desired 保持
  python 两用例；`REMY_SCANNER_PROVIDER`/`REMY_FULL_SCAN_TIMEOUT`（60–86400，默认
  1800）进入 PARAM_REGISTRY 与 rconfig 双侧契约锁定。

```bash
python -m pytest Remy-CC/tests/test_daemon_provider.py Remy-CC/tests/test_daemon_ipc.py Remy-CC/tests/test_remy_config.py Remy-CC/tests/test_postprocess_parity.py -q -p no:cacheprovider
cargo test --workspace --manifest-path Remy-CC/remy-daemon/Cargo.toml
```

## F.1 增量后处理（scanner-core 0.2.0）

Rust 侧 `scan_files` 的后处理从全局重算切换为 `postprocess::run_incremental`：
直接边消歧只重置受影响边集（Δ 源边 ∪ callee ∈ 变更文件新旧名字超集 ∪ .py 增删
触及的 import 绑定宿主边，reset 前捕获旧 callee_file，同步删除 edge_candidates），
purge/synth/trait-bases 保持全局但以 `(source_file, callee_file, callee_qualified)`
三元组计数快照包夹，差分端点与直接边新旧端点并入受影响文件集，kind_hint 按
文件集、cluster 按其顶层组目标化重算。`scan_full` 仍走全局 `run()`。等价门槛：
增量与全量重扫的全部 VIEWS 状态逐字节相同（含 clusters/edge_candidates/
retrieval_documents），由以下用例锁定：

- `scan.rs` 内联测试：扰动序列（重命名/加文件/删文件）逐步与全量重扫比对含
  kind/cluster/candidates 的扩展投影；两文件增量顺序交换律；edge_candidates
  零孤儿（`edges.id` 为 AUTOINCREMENT 且连接不启用 foreign_keys，写入层在
  DELETE edges 前先删 candidates）。
- `test_scanner_core_diff.py`：扰动序列全视图零 blocking（跨文件重命名翻转
  twin 候选、字典序更前的同短名新文件、删文件）+ 孤儿回归；fanout 越限用例
  （`REMY_SYNTH_EVENT_FANOUT_CAP=1`，delta 文件把 observer 信号推过上限，
  两个非 delta 文件间的 inferred 边被丢弃并使 pkg 集群跌破密度阈值——检验
  快照差分对非 delta 端点的覆盖）。
- `test_postprocess_parity.py`：增量路径的 summary/retrieval 状态与 Python
  oracle 逐值一致。

登记事项：python 回切臂对大仓增量沿用 `REMY_STRUCT_SCAN_TIMEOUT`（默认 60 s，
gpu 语料实测 65.3 s 贴顶），回切前需按仓库规模上调该值。

## C2 docstring 剥出 hash（contract version 3）

C2 裁定（docs/RETIREMENT_zh.md §4）在双实现中把 Python docstring 字面量剥出
symbol hash 输入。parser 定位 docstring 节点（ast / tree-sitter），把其字节
span 从符号段落中挖出写入 `SymbolInfo.hash_source_segment`；hash 消费端
（`scanner.scan_file`、Rust `parse_one` 与 `write_file_facts`）改为哈希
`hash_segment()` 而非 `source_segment`。`source_segment` 本体不变——LLM 摘要
与 filter-small 行数判定仍消费完整文本。双侧 `CACHE_CONTRACT_VERSION` 2 → 3；
存量库首次扫描经 identity-invalid 路径重解析 `.py` 行。

等价门槛：双实现产出逐字节一致的 hash 输入。锁定用例：

- `test_struct_scan.py::TestDocstringExcludedFromHash`：四种字面量风格
  （普通、docstring 含 `#`、raw 单引号、类 docstring）下 docstring-only
  编辑保持 hash、函数体编辑改变 hash；三引号赋值值保留在 hash 内；无
  docstring 符号 `hash_source_segment=None`。
- `parse_python.rs` 内联测试：剥除正确性、拼接普通字面量与 CPython 常量
  折叠对齐、f-string/bytes/赋值首语句的拒绝、类/方法逐符号剥除。
- `test_scanner_core_diff.py::test_docstring_hash_exclusion_matches_across_implementations`
  与 `::test_docstring_only_edit_is_hash_neutral_in_rust`：混合语料下双实现
  `symbols.hash` 全等；Rust 臂 docstring-only 编辑 hash 中性。

已登记非对称项（非门槛）：CPython 把相邻普通字符串字面量折叠为单个
`Constant`，因此拼接式 docstring 双侧均被剥除（差分语料 `concat.py`）；
f-string 与 bytes 字面量在双侧均不构成 docstring。

## state.db WAL 备份约束

`~/.remy-cc/state.db` 以 WAL 日志模式运行。未 checkpoint 的行只存在于
`state.db-wal` 中，仅复制主数据库文件会静默丢弃这些行。2026-08-21 的工作区
实测确认了该失效形态：真实状态库含 47 个作业且全部仍在 WAL 中，裸拷
`state.db` 得到的数据库为 0 作业。

对运行中状态库做快照必须使用以下方式之一：

- SQLite backup API（`rusqlite` 的 `backup` feature、`sqlite3 ".backup"` 或
  Python `sqlite3.Connection.backup`），备份时自动并入 WAL 内容；或
- 文件级复制时连带 `state.db-wal` 与 `state.db-shm`，且复制期间无写入者持有
  数据库。

daemon 内置的 v1→v2 迁移使用 backup API 生成迁移前的 `state.db.bak`，交付
路径不受影响。该约束适用于手工快照、测试 fixture 以及未来任何复制运行中
状态库的工具。

## 边界

已提交测试使用合成源码或固定的MulanPSL-2.0 TEE fixture、临时目录和临时SQLite数据库，不需要LLM API key或网络。P0.3比较全量与增量扫描的规范化状态。P0.4增加固定版本符号和关系、重复全量幂等性、handler重命名/删除比较、解析后端报告及本地完整项目测量命令。P0.5将结构扫描实现拆分到`schema.py`、`symbol_names.py`、`migrations.py`和`scanner.py`，`struct_scan.py`继续作为稳定CLI和导入入口。P0.6在生成位置注册事实前拒绝普通标量和字节数组，拒绝数值与表达式handler，保留Unicode单词标识符，报告pattern类型与来源，并检查固定完整项目中的三个已知图片数组。固定项目没有发现省略内层聚合花括号的已知函数指针结构体表，该C形式不属于当前已验证的解析契约。migration测试验证导入时不加载parser模块；完整测试、Pyright、兼容再导出、两种fixture后端和三次固定完整项目扫描验证当前行为。
