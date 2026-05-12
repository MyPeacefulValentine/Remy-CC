#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MANIFEST_FILE = ".installer_manifest.json"
BACKUP_SUFFIX = ".bak"
DEPLOY_DIRS = ["hooks", "skills", "output-styles", "remy-src", "remy-assets"]


def get_claude_home():
    return Path.home() / ".claude"


def get_version():
    manifest = get_claude_home() / ".installer_manifest.json"
    if manifest.exists():
        try:
            with open(manifest, "r", encoding="utf-8") as f:
                return json.load(f).get("version", "unknown")
        except (json.JSONDecodeError, OSError):
            pass
    return "unknown"


def _load_config_ui():
    script = Path(__file__).resolve().parent / "config_ui.py"
    if not script.exists():
        print("Error: config_ui.py not found at " + str(script), file=sys.stderr)
        sys.exit(1)
    import importlib.util
    spec = importlib.util.spec_from_file_location("config_ui", str(script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_ui(_args):
    _load_config_ui().main()


def cmd_project(args):
    project_dir = Path(args.path).resolve()
    if not project_dir.is_dir():
        print("Error: directory not found: " + str(project_dir), file=sys.stderr)
        sys.exit(1)
    _load_config_ui().main(mode="project", target_path=str(project_dir))


def cmd_verify(_args):
    claude_home = get_claude_home()
    errors = []

    if sys.version_info < (3, 7):
        errors.append("Python version too old: {} (requires >= 3.7)".format(sys.version))

    settings_path = claude_home / "settings.json"
    if not settings_path.exists():
        errors.append("settings.json not found")
    else:
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
            hooks = settings.get("hooks", {})
            for _event, entries in hooks.items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    for hook in entry.get("hooks", []):
                        cmd = hook.get("command", "")
                        parts = cmd.split('"')
                        if len(parts) >= 2:
                            hook_path = Path(parts[1])
                            if not hook_path.exists():
                                errors.append("Hook file not found: " + str(hook_path))
        except json.JSONDecodeError as e:
            errors.append("settings.json format error: " + str(e))

    manifest_path = claude_home / ".installer_manifest.json"
    if not manifest_path.exists():
        errors.append("Install manifest not found")
    else:
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            missing = sum(1 for e in manifest.get("files", []) if not Path(e["path"]).exists())
            if missing:
                errors.append("{} files missing from manifest".format(missing))
        except (json.JSONDecodeError, OSError) as e:
            errors.append("Manifest read error: " + str(e))

    ver = get_version()
    pyver = "{}.{}.{}".format(*sys.version_info[:3])
    print("Remy v{}".format(ver))
    print("  Python: {}".format(pyver))
    print("  Target: {}".format(claude_home))
    print()

    if errors:
        print("Found {} issues:".format(len(errors)))
        for err in errors:
            print("  [X] " + err)
        sys.exit(1)
    else:
        print("Verification passed.")


def cmd_version(_args):
    print("Remy v{}".format(get_version()))


REPO_URL = "https://github.com/Till-Crazy-Tears-Us-Apart/Remy-CC.git"
BRANCH = "main"
VERSION_RAW_URL = "https://raw.githubusercontent.com/Till-Crazy-Tears-Us-Apart/Remy-CC/{}/install.py".format(BRANCH)


def _fetch_remote_version():
    import re
    import urllib.request
    try:
        req = urllib.request.Request(VERSION_RAW_URL, headers={"User-Agent": "remy-cc"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            for line in resp:
                decoded = line.decode("utf-8", errors="ignore")
                m = re.match(r'^SUITE_VERSION\s*=\s*["\'](.+?)["\']', decoded)
                if m:
                    return m.group(1)
    except (OSError, urllib.request.URLError):
        return None
    return None


def cmd_update(_args):
    if not shutil.which("git"):
        print("Error: git is required for update.", file=sys.stderr)
        sys.exit(1)

    local_ver = get_version()
    remote_ver = _fetch_remote_version()

    if remote_ver and local_ver == remote_ver:
        print("Already up to date (v{}).".format(local_ver))
        return

    if remote_ver:
        print("[*] Update available: v{} -> v{}".format(local_ver, remote_ver))
    else:
        print("[*] Could not determine remote version. Proceeding with update...")

    tmp_dir = tempfile.mkdtemp(prefix="remy-cc-update-")
    clone_dir = os.path.join(tmp_dir, "remy-cc")
    try:
        print("[*] Fetching latest version...")
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", BRANCH, REPO_URL, clone_dir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("Error: git clone failed.\n" + result.stderr.strip(), file=sys.stderr)
            sys.exit(1)

        print("[*] Running installer...")
        installer = os.path.join(clone_dir, "install.py")
        rc = subprocess.run([sys.executable, installer]).returncode
        if rc != 0:
            sys.exit(rc)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Uninstall ─────────────────────────────────────────────────

_UNINSTALL_MSG = {
    "en": {
        "no_manifest": "No install manifest found. Cannot uninstall.",
        "confirm": "This will remove all Remy-CC files and settings. Continue? [y/N] ",
        "aborted": "Uninstall cancelled.",
        "skip_modified": "  [~] Skipped (modified): {name}",
        "hooks_removed": "  [+] Suite hooks/permissions removed from settings.json",
        "hooks_no_data": "  [i] Manifest has no hook/permission data. Run 'python install.py --uninstall' from source for full settings cleanup.",
        "claude_restored": "  [+] CLAUDE.md restored from backup",
        "shim_removed": "  [+] CLI shim removed",
        "shim_deferred": "  [i] Could not remove {path} (in use). Delete manually after exit.",
        "done": "\nUninstall complete. Removed {removed} files, skipped {skipped} modified files.",
    },
    "zh-CN": {
        "no_manifest": "未找到安装记录，无法执行卸载。",
        "confirm": "此操作将移除所有 Remy-CC 文件和配置。是否继续？[y/N] ",
        "aborted": "卸载已取消。",
        "skip_modified": "  [~] 跳过（已被修改）: {name}",
        "hooks_removed": "  [+] settings.json 中的套件配置已移除",
        "hooks_no_data": "  [i] 安装记录中无 hook/permission 数据。请从源码运行 'python install.py --uninstall' 以完整清理。",
        "claude_restored": "  [+] CLAUDE.md 已从备份恢复",
        "shim_removed": "  [+] CLI 入口已移除",
        "shim_deferred": "  [i] 无法删除 {path}（正在使用）。退出后请手动删除。",
        "done": "\n卸载完成。删除 {removed} 个文件，跳过 {skipped} 个已修改文件。",
    },
}


def _get_lang():
    settings_path = get_claude_home() / "settings.json"
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                return json.load(f).get("env", {}).get("REMY_LANG", "en")
        except (json.JSONDecodeError, OSError):
            pass
    return "en"


def _um(key, **kwargs):
    lang = _get_lang()
    msgs = _UNINSTALL_MSG.get(lang, _UNINSTALL_MSG["en"])
    template = msgs.get(key, _UNINSTALL_MSG["en"].get(key, key))
    return template.format(**kwargs) if kwargs else template


def compute_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def hooks_equal(h1, h2):
    return h1.get("command", "").strip() == h2.get("command", "").strip()


def cmd_uninstall(args):
    claude_home = get_claude_home()
    manifest_path = claude_home / MANIFEST_FILE

    if not manifest_path.exists():
        print(_um("no_manifest"))
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    if not getattr(args, "yes", False):
        try:
            answer = input(_um("confirm")).strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            print(_um("aborted"))
            return

    files = manifest.get("files", [])
    removed = 0
    skipped = 0
    for entry in files:
        fpath = Path(entry["path"])
        if not fpath.exists():
            continue
        try:
            current_hash = compute_sha256(fpath)
        except OSError:
            skipped += 1
            continue
        if current_hash != entry["sha256"]:
            print(_um("skip_modified", name=fpath.name))
            skipped += 1
            continue
        try:
            fpath.unlink()
            removed += 1
        except PermissionError:
            pass

    settings_path = claude_home / "settings.json"
    injected_hooks = manifest.get("injected_hooks", {})
    injected_perms = manifest.get("injected_permissions", [])

    if (injected_hooks or injected_perms) and settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)

            ext_hooks = settings.get("hooks", {})
            for event, tpl_entries in injected_hooks.items():
                if event not in ext_hooks:
                    continue
                if not isinstance(tpl_entries, list):
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
                    ext_hooks[event] = [e for e in ext_hooks[event] if e.get("hooks")]
                    if not ext_hooks[event]:
                        del ext_hooks[event]
            if not ext_hooks:
                settings.pop("hooks", None)

            ext_perms = settings.get("permissions", {}).get("allow", [])
            if ext_perms and injected_perms:
                settings["permissions"]["allow"] = [
                    p for p in ext_perms if p not in injected_perms
                ]
                if not settings["permissions"]["allow"]:
                    settings["permissions"].pop("allow", None)
                if not settings.get("permissions"):
                    settings.pop("permissions", None)

            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(_um("hooks_removed"))
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    elif not injected_hooks and not injected_perms and manifest.get("files"):
        print(_um("hooks_no_data"))

    claude_md = claude_home / "CLAUDE.md"
    claude_md_bak = claude_home / ("CLAUDE.md" + BACKUP_SUFFIX)
    if claude_md_bak.exists():
        shutil.copy2(claude_md_bak, claude_md)
        claude_md_bak.unlink()
        print(_um("claude_restored"))

    bin_dir = claude_home / "bin"
    if bin_dir.exists():
        try:
            shutil.rmtree(bin_dir)
            print(_um("shim_removed"))
        except OSError:
            print(_um("shim_deferred", path=bin_dir))

    try:
        manifest_path.unlink()
    except (PermissionError, FileNotFoundError):
        pass

    for dirname in DEPLOY_DIRS:
        dirpath = claude_home / dirname
        if not dirpath.exists():
            continue
        for root, _dirs, _files in os.walk(str(dirpath), topdown=False):
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass

    print(_um("done", removed=removed, skipped=skipped))


def main():
    parser = argparse.ArgumentParser(prog="remy-cc", description="Remy - CLI for Claude Code configuration")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("ui", help="Open global configuration UI in browser")
    p_project = sub.add_parser("project", help="Open project-level configuration UI")
    p_project.add_argument("path", help="Project root directory (absolute path)")
    sub.add_parser("update", help="Fetch and install latest version from remote")
    p_uninstall = sub.add_parser("uninstall", help="Remove all Remy-CC files and settings")
    p_uninstall.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    sub.add_parser("verify", help="Verify installation integrity")
    sub.add_parser("version", help="Show installed version")
    args = parser.parse_args()

    commands = {"ui": cmd_ui, "project": cmd_project, "update": cmd_update, "uninstall": cmd_uninstall, "verify": cmd_verify, "version": cmd_version}
    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
