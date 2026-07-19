# 项目类型 profile 扩展计划

> 状态:计划(未实现)。本文件用于记录约定,避免未来遗忘。
> 创建于 task_20260719_214814 之后的讨论,经使用者同意采用"混合"方案并同意暂不实现。

## 1. 目的

`c_fnptr_profiles/` 存放按项目类型区分的调优数据,供 C 函数指针调度合成器
(`../c_fnptr_dispatch.py`)使用。首个文件是 `tee.py`(iTrustee / tee_os_framework)。
本文件说明:未来新增其他项目类型(例如某个 Linux 驱动仓库、redis 等)时,应如何
组织这些文件、它们以什么方式生效。

## 2. 当前实现状态(截至撰写时)

- `__init__.py` 提供 `PROFILES = {"tee": TEE_PROFILE}` 与 `get_profile(name)`。
- `c_fnptr_dispatch.py` 的入口为 `synthesize_c_fnptr_dispatch_edges(db, profile_name="tee")`,
  内部 `profile = get_profile(profile_name)`。
- `synthesizers/__init__.py` 中 `run_all_synthesizers(db)` 调用时**未传** `profile_name`,
  因此始终使用默认的 `"tee"`。
- 引擎目前**只读取 `fanout_cap` 一个字段**;`table_builder_macros`、`assumed_defined`
  是预留字段,尚未被引擎使用。

因此当前是"单选一个 profile,且固定为 tee"。**仅往本文件夹新增文件并不会自动生效**,
因为没有任何选择逻辑,默认永远落在 tee。这是有意为之:只有一个 profile 时,做选择
机制没有意义。

## 3. 未来目标:混合方式

profile 内的字段分两类,生效方式不同:

| 字段 | 性质 | 生效方式 |
| :--- | :--- | :--- |
| `table_builder_macros`(例如 redis 的 `MAKE_CMD`) | 识别器:只有源码里真出现该宏名才触发,不出现即无任何作用 | **并集**:合并所有 profile 的该列表一起使用 |
| `assumed_defined`(#ifdef 使用的配置宏) | 识别器,轻度依赖上下文 | 并集(基本安全) |
| `fanout_cap`(单个调度点最多连接的处理函数数量) | 单一数值 | **按项目选取**:取当前项目 profile 的值 |

规则总结:

- **识别类字段用并集(扩展)**:因为这类字段是自门控的——源码里没有对应的宏,它就
  永不匹配。合并所有项目类型的识别器一起使用是安全的,并且好处是"新增一个文件 = 纯
  增加覆盖能力",无需判断当前是什么项目,也不会影响其他项目。
- **单一数值字段用按项目选取(切换)**:数值无法合并,应按当前正在索引的项目取值;
  若无法确定项目类型,取一个保守默认值(或所有 profile 中的最大值)。

## 4. 需要新增的选择机制(当前完全没有)

未来在两种方式中选其一(推荐第一种):

1. **显式配置(推荐)**:在 `.claude/logic_index_config` 或环境变量(例如
   `C_FNPTR_PROFILE=tee`)中写明项目类型,由 `run_all_synthesizers` 读取后传给引擎。
   行为可预测、无误判。
2. **自动识别**:引擎根据源码中的特征标志判断项目类型(例如出现 `smc_cmd_t` /
   `TEE_Result` 判为 tee,出现 `struct file_operations` 判为 linux)。省去配置,但存在
   判断错误的风险。

## 5. 实现清单(未来接手时按此执行)

1. 每个新项目类型:在本文件夹新增 `<类型>.py`,定义 `<类型大写>_PROFILE` 数据字典,
   在 `__init__.py` 的 `PROFILES` 中登记。
2. 在 `__init__.py` 增加:
   - `merged_recognizers()`:返回所有 profile 的 `table_builder_macros` 与
     `assumed_defined` 的并集。
   - 保留 `get_profile(name)` 用于取单一数值字段。
3. 修改 `c_fnptr_dispatch.py` 的入口:
   - 识别类字段改为从 `merged_recognizers()` 取(并集)。
   - `fanout_cap` 仍从选中的 profile 取(切换)。
   - 当引擎开始真正使用 `table_builder_macros` 时(即支持宏构建的表,如 redis
     `MAKE_CMD`),需要在解析器 `extract_patterns` 侧同步支持宏展开——这是目前有意
     推迟的更全量覆盖内容,详见 task_20260719_214814 的记录。
4. 修改 `synthesizers/__init__.py`:`run_all_synthesizers` 读取配置/环境变量,把项目
   类型作为 `profile_name` 传入。
5. 补充测试:在 `tests/test_c_fnptr_dispatch.py` 增加一个第二类型的样例,验证识别器
   并集不影响 tee 结果、且数值字段按项目正确选取。

## 6. 触发时机

不必提前实现。等到**真正出现第二个项目类型**时再动手——那时才有具体的第二组识别器
来检验"并集"是否确实即插即用,避免现在凭空设计一套暂时用不上的选择机制。

## 7. 关联

- 引擎:`../c_fnptr_dispatch.py`
- 首个 profile:`tee.py`
- 背景与被推迟的更全量覆盖(宏构建的表、裸函数指针数组、字段间传播):见记忆
  `codegraph-adoption-status` 与任务 task_20260719_214814。
