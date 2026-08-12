#!/usr/bin/env python3
"""
Remy - Installer

Usage:
    python install.py              # Install (default)
    python install.py --uninstall  # Uninstall
    python install.py --verify     # Verify installation
"""

import argparse
import contextlib
import getpass
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_version_file = Path(__file__).resolve().parent / "VERSION"
if not _version_file.exists():
    print("Fatal: VERSION file not found at " + str(_version_file), file=sys.stderr)
    sys.exit(1)
_version_lines = _version_file.read_text(encoding="utf-8").splitlines()
SUITE_VERSION = _version_lines[0].strip().lstrip("﻿") if _version_lines else ""
if not SUITE_VERSION:
    print("Fatal: VERSION file is empty", file=sys.stderr)
    sys.exit(1)
MANIFEST_FILE = ".installer_manifest.json"
MANIFEST_SCHEMA_VERSION = 2

DEPLOY_DIRS = ["hooks", "skills", "output-styles", "remy-src/install_runtime"]
LEGACY_HOOK_PATHS = [
    "hooks/doc_manager",
    "hooks/env_system",
    "hooks/tree_system",
    "hooks/logic_dirty_tracker.py",
    "hooks/logic_enrichment_hook.py",
    "hooks/pre_tool_guard.py",
]
DEPLOY_FILES_MAP = {
    "CLAUDE.md": "CLAUDE.md",
    "style.md": "style.md",
    "tools_ref.md": "tools_ref.md",
    "remy-src/cli.py": "remy-src/cli.py",
    "remy-src/config_ui.py": "remy-src/config_ui.py",
    "remy-src/config_ui.html": "remy-src/config_ui.html",
    "remy-assets/logo.svg": "remy-assets/logo.svg",
    "remy-src/patch_descriptions.py": "remy-src/patch_descriptions.py",
    "remy-src/index_mcp_server.py": "remy-src/index_mcp_server.py",
    "remy-src/index_mcp_common.py": "remy-src/index_mcp_common.py",
    "remy-src/index_mcp_graph.py": "remy-src/index_mcp_graph.py",
    "remy-src/index_mcp_search.py": "remy-src/index_mcp_search.py",
    "remy-src/index_mcp_facts.py": "remy-src/index_mcp_facts.py",
    "remy-src/index_mcp_navigate.py": "remy-src/index_mcp_navigate.py",
    "remy-src/index_mcp_queries.py": "remy-src/index_mcp_queries.py",
    "remy-src/remy_config.py": "remy-src/remy_config.py",
}
SETTINGS_TEMPLATE = "settings.example.json"
MCP_TEMPLATE = "remy_mcp.json"

BACKUP_SUFFIX = ".bak"
API_KEY_PLACEHOLDER = "YOUR_API_KEY_HERE"

MIGRATIONS = {
    "permissions": {
        "Skill(update-logic-index)": "Skill(remy-index)",
        "Skill(read-logic-index)": "Skill(remy-lookup)",
        "Skill(deep-plan)": "Skill(remy-plan)",
        "Skill(code-modification)": "Skill(remy-patch)",
        "Skill(post-verify)": "Skill(remy-inspect)",
        "Skill(auditor)": "Skill(remy-audit)",
        "Skill(log-change)": "Skill(remy-changelog)",
        "Skill(milestone)": "Skill(remy-milestone)",
        "Skill(security-audit)": "Skill(remy-secure)",
        "Skill(update-tree)": "Skill(remy-tree)",
        "Skill(repo-audit)": "Skill(remy-reposcout)",
    },
    "directories": {
        "skills/update-logic-index": "skills/remy-index",
        "skills/read-logic-index": "skills/remy-lookup",
        "skills/deep-plan": "skills/remy-plan",
        "skills/code-modification": "skills/remy-patch",
        "skills/post-verify": "skills/remy-inspect",
        "skills/auditor": "skills/remy-audit",
        "skills/log-change": "skills/remy-changelog",
        "skills/milestone": "skills/remy-milestone",
        "skills/security-audit": "skills/remy-secure",
        "skills/update-tree": "skills/remy-tree",
        "skills/repo-audit": "skills/remy-reposcout",
    },
}

DEPRECATED_ENV_KEYS = {
    "LOGIC_DENSITY_FULL_MAX",
    "LOGIC_DENSITY_COMPACT_MAX",
    "LOGIC_DENSITY_CORE_MAX",
}

DEPRECATED_PERMISSIONS = {
}

SCRIPT_DIR = Path(__file__).resolve().parent

DAEMON_SOURCE_DIR = SCRIPT_DIR / "remy-daemon" / "target" / "release"
DAEMON_PROBE_TIMEOUT = 10

# ── Bilingual UI Messages ─────────────────────────────────────

