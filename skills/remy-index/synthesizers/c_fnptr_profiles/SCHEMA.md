# 函数指针 profile 字段定义(schema)

本文件集中说明 `c_fnptr_profiles/` 下各项目类型档案(如 `tee.py`)中 profile 字典的
字段含义,使各档案文件本身保持精简、不重复注释。

## profile 字典字段

| 字段 | 类型 | 是否已被引擎使用 | 含义 |
| :--- | :--- | :--- | :--- |
| `name` | str | 否(仅标识) | 项目类型名称,用于登记与辨识 |
| `fanout_cap` | int | 是 | 单个调度点最多连接的处理函数数量;超出则截断,防止异常情况下的过度扇出。引擎默认值为 300 |
| `table_builder_macros` | list[str] | 否(预留) | 会展开成表项的宏名(例如 redis 的 `MAKE_CMD` 式写法)。当前引擎不读取;待"更全量覆盖"实现后启用 |
| `assumed_defined` | list[str] | 否(预留) | 表项处于 `#ifdef` 内时,视为"已定义"的配置宏名。当前引擎对被守护的表项一律保留(多一条边无害),故暂不使用此字段 |

## 使用与选择

- 档案通过 `c_fnptr_profiles/__init__.py` 的 `PROFILES` 字典登记,`get_profile(name)`
  返回;当前默认使用 `"tee"`,选择机制尚未接入。
- 引擎(`../c_fnptr_dispatch.py`)目前仅读取 `fanout_cap`;其余字段为预留,含义如上。

## 新增一个项目类型

1. 在本文件夹新增 `<类型>.py`,定义 `<类型大写>_PROFILE` 字典(字段同上)。
2. 在 `__init__.py` 的 `PROFILES` 中登记。

## 未来计划

字段的启用顺序、"识别类字段取并集 / 单值字段按项目选取"的混合生效方式,以及选择
机制的接入,记录于同目录的 `EXTENSION_PLAN.md`。
