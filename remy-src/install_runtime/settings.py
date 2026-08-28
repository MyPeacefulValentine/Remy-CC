"""Claude settings ownership and Hook command rendering."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from .models import InstallRuntimeError, MetadataError, RootPaths

ENRICH_PLACEHOLDER = "__REMY_ENRICH_COMMAND__"
DIRTY_PLACEHOLDER = "__REMY_DIRTY_COMMAND__"
# Script names identify retired python-arm hook registrations so upgrades
# still remove them from settings.json.
TARGET_HOOKS = {
    ("PreToolUse", "Read|Glob|Grep"): "logic_enrichment_hook.py",
    ("PostToolUse", "Edit|Write"): "logic_dirty_tracker.py",
}


def quote_command_arg(value: str) -> str:
    if '"' in value or "\r" in value or "\n" in value:
        raise InstallRuntimeError("managed command path contains unsupported characters")
    return '"{}"'.format(value)


def hook_commands(roots: RootPaths, hook_mode: str, python_executable: str) -> dict[str, str]:
    del python_executable
    if hook_mode == "rust":
        executable = roots.remy / "bin" / ("remy-daemon.exe" if _is_windows_path(roots.remy) else "remy-daemon")
        prefix = quote_command_arg(str(executable))
        return {"enrich": prefix + " hook enrich", "dirty": prefix + " hook dirty"}
    raise InstallRuntimeError("unsupported Hook mode")


def load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataError("settings.json is invalid") from exc
    if not isinstance(value, dict):
        raise MetadataError("settings.json must be an object")
    return value


def merge_settings_document(
    existing: Mapping[str, Any],
    template: Mapping[str, Any],
    roots: RootPaths,
    commands: Mapping[str, str],
    prior_claim: Optional[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = copy.deepcopy(dict(existing))
    rendered = _render_template(template, roots, commands)
    if prior_claim:
        verify_settings_claim(result, prior_claim)
    _remove_prior_target_hooks(result, roots, prior_claim)

    result_hooks = result.setdefault("hooks", {})
    if not isinstance(result_hooks, dict):
        raise MetadataError("settings hooks must be an object")
    claim_hooks: list[dict[str, str]] = []
    for event, entries in rendered.get("hooks", {}).items():
        if not isinstance(entries, list):
            raise MetadataError("settings template hooks must be arrays")
        destination = result_hooks.setdefault(event, [])
        if not isinstance(destination, list):
            raise MetadataError("settings event hooks must be arrays")
        for entry in entries:
            if not isinstance(entry, dict):
                raise MetadataError("settings hook entry must be an object")
            matcher = str(entry.get("matcher", ""))
            target_entry = next(
                (item for item in destination if isinstance(item, dict) and str(item.get("matcher", "")) == matcher),
                None,
            )
            if target_entry is None:
                target_entry = {"matcher": matcher, "hooks": []}
                destination.append(target_entry)
            hooks = target_entry.setdefault("hooks", [])
            if not isinstance(hooks, list):
                raise MetadataError("settings hooks list must be an array")
            for hook in entry.get("hooks", []):
                if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
                    raise MetadataError("settings hook command must be a string")
                command = hook["command"].strip()
                if not any(isinstance(current, dict) and current.get("command", "").strip() == command for current in hooks):
                    hooks.append(copy.deepcopy(hook))
                claim_hooks.append({"event": event, "matcher": matcher, "command": command})

    permissions = rendered.get("permissions", {}).get("allow", [])
    if not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions):
        raise MetadataError("settings template permissions must be strings")
    permissions_section = result.setdefault("permissions", {})
    if not isinstance(permissions_section, dict):
        raise MetadataError("settings permissions must be an object")
    allow = permissions_section.setdefault("allow", [])
    if not isinstance(allow, list):
        raise MetadataError("settings permissions.allow must be an array")
    prior_permissions = {
        item
        for item in (prior_claim or {}).get("permissions", [])
        if isinstance(item, str)
    }
    claimed_permissions: list[str] = []
    for permission in permissions:
        if permission not in allow:
            allow.append(permission)
            claimed_permissions.append(permission)
        elif permission in prior_permissions:
            claimed_permissions.append(permission)

    template_env = rendered.get("env", {})
    env = result.setdefault("env", {})
    if not isinstance(template_env, dict) or not isinstance(env, dict):
        raise MetadataError("settings env must be an object")
    for key, value in template_env.items():
        env.setdefault(key, value)
    if "outputStyle" not in result and "outputStyle" in rendered:
        result["outputStyle"] = rendered["outputStyle"]
    if "spinnerTipsEnabled" not in result and "spinnerTipsEnabled" in rendered:
        result["spinnerTipsEnabled"] = rendered["spinnerTipsEnabled"]
    result.pop("mcpServers", None)

    claim = {"hooks": claim_hooks, "permissions": claimed_permissions}
    return result, claim


def remove_settings_claim(existing: Mapping[str, Any], claim: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(existing))
    verify_settings_claim(result, claim)
    hooks = result.get("hooks", {})
    for item in claim.get("hooks", []):
        event = item["event"]
        matcher = item["matcher"]
        command = item["command"]
        entries = hooks.get(event, [])
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("matcher", "")) == matcher:
                entry["hooks"] = [
                    hook
                    for hook in entry.get("hooks", [])
                    if not (isinstance(hook, dict) and hook.get("command", "").strip() == command)
                ]
        hooks[event] = [entry for entry in entries if entry.get("hooks")]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        result.pop("hooks", None)

    allow = result.get("permissions", {}).get("allow", [])
    claimed_permissions = set(claim.get("permissions", []))
    if isinstance(allow, list):
        result["permissions"]["allow"] = [item for item in allow if item not in claimed_permissions]
        if not result["permissions"]["allow"]:
            result["permissions"].pop("allow", None)
        if not result["permissions"]:
            result.pop("permissions", None)
    return result


def _render_template(
    template: Mapping[str, Any], roots: RootPaths, commands: Mapping[str, str]
) -> dict[str, Any]:
    rendered = copy.deepcopy(dict(template))
    claude_prefix = str(roots.claude).replace("\\", "/") + "/"
    for entries in rendered.get("hooks", {}).values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                command = str(hook.get("command", ""))
                command = command.replace(ENRICH_PLACEHOLDER, commands["enrich"])
                command = command.replace(DIRTY_PLACEHOLDER, commands["dirty"])
                hook["command"] = command.replace("~/.claude/", claude_prefix)
    return rendered


def verify_settings_claim(settings: Mapping[str, Any], claim: Mapping[str, Any]) -> None:
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise MetadataError("settings hooks must be an object")
    for item in claim.get("hooks", []):
        if not isinstance(item, dict):
            raise MetadataError("invalid settings Hook claim")
        event = item.get("event")
        matcher = item.get("matcher")
        command = item.get("command")
        found = False
        for entry in hooks.get(event, []):
            if isinstance(entry, dict) and str(entry.get("matcher", "")) == matcher:
                found = any(
                    isinstance(hook, dict) and hook.get("command", "").strip() == command
                    for hook in entry.get("hooks", [])
                )
                if found:
                    break
        if not found:
            raise InstallRuntimeError("a managed settings Hook was modified")
    permissions_section = settings.get("permissions", {})
    if not isinstance(permissions_section, dict):
        raise MetadataError("settings permissions must be an object")
    allow = permissions_section.get("allow", [])
    if not isinstance(allow, list):
        raise MetadataError("settings permissions.allow must be an array")
    for permission in claim.get("permissions", []):
        if permission not in allow:
            raise InstallRuntimeError("a managed settings permission was modified")


def _remove_prior_target_hooks(
    settings: dict[str, Any], roots: RootPaths, prior_claim: Optional[Mapping[str, Any]]
) -> None:
    hooks = settings.get("hooks", {})
    claimed_commands = {
        str(item.get("command", "")).strip()
        for item in (prior_claim or {}).get("hooks", [])
        if isinstance(item, dict)
    }
    for (event, matcher), script_name in TARGET_HOOKS.items():
        entries = hooks.get(event, [])
        for entry in entries:
            if not isinstance(entry, dict) or str(entry.get("matcher", "")) != matcher:
                continue
            retained = []
            for hook in entry.get("hooks", []):
                if not isinstance(hook, dict):
                    retained.append(hook)
                    continue
                command = str(hook.get("command", "")).strip()
                if command in claimed_commands or _is_legacy_default(command, roots, script_name):
                    continue
                if script_name in command or ("remy-daemon" in command and " hook " in command):
                    raise InstallRuntimeError("an existing target Hook command was modified")
                retained.append(hook)
            entry["hooks"] = retained
        hooks[event] = [entry for entry in entries if entry.get("hooks")]
        if not hooks[event]:
            del hooks[event]


def _is_legacy_default(command: str, roots: RootPaths, script_name: str) -> bool:
    normalized = command.replace("\\", "/")
    absolute_script = str(roots.claude / "hooks" / script_name).replace("\\", "/")
    return normalized in {
        'python "~/.claude/hooks/{}"'.format(script_name),
        'python "{}"'.format(absolute_script),
    }


def _is_windows_path(path: Path) -> bool:
    return path.drive != "" or str(path).startswith("\\\\")
