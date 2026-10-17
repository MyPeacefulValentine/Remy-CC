#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PreToolUse context and selected packet, path, and agent decisions.

Inputs are preserved. Auxiliary context makes no permission decision. Internal
errors produce a diagnostic and no decision; packet checks are not authorization
or a target-file allowlist.
"""

import json
import os
import sys

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "remy-src"))
if not os.path.isdir(_REMY_SRC):
    _REMY_SRC = os.path.join(os.path.expanduser("~"), ".claude", "remy-src")
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOKS_DIR not in sys.path:
    sys.path.insert(0, _HOOKS_DIR)
try:
    import session_anchor
except Exception:
    session_anchor = None

READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}
MESSAGES = {
    "agent_intercept": {
        "zh-CN": "启动 '{subagent}' 代理需要确认。拒绝此委派不授权其他执行方式。",
        "en": "Starting '{subagent}' requires confirmation. Refusing delegation does not authorize another execution route.",
    },
    "plan_agent_lang": {
        "zh-CN": "计划内容使用简体中文。",
        "en": "Write the plan in English.",
    },
    "subagent_evidence": {
        "zh-CN": "使用子代理结论决定修改或设计前核对引用来源；不存在某项的判断须说明搜索范围。",
        "en": "Check cited sources before using agent conclusions for changes or designs; absence claims must state the search scope.",
    },
    "evidence_id_missing": {
        "zh-CN": "证据 ID '{ref_id}' 不存在（变更 '{change_id}'）。",
        "en": "Evidence ID '{ref_id}' is missing (change '{change_id}').",
    },
    "evidence_status_blocked": {
        "zh-CN": "证据 '{ref_id}' 状态为 '{status}'，不能支持此写操作。核对来源后再判断状态，不为通过检查而修改标签。",
        "en": "Evidence '{ref_id}' is '{status}' and cannot support this write. Check its source before reassessing; do not relabel evidence to pass the check.",
    },
    "packet_malformed": {
        "zh-CN": "任务包结构不合规（{detail}）。核对并修正任务包，不删除活动标记绕过检查。",
        "en": "Task packet is invalid ({detail}). Inspect and repair it; do not remove the active marker to bypass the check.",
    },
    "path_outside_project": {
        "zh-CN": "目标 '{file_path}' 不在事件工作目录 '{cwd}' 内，修改需要确认。",
        "en": "Target '{file_path}' is outside event working directory '{cwd}'; modification requires confirmation.",
    },
    "cwd_drift": {
        "zh-CN": "工作目录 {cwd} 偏离会话根 {anchor}。核对工件目标位置，不自动切换目录。",
        "en": "Working directory {cwd} differs from session root {anchor}. Check artifact destinations; do not change directories automatically.",
    },
    "code_hygiene": {
        "zh-CN": "修改遵循现有约定；注释解释约束而非编辑过程，不加入无关改动或占位实现。",
        "en": "Follow existing conventions; comments explain constraints, not editing steps. Avoid unrelated changes or placeholder implementations.",
    },
}


def _msg(key, project_root=None, **kwargs):
    root = remy_config.discover_project_root(project_root) if project_root is not None else None
    lang = str(remy_config.load_config(root or project_root, strict=False).get("REMY_LANG", "en"))
    choices = MESSAGES.get(key, {})
    template = choices.get(lang) or choices.get("en") or key
    return template.format(**kwargs) if kwargs else template


def is_absolute_path(path):
    return os.path.isabs(path)


def path_is_contained(inner_path, root_path):
    """Resolve links and compare path components, including Windows casing."""
    try:
        inner = os.path.normcase(os.path.realpath(inner_path))
        root = os.path.normcase(os.path.realpath(root_path))
        return os.path.commonpath([root, inner]) == root
    except ValueError:
        return False


def validate_packet(cwd):
    """Check active packet references; absent packets and I/O errors pass.

    Invalid JSON/structure and suspected/stale references deny. These existing
    checks neither require confirmed status nor match the requested target.
    """
    active_marker = os.path.join(cwd, ".claude", "temp_task", ".active_packet")
    if not os.path.exists(active_marker):
        return True, None
    try:
        with open(active_marker, "r", encoding="utf-8") as f:
            packet_filename = f.read().strip()
        packet_path = os.path.join(cwd, ".claude", "temp_task", packet_filename)
        if not os.path.exists(packet_path):
            return True, None
        with open(packet_path, "r", encoding="utf-8") as f:
            raw_packet = f.read()
    except (IOError, OSError):
        return True, None
    try:
        packet = json.loads(raw_packet)
    except json.JSONDecodeError:
        return False, _msg("packet_malformed", cwd, detail="invalid JSON")
    if not isinstance(packet, dict):
        return False, _msg("packet_malformed", cwd, detail="packet root is not an object")
    evidence_packet = packet.get("evidence_packet", {})
    if not isinstance(evidence_packet, dict):
        return False, _msg("packet_malformed", cwd, detail="evidence_packet is not an object")
    evidence_list = evidence_packet.get("evidence", [])
    proposed_changes = evidence_packet.get("proposed_changes", [])
    if not isinstance(evidence_list, list) or not isinstance(proposed_changes, list):
        return False, _msg("packet_malformed", cwd, detail="evidence/proposed_changes is not a list")
    if not proposed_changes:
        return True, None
    evidence_by_id = {}
    for item in evidence_list:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return False, _msg("packet_malformed", cwd, detail="evidence entry without a string 'id'")
        evidence_by_id[item["id"]] = item
    for change in proposed_changes:
        if not isinstance(change, dict):
            return False, _msg("packet_malformed", cwd, detail="proposed_changes entry is not an object")
        refs = change.get("evidence_refs", [])
        if not isinstance(refs, list):
            return False, _msg("packet_malformed", cwd, detail="evidence_refs is not a list")
        for ref_id in refs:
            if not isinstance(ref_id, str):
                return False, _msg("packet_malformed", cwd, detail="evidence_refs entry is not a string")
            ev = evidence_by_id.get(ref_id)
            if ev is None:
                return False, _msg("evidence_id_missing", cwd, ref_id=ref_id, change_id=change.get("id", "?"))
            if ev.get("status") in ("suspected", "stale"):
                return False, _msg("evidence_status_blocked", cwd, ref_id=ref_id, status=ev.get("status"))
    return True, None


def main():
    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    try:
        if sys.stdin.isatty():
            return
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("hook input must be an object")
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})
        cwd = data.get("cwd", os.getcwd())
        if not isinstance(tool_input, dict) or not isinstance(cwd, str) or not cwd:
            raise ValueError("tool_input must be an object and cwd a nonempty string")
        context = []
        output: dict[str, object] = {"hookEventName": "PreToolUse"}
        if session_anchor is not None:
            anchor = session_anchor.drift(data.get("session_id", ""), cwd)
            if anchor:
                context.append(_msg("cwd_drift", cwd, cwd=cwd, anchor=anchor))
        if tool_name == "Agent":
            subagent = tool_input.get("subagent_type", "")
            context.append(_msg("subagent_evidence", cwd))
            if subagent == "Plan":
                context.append(_msg("plan_agent_lang", cwd))
            elif subagent == "general-purpose":
                output["permissionDecision"] = "ask"
                output["permissionDecisionReason"] = _msg("agent_intercept", cwd, subagent=subagent)
        elif tool_name not in ("Bash", "PowerShell"):
            target = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
            target_abs = None
            if target:
                if not isinstance(target, str):
                    raise ValueError("target path must be a string")
                target_abs = target if is_absolute_path(target) else os.path.join(cwd, target)
            if tool_name in ("Edit", "Write", "NotebookEdit"):
                packet_dir = os.path.join(cwd, ".claude", "temp_task")
                if not target_abs or not path_is_contained(target_abs, packet_dir):
                    valid, error = validate_packet(cwd)
                    if not valid:
                        output["permissionDecision"] = "deny"
                        output["permissionDecisionReason"] = error
                context.append(_msg("code_hygiene", cwd))
            if target_abs and tool_name not in READ_ONLY_TOOLS and not path_is_contained(target_abs, cwd):
                if output.get("permissionDecision") != "deny":
                    output["permissionDecision"] = "ask"
                    output["permissionDecisionReason"] = _msg("path_outside_project", cwd, cwd=cwd, file_path=target)
        if context:
            output["additionalContext"] = "\n".join(context)
        if len(output) > 1:
            print(json.dumps({"hookSpecificOutput": output}, ensure_ascii=False))
    except Exception as error:
        print(f"pre_tool_guard: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
