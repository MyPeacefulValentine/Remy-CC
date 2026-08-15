#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@FileName    : pre_tool_guard.py
@Description : PreToolUse hook for path validation, snake_case correction, and Bash environment enforcement.
               Features:
               1. Absolute Path Check: Warns on absolute paths for modification tools.
               2. Snake Case Guard: Smartly detects and corrects kebab-case to snake_case.
               3. Bash Environment Guard: Auto-injects POSIX/Python/Mamba environment setup into Bash commands.
               4. Agent Speed Bump: Intercepts high-cost agent calls for user confirmation.
@Author      : MyPeacefulValentine
@CreationDate: 2026-01-10
"""

import sys
import json
import re
import os

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

# Tools that are allowed to use absolute paths (Read-only tools)
READ_ONLY_TOOLS = {"Read", "Glob", "Grep"}

# Compiled regex for performance
PYTHON_RELATED_PATTERN = re.compile(
    r'\b(python3?|pip3?|pytest|uv|poetry|pdm|conda|mamba|ipython|jupyter|twine|tox)\b|\.py\b',
    re.IGNORECASE
)

# -------------------------------------------------------------------------
# Bilingual Message Lookup
# -------------------------------------------------------------------------
MESSAGES = {
    "env_auto_fix": {
        "zh-CN": "🛡️ 环境自动修正：已注入 PYTHONIOENCODING 及 Mamba 激活脚本。\n(原: {cmd}...)",
        "en": "🛡️ Env auto-fix: Injected PYTHONIOENCODING and Mamba activation.\n(Original: {cmd}...)",
    },
    "plan_agent_lang": {
        "zh-CN": "<system_reminder>IMPORTANT: You MUST generate the plan content in Simplified Chinese (简体中文). Do NOT use English for the plan description.</system_reminder>",
        "en": "<system_reminder>IMPORTANT: You MUST generate the plan content in English. Do NOT use other languages for the plan description.</system_reminder>",
    },
    "agent_intercept": {
        "zh-CN": "🛑 Agent 拦截警告：\n即将启动 '{subagent}' 代理。\n耗时操作需确认。\n[y] 允许 Agent 执行\n[n] 拒绝 (转为手动扁平化执行)",
        "en": "🛑 Agent intercept warning:\nAbout to launch '{subagent}' agent.\nHigh-latency operation requires confirmation.\n[y] Allow agent execution\n[n] Reject (switch to manual flat execution)",
    },
    "evidence_id_missing": {
        "zh-CN": "证据 ID '{ref_id}' 在 evidence 列表中不存在（变更 '{change_id}'）",
        "en": "Evidence ID '{ref_id}' not found in evidence list (change '{change_id}')",
    },
    "evidence_status_blocked": {
        "zh-CN": "证据 '{ref_id}' 的状态为 '{status}'，不允许用于写操作。请先读取并将其更新为 confirmed。",
        "en": "Evidence '{ref_id}' has status '{status}', not allowed for write operations. Read and update to confirmed first.",
    },
    "packet_malformed": {
        "zh-CN": "任务包结构不合规（{detail}）。请修正 .claude/temp_task/ 下的 packet 文件，或删除 .claude/temp_task/.active_packet 解除门禁。",
        "en": "Task packet is structurally invalid ({detail}). Fix the packet file under .claude/temp_task/, or delete .claude/temp_task/.active_packet to lift the gate.",
    },
    "path_optimized": {
        "zh-CN": "🛡️ 路径优化：将绝对路径转换为相对路径 '{rel_path}' (项目内文件安全)",
        "en": "🛡️ Path optimization: Converted absolute path to relative '{rel_path}' (safe project-internal file)",
    },
    "path_outside_project": {
        "zh-CN": "⚠️ 检测到项目外绝对路径: '{file_path}'。\n工作目录: {cwd}\n为了安全，修改外部文件需确认。",
        "en": "⚠️ Absolute path outside project detected: '{file_path}'.\nWorking dir: {cwd}\nModifying external files requires confirmation.",
    },
    "naming_ambiguity": {
        "zh-CN": "⚠️ 命名歧义警告：同时存在 '{original}' 和 '{snake}'。\n建议使用 snake_case。",
        "en": "⚠️ Naming ambiguity: Both '{original}' and '{snake}' exist.\nPrefer snake_case.",
    },
    "naming_auto_fix": {
        "zh-CN": "自动纠错：将 '{original}' 修正为存在的 '{snake}'",
        "en": "Auto-correction: Corrected '{original}' to existing '{snake}'",
    },
    "naming_enforce": {
        "zh-CN": "命名规范强制：试图创建非 snake_case 文件 '{original}'。\n请使用 '{snake}' 重试。",
        "en": "Naming convention enforced: Attempted to create non-snake_case file '{original}'.\nRetry with '{snake}'.",
    },
    "cwd_drift": {
        "zh-CN": "⚠️ CWD 漂移：当前工作目录 {cwd} 已偏离会话根 {anchor}。请尽快 cd 回会话根，避免 .claude/、CLAUDE.md 等工件在错误目录生成。",
        "en": "⚠️ CWD drift: working directory {cwd} has left the session root {anchor}. cd back to the session root soon so .claude/ and CLAUDE.md artifacts are not created in the wrong directory.",
    },
}


def _msg(key, **kwargs):
    lang = str(remy_config.load_config(strict=False).get("REMY_LANG", "en"))
    template = MESSAGES.get(key, {}).get(lang) or MESSAGES.get(key, {}).get("en", key)
    return template.format(**kwargs) if kwargs else template

# -------------------------------------------------------------------------
# Bash Environment Injection Templates
# -------------------------------------------------------------------------
BASH_PREAMBLE = """
if [ -f ".env_setup.sh" ]; then
    source ".env_setup.sh";
