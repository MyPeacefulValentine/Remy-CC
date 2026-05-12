# Update Tree（项目树快照）

Update Tree 生成项目目录结构的文本快照，保存到 `.claude/project_tree.md`。该快照被注入 `CLAUDE.md`，作为 AI 的结构导航参考。项目树也会在会话生命周期事件中自动更新。

## 何时使用

- 批量文件操作后（创建、移动或删除多个文件）
- 改变模块结构或目录布局的重构后
- 破坏性操作（`rm`、`mv`）改变目录结构后
- AI 开始引用不存在的文件路径时（上下文过时）

## 工作流

### 手动调用

1. 运行 `/update-tree`。Skill 执行 `generate_smart_tree.py` 生成 `.claude/project_tree.md`。
2. 文档注入器（`hooks/doc_manager/injector.py`）更新 `CLAUDE.md` 中的引用。

### 自动更新

项目树也通过 `hooks/tree_system/lifecycle_hook.py` 自动更新：

| 事件 | 触发时机 |
| :--- | :--- |
| `SessionStart` | 会话启动 |
| `PreCompact` | 上下文压缩前 |
| `SessionEnd` | 会话结束 |

## 配置

Skill 读取 `.claude/tree_config` 中的规则。文件不存在时，首次运行会从默认模板创建。

### 语法

- **排除规则**（`!` 前缀）：
    - `!node_modules` — 排除名为 node_modules 的目录/文件
    - `!*.log` — 排除 .log 结尾的文件
- **包含规则**（`[路径] [参数]`）：
    - `-depth N` — 遍历深度（0 = 仅当前层，-1 = 无限递归）
    - `-if_file true/false` — 是否列出单个文件

### 示例

```text
# 排除
!__pycache__
!.git
!dist

# 根目录：深度 2，显示文件
. -depth 2 -if_file true

# 资源目录：深度 1，仅目录
src/assets -depth 1 -if_file false

# 核心代码：无限深度，显示文件
src/core -depth -1 -if_file true
```

## 相关文件

| 文件 | 用途 |
| :--- | :--- |
| `SKILL.md` | 协议定义（由 Claude Code 加载） |
| `../../hooks/tree_system/generate_smart_tree.py` | 树生成脚本 |
| `../../hooks/tree_system/lifecycle_hook.py` | 会话生命周期处理器 |
| `../../hooks/tree_system/default_tree_config.template` | 默认配置模板 |
| `../../hooks/doc_manager/injector.py` | CLAUDE.md 引用注入 |
