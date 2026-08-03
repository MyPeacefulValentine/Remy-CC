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
heartbeat保护、启动宽限和禁用控件保护。Node测试从`config_ui.html`提取并执行
payload、实际差异和保存结果状态函数。

```bash
python -m pytest tests/test_remy_config.py tests/test_config_ui.py -q -p no:cacheprovider
python -m pytest tests -q -p no:cacheprovider
pyright -p pyrightconfig.json
python -m compileall -q remy-src tests
```

UI-A验证基线为604项测试通过，Pyright没有错误或警告。本地Edge 151验证初始
保存按钮处于禁用状态、禁用样式可辨识，并且无修改点击保存后退出不会出现未保存
确认。浏览器自动化、API端点通信测试、响应式布局、动效和可访问性留在B1/B2阶段。

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