else
    if command -v mamba >/dev/null 2>&1; then
        eval "$(mamba shell hook --shell=bash 2>/dev/null)";
    elif command -v conda >/dev/null 2>&1; then
        eval "$(conda shell.bash hook 2>/dev/null)";
    fi;
fi;
"""

def is_python_related(command):
    """Check if the command involves Python execution."""
    return bool(PYTHON_RELATED_PATTERN.search(command))

def is_absolute_path(path):
    """Check if path is absolute in a cross-platform way."""
    # On Windows, a path like \Users\foo is absolute on current drive but not fully qualified.
    # os.path.abspath resolves this, but here we just want to know if user provided an abs path.
    return os.path.isabs(path)

def path_is_contained(inner_path, root_path):
    """
    Check if inner_path is inside root_path using rigorous path resolution.
    Resolves symlinks and normalizes case.
    """
    try:
        abs_inner = os.path.realpath(inner_path)
        abs_root = os.path.realpath(root_path)
        return os.path.commonpath([abs_root, abs_inner]) == abs_root
    except ValueError:
        # Can happen on Windows if paths are on different drives
        return False

def to_snake_case(path):
    """Convert path basename from kebab-case to snake_case."""
    directory, filename = os.path.split(path)
    new_filename = filename.replace('-', '_')
    return os.path.join(directory, new_filename)

def has_kebab_case(path):
    """Check if the filename part contains hyphens."""
    _, filename = os.path.split(path)
    return '-' in filename

def inject_bash_env(original_command):
    """
    Injects environment variables and activation scripts into the bash command.
    Skips commands that already carry the injected preamble marker
    (".env_setup.sh"), so re-feeding an injected command never accumulates a
    second preamble; the encoding export is likewise added at most once.
    """
    if ".env_setup.sh" in original_command:
        return None
    if "PYTHONIOENCODING" in original_command and "miniforge3" in original_command:
        return None

    clean_preamble = BASH_PREAMBLE.strip().replace('\n', ' ')
    env_vars = ""

    if is_python_related(original_command) and "PYTHONIOENCODING" not in original_command:
        env_vars = 'export PYTHONIOENCODING="utf-8"; '

    return f"{env_vars}{clean_preamble} {original_command}"

def validate_packet(cwd):
    """
    Validate the active task packet before allowing Edit/Write operations.
    Returns (is_valid, error_message). Returns (True, None) if no active packet exists.
    Fails open on I/O errors only; a packet that does not parse or violates the
    expected structure fails closed, so a corrupt packet cannot silently disable
    the evidence gate. Remediation stays possible because main() exempts writes
    under .claude/temp_task/ from this gate.
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
        return False, _msg("packet_malformed", detail="invalid JSON")

    if not isinstance(packet, dict):
        return False, _msg("packet_malformed", detail="packet root is not an object")

    evidence_packet = packet.get("evidence_packet", {})
    if not isinstance(evidence_packet, dict):
        return False, _msg("packet_malformed", detail="evidence_packet is not an object")

    evidence_list = evidence_packet.get("evidence", [])
    proposed_changes = evidence_packet.get("proposed_changes", [])
    if not isinstance(evidence_list, list) or not isinstance(proposed_changes, list):
        return False, _msg("packet_malformed", detail="evidence/proposed_changes is not a list")

    if not proposed_changes:
        return True, None

    evidence_by_id = {}
    for item in evidence_list:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return False, _msg("packet_malformed", detail="evidence entry without a string 'id'")
        evidence_by_id[item["id"]] = item

    for change in proposed_changes:
        if not isinstance(change, dict):
            return False, _msg("packet_malformed", detail="proposed_changes entry is not an object")
        refs = change.get("evidence_refs", [])
        if not isinstance(refs, list):
            return False, _msg("packet_malformed", detail="evidence_refs is not a list")
        for ref_id in refs:
            if not isinstance(ref_id, str):
                return False, _msg("packet_malformed", detail="evidence_refs entry is not a string")
            ev = evidence_by_id.get(ref_id)
            if ev is None:
                return False, _msg("evidence_id_missing", ref_id=ref_id, change_id=change.get('id', '?'))
            if ev.get("status") in ("suspected", "stale"):
                return False, _msg("evidence_status_blocked", ref_id=ref_id, status=ev.get('status'))

    return True, None