UI = {
    "en": {
        "target_dir": "Target directory: {path}",
        "src_missing_file": "  [!] Source file missing: {name}, skipping",
        "backed_up": "  [~] Backed up {name} -> {bak}",
        "copied_file": "  [+] {name}",
        "src_missing_dir": "  [!] Source directory missing: {name}/, skipping",
        "copied_dir": "  [+] {name}/ ({count} files)",
        "settings_corrupted": "  [!] settings.json corrupted, backed up to {name}",
        "settings_merged": "  [+] settings.json (merged)",
        "settings_tpl_missing": "  [!] {name} missing, skipping settings.json merge",
        "env_new_keys": "  [i] New keys added to env (configure actual values): {keys}",
        "env_removed_keys": "  [-] Removed deprecated env keys: {keys}",
        "perm_removed": "  [-] Removed deprecated permissions: {keys}",
        "mcp_registered": "  [+] MCP server registered in ~/.claude.json: {name}",
        "manifest_written": "  [+] {name}",
        "removed_old_dir": "  [-] Removed obsolete: {name}/",
        "db_migrate_notice": "  [i] Logic Index storage upgraded: JSON → SQLite.\n      Existing projects will auto-migrate on next SessionStart.",
        "ts_installed": "  [i] tree-sitter already installed, skipping",
        "ts_prompt": "Install tree-sitter (high-precision C/C++/TypeScript parsing)? [Y/n] ",
        "ts_installing": "  Installing tree-sitter ...",
        "j2_installed": "  [i] Jinja2 already installed, skipping",
        "j2_prompt": "Install Jinja2 (remy-inspect template rendering)? [Y/n] ",
        "j2_installing": "  Installing Jinja2 ...",
        "mcp_installed": "  [i] MCP SDK (mcp) already installed",
        "mcp_installing": "  Installing MCP SDK (required by the remy-index MCP server) ...",
        "mcp_install_failed": "  [x] MCP SDK installation failed. The MCP server is a required component; resolve the pip failure and re-run install.py.",
        "gh_installed": "  [i] GitHub CLI (gh) already installed, skipping",
        "gh_not_found": "  [i] gh not found. Install from https://cli.github.com for /remy-ci GitHub Actions mode.\n      /remy-ci will still work with paste (--paste) or file input modes.",
        "verify_gh": "  GitHub CLI (gh): {status}",
        "api_config_new": "Configure LLM API for Logic Index? [Y/n] ",
        "api_config_existing": "Existing API config detected. Reconfigure? [y/N] ",
        "api_cost_hint": "  [i] Logic Index may require many API calls.\n      Cost-effective models recommended (e.g. deepseek-v4-flash, or pay-per-call coding plans).",
        "api_url_prompt": "  API URL\n  (e.g. https://api.deepseek.com/v1/chat/completions)\n  [default: {default}]: ",
        "api_model_prompt": "  Model name (e.g. deepseek-v4-flash)\n  [default: {default}]: ",
        "api_key_prompt": "  API Key: ",
        "api_key_empty": "  [!] API Key is empty, skipping API configuration.",
        "api_configured": "  [+] API configuration saved",
        "api_test_prompt": "Test API connectivity? [y/N] ",
        "api_test_running": "  Testing API connectivity ...",
        "api_test_ok": "  [+] API connectivity test passed",
        "api_test_attempt": "  [!] Attempt {n}/3 failed: {err}",
        "api_test_all_failed": "  [!] All 3 connectivity tests failed.",
        "api_test_reconfigure": "Reconfigure API? [Y/n] ",
        "install_done": "\nInstallation complete. {count} files deployed.",
        "install_verify_hint": "Restart your terminal, then run remy-cc verify to check the installation.",
        "no_manifest": "No install manifest (.installer_manifest.json) found. Cannot uninstall.",
        "skip_modified": "  [~] Skipped (modified): {name}",
        "hooks_removed": "  [+] Suite hooks/permissions removed from settings.json",
        "claude_restored": "  [+] CLAUDE.md restored from backup",
        "uninstall_done": "\nUninstall complete. Removed {removed} files, skipped {skipped} modified files.",
        "uninstall_confirm": "This will remove all Remy-CC files and settings. Continue? [y/N] ",
        "uninstall_aborted": "Uninstall cancelled.",
        "uninstall_daemon_running": "  [!] remy-daemon is running; uninstall did not change any files.\n      Stop it and retry: remy-cc daemon stop",
        "uninstall_daemon_status_unknown": "  [!] Could not verify that remy-daemon is stopped; uninstall did not change any files.\n      Check it with: remy-cc daemon status",
        "verify_python_old": "Python version too old: {ver} (requires >= 3.7)",
        "verify_settings_missing": "settings.json not found",
        "verify_settings_invalid": "settings.json JSON format error: {err}",
        "verify_hook_missing": "Hook file not found: {path}",
        "verify_manifest_missing": "{name} not found",
        "verify_files_missing": "{count} files missing from manifest",
        "verify_files_mismatch": "{count} files differ from manifest",
        "verify_header": "Remy v{ver} - Installation Verification\n",
        "verify_python": "  Python: {ver}",
        "verify_target": "  Target directory: {path}",
        "verify_ts": "  tree-sitter: {status}",
        "verify_j2": "  Jinja2: {status}",
        "verify_mcp": "  MCP SDK: {status}",
        "verify_ts_yes": "installed",
        "verify_ts_no": "not installed (optional)",
        "verify_mcp_no": "not installed (required)",
        "verify_mcp_missing": "MCP SDK (mcp) is a required component but is not installed; run: pip install --user mcp",
        "verify_issues": "Found {count} issues:",
        "verify_ok": "Verification passed. All checks OK.",
        "argparse_desc": "Remy Installer",
        "argparse_uninstall": "Uninstall the suite",
        "argparse_verify": "Verify installation",
        "verify_api_not_configured": "  [i] LLM API not configured (Logic Index will not generate summaries)",
        "argparse_lang": "Language for UI and REMY_LANG (interactive prompt if omitted)",
        "shim_created": "  [+] CLI command created: {path}",
        "path_already": "  [i] {dir} is already in PATH",
        "path_prompt": "Add remy-cc to PATH for global access? [Y/n] ",
        "path_manual": "  [i] To use remy-cc globally, add to PATH:\n      {path}",
        "path_too_long": "  [!] PATH variable exceeds 1024 characters, cannot auto-modify",
        "path_set_win": "  [+] PATH updated (restart terminal to take effect)",
        "path_set_unix": "  [+] Added to ~/{rc} (run 'source ~/{rc}' or restart terminal)",
        "path_cleanup": "  [+] CLI shim removed",
        "warn_sudo": "  [!] Running as root via sudo (SUDO_USER={user}).\n      Files will install to {path}, not /home/{user}/.claude.\n      If unintended, re-run without sudo.",
        "err_home_is_file": "  [!] {path} exists as a regular file, not a directory.\n      Remove or rename it, then retry.",
        "err_home_not_found": "  [!] Cannot determine home directory. Set $HOME and retry.",
        "err_permission": "\n  [!] Permission denied: {err}\n      Check directory permissions or avoid running with sudo.",
        "daemon_src_missing": "  [i] remy-daemon binary not built; skipping daemon deployment.\n      Build it with: cargo build --release --manifest-path remy-daemon/Cargo.toml, then re-run install.py",
        "daemon_verify_failed": "  [!] remy-daemon binary failed the --version check ({err}); skipped deployment.",
        "daemon_running": "  [!] A remy-daemon instance is running; skipped deploying the new binary.\n      Stop it and re-run install: remy-cc daemon stop",
        "daemon_deployed": "  [+] remy-daemon binary deployed: {path}",
        "daemon_copy_failed": "  [!] Could not write the remy-daemon binary ({err}); kept the existing one.",
    },
    "zh-CN": {
        "target_dir": "目标目录: {path}",
        "src_missing_file": "  [!] 源文件缺失: {name}，跳过",
        "backed_up": "  [~] 已备份 {name} -> {bak}",
        "copied_file": "  [+] {name}",
        "src_missing_dir": "  [!] 源目录缺失: {name}/，跳过",
        "copied_dir": "  [+] {name}/ ({count} 个文件)",
        "settings_corrupted": "  [!] settings.json 格式损坏，已备份至 {name}",
        "settings_merged": "  [+] settings.json (合并)",
        "settings_tpl_missing": "  [!] {name} 缺失，跳过 settings.json 合并",
        "env_new_keys": "  [i] env 中新增以下 key（需手动配置实际值）：{keys}",
        "env_removed_keys": "  [-] 已移除废弃参数：{keys}",
        "perm_removed": "  [-] 已移除废弃权限：{keys}",
        "mcp_registered": "  [+] MCP 服务器已注册到 ~/.claude.json：{name}",
        "manifest_written": "  [+] {name}",
        "removed_old_dir": "  [-] 已删除旧目录：{name}/",
        "db_migrate_notice": "  [i] Logic Index 存储格式已升级：JSON → SQLite。\n      已有项目将在下次 SessionStart 时自动迁移。",
        "ts_installed": "  [i] tree-sitter 已安装，跳过",
        "ts_prompt": "是否安装 tree-sitter（C/C++/TypeScript 高精度解析）？[Y/n] ",
        "ts_installing": "  正在安装 tree-sitter ...",
        "j2_installed": "  [i] Jinja2 已安装，跳过",
        "j2_prompt": "是否安装 Jinja2（post-verify 模板渲染增强）？[Y/n] ",
        "j2_installing": "  正在安装 Jinja2 ...",
        "mcp_installed": "  [i] MCP SDK (mcp) 已安装",
        "mcp_installing": "  正在安装 MCP SDK（remy-index MCP 服务器的必需组件）...",
        "mcp_install_failed": "  [x] MCP SDK 安装失败。MCP 服务器为必需组件；请解决 pip 失败后重新运行 install.py。",
        "gh_installed": "  [i] GitHub CLI (gh) 已安装，跳过",
        "gh_not_found": "  [i] 未找到 gh。请从 https://cli.github.com 安装以使用 /remy-ci 的 GitHub Actions 模式。\n      /remy-ci 仍可通过粘贴 (--paste) 或文件输入模式使用。",
        "verify_gh": "  GitHub CLI (gh): {status}",
        "api_config_new": "是否配置 Logic Index 的 LLM API？[Y/n] ",
        "api_config_existing": "检测到已有 API 配置，是否重新配置？[y/N] ",
        "api_cost_hint": "  [i] Logic Index 可能产生较多 API 调用。\n      建议选择低成本模型（如 deepseek-v4-flash）或按次计费方案。",
        "api_url_prompt": "  API URL\n  （例如 https://api.deepseek.com/v1/chat/completions）\n  [默认: {default}]: ",
        "api_model_prompt": "  模型名称（例如 deepseek-v4-flash）\n  [默认: {default}]: ",
        "api_key_prompt": "  API Key: ",
        "api_key_empty": "  [!] API Key 为空，跳过 API 配置。",
        "api_configured": "  [+] API 配置已保存",
        "api_test_prompt": "是否测试 API 连通性？[y/N] ",
        "api_test_running": "  正在测试 API 连通性 ...",
        "api_test_ok": "  [+] API 连通性测试通过",
        "api_test_attempt": "  [!] 第 {n}/3 次尝试失败: {err}",
        "api_test_all_failed": "  [!] 3 次连通性测试均失败。",
        "api_test_reconfigure": "是否重新配置 API？[Y/n] ",
        "install_done": "\n安装完成。共部署 {count} 个文件。",
        "install_verify_hint": "建议重启终端并运行 remy-cc verify 检查安装结果。",
        "no_manifest": "未找到安装记录 (.installer_manifest.json)，无法执行卸载。",
        "skip_modified": "  [~] 跳过（已被修改）: {name}",
        "hooks_removed": "  [+] settings.json 中的套件配置已移除",
        "claude_restored": "  [+] CLAUDE.md 已从备份恢复",
        "uninstall_done": "\n卸载完成。删除 {removed} 个文件，跳过 {skipped} 个已修改文件。",
        "uninstall_confirm": "此操作将移除所有 Remy-CC 文件和配置。是否继续？[y/N] ",
        "uninstall_aborted": "卸载已取消。",
        "uninstall_daemon_running": "  [!] remy-daemon 正在运行，卸载未修改任何文件。\n      请先停止后重试：remy-cc daemon stop",
        "uninstall_daemon_status_unknown": "  [!] 无法确认 remy-daemon 已停止，卸载未修改任何文件。\n      请执行以下命令检查：remy-cc daemon status",
        "verify_python_old": "Python 版本过低: {ver} (需要 >= 3.7)",
        "verify_settings_missing": "settings.json 不存在",
        "verify_settings_invalid": "settings.json JSON 格式错误: {err}",
        "verify_hook_missing": "hook 文件不存在: {path}",
        "verify_manifest_missing": "{name} 不存在",
        "verify_files_missing": "manifest 中 {count} 个文件缺失",
        "verify_files_mismatch": "manifest 中 {count} 个文件内容不一致",
        "verify_header": "Remy v{ver} - 安装验证\n",
        "verify_python": "  Python: {ver}",
        "verify_target": "  目标目录: {path}",
        "verify_ts": "  tree-sitter: {status}",
        "verify_j2": "  Jinja2: {status}",
        "verify_mcp": "  MCP SDK: {status}",
        "verify_ts_yes": "已安装",
        "verify_ts_no": "未安装（可选）",
        "verify_mcp_no": "未安装（必需）",
        "verify_mcp_missing": "MCP SDK (mcp) 为必需组件但未安装；请执行：pip install --user mcp",
        "verify_issues": "发现 {count} 个问题：",
        "verify_ok": "验证通过。所有检查项正常。",
        "argparse_desc": "Remy 安装工具",
        "argparse_uninstall": "卸载套件",
        "argparse_verify": "验证安装",
        "verify_api_not_configured": "  [i] LLM API 未配置（Logic Index 将无法生成摘要）",
        "argparse_lang": "界面语言及 REMY_LANG 配置值（未指定时交互式选择）",
        "shim_created": "  [+] CLI 命令已创建: {path}",
        "path_already": "  [i] {dir} 已在 PATH 中",
        "path_prompt": "是否将 remy-cc 添加到 PATH 以便全局访问？[Y/n] ",
        "path_manual": "  [i] 如需全局使用 remy-cc，请将以下目录加入 PATH：\n      {path}",
        "path_too_long": "  [!] PATH 变量超过 1024 字符，无法自动修改",
        "path_set_win": "  [+] PATH 已更新（重启终端生效）",
        "path_set_unix": "  [+] 已添加到 ~/{rc}（运行 'source ~/{rc}' 或重启终端生效）",
        "path_cleanup": "  [+] CLI 入口已移除",
        "warn_sudo": "  [!] 当前以 root 身份运行（SUDO_USER={user}）。\n      文件将安装到 {path}，而非 /home/{user}/.claude。\n      如非预期，请去掉 sudo 重新执行。",
        "err_home_is_file": "  [!] {path} 是普通文件而非目录。\n      请移除或重命名后重试。",
        "err_home_not_found": "  [!] 无法确定用户主目录。请设置 $HOME 环境变量后重试。",
        "err_permission": "\n  [!] 权限不足: {err}\n      请检查目录权限，或避免使用 sudo 执行。",
        "daemon_src_missing": "  [i] 未找到已构建的 remy-daemon 二进制，跳过 daemon 部署。\n      构建命令: cargo build --release --manifest-path remy-daemon/Cargo.toml，然后重新运行 install.py",
        "daemon_verify_failed": "  [!] remy-daemon 二进制 --version 验证失败（{err}），已跳过部署。",
        "daemon_running": "  [!] 检测到 remy-daemon 正在运行，已跳过二进制部署。\n      请执行 remy-cc daemon stop 后重新运行 install.py",
        "daemon_deployed": "  [+] remy-daemon 二进制已部署: {path}",
        "daemon_copy_failed": "  [!] 无法写入 remy-daemon 二进制（{err}），保留原有版本。",
    },
}

