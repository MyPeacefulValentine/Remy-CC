#!/usr/bin/env python3
"""
Remy - Installer

Usage:
    python install.py              # Install (default)
    python install.py --uninstall  # Uninstall
    python install.py --verify     # Verify installation
"""

import argparse
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SUITE_VERSION = "0.9.0"
MANIFEST_FILE = ".installer_manifest.json"

DEPLOY_DIRS = ["hooks", "skills", "output-styles"]
DEPLOY_FILES_MAP = {
    "CLAUDE.md": "CLAUDE.md",
    "language.md": "language.md",
    "style.md": "style.md",
    "tools_ref.md": "tools_ref.md",
    "remy-src/cli.py": "remy-src/cli.py",
    "remy-src/config_ui.py": "remy-src/config_ui.py",
    "remy-src/config_ui.html": "remy-src/config_ui.html",
    "remy-src/logic_scope_ui.py": "remy-src/logic_scope_ui.py",
    "remy-src/logic_scope_ui.html": "remy-src/logic_scope_ui.html",
    "remy-assets/logo.svg": "remy-assets/logo.svg",
}
SETTINGS_TEMPLATE = "settings.example.json"

BACKUP_SUFFIX = ".bak"
API_KEY_PLACEHOLDER = "YOUR_API_KEY_HERE"

SCRIPT_DIR = Path(__file__).resolve().parent

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
        "manifest_written": "  [+] {name}",
        "ts_installed": "  [i] tree-sitter already installed, skipping",
        "ts_prompt": "Install tree-sitter (high-precision C/C++/TypeScript parsing)? [Y/n] ",
        "ts_installing": "  Installing tree-sitter ...",
        "j2_installed": "  [i] Jinja2 already installed, skipping",
        "j2_prompt": "Install Jinja2 (post-verify template rendering)? [Y/n] ",
        "j2_installing": "  Installing Jinja2 ...",
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
        "verify_python_old": "Python version too old: {ver} (requires >= 3.7)",
        "verify_settings_missing": "settings.json not found",
        "verify_settings_invalid": "settings.json JSON format error: {err}",
        "verify_hook_missing": "Hook file not found: {path}",
        "verify_manifest_missing": "{name} not found",
        "verify_files_missing": "{count} files missing from manifest",
        "verify_header": "Remy v{ver} - Installation Verification\n",
        "verify_python": "  Python: {ver}",
        "verify_target": "  Target directory: {path}",
        "verify_ts": "  tree-sitter: {status}",
        "verify_j2": "  Jinja2: {status}",
        "verify_ts_yes": "installed",
        "verify_ts_no": "not installed (optional)",
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
        "manifest_written": "  [+] {name}",
        "ts_installed": "  [i] tree-sitter 已安装，跳过",
        "ts_prompt": "是否安装 tree-sitter（C/C++/TypeScript 高精度解析）？[Y/n] ",
        "ts_installing": "  正在安装 tree-sitter ...",
        "j2_installed": "  [i] Jinja2 已安装，跳过",
        "j2_prompt": "是否安装 Jinja2（post-verify 模板渲染增强）？[Y/n] ",
        "j2_installing": "  正在安装 Jinja2 ...",
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
        "verify_python_old": "Python 版本过低: {ver} (需要 >= 3.7)",
        "verify_settings_missing": "settings.json 不存在",
        "verify_settings_invalid": "settings.json JSON 格式错误: {err}",
        "verify_hook_missing": "hook 文件不存在: {path}",
        "verify_manifest_missing": "{name} 不存在",
        "verify_files_missing": "manifest 中 {count} 个文件缺失",
        "verify_header": "Remy v{ver} - 安装验证\n",
        "verify_python": "  Python: {ver}",
        "verify_target": "  目标目录: {path}",
        "verify_ts": "  tree-sitter: {status}",
        "verify_j2": "  Jinja2: {status}",
        "verify_ts_yes": "已安装",
        "verify_ts_no": "未安装（可选）",
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
    },
}

_ui_lang = "en"


def _t(key, **kwargs):
    template = UI.get(_ui_lang, UI["en"]).get(key, UI["en"].get(key, key))
    return template.format(**kwargs) if kwargs else template


def get_claude_home() -> Path:
    try:
        home = Path.home()
    except RuntimeError:
        home_str = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
        if not home_str:
            print(_t("err_home_not_found"))
            sys.exit(1)
        home = Path(home_str)
    return home / ".claude"


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_tree(src: Path, dst: Path) -> list:
    """Recursively copy directory, return list of {path, sha256} records."""
    records = []
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    for root, _dirs, files in os.walk(dst):
        for fname in files:
            fpath = Path(root) / fname
            records.append({
                "path": str(fpath),
                "sha256": compute_sha256(fpath),
            })
    return records


def copy_file(src: Path, dst: Path) -> dict:
    """Copy single file, return {path, sha256} record."""
    shutil.copy2(src, dst)
    return {
        "path": str(dst),
        "sha256": compute_sha256(dst),
    }


