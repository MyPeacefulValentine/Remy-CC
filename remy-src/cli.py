#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


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


REPO_URL = "https://github.com/patchescamerababy/Remy-CC.git"
BRANCH = "main"
VERSION_RAW_URL = "https://raw.githubusercontent.com/patchescamerababy/Remy-CC/{}/install.py".format(BRANCH)


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


def main():
    parser = argparse.ArgumentParser(prog="remy-cc", description="Remy - CLI for Claude Code configuration")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("ui", help="Open global configuration UI in browser")
    p_project = sub.add_parser("project", help="Open project-level configuration UI")
    p_project.add_argument("path", help="Project root directory (absolute path)")
    sub.add_parser("update", help="Fetch and install latest version from remote")
    sub.add_parser("verify", help="Verify installation integrity")
    sub.add_parser("version", help="Show installed version")
    args = parser.parse_args()

    commands = {"ui": cmd_ui, "project": cmd_project, "update": cmd_update, "verify": cmd_verify, "version": cmd_version}
    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