_ui_lang = "en"


def _t(key, **kwargs):
    template = UI.get(_ui_lang, UI["en"]).get(key, UI["en"].get(key, key))
    return template.format(**kwargs) if kwargs else template


def _user_home() -> Path:
    try:
        return Path.home()
    except RuntimeError:
        home_str = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
        if not home_str:
            print(_t("err_home_not_found"))
            sys.exit(1)
        return Path(home_str)


def get_claude_home() -> Path:
    return _user_home() / ".claude"


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_tree(src: Path, dst: Path, claude_home: Path) -> list:
    """Merge directory contents from src into dst, preserving pre-existing entries.
    Returns records with paths relative to claude_home (POSIX form)."""
    records = []
    if dst.is_symlink():
        dst.unlink()
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    for root, _dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for fname in files:
            target = dst / rel_root / fname
            records.append({
                "path": target.relative_to(claude_home).as_posix(),
                "sha256": compute_sha256(target),
            })
    return records


def copy_file(src: Path, dst: Path, claude_home: Path) -> dict:
    """Copy single file, return {path, sha256} record with path relative to claude_home."""
    shutil.copy2(src, dst)
    return {
        "path": dst.relative_to(claude_home).as_posix(),
        "sha256": compute_sha256(dst),
    }


def refresh_record_hashes(records: list, claude_home: Path) -> None:
    """Refresh manifest hashes after post-copy transformations."""
    for record in records:
        target = _resolve_record_path(record, claude_home)
        if target.is_file():
            record["sha256"] = compute_sha256(target)


def backup_file(path: Path) -> Optional[Path]:
    """Backup file to path.bak. Returns backup path or None if source absent."""
    if not path.exists():
        return None
    backup_path = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    shutil.copy2(path, backup_path)
    return backup_path


def _resolve_record_path(rec: dict, claude_home: Path) -> Path:
    """Resolve a manifest record's path to an absolute Path.
    Supports both schema_version >= 2 (POSIX relative) and legacy absolute paths."""
    p = Path(rec["path"])
    if p.is_absolute():
        return p
    return claude_home / p


def _within_root(path: Path, root: Path) -> bool:
    """Whether path resolves inside root. Reinstall cleanup is scoped to claude_home
    by this check: records outside it (the daemon binary under ~/.remy-cc/) are
    refreshed in place by their own deploy step, never deleted and rebuilt."""
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _remove_empty_dirs(start_dir: Path, stop_at: Path) -> None:
    """Remove empty directories from start_dir upward; stop at stop_at boundary or first non-empty parent."""
    try:
        stop_resolved = stop_at.resolve()
    except OSError:
        return
    current = start_dir
    while True:
        try:
            current_resolved = current.resolve()
        except OSError:
            return
        if current_resolved == stop_resolved:
            return
        try:
            current_resolved.relative_to(stop_resolved)
        except ValueError:
            return
        if not current.is_dir():
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def cleanup_from_manifest(old_manifest: dict, claude_home: Path) -> None:
    """Delete files listed in the previous manifest. Files with mismatched sha256 are first
    backed up to .bak, then deleted. Empty parent directories are pruned up to claude_home.
    Records outside claude_home are left alone — see _within_root."""
    files = old_manifest.get("files", [])
    if not files:
        return
    affected_parents = set()
    for entry in files:
        fpath = _resolve_record_path(entry, claude_home)
        if not _within_root(fpath, claude_home):
            continue
        if not fpath.exists():
            continue
        expected_hash = entry.get("sha256")
        if expected_hash:
            try:
                current_hash = compute_sha256(fpath)
            except OSError:
                current_hash = None
            if current_hash and current_hash != expected_hash:
                backup_file(fpath)
        try:
            fpath.unlink()
        except OSError:
            continue
        affected_parents.add(fpath.parent)
    for parent in affected_parents:
        _remove_empty_dirs(parent, claude_home)


