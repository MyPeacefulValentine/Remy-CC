# 测试

## 环境

Remy-CC 支持 Python 3.10 及以上版本。使用以下命令安装固定版本的开发工具：

```bash
python -m pip install -r requirements-dev.txt
```

使用以下命令安装可选的高精度解析器包：

```bash
python -m pip install -r requirements-tree-sitter.txt
```

## 验证基线

P0.1a/P0.1b实施前，仓库工作树可收集454项测试。2026-08-01的P0.5结构模块重构验证收集并通过509项测试。

```bash
python -m pytest tests -q -p no:cacheprovider
pyright -p pyrightconfig.json
python -m pytest tests/test_struct_scan.py tests/test_enrichment_hook.py tests/test_index_state.py -q -p no:cacheprovider
python -m pytest tests/test_migration_ladder.py -q -p no:cacheprovider
python -m pytest tests/test_synthesizers.py tests/test_c_fnptr_dispatch.py -q -p no:cacheprovider
python -m pytest tests/test_struct_scan.py tests/test_fts_three_layer.py -q -p no:cacheprovider
python -m pytest tests/test_tee_canary.py -q -p no:cacheprovider
python tests/tee_project_canary.py tests/fixtures/tee_canary --fixture --backend regex --scope product
# 已安装 requirements-tree-sitter.txt 时：
python tests/tee_project_canary.py tests/fixtures/tee_canary --fixture --backend tree-sitter --scope product
```

持续集成在不安装tree-sitter的Python 3.10环境和安装固定tree-sitter包的Python 3.12环境运行全部测试。两个作业分别使用可用解析后端执行固定公开TEE fixture canary。Windows Python 3.12作业运行进程锁和脏队列测试。

## 公开TEE canary

已提交fixture来自`openharmony-sig/tee_tee_os_framework`的`b11ffb19d83da42047cc0b5cbfbbfb95ba3304f4`提交，许可证为MulanPSL-2.0。清单记录每个复制文件的Git blob SHA。fixture保留上游许可证和源码文件头。CI不访问网络。

tree-sitter配置断言已知符号、`handle_ns_cmd -> dispatch_ns_cmd`直接边，以及`dispatch_ns_global_cmd -> need_load_app`推断边。正则回退不提取C直接调用边，但仍断言符号、函数指针事实和推断边。

使用固定提交的Git检出目录执行本地完整项目验证：

```bash
python tests/tee_project_canary.py /path/to/tee_tee_os_framework --backend tree-sitter --scope product --output tee-product.json
python tests/tee_project_canary.py /path/to/tee_tee_os_framework --backend tree-sitter --scope full-tree --output tee-full-tree.json
python tests/tee_project_canary.py /path/to/tee_tee_os_framework --backend regex --scope product --output tee-regex.json
```

完整项目命令拒绝非Git目录和不等于固定提交的版本。脚本将已提交源码归档到系统临时目录后扫描，不会在输入检出目录中创建数据库。JSON报告包含解析后端、范围、源码版本、扫描状态、文件/符号/pattern/直接边/推断边/函数指针边数量、耗时、数据库字节数和WAL字节数。耗时和存储字段只作记录，不作为通过阈值。

## 边界

已提交测试使用合成源码或固定的MulanPSL-2.0 TEE fixture、临时目录和临时SQLite数据库，不需要LLM API key或网络。P0.3比较全量与增量扫描的规范化状态。P0.4增加固定版本符号和关系、重复全量幂等性、handler重命名/删除比较、解析后端报告及本地完整项目测量命令。P0.5将结构扫描实现拆分到`schema.py`、`symbol_names.py`、`migrations.py`和`scanner.py`，`struct_scan.py`继续作为稳定CLI和导入入口。migration测试验证导入时不加载parser模块；完整测试、Pyright、兼容再导出、两种fixture后端和三次固定完整项目扫描验证外部行为不变。