def backup_file(path: Path) -> Optional[Path]:
    """Backup file to path.bak. Returns backup path or None if source absent."""
    if not path.exists():
        return None
    backup_path = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    shutil.copy2(path, backup_path)
    return backup_path


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

    # --- permissions.allow: array dedup append ---
    tpl_perms = template.get("permissions", {}).get("allow", [])
    ext_perms = existing.setdefault("permissions", {}).setdefault("allow", [])
    for perm in tpl_perms:
        if perm not in ext_perms:
            ext_perms.append(perm)

    # --- env: write only missing keys ---
    tpl_env = template.get("env", {})
    ext_env = existing.setdefault("env", {})
    missing_keys = []
    for key, value in tpl_env.items():
        if key not in ext_env:
            ext_env[key] = value
            missing_keys.append(key)

    # --- REMY_LANG override from --lang ---
    if lang_override:
        ext_env["REMY_LANG"] = lang_override

    # --- outputStyle: write if absent ---
    if "outputStyle" not in existing and "outputStyle" in template:
        existing["outputStyle"] = template["outputStyle"]

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

    return settings_backup


def write_manifest(claude_home: Path, records: list, settings_backup: Optional[Path],
                    injected_hooks: dict = None, injected_permissions: list = None) -> None:
    manifest = {
        "version": SUITE_VERSION,
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


def configure_api(settings_path: Path) -> None:
    """Interactive LLM API configuration for Logic Index."""
    if not settings_path.exists():
        return

    with open(settings_path, "r", encoding="utf-8") as f:
        settings = json.load(f)
    env = settings.setdefault("env", {})

    current_key = env.get("OPENAI_API_KEY", "").strip()
    has_config = current_key not in ("", API_KEY_PLACEHOLDER)

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

            default_url = env.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1/chat/completions")
            default_model = env.get("OPENAI_MODEL", "deepseek-v4-flash")

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

            env["OPENAI_BASE_URL"] = url
            env["OPENAI_MODEL"] = model
            env["OPENAI_API_KEY"] = api_key

            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.write("\n")
            if sys.platform != "win32":
                os.chmod(settings_path, 0o600)
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


def do_install() -> None:
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
        rec = copy_file(src, dst)
        records.append(rec)
        print(_t("copied_file", name=dst_name))

    for dirname in DEPLOY_DIRS:
        src = SCRIPT_DIR / dirname
        dst = claude_home / dirname
        if not src.exists():
            print(_t("src_missing_dir", name=dirname))
            continue
        dir_records = copy_tree(src, dst)
        records.extend(dir_records)
        print(_t("copied_dir", name=dirname, count=len(dir_records)))

    tpl_path = SCRIPT_DIR / SETTINGS_TEMPLATE
    if tpl_path.exists():
        with open(tpl_path, "r", encoding="utf-8") as f:
            template = json.load(f)
        settings_path = claude_home / "settings.json"
        settings_backup = merge_settings(template, settings_path, claude_home, lang_override=_ui_lang)
        print(_t("settings_merged"))
        print()
        configure_api(settings_path)
    else:
        print(_t("settings_tpl_missing", name=SETTINGS_TEMPLATE))
        settings_backup = None

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
    bin_dir = create_shim(claude_home)
    register_path(bin_dir)

    print(_t("install_done", count=len(records)))
    print(_t("install_verify_hint"))

    lang_directives = {"zh-CN": "Always respond in Chinese-simplified", "en": "Always respond in English"}
    lang_md_path = claude_home / "language.md"
    lang_md_path.write_text(lang_directives.get(_ui_lang, lang_directives["en"]) + "\n", encoding="utf-8")


def do_uninstall() -> None:
    claude_home = get_claude_home()
    manifest_path = claude_home / MANIFEST_FILE

    if not manifest_path.exists():
        print(_t("no_manifest"))
        return

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    files = manifest.get("files", [])
    removed = 0
    skipped = 0

    for entry in files:
        fpath = Path(entry["path"])
        if not fpath.exists():
            continue
        current_hash = compute_sha256(fpath)
        if current_hash != entry["sha256"]:
            print(_t("skip_modified", name=fpath.name))
            skipped += 1
            continue
        fpath.unlink()
        removed += 1

    for dirname in DEPLOY_DIRS + ["remy-src", "remy-assets"]:
        dirpath = claude_home / dirname
        if dirpath.is_symlink():
            dirpath.unlink()
        elif dirpath.exists():
            try:
                shutil.rmtree(dirpath)
            except OSError:
                pass

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

    if sys.version_info < (3, 7):
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
        for entry in manifest.get("files", []):
            if not Path(entry["path"]).exists():
                missing += 1
        if missing:
            errors.append(_t("verify_files_missing", count=missing))

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

    pyver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(_t("verify_header", ver=SUITE_VERSION))
    print(_t("verify_python", ver=pyver))
    print(_t("verify_target", path=claude_home))
    print(_t("verify_ts", status=_t("verify_ts_yes") if ts_available else _t("verify_ts_no")))
    print(_t("verify_j2", status=_t("verify_ts_yes") if j2_available else _t("verify_ts_no")))

    api_configured = False
    if settings_path.exists() and settings:
        api_key = settings.get("env", {}).get("OPENAI_API_KEY", "").strip()
        api_configured = api_key not in ("", API_KEY_PLACEHOLDER)
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
    args = parser.parse_args()

    if args.lang:
        _ui_lang = args.lang
    elif not args.uninstall and not args.verify:
        _ui_lang = prompt_language()

    if args.uninstall:
        do_uninstall()
    elif args.verify:
        do_verify()
    else:
        do_install()


if __name__ == "__main__":
    try:
        main()
    except PermissionError as e:
        print(_t("err_permission", err=e))
        sys.exit(1)