def cleanup_fallback(claude_home: Path) -> None:
    """Cleanup strategy when no previous manifest exists. Targets:
    ~/.claude/skills/remy-* glob and LEGACY_HOOK_PATHS. output-styles is left untouched."""
    skills_dir = claude_home / "skills"
    if skills_dir.is_dir():
        for child in skills_dir.iterdir():
            if not child.name.startswith("remy-"):
                continue
            try:
                if child.is_symlink() or child.is_file():
                    child.unlink()
                else:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue
    for rel in LEGACY_HOOK_PATHS:
        target = claude_home / rel
        if not target.exists():
            continue
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target, ignore_errors=True)
        except OSError:
            continue


def expand_hook_paths(settings: dict, claude_home: Path) -> None:
    """Replace ~/.claude/ in hook commands with actual absolute path."""
    abs_prefix = str(claude_home).replace("\\", "/")
    hooks = settings.get("hooks", {})
    for _event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            hook_list = entry.get("hooks", [])
            for hook in hook_list:
                cmd = hook.get("command", "")
                hook["command"] = cmd.replace("~/.claude/", abs_prefix + "/")


def hooks_equal(h1: dict, h2: dict) -> bool:
    """Check if two hook entries have the same command."""
    return h1.get("command", "").strip() == h2.get("command", "").strip()


def _load_remy_config_module():
    module_dir = SCRIPT_DIR / "remy-src"
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))
    import remy_config
    return remy_config


def _load_install_runtime_module():
    module_dir = SCRIPT_DIR / "remy-src"
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))
    install_runtime = importlib.import_module("install_runtime")
    facade = importlib.import_module("install_runtime.facade")
    return install_runtime, facade.result_for_error


def merge_settings(template: dict, target_path: Path, claude_home: Path, lang_override: str = None) -> Optional[Path]:
    """
    Merge template settings into existing settings.json.
    Returns backup path if settings.json existed, else None.
    """
    existing = {}
    settings_backup = None

    if target_path.exists():
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            settings_backup = backup_file(target_path)
        except json.JSONDecodeError:
            corrupted = target_path.with_suffix(".json.corrupted")
            shutil.copy2(target_path, corrupted)
            print(_t("settings_corrupted", name=corrupted.name))
            existing = {}

    # --- expand template hook paths before comparison ---
    expand_hook_paths(template, claude_home)

    # --- hooks: deep merge by event type, deduplicate by command ---
    tpl_hooks = template.get("hooks", {})
    ext_hooks = existing.setdefault("hooks", {})

    for event, tpl_entries in tpl_hooks.items():
        if not isinstance(tpl_entries, list):
            continue
        ext_entries = ext_hooks.setdefault(event, [])

        for tpl_entry in tpl_entries:
            tpl_hook_list = tpl_entry.get("hooks", [])
            matched_ext_entry = None

            # Find existing entry with same matcher
            for ext_entry in ext_entries:
                if ext_entry.get("matcher", "") == tpl_entry.get("matcher", ""):
                    matched_ext_entry = ext_entry
                    break

            if matched_ext_entry is not None:
                ext_hook_list = matched_ext_entry.setdefault("hooks", [])
                for tpl_hook in tpl_hook_list:
                    already_exists = any(
                        hooks_equal(tpl_hook, eh) for eh in ext_hook_list
                    )
                    if not already_exists:
                        ext_hook_list.append(tpl_hook)
            else:
                ext_entries.append(tpl_entry)

        # --- upgrade cleanup: remove matcher-less entries superseded by matched ones ---
        matched_commands = set()
        for entry in ext_entries:
            if entry.get("matcher"):
                for hook in entry.get("hooks", []):
                    matched_commands.add(hook.get("command", "").strip())
        if matched_commands:
            ext_hooks[event] = [
                entry for entry in ext_entries
                if entry.get("matcher") or not all(
                    hook.get("command", "").strip() in matched_commands
                    for hook in entry.get("hooks", [])
                )
            ]

    # --- permissions.allow: array dedup append ---
    tpl_perms = template.get("permissions", {}).get("allow", [])
    ext_perms = existing.setdefault("permissions", {}).setdefault("allow", [])
    for perm in tpl_perms:
        if perm not in ext_perms:
            ext_perms.append(perm)

    # --- env: keep only Claude-native and skill-protocol settings ---
    tpl_env = template.get("env", {})
    ext_env = existing.setdefault("env", {})
    missing_keys = []
    for key, value in tpl_env.items():
        if key not in ext_env:
            ext_env[key] = value
            missing_keys.append(key)

    # --- env: remove deprecated keys ---
    removed_keys = []
    for key in DEPRECATED_ENV_KEYS:
        if key in ext_env:
            del ext_env[key]
            removed_keys.append(key)

    # --- outputStyle: write if absent ---
    if "outputStyle" not in existing and "outputStyle" in template:
        existing["outputStyle"] = template["outputStyle"]

    # --- remove stale mcpServers from settings.json (moved to ~/.claude.json) ---
    existing.pop("mcpServers", None)

    # --- expand hook paths ---
    expand_hook_paths(existing, claude_home)

    # --- write ---
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if sys.platform != "win32":
        os.chmod(target_path, 0o600)

    if missing_keys:
        print(_t("env_new_keys", keys=', '.join(missing_keys)))
    if removed_keys:
        print(_t("env_removed_keys", keys=', '.join(removed_keys)))

    return settings_backup


def write_manifest(claude_home: Path, records: list, settings_backup: Optional[Path],
                    injected_hooks: dict = None, injected_permissions: list = None) -> None:
    manifest = {
        "version": SUITE_VERSION,
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "settings_backup": str(settings_backup) if settings_backup else None,
        "files": records,
        "injected_hooks": injected_hooks or {},
        "injected_permissions": injected_permissions or [],
    }
    manifest_path = claude_home / MANIFEST_FILE
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")


def remove_suite_hooks(settings: dict, template: dict) -> None:
    """Remove hooks injected by the suite from settings."""
    tpl_hooks = template.get("hooks", {})
    ext_hooks = settings.get("hooks", {})

    for event, tpl_entries in tpl_hooks.items():
        if event not in ext_hooks:
            continue
        for tpl_entry in tpl_entries:
            tpl_hook_list = tpl_entry.get("hooks", [])
            for ext_entry in ext_hooks[event]:
                if ext_entry.get("matcher", "") != tpl_entry.get("matcher", ""):
                    continue
                ext_hook_list = ext_entry.get("hooks", [])
                ext_entry["hooks"] = [
                    eh for eh in ext_hook_list
                    if not any(hooks_equal(eh, th) for th in tpl_hook_list)
                ]
        # Remove empty entries
        ext_hooks[event] = [
            e for e in ext_hooks[event] if e.get("hooks")
        ]
        if not ext_hooks[event]:
            del ext_hooks[event]

    if not ext_hooks:
        settings.pop("hooks", None)


