# remy-ci

分析 CI/CD 失败日志，诊断构建、测试和门禁失败。为 `/remy-patch` 生成证据包。

## 使用方式

```
/remy-ci [run_id | log_file_path | --paste]
```

- **无参数**：引导模式，提示选择输入来源。
- **数字参数**：GitHub Actions run ID（需要 `gh` CLI）。
- **文件路径**：读取并分析本地日志文件。
- **`--paste`**：手动粘贴日志内容。

## 支持的失败类型

| 类型 | 示例 |
| :--- | :--- |
| 编译错误 | gcc/clang 输出的 file:line:col 格式错误 |
| 链接错误 | undefined reference、multiple definition |
| 测试失败 | pytest、gtest、kunit、TAP 格式 |
| Sanitizer 报告 | KASAN、UBSAN、KCSAN、ASan、TSan |
| QEMU / 仿真失败 | Kernel panic、启动失败、超时 |
| 代码风格检查 | checkpatch、clang-format、各类 linter |
| 静态分析 | sparse、smatch、Coverity、clang-tidy |
| 构建配置错误 | Kconfig 错误、依赖未满足 |

## 输入模式

### 模式 A — 粘贴

无外部依赖。直接粘贴错误输出。

### 模式 B — 本地文件

读取从 CI 输出中保存的日志文件。

### 模式 C — GitHub Actions（`gh` CLI）

需要安装并认证 `gh` CLI（`gh auth login`）。自动获取结构化的 job 元数据和失败步骤日志。未提供 run ID 时，自动检测当前分支的最近一次失败 run。

## 输出

- **诊断报告**：`.claude/temp_ci/ci_{timestamp}.md`
- **证据包**：`.claude/temp_task/ci_{timestamp}.json`（兼容 `/remy-patch`）

## 配置

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `CI_LOG_MAX_LINES` | `500` | 每个失败步骤保留的最大行数 |

## 依赖

- `gh` CLI（可选，仅模式 C 需要）— 安装地址：https://cli.github.com
