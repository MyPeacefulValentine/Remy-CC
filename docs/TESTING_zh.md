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

持续集成在不安装tree-sitter的Python 3.10环境和安装固定tree-sitter包的Python 3.12环境运行全部测试。两个作业分别使用可用解析后端执行固定公开TEE fixture canary。Windows Python 3.12作业运行进程锁和脏队列测试。

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
python -m pytest Remy-CC/tests/test_mcp_queries.py -k Impact -v
```

`TestQueryImpactRendering`覆盖标签去重、层内计数、两个result limit下输出一致、
limit小于该层符号数时的文件总数、截断标记，以及全部文件都展示时不出现该标记。

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

## 边界

已提交测试使用合成源码或固定的MulanPSL-2.0 TEE fixture、临时目录和临时SQLite数据库，不需要LLM API key或网络。P0.3比较全量与增量扫描的规范化状态。P0.4增加固定版本符号和关系、重复全量幂等性、handler重命名/删除比较、解析后端报告及本地完整项目测量命令。P0.5将结构扫描实现拆分到`schema.py`、`symbol_names.py`、`migrations.py`和`scanner.py`，`struct_scan.py`继续作为稳定CLI和导入入口。P0.6在生成位置注册事实前拒绝普通标量和字节数组，拒绝数值与表达式handler，保留Unicode单词标识符，报告pattern类型与来源，并检查固定完整项目中的三个已知图片数组。固定项目没有发现省略内层聚合花括号的已知函数指针结构体表，该C形式不属于当前已验证的解析契约。migration测试验证导入时不加载parser模块；完整测试、Pyright、兼容再导出、两种fixture后端和三次固定完整项目扫描验证当前行为。