def remove_suite_permissions(settings: dict, template: dict) -> None:
    """Remove permissions injected by the suite."""
    tpl_perms = template.get("permissions", {}).get("allow", [])
    ext_perms = settings.get("permissions", {}).get("allow", [])
    if ext_perms:
        settings["permissions"]["allow"] = [
            p for p in ext_perms if p not in tpl_perms
        ]
        if not settings["permissions"]["allow"]:
            settings["permissions"].pop("allow", None)
        if not settings["permissions"]:
            settings.pop("permissions", None)


def migrate_permissions(settings_path: Path) -> None:
    """Replace renamed and remove deprecated skill permissions in settings.json."""
    perm_map = MIGRATIONS.get("permissions", {})
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    perms = settings.get("permissions", {}).get("allow", [])
    if not perms:
        return
    migrated = []
    seen = set()
    removed = []
    for p in perms:
        if p in DEPRECATED_PERMISSIONS:
            removed.append(p)
            continue
        replacement = perm_map.get(p, p)
        if replacement not in seen:
            migrated.append(replacement)
            seen.add(replacement)
    if migrated != perms:
        settings["permissions"]["allow"] = migrated
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if sys.platform != "win32":
            os.chmod(settings_path, 0o600)
    if removed:
        print(_t("perm_removed", keys=', '.join(removed)))


def cleanup_old_skill_dirs(claude_home: Path) -> None:
    """Remove old skill directories superseded by renames."""
    dir_map = MIGRATIONS.get("directories", {})
    for old_rel, new_rel in dir_map.items():
        old_path = claude_home / old_rel
        new_path = claude_home / new_rel
        if old_path.exists() and new_path.exists():
            shutil.rmtree(old_path)
            print(_t("removed_old_dir", name=old_rel))


def register_mcp_server(claude_home: Path) -> None:
    """Register remy-index MCP server in ~/.claude.json (user-level config)."""
    mcp_tpl_path = SCRIPT_DIR / MCP_TEMPLATE
    if not mcp_tpl_path.exists():
        return
    with open(mcp_tpl_path, "r", encoding="utf-8") as f:
        mcp_entries = json.load(f)

    abs_prefix = str(claude_home).replace("\\", "/")
    for _name, conf in mcp_entries.items():
        args = conf.get("args", [])
        for i, arg in enumerate(args):
            if isinstance(arg, str) and "~/.claude/" in arg:
                args[i] = arg.replace("~/.claude/", abs_prefix + "/")

    claude_json_path = claude_home.parent / ".claude.json"
    existing = {}
    if claude_json_path.exists():
        try:
            with open(claude_json_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(".claude.json is invalid") from exc
        if not isinstance(existing, dict):
            raise ValueError(".claude.json must be an object")

    ext_mcp = existing.setdefault("mcpServers", {})
    for server_name, server_conf in mcp_entries.items():
        ext_mcp[server_name] = server_conf

    module_dir = SCRIPT_DIR / "remy-src"
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))
    storage = importlib.import_module("install_runtime.storage")
    storage.atomic_write_json(claude_json_path, existing)

    print(_t("mcp_registered", name=", ".join(mcp_entries.keys())))


def prompt_language() -> str:
    """Interactive bilingual language selection."""
    print("Select language / 选择语言:")
    print("  1. English")
    print("  2. 简体中文")
    try:
        choice = input("Choice / 选择 [1]: ").strip()
    except EOFError:
        return "en"
    if choice == "2":
        return "zh-CN"
    return "en"


def configure_api(config_path: Path) -> None:
    """Interactive LLM API configuration for Logic Index."""
    remy_config = _load_remy_config_module()
    document = (
        remy_config.read_document(config_path, strict=True)
        if config_path.exists()
        else {"schema_version": remy_config.SCHEMA_VERSION, "values": {}}
    )
    values = document["values"]

    current_key = values.get("REMY_LLM_API_KEY", "").strip()
    has_config = current_key not in remy_config.INVALID_SECRET_VALUES

    try:
        if has_config:
            answer = input(_t("api_config_existing")).strip().lower()
            if answer != "y":
                return
        else:
            answer = input(_t("api_config_new")).strip().lower()
            if answer == "n":
                return

        print()
        print(_t("api_cost_hint"))

        while True:
            print()

            default_url = values.get("REMY_LLM_BASE_URL", "https://api.deepseek.com/v1/chat/completions")
            default_model = values.get("REMY_LLM_MODEL", "deepseek-v4-flash")

            url = input(_t("api_url_prompt", default=default_url)).strip()
            if not url:
                url = default_url

            model = input(_t("api_model_prompt", default=default_model)).strip()
            if not model:
                model = default_model

            api_key = getpass.getpass(_t("api_key_prompt")).strip()

            if not api_key:
                print(_t("api_key_empty"))
                return

            updates = {
                "REMY_LLM_BASE_URL": url,
                "REMY_LLM_MODEL": model,
                "REMY_LLM_API_KEY": api_key,
            }
            remy_config.save_config(config_path, updates)
            values.update(updates)
            print(_t("api_configured"))

            print()
            test_answer = input(_t("api_test_prompt")).strip().lower()
            if test_answer != "y":
                break

            if test_api_connectivity(url, api_key, model):
                break

            print()
            reconfigure = input(_t("api_test_reconfigure")).strip().lower()
            if reconfigure == "n":
                break
    except EOFError:
        return


def test_api_connectivity(url: str, api_key: str, model: str) -> bool:
    """Send a minimal request to verify API URL and key. Retries up to 3 times."""
    print(_t("api_test_running"))
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "max_tokens": 5,
    }).encode("utf-8")

    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            print(_t("api_test_ok"))
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            print(_t("api_test_attempt", n=attempt, err=str(e)))

    print(_t("api_test_all_failed"))
    return False


# ── CLI Shim & PATH ───────────────────────────────────────────


def create_shim(claude_home: Path) -> Path:
    bin_dir = claude_home / "bin"
    bin_dir.mkdir(exist_ok=True)
    cli_path = claude_home / "remy-src" / "cli.py"

    if sys.platform == "win32":
        shim = bin_dir / "remy-cc.cmd"
        shim.write_text('@echo off\npython "{}" %*\n'.format(cli_path), encoding="utf-8")
    else:
        shim = bin_dir / "remy-cc"
        shim.write_text('#!/bin/sh\nexec python3 "{}" "$@"\n'.format(cli_path), encoding="utf-8")
        shim.chmod(0o755)

    print(_t("shim_created", path=shim))
    return bin_dir


def _is_in_path(bin_dir: Path) -> bool:
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    target = os.path.normcase(os.path.normpath(str(bin_dir)))
    return any(os.path.normcase(os.path.normpath(d)) == target for d in path_dirs if d)


def register_path(bin_dir: Path) -> None:
    bin_str = str(bin_dir)

    if _is_in_path(bin_dir):
        print(_t("path_already", dir=bin_str))
        return

    try:
        answer = input(_t("path_prompt")).strip().lower()
    except EOFError:
        answer = ""

    if answer == "n":
        print(_t("path_manual", path=bin_str))
        return

    if sys.platform == "win32":
        result = subprocess.run(
            ["reg", "query", "HKCU\\Environment", "/v", "PATH"],
            capture_output=True, text=True,
        )
        current_user_path = ""
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "PATH" in line.upper() and "REG_" in line.upper():
                    parts = line.split("    ")
                    if len(parts) >= 3:
                        current_user_path = parts[-1].strip()
        new_path = (current_user_path + os.pathsep + bin_str) if current_user_path else bin_str
        if len(new_path) > 1024:
            print(_t("path_too_long"))
            print(_t("path_manual", path=bin_str))
            return
        subprocess.run(["setx", "PATH", new_path], capture_output=True, check=False)
        print(_t("path_set_win"))
    else:
        shell = os.environ.get("SHELL", "/bin/bash")
        rc_file = Path.home() / (".zshrc" if "zsh" in shell else ".bashrc")
        export_line = 'export PATH="$PATH:{}"'.format(bin_str)
        if rc_file.exists():
            content = rc_file.read_text(encoding="utf-8")
            if bin_str in content:
                print(_t("path_already", dir=bin_str))
                return
        with open(rc_file, "a", encoding="utf-8") as f:
            f.write("\n# Remy-CC CLI\n{}\n".format(export_line))
        print(_t("path_set_unix", rc=rc_file.name))


