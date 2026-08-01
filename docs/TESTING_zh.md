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

P0.1a/P0.1b 实施前，仓库工作树可收集454项测试。本轮新增测试会增加该数字。

```bash
python -m pytest tests -q -p no:cacheprovider
pyright -p pyrightconfig.json
python -m pytest tests/test_struct_scan.py tests/test_enrichment_hook.py tests/test_index_state.py -q -p no:cacheprovider
python -m pytest tests/test_migration_ladder.py -q -p no:cacheprovider
python -m pytest tests/test_synthesizers.py tests/test_c_fnptr_dispatch.py -q -p no:cacheprovider
python -m pytest tests/test_struct_scan.py tests/test_fts_three_layer.py -q -p no:cacheprovider
```

持续集成在不安装 tree-sitter 的 Python 3.10 环境和安装固定 tree-sitter 包的 Python 3.12 环境运行全部测试。Windows Python 3.12 作业运行进程锁和脏队列测试。

## 边界

已提交测试只使用合成源码、临时目录和临时 SQLite 数据库，不需要 LLM API key 或网络。P0.3 比较全量与增量扫描的规范化状态，并记录全局直接边消歧和合成器耗时。公开 TEE 项目扫描不属于当前持续集成基线。P0.4 将固定仓库版本、排除规则和预期测量值。