def main():
    # Force UTF-8 for stdin/stdout to handle Chinese paths correctly on Windows
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')

    try:
        # Debug: Check if input is empty
        if sys.stdin.isatty():
             # No input piped
             sys.exit(0)

        input_data = json.load(sys.stdin)
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        cwd = input_data.get("cwd", os.getcwd())

        drift_note = ""
        if session_anchor is not None:
            drift_root = session_anchor.drift(input_data.get("session_id", ""), cwd)
            if drift_root:
                drift_note = _msg("cwd_drift", cwd=cwd, anchor=drift_root)

        # ---------------------------------------------------------
        # Logic 0: Bash Environment Auto-Correction
        # ---------------------------------------------------------
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            new_command = inject_bash_env(command)

            bash_context = "<system_reminder>Bash Constraint: Use POSIX syntax. Ensure all paths are relative.</system_reminder>"
            if drift_note:
                bash_context += "<system_reminder>" + drift_note + "</system_reminder>"
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "additionalContext": bash_context
                }
            }

            if new_command:
                new_input = tool_input.copy()
                new_input["command"] = new_command
                response["hookSpecificOutput"]["updatedInput"] = new_input
                response["hookSpecificOutput"]["permissionDecisionReason"] = _msg("env_auto_fix", cmd=command[:20])

            print(json.dumps(response))
            sys.exit(0)

        if tool_name == "PowerShell":
            command = tool_input.get("command", "")
            if is_python_related(command) and "PYTHONIOENCODING" not in command:
                new_input = tool_input.copy()
                new_input["command"] = "$env:PYTHONIOENCODING='utf-8'; " + command
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "updatedInput": new_input,
                        "permissionDecisionReason": _msg("env_auto_fix", cmd=command[:20])
                    }
                }
            else:
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                    }
                }
            if drift_note:
                response["hookSpecificOutput"]["additionalContext"] = "<system_reminder>" + drift_note + "</system_reminder>"
            print(json.dumps(response))
            sys.exit(0)

        # ---------------------------------------------------------
        # Logic 0.5: Agent "Speed Bump" & Plan Config
        # ---------------------------------------------------------
        if tool_name == "Agent":
            subagent = tool_input.get("subagent_type", "")

            # Enforce configured language for Plan agent
            if subagent == "Plan":
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "additionalContext": _msg("plan_agent_lang")
                    }
                }))
                sys.exit(0)

            # Intercept high-cost modifying agents; read-only Explore passes freely
            if subagent in ["general-purpose"]:
                 print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": _msg("agent_intercept", subagent=subagent)
                    }
                }))
                 sys.exit(0)

        # ---------------------------------------------------------
        # Logic 0.6: Edit/Write Safety Context (Accumulator)
        # ---------------------------------------------------------
        additional_context_buffer = []
        if drift_note:
            additional_context_buffer.append(drift_note)

        if tool_name in ["Edit", "Write", "NotebookEdit"]:
            target_path = tool_input.get("file_path") or tool_input.get("notebook_path", "")
            packet_dir = os.path.join(cwd, ".claude", "temp_task")
            target_abs = target_path if is_absolute_path(target_path) else os.path.join(cwd, target_path)
            packet_exempt = bool(target_path) and path_is_contained(target_abs, packet_dir)
            if not packet_exempt:
                is_valid, error_msg = validate_packet(cwd)
                if not is_valid:
                    print(json.dumps({
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": f"🛑 Evidence 验证失败：{error_msg}"
                        }
                    }))
                    sys.exit(0)

            strict_rules = (
                "CRITICAL CODE HYGIENE:\n"
                "1. NO thought process/plans in comments (e.g., 'Step 1...', 'I will fix...').\n"
                "2. NO irrelevant changes to whitespace/indentation/variables.\n"
                "3. NO partial/toy implementations or 'TODO' placeholders for requested features."
            )
            additional_context_buffer.append(strict_rules)

            path = tool_input.get("file_path") or tool_input.get("notebook_path", "")
            if path:
                # Check for lock files
                if "lock" in path:
                    additional_context_buffer.append("Warning: You are editing a lock file (e.g., package-lock.json). Manual edits may be overwritten.")

                # Check for strict snake_case requirement (Soft reminder for Edit, Hard for Write is below)
                if has_kebab_case(path) and tool_name == "Edit":
                    additional_context_buffer.append("Warning: File uses kebab-case. Prefer snake_case for new files.")

        # ---------------------------------------------------------
        # Path Logic
        # ---------------------------------------------------------
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
        if not file_path:
            # If no path, we can't do path logic.
            # But if we have accumulated context, we should output it?
            # Unlikely to have context without path (as context logic depends on path).
            sys.exit(0)

        # Helper to attach context
        def attach_context(response_dict):
            if additional_context_buffer:
                ctx_str = "<system_reminder>" + " ".join(additional_context_buffer) + "</system_reminder>"
                response_dict["hookSpecificOutput"]["additionalContext"] = ctx_str
            return response_dict

        # ---------------------------------------------------------
        # Logic 1: Absolute Path Check & Correction
        # ---------------------------------------------------------
        if is_absolute_path(file_path):
            if path_is_contained(file_path, cwd):
                # Case 1: Absolute path INSIDE project -> Auto-convert to relative
                rel_path = os.path.relpath(file_path, cwd)
                new_input = tool_input.copy()
                if "file_path" in new_input: new_input["file_path"] = rel_path
                if "path" in new_input: new_input["path"] = rel_path

                response = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": _msg("path_optimized", rel_path=rel_path),
                        "updatedInput": new_input
                    }
                }
                print(json.dumps(attach_context(response)))
                sys.exit(0)
            else:
                # Case 2: Absolute path OUTSIDE project -> Block/Ask
                if tool_name not in READ_ONLY_TOOLS:
                    response = {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "ask",
                            "permissionDecisionReason": _msg("path_outside_project", file_path=file_path, cwd=cwd)
                        }
                    }
                    print(json.dumps(attach_context(response)))
                    sys.exit(0)

        # ---------------------------------------------------------
        # Logic 2: Smart Snake Case Correction
        # ---------------------------------------------------------
        if has_kebab_case(file_path):
            original_path = file_path
            snake_path = to_snake_case(file_path)

            # Check existence using absolute paths relative to CWD
            abs_original = os.path.abspath(os.path.join(cwd, original_path))
            abs_snake = os.path.abspath(os.path.join(cwd, snake_path))

            exists_original = os.path.exists(abs_original)
            exists_snake = os.path.exists(abs_snake)

            if exists_original and exists_snake:
                response = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "ask",
                        "permissionDecisionReason": _msg("naming_ambiguity", original=original_path, snake=snake_path)
                    }
                }
                print(json.dumps(attach_context(response)))
                sys.exit(0)

            elif not exists_original and exists_snake:
                new_input = tool_input.copy()
                if "file_path" in new_input: new_input["file_path"] = snake_path
                if "path" in new_input: new_input["path"] = snake_path

                response = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "permissionDecisionReason": _msg("naming_auto_fix", original=original_path, snake=snake_path),
                        "updatedInput": new_input
                    }
                }
                print(json.dumps(attach_context(response)))
                sys.exit(0)

            elif not exists_snake and tool_name == "Write":
                 response = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": _msg("naming_enforce", original=original_path, snake=snake_path)
                    }
                }
                 print(json.dumps(attach_context(response)))
                 sys.exit(0)

        # ---------------------------------------------------------
        # Final Fallthrough: If we have context but no blocks triggered
        # ---------------------------------------------------------
        if additional_context_buffer:
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    # No reason needed for simple allow
                }
            }
            print(json.dumps(attach_context(response)))
            sys.exit(0)

        sys.exit(0)

    except Exception as e:
        # Fallback: Just let it pass if hook fails, but log to stderr
        print(f"Error in pre_tool_guard.py: {e}", file=sys.stderr)
        sys.exit(0)

if __name__ == "__main__":
    main()