# ── Main Commands ──────────────────────────────────────────────


def _remy_cc_home() -> Path:
    env_home = os.environ.get("REMY_CC_HOME")
    if env_home:
        return Path(env_home)
    return _user_home() / ".remy-cc"


def _daemon_exe_name() -> str:
    return "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"


def _daemon_status(exe: Path) -> Optional[bool]:
    """Return True when the daemon lock is held, False when it is not, and
    None when the status probe cannot establish either state."""
    try:
        result = subprocess.run(
            [str(exe), "status"], capture_output=True, timeout=DAEMON_PROBE_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _daemon_running(exe: Path) -> bool:
    """Return whether `status` confirms a held daemon lock."""
    return _daemon_status(exe) is True


def _reclaim_deployed_daemon(records: list) -> None:
    """Re-record an already-deployed binary when this run skips deployment.
    cleanup_from_manifest no longer deletes it, so without re-recording the
    entry would drop out of the manifest and uninstall would strand the file."""
    dst = _remy_cc_home() / "bin" / _daemon_exe_name()
    if dst.is_file():
        records.append({"path": str(dst), "sha256": compute_sha256(dst)})


def deploy_daemon_binary(records: list) -> None:
    """Deploy a locally built remy-daemon binary to ~/.remy-cc/bin/.

    Skips (with a printed reason) when the source binary is absent, fails the
    --version check, or a daemon instance is currently running (overwriting a
    running executable fails on Windows; the stale-version prompt tells the
    user to stop it first). Every skip path re-records the existing binary so
    it stays under manifest hash claim. The deployed path is recorded as an
    absolute path (legacy-format record, resolved as-is by
    _resolve_record_path)."""
    exe_name = _daemon_exe_name()
    src = DAEMON_SOURCE_DIR / exe_name
    if not src.exists():
        print(_t("daemon_src_missing"))
        _reclaim_deployed_daemon(records)
        return
    try:
        probe = subprocess.run(
            [str(src), "--version"], capture_output=True, timeout=DAEMON_PROBE_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        print(_t("daemon_verify_failed", err="timeout"))
        _reclaim_deployed_daemon(records)
        return
    except OSError as e:
        print(_t("daemon_verify_failed", err=e))
        _reclaim_deployed_daemon(records)
        return
    if probe.returncode != 0:
        print(_t("daemon_verify_failed", err="exit code {}".format(probe.returncode)))
        _reclaim_deployed_daemon(records)
        return
    if _daemon_running(src):
        print(_t("daemon_running"))
        _reclaim_deployed_daemon(records)
        return
    dst_dir = _remy_cc_home() / "bin"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / exe_name
    try:
        shutil.copy2(src, dst)
    except OSError as e:
        print(_t("daemon_copy_failed", err=e))
        _reclaim_deployed_daemon(records)
        return
    if os.name == "posix":
        os.chmod(dst, 0o755)
    records.append({"path": str(dst), "sha256": compute_sha256(dst)})
    print(_t("daemon_deployed", path=dst))


def do_install() -> None:
    if sys.version_info < (3, 10):
        print(_t("verify_python_old", ver=sys.version), file=sys.stderr)
        sys.exit(1)

    claude_home = get_claude_home()

    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and hasattr(os, "getuid") and os.getuid() == 0:
        print(_t("warn_sudo", user=sudo_user, path=claude_home))

    if claude_home.exists() and not claude_home.is_dir():
        print(_t("err_home_is_file", path=claude_home))
        sys.exit(1)

    claude_home.mkdir(parents=True, exist_ok=True)

    print(f"Remy v{SUITE_VERSION}")
    print(_t("target_dir", path=claude_home) + "\n")

    records = []

    old_manifest_path = claude_home / MANIFEST_FILE
    if old_manifest_path.exists():
        try:
            with open(old_manifest_path, "r", encoding="utf-8") as f:
                old_manifest = json.load(f)
            cleanup_from_manifest(old_manifest, claude_home)
        except (json.JSONDecodeError, OSError):
            cleanup_fallback(claude_home)
    else:
        cleanup_fallback(claude_home)

    lang_directives = {"zh-CN": "Always respond in Chinese-simplified", "en": "Always respond in English"}
    lang_md_path = claude_home / "language.md"
    if lang_md_path.exists():
        bp = backup_file(lang_md_path)
        if bp:
            print(_t("backed_up", name="language.md", bak=bp.name))
    lang_md_path.write_text(lang_directives.get(_ui_lang, lang_directives["en"]) + "\n", encoding="utf-8")
    records.append({
        "path": "language.md",
        "sha256": compute_sha256(lang_md_path),
    })
    print(_t("copied_file", name="language.md"))

    for src_rel, dst_name in DEPLOY_FILES_MAP.items():
        src = SCRIPT_DIR / src_rel
        dst = claude_home / dst_name
        if not src.exists():
            print(_t("src_missing_file", name=src_rel))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            bp = backup_file(dst)
            if bp:
                print(_t("backed_up", name=dst_name, bak=bp.name))
        rec = copy_file(src, dst, claude_home)
        records.append(rec)
        print(_t("copied_file", name=dst_name))

    for dirname in DEPLOY_DIRS:
        src = SCRIPT_DIR / dirname
        dst = claude_home / dirname
        if not src.exists():
            print(_t("src_missing_dir", name=dirname))
            continue
        dir_records = copy_tree(src, dst, claude_home)
        records.extend(dir_records)
        print(_t("copied_dir", name=dirname, count=len(dir_records)))

    tpl_path = SCRIPT_DIR / SETTINGS_TEMPLATE
    if tpl_path.exists():
        with open(tpl_path, "r", encoding="utf-8") as f:
            template = json.load(f)
        settings_path = claude_home / "settings.json"
        remy_config = _load_remy_config_module()
        remy_config_path = claude_home / remy_config.CONFIG_FILE_NAME
        settings_backup = merge_settings(template, settings_path, claude_home, lang_override=_ui_lang)
        remy_config.migrate_settings_file(settings_path, remy_config_path)
        remy_config.save_config(remy_config_path, {"REMY_LANG": _ui_lang})
        migrate_permissions(settings_path)
        cleanup_old_skill_dirs(claude_home)
        print(_t("db_migrate_notice"))
        patch_script = claude_home / "remy-src" / "patch_descriptions.py"
        if patch_script.exists():
            subprocess.run(
                [sys.executable, str(patch_script),
                 "--claude-home", str(claude_home), "--lang", _ui_lang],
                check=False,
            )
        print(_t("settings_merged"))
        print()
        configure_api(remy_config_path)
        refresh_record_hashes(records, claude_home)
    else:
        print(_t("settings_tpl_missing", name=SETTINGS_TEMPLATE))
        settings_backup = None

    print()
    deploy_daemon_binary(records)

    injected_hooks = template.get("hooks", {}) if tpl_path.exists() else {}
    injected_perms = template.get("permissions", {}).get("allow", []) if tpl_path.exists() else []
    write_manifest(claude_home, records, settings_backup,
                   injected_hooks=injected_hooks, injected_permissions=injected_perms)
    print(_t("manifest_written", name=MANIFEST_FILE))

    print()
    ts_installed = False
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_c  # noqa: F401
        import tree_sitter_cpp  # noqa: F401
        import tree_sitter_typescript  # noqa: F401
        ts_installed = True
    except ImportError:
        pass

    if ts_installed:
        print(_t("ts_installed"))
    else:
        try:
            answer = input(_t("ts_prompt")).strip().lower()
        except EOFError:
            answer = ""
        if answer != "n":
            print(_t("ts_installing"))
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user",
                 "tree-sitter", "tree-sitter-c", "tree-sitter-cpp",
                 "tree-sitter-typescript"],
                check=False,
            )

    print()
    j2_installed = False
    try:
        import jinja2  # noqa: F401
        j2_installed = True
    except ImportError:
        pass

    if j2_installed:
        print(_t("j2_installed"))
    else:
        try:
            answer = input(_t("j2_prompt")).strip().lower()
        except EOFError:
            answer = ""
        if answer != "n":
            print(_t("j2_installing"))
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user", "Jinja2"],
                check=False,
            )

    print()
    mcp_installed = False
    try:
        import mcp  # noqa: F401
        mcp_installed = True
    except ImportError:
        pass

    if mcp_installed:
        print(_t("mcp_installed"))
    else:
        print(_t("mcp_installing"))
        mcp_proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "mcp"],
            check=False,
        )
        if mcp_proc.returncode != 0:
            print(_t("mcp_install_failed"), file=sys.stderr)
            sys.exit(1)

    register_mcp_server(claude_home)

    print()
    gh_available = shutil.which("gh") is not None
    if gh_available:
        print(_t("gh_installed"))
    else:
        print(_t("gh_not_found"))

    print()
    bin_dir = create_shim(claude_home)
    register_path(bin_dir)

    print(_t("install_done", count=len(records)))
    print(_t("install_verify_hint"))


def do_uninstall() -> None:
    claude_home = get_claude_home()
    manifest_path = claude_home / MANIFEST_FILE

    if not manifest_path.exists():
        print(_t("no_manifest"))
        return

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    daemon_exe = _remy_cc_home() / "bin" / _daemon_exe_name()
    if daemon_exe.is_file():
        daemon_status = _daemon_status(daemon_exe)
        if daemon_status is True:
            print(_t("uninstall_daemon_running"))
            return
        if daemon_status is None:
            print(_t("uninstall_daemon_status_unknown"))
            return

    files = manifest.get("files", [])
    removed = 0
    skipped = 0

    affected_parents = set()
    for entry in files:
        fpath = _resolve_record_path(entry, claude_home)
        if not fpath.exists():
            continue
        expected_hash = entry.get("sha256")
        if expected_hash is not None:
            current_hash = compute_sha256(fpath)
            if current_hash != expected_hash:
                print(_t("skip_modified", name=fpath.name))
                skipped += 1
                continue
        fpath.unlink()
        removed += 1
        affected_parents.add(fpath.parent)

    for parent in affected_parents:
        _remove_empty_dirs(parent, claude_home)

    settings_path = claude_home / "settings.json"
    tpl_path = SCRIPT_DIR / SETTINGS_TEMPLATE
    if settings_path.exists() and tpl_path.exists():
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        with open(tpl_path, "r", encoding="utf-8") as f:
            template = json.load(f)

        expand_hook_paths(template, claude_home)
        remove_suite_hooks(settings, template)
        remove_suite_permissions(settings, template)

        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(_t("hooks_removed"))

    claude_md_bak = claude_home / ("CLAUDE.md" + BACKUP_SUFFIX)
    claude_md = claude_home / "CLAUDE.md"
    if claude_md_bak.exists():
        shutil.copy2(claude_md_bak, claude_md)
        claude_md_bak.unlink()
        print(_t("claude_restored"))

    bin_dir = claude_home / "bin"
    if bin_dir.is_symlink():
        bin_dir.unlink()
    elif bin_dir.exists():
        shutil.rmtree(bin_dir, ignore_errors=True)
        print(_t("path_cleanup"))

    manifest_path.unlink()

    print(_t("uninstall_done", removed=removed, skipped=skipped))


def do_verify() -> None:
    claude_home = get_claude_home()
    errors = []
    settings = None

    if sys.version_info < (3, 10):
        errors.append(_t("verify_python_old", ver=sys.version))

    settings_path = claude_home / "settings.json"
    if not settings_path.exists():
        errors.append(_t("verify_settings_missing"))
    else:
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except json.JSONDecodeError as e:
            errors.append(_t("verify_settings_invalid", err=e))
            settings = None

        if settings:
            hooks = settings.get("hooks", {})
            for event, entries in hooks.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    for hook in entry.get("hooks", []):
                        cmd = hook.get("command", "")
                        parts = cmd.split('"')
                        if len(parts) >= 2:
                            hook_path = Path(parts[1])
                            if not hook_path.exists():
                                errors.append(_t("verify_hook_missing", path=hook_path))

    manifest_path = claude_home / MANIFEST_FILE
    if not manifest_path.exists():
        errors.append(_t("verify_manifest_missing", name=MANIFEST_FILE))
    else:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        missing = 0
        mismatched = 0
        for entry in manifest.get("files", []):
            target = _resolve_record_path(entry, claude_home)
            if not target.exists():
                missing += 1
                continue
            expected_hash = entry.get("sha256")
            if expected_hash:
                try:
                    actual_hash = compute_sha256(target)
                except OSError:
                    mismatched += 1
                else:
                    if actual_hash != expected_hash:
                        mismatched += 1
        if missing:
            errors.append(_t("verify_files_missing", count=missing))
        if mismatched:
            errors.append(_t("verify_files_mismatch", count=mismatched))

    ts_available = False
    try:
        import tree_sitter  # noqa: F401
        ts_available = True
    except ImportError:
        pass

    j2_available = False
    try:
        import jinja2  # noqa: F401
        j2_available = True
    except ImportError:
        pass

    mcp_available = False
    try:
        import mcp  # noqa: F401
        mcp_available = True
    except ImportError:
        pass
    if not mcp_available:
        errors.append(_t("verify_mcp_missing"))

    pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(_t("verify_header", ver=SUITE_VERSION))
    print(_t("verify_python", ver=pyver))
    print(_t("verify_target", path=claude_home))
    print(_t("verify_ts", status=_t("verify_ts_yes") if ts_available else _t("verify_ts_no")))
    print(_t("verify_j2", status=_t("verify_ts_yes") if j2_available else _t("verify_ts_no")))
    print(_t("verify_mcp", status=_t("verify_ts_yes") if mcp_available else _t("verify_mcp_no")))
    gh_available = shutil.which("gh") is not None
    print(_t("verify_gh", status=_t("verify_ts_yes") if gh_available else _t("verify_ts_no")))

    api_configured = False
    try:
        remy_config = _load_remy_config_module()
        snapshot = remy_config.load_config(strict=True)
        api_key = str(snapshot.get("REMY_LLM_API_KEY", "")).strip()
        api_configured = api_key not in remy_config.INVALID_SECRET_VALUES
    except (OSError, ValueError):
        api_configured = False
    if not api_configured:
        print(_t("verify_api_not_configured"))

    print()

    if errors:
        print(_t("verify_issues", count=len(errors)))
        for err in errors:
            print(f"  [X] {err}")
        sys.exit(1)
    else:
        print(_t("verify_ok"))


def _prepare_dependencies(non_interactive: bool) -> bool:
    required = ("mcp",)
    missing_required = []
    for name in required:
        try:
            __import__(name)
        except ImportError:
            missing_required.append(name)
    if missing_required:
        if non_interactive:
            print(_t("mcp_install_failed"), file=sys.stderr)
            return False
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", *missing_required],
            check=False,
        )
        if result.returncode != 0:
            print(_t("mcp_install_failed"), file=sys.stderr)
            return False
    if non_interactive:
        return True
    optional = (
        ("tree_sitter", ["tree-sitter", "tree-sitter-c", "tree-sitter-cpp", "tree-sitter-typescript"], "ts_prompt"),
        ("jinja2", ["Jinja2"], "j2_prompt"),
    )
    for module_name, packages, prompt_key in optional:
        try:
            __import__(module_name)
            continue
        except ImportError:
            pass
        try:
            answer = input(_t(prompt_key)).strip().lower()
        except EOFError:
            answer = "n"
        if answer != "n":
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--user", *packages],
                check=False,
            )
    return True


def _build_install_candidates(stage_root: Path, lang: str, install_runtime, roots):
    stage_claude = stage_root / "claude"
    stage_claude.mkdir(parents=True)
    directive = "Always respond in Chinese-simplified" if lang == "zh-CN" else "Always respond in English"
    (stage_claude / "language.md").write_text(directive + "\n", encoding="utf-8")
    for src_rel, dst_name in DEPLOY_FILES_MAP.items():
        src = SCRIPT_DIR / src_rel
        if not src.is_file():
            raise FileNotFoundError(src_rel)
        dst = stage_claude / dst_name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for dirname in DEPLOY_DIRS:
        src = SCRIPT_DIR / dirname
        if not src.is_dir():
            raise FileNotFoundError(dirname)
        shutil.copytree(
            src,
            stage_claude / dirname,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
    shim_dir = stage_claude / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    final_cli = roots.claude / "remy-src" / "cli.py"
    if sys.platform == "win32":
        shim_path = shim_dir / "remy-cc.cmd"
        shim_path.write_text(
            '@echo off\r\n"{}" "{}" %*\r\n'.format(sys.executable, final_cli),
            encoding="utf-8",
        )
    else:
        shim_path = shim_dir / "remy-cc"
        shim_path.write_text(
            '#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, final_cli),
            encoding="utf-8",
        )
        shim_path.chmod(0o755)
    patch_module_dir = SCRIPT_DIR / "remy-src"
    if str(patch_module_dir) not in sys.path:
        sys.path.insert(0, str(patch_module_dir))
    patch_descriptions = importlib.import_module("patch_descriptions")
    patch_descriptions.patch(stage_claude, lang)
    candidates = []
    for path in sorted(item for item in stage_claude.rglob("*") if item.is_file()):
        relative = path.relative_to(stage_claude).as_posix()
        if relative.startswith("hooks/"):
            role = "python_hook"
        elif relative.startswith("skills/"):
            role = "claude_skill"
        elif relative.startswith("output-styles/"):
            role = "output_style"
        elif relative.startswith("remy-src/install_runtime/"):
            role = "install_runtime"
        elif relative.startswith("remy-src/"):
            role = "cli_runtime"
        else:
            role = "claude_protocol"
        candidates.append(
            install_runtime.CandidateFile(
                "claude", relative, path, role, executable=(relative == "bin/remy-cc")
            )
        )
    return candidates


def _emit_operation_result(result, json_mode: bool) -> int:
    if json_mode:
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        for warning in result.warnings:
            print("  [!] " + warning, file=sys.stderr)
        if result.exit_code == 0:
            print(_t("install_done", count=len(result.changed)))
    return int(result.exit_code)


def do_install_v3(args) -> None:
    if sys.version_info < (3, 10):
        if args.json:
            print(json.dumps({
                "schema_version": 1,
                "operation": "install",
                "status": "preflight_rejected",
                "exit_code": 1,
                "hook_mode": None,
                "changed": [],
                "warnings": ["Python 3.10 or newer is required"],
                "recovery": None,
            }, sort_keys=True))
        else:
            print(_t("verify_python_old", ver=sys.version), file=sys.stderr)
        raise SystemExit(1)
    non_interactive = bool(args.non_interactive or args.json)
    install_runtime, result_for_error = _load_install_runtime_module()
    if not _prepare_dependencies(non_interactive):
        result = result_for_error(
            "install",
            install_runtime.InstallRuntimeError("required dependencies are unavailable"),
        )
        raise SystemExit(_emit_operation_result(result, bool(args.json)))
    roots = install_runtime.roots_from_environment()
    runtime = install_runtime.InstallRuntime(roots)
    with tempfile.TemporaryDirectory(prefix="remy-install-candidates-") as temporary:
        candidates = _build_install_candidates(Path(temporary), _ui_lang, install_runtime, roots)
        template = json.loads((SCRIPT_DIR / SETTINGS_TEMPLATE).read_text(encoding="utf-8"))
        request = install_runtime.InstallRequest(
            suite_version=SUITE_VERSION,
            candidates=candidates,
            settings_template=template,
            python_executable=sys.executable,
            daemon_candidate=DAEMON_SOURCE_DIR / _daemon_exe_name(),
        )
        try:
            result = runtime.install(request)
        except install_runtime.InstallRuntimeError as exc:
            result = result_for_error("install", exc)
    if result.exit_code != 0:
        raise SystemExit(_emit_operation_result(result, bool(args.json)))

    try:
        output_context = contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext()
        with output_context:
            settings_path = roots.claude / "settings.json"
            remy_config = _load_remy_config_module()
            remy_config_path = roots.claude / remy_config.CONFIG_FILE_NAME
            remy_config.migrate_settings_file(settings_path, remy_config_path)
            remy_config.save_config(remy_config_path, {"REMY_LANG": _ui_lang})
            migrate_permissions(settings_path)
            if not non_interactive:
                configure_api(remy_config_path)
            register_mcp_server(roots.claude)
            if not non_interactive:
                register_path(roots.claude / "bin")
    except Exception as exc:
        result = result_for_error(
            "install",
            install_runtime.InstallRuntimeError(
                "installation committed but post-install configuration failed",
                category="cleanup",
            ),
        )
        if not args.json:
            print("  [!] " + type(exc).__name__, file=sys.stderr)
    code = _emit_operation_result(result, bool(args.json))
    if code != 0:
        raise SystemExit(code)


def do_uninstall_v3(args) -> None:
    if not args.non_interactive and not args.json:
        try:
            answer = input(_t("uninstall_confirm")).strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            print(_t("uninstall_aborted"))
            return
    install_runtime, result_for_error = _load_install_runtime_module()
    runtime = install_runtime.InstallRuntime(install_runtime.roots_from_environment())
    try:
        result = runtime.uninstall(purge_state=bool(args.purge_state))
    except install_runtime.InstallRuntimeError as exc:
        result = result_for_error("uninstall", exc)
    code = _emit_operation_result(result, bool(args.json))
    if code != 0:
        raise SystemExit(code)


def do_verify_v3(args) -> None:
    install_runtime, _ = _load_install_runtime_module()
    runtime = install_runtime.InstallRuntime(install_runtime.roots_from_environment())
    result = runtime.verify_environment()
    code = _emit_operation_result(result, bool(args.json))
    if code != 0:
        raise SystemExit(code)


def main() -> None:
    global _ui_lang
    parser = argparse.ArgumentParser(
        description=_t("argparse_desc"),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--uninstall", action="store_true", help=_t("argparse_uninstall"))
    group.add_argument("--verify", action="store_true", help=_t("argparse_verify"))
    parser.add_argument("--lang", default=None, choices=["en", "zh-CN"],
                        help=_t("argparse_lang"))
    parser.add_argument("--non-interactive", action="store_true",
                        help="Disable prompts and dependency installation")
    parser.add_argument("--json", action="store_true",
                        help="Emit one JSON result object and imply --non-interactive")
    parser.add_argument("--purge-state", action="store_true",
                        help="With --uninstall, also remove user-level engine state")
    args = parser.parse_args()

    if args.purge_state and not args.uninstall:
        parser.error("--purge-state requires --uninstall")
    if args.lang:
        _ui_lang = args.lang
    elif not args.uninstall and not args.verify and not args.non_interactive and not args.json:
        _ui_lang = prompt_language()

    if args.uninstall:
        do_uninstall_v3(args)
    elif args.verify:
        do_verify_v3(args)
    else:
        do_install_v3(args)


if __name__ == "__main__":
    try:
        main()
    except PermissionError as e:
        if "--json" in sys.argv:
            operation = "uninstall" if "--uninstall" in sys.argv else "verify" if "--verify" in sys.argv else "install"
            print(json.dumps({
                "schema_version": 1,
                "operation": operation,
                "status": "preflight_rejected",
                "exit_code": 1,
                "hook_mode": None,
                "changed": [],
                "warnings": ["permission denied"],
                "recovery": None,
            }, ensure_ascii=False, sort_keys=True))
        else:
            print(_t("err_permission", err=e))
        sys.exit(1)
