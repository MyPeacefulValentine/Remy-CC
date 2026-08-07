"""Tests for pre_tool_guard.py: pure helpers, packet validation, and the main() decision matrix.

Four defects originally recorded here with ``xfail(strict=True)`` markers are
now resolved: three were fixed (injection idempotency, structural packet
rejection, the temp_task remediation exemption) and the Bash bypass was ruled
an intentional escape hatch. All four cases are regular regression tests now.
See docs/TESTING.md "PreToolUse guard" for the fix record.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "hooks" / "pre_tool_guard.py"
spec = importlib.util.spec_from_file_location("pre_tool_guard_tested", MODULE_PATH)
assert spec and spec.loader
guard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = guard
spec.loader.exec_module(guard)


@pytest.fixture(autouse=True)
def isolated_remy_user_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(guard.remy_config.Path, "home", classmethod(lambda _cls: home))
    with guard.remy_config._CACHE_LOCK:
        guard.remy_config._FILE_CACHE.clear()
    yield
    with guard.remy_config._CACHE_LOCK:
        guard.remy_config._FILE_CACHE.clear()


def _write_packet(cwd, evidence, changes, marker="p.json",
                  packet_name: Optional[str] = "p.json"):
    task_dir = Path(cwd) / ".claude" / "temp_task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / ".active_packet").write_text(marker, encoding="utf-8")
    if packet_name:
        (task_dir / packet_name).write_text(
            json.dumps({
                "evidence_packet": {"evidence": evidence, "proposed_changes": changes}
            }),
            encoding="utf-8",
        )
    return task_dir


def _run_hook(payload, home):
    env = dict(os.environ)
    env["REMY_LANG"] = "en"
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return subprocess.run(
        [sys.executable, str(MODULE_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _output(proc):
    """Return the hookSpecificOutput dict, or None when the hook stayed silent."""
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _decision(proc):
    payload = _output(proc)
    return None if payload is None else payload.get("permissionDecision")


class TestIsPythonRelated:
    @pytest.mark.parametrize("command", [
        "python -c pass",
        "PYTEST -q",
        "pip install x",
        "run a.py",
        "mamba activate env",
    ])
    def test_python_commands_match(self, command):
        assert guard.is_python_related(command) is True

    @pytest.mark.parametrize("command", [
        "pythonic script",
        "ls -la",
        "echo pipeline",
    ])
    def test_unrelated_commands_do_not_match(self, command):
        assert guard.is_python_related(command) is False


class TestIsAbsolutePath:
    def test_absolute_path_detected(self, tmp_path):
        assert guard.is_absolute_path(str(tmp_path)) is True

    def test_relative_path_not_detected(self):
        assert guard.is_absolute_path(os.path.join("sub", "f.py")) is False


class TestPathIsContained:
    def test_child_is_contained(self, tmp_path):
        child = tmp_path / "sub"
        child.mkdir()
        assert guard.path_is_contained(str(child), str(tmp_path)) is True

    def test_root_itself_is_contained(self, tmp_path):
        assert guard.path_is_contained(str(tmp_path), str(tmp_path)) is True

    def test_sibling_sharing_string_prefix_is_not_contained(self, tmp_path):
        root = tmp_path / "proj"
        sibling = tmp_path / "proj_other"
        root.mkdir()
        sibling.mkdir()
        assert guard.path_is_contained(str(sibling), str(root)) is False

    def test_nonexistent_child_is_contained(self, tmp_path):
        target = tmp_path / "missing" / "f.py"
        assert guard.path_is_contained(str(target), str(tmp_path)) is True


class TestSnakeCaseHelpers:
    def test_basename_hyphen_detected(self):
        assert guard.has_kebab_case("x-y.py") is True

    def test_directory_hyphen_is_ignored(self):
        assert guard.has_kebab_case(os.path.join("a-b", "c.py")) is False

    def test_to_snake_case_rewrites_basename_only(self):
        result = guard.to_snake_case(os.path.join("a-b", "c-d.py"))
        assert os.path.basename(result) == "c_d.py"
        assert os.path.dirname(result) == "a-b"

    def test_to_snake_case_rewrites_every_hyphen(self):
        assert guard.to_snake_case("a-b-c.py") == "a_b_c.py"

    def test_to_snake_case_leaves_snake_name_unchanged(self):
        assert guard.to_snake_case("already_snake.py") == "already_snake.py"


class TestInjectBashEnv:
    def test_returns_none_when_both_markers_present(self):
        command = 'export PYTHONIOENCODING=utf-8; /c/tools/miniforge3/python x.py'
        assert guard.inject_bash_env(command) is None

    def test_python_command_gets_encoding_export(self):
        result = guard.inject_bash_env("python x.py")
        assert result is not None
        assert 'export PYTHONIOENCODING="utf-8"' in result
        assert result.endswith("python x.py")

    def test_non_python_command_gets_preamble_without_export(self):
        result = guard.inject_bash_env("ls -la")
        assert result is not None
        assert "PYTHONIOENCODING" not in result
        assert "mamba shell hook" in result

    def test_does_not_reinject_when_encoding_already_set(self):
        result = guard.inject_bash_env('export PYTHONIOENCODING=utf-8; python x.py')
        assert result is not None
        assert result.count("PYTHONIOENCODING") == 1
        assert "mamba shell hook" in result

    def test_reinjecting_injected_python_output_is_skipped(self):
        first = guard.inject_bash_env("python x.py")
        assert first is not None
        assert guard.inject_bash_env(first) is None

    def test_reinjecting_injected_non_python_output_is_skipped(self):
        first = guard.inject_bash_env("ls -la")
        assert first is not None
        assert guard.inject_bash_env(first) is None


class TestValidatePacket:
    _CONFIRMED = [{"id": "E-1", "status": "confirmed"}]
    _CHANGE = [{"id": "C-1", "evidence_refs": ["E-1"]}]

    def test_no_active_marker_passes(self, tmp_path):
        assert guard.validate_packet(str(tmp_path))[0] is True

    def test_marker_pointing_at_missing_packet_passes(self, tmp_path):
        _write_packet(tmp_path, [], [], marker="gone.json", packet_name=None)
        assert guard.validate_packet(str(tmp_path))[0] is True

    def test_malformed_json_is_rejected(self, tmp_path):
        task_dir = tmp_path / ".claude" / "temp_task"
        task_dir.mkdir(parents=True)
        (task_dir / ".active_packet").write_text("p.json", encoding="utf-8")
        (task_dir / "p.json").write_text("{not json", encoding="utf-8")
        is_valid, message = guard.validate_packet(str(tmp_path))
        assert is_valid is False
        assert message

    def test_empty_proposed_changes_passes(self, tmp_path):
        _write_packet(tmp_path, self._CONFIRMED, [])
        assert guard.validate_packet(str(tmp_path))[0] is True

    def test_confirmed_evidence_passes(self, tmp_path):
        _write_packet(tmp_path, self._CONFIRMED, self._CHANGE)
        assert guard.validate_packet(str(tmp_path))[0] is True

    @pytest.mark.parametrize("status", ["suspected", "stale"])
    def test_unconfirmed_evidence_blocks(self, tmp_path, status):
        _write_packet(tmp_path, [{"id": "E-1", "status": status}], self._CHANGE)
        assert guard.validate_packet(str(tmp_path))[0] is False

    def test_dangling_evidence_ref_blocks(self, tmp_path):
        _write_packet(tmp_path, self._CONFIRMED,
                      [{"id": "C-1", "evidence_refs": ["E-9"]}])
        assert guard.validate_packet(str(tmp_path))[0] is False

    def test_change_without_evidence_refs_passes(self, tmp_path):
        _write_packet(tmp_path, self._CONFIRMED, [{"id": "C-1"}])
        assert guard.validate_packet(str(tmp_path))[0] is True

    def test_validation_is_not_scoped_to_a_file_path(self, tmp_path):
        """One unconfirmed reference blocks edits to every file, not just its own."""
        _write_packet(
            tmp_path,
            [{"id": "E-1", "status": "confirmed"}, {"id": "E-2", "status": "suspected"}],
            [{"id": "C-1", "evidence_refs": ["E-1"]},
             {"id": "C-2", "evidence_refs": ["E-2"]}],
        )
        assert guard.validate_packet(str(tmp_path))[0] is False
        assert "file_path" not in guard.validate_packet.__code__.co_varnames

    def test_evidence_entry_missing_id_is_rejected(self, tmp_path):
        _write_packet(tmp_path, [{"status": "confirmed"}], self._CHANGE)
        assert guard.validate_packet(str(tmp_path))[0] is False

    def test_non_dict_evidence_entry_is_rejected(self, tmp_path):
        _write_packet(tmp_path, ["not a dict"], self._CHANGE)
        assert guard.validate_packet(str(tmp_path))[0] is False

    def test_non_string_evidence_id_is_rejected(self, tmp_path):
        _write_packet(tmp_path, [{"id": ["E-1"], "status": "confirmed"}], self._CHANGE)
        assert guard.validate_packet(str(tmp_path))[0] is False

    def test_non_dict_change_entry_is_rejected(self, tmp_path):
        _write_packet(tmp_path, self._CONFIRMED, ["not a dict"])
        assert guard.validate_packet(str(tmp_path))[0] is False

    def test_non_list_evidence_refs_is_rejected(self, tmp_path):
        _write_packet(tmp_path, self._CONFIRMED,
                      [{"id": "C-1", "evidence_refs": "E-1"}])
        assert guard.validate_packet(str(tmp_path))[0] is False

    def test_non_list_evidence_container_is_rejected(self, tmp_path):
        _write_packet(tmp_path, {"E-1": {"status": "confirmed"}}, self._CHANGE)
        assert guard.validate_packet(str(tmp_path))[0] is False


class TestMainBashAndPowerShell:
    def test_bash_python_command_gets_updated_input(self, tmp_path):
        proc = _run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "python x.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert payload["permissionDecision"] == "allow"
        assert "PYTHONIOENCODING" in payload["updatedInput"]["command"]

    def test_bash_always_carries_posix_reminder(self, tmp_path):
        proc = _run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert "POSIX" in payload["additionalContext"]

    def test_powershell_python_command_gets_updated_input(self, tmp_path):
        proc = _run_hook(
            {"tool_name": "PowerShell", "tool_input": {"command": "python x.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert payload["permissionDecision"] == "allow"
        assert payload["updatedInput"]["command"].startswith("$env:PYTHONIOENCODING")

    def test_powershell_non_python_command_is_untouched(self, tmp_path):
        proc = _run_hook(
            {"tool_name": "PowerShell", "tool_input": {"command": "Get-ChildItem"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert payload["permissionDecision"] == "allow"
        assert "updatedInput" not in payload

    def test_bash_command_already_carrying_both_markers_is_untouched(self, tmp_path):
        command = 'export PYTHONIOENCODING=utf-8; /c/tools/miniforge3/python x.py'
        proc = _run_hook(
            {"tool_name": "Bash", "tool_input": {"command": command},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert payload["permissionDecision"] == "allow"
        assert "updatedInput" not in payload


class TestMainAgentGate:
    def test_plan_agent_gets_language_context(self, tmp_path):
        proc = _run_hook(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "Plan"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert payload["permissionDecision"] == "allow"
        assert "additionalContext" in payload

    @pytest.mark.parametrize("subagent", ["Explore", "general-purpose"])
    def test_high_cost_agents_require_confirmation(self, tmp_path, subagent):
        proc = _run_hook(
            {"tool_name": "Agent", "tool_input": {"subagent_type": subagent},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "ask"

    def test_other_agent_types_fall_through_silently(self, tmp_path):
        proc = _run_hook(
            {"tool_name": "Agent", "tool_input": {"subagent_type": "statusline-setup"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _output(proc) is None
        assert proc.returncode == 0


class TestMainPacketGate:
    _SUSPECTED = [{"id": "E-1", "status": "suspected"}]
    _CHANGE = [{"id": "C-1", "evidence_refs": ["E-1"]}]

    @pytest.mark.parametrize("tool_name,key", [
        ("Write", "file_path"),
        ("Edit", "file_path"),
        ("NotebookEdit", "notebook_path"),
    ])
    def test_write_tools_are_denied_on_unconfirmed_evidence(self, tmp_path, tool_name, key):
        _write_packet(tmp_path, self._SUSPECTED, self._CHANGE)
        proc = _run_hook(
            {"tool_name": tool_name, "tool_input": {key: "a.py"}, "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "deny"

    def test_write_is_allowed_on_confirmed_evidence(self, tmp_path):
        _write_packet(tmp_path, [{"id": "E-1", "status": "confirmed"}], self._CHANGE)
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": "a.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "allow"

    def test_bash_bypasses_packet_validation_by_design(self, tmp_path):
        """Bash is a documented escape hatch: a shell command cannot be
        statically classified as read or write, and skill protocols write into
        .claude/temp_task/ through Bash."""
        _write_packet(tmp_path, self._SUSPECTED, self._CHANGE)
        proc = _run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "echo x > a.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "allow"

    def test_writing_to_the_active_packet_is_permitted(self, tmp_path):
        _write_packet(tmp_path, self._SUSPECTED, self._CHANGE)
        packet_rel = os.path.join(".claude", "temp_task", "p.json")
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": packet_rel},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "allow"

    def test_absolute_path_into_temp_task_is_permitted(self, tmp_path):
        _write_packet(tmp_path, self._SUSPECTED, self._CHANGE)
        packet_abs = tmp_path / ".claude" / "temp_task" / "p.json"
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(packet_abs)},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "allow"

    def test_new_file_under_temp_task_is_permitted(self, tmp_path):
        _write_packet(tmp_path, self._SUSPECTED, self._CHANGE)
        new_rel = os.path.join(".claude", "temp_task", "next_packet.json")
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": new_rel},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "allow"

    def test_claude_dir_outside_temp_task_is_still_gated(self, tmp_path):
        _write_packet(tmp_path, self._SUSPECTED, self._CHANGE)
        other_rel = os.path.join(".claude", "settings.local.json")
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": other_rel},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "deny"

    def test_structurally_invalid_packet_denies_writes(self, tmp_path):
        task_dir = tmp_path / ".claude" / "temp_task"
        task_dir.mkdir(parents=True)
        (task_dir / ".active_packet").write_text("p.json", encoding="utf-8")
        (task_dir / "p.json").write_text("{not json", encoding="utf-8")
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": "a.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "deny"


class TestMainPathLogic:
    def test_absolute_path_inside_project_is_rewritten_to_relative(self, tmp_path):
        target = tmp_path / "sub" / "f.py"
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(target)},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert payload["permissionDecision"] == "allow"
        assert payload["updatedInput"]["file_path"] == os.path.join("sub", "f.py")

    def test_absolute_path_outside_project_requires_confirmation(self, tmp_path):
        outside = tmp_path.parent / "elsewhere" / "f.py"
        project = tmp_path / "project"
        project.mkdir()
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(outside)},
             "cwd": str(project)},
            tmp_path / "home",
        )
        assert _decision(proc) == "ask"

    def test_read_only_tool_outside_project_is_not_blocked(self, tmp_path):
        outside = tmp_path.parent / "elsewhere" / "f.py"
        project = tmp_path / "project"
        project.mkdir()
        proc = _run_hook(
            {"tool_name": "Read", "tool_input": {"file_path": str(outside)},
             "cwd": str(project)},
            tmp_path / "home",
        )
        assert _output(proc) is None

    @pytest.mark.parametrize("tool_name", ["Glob", "Grep"])
    def test_every_read_only_tool_is_exempt_from_the_outside_path_gate(
        self, tmp_path, tool_name
    ):
        outside = tmp_path.parent / "elsewhere" / "f.py"
        project = tmp_path / "project"
        project.mkdir()
        proc = _run_hook(
            {"tool_name": tool_name, "tool_input": {"path": str(outside)},
             "cwd": str(project)},
            tmp_path / "home",
        )
        assert _output(proc) is None

    def test_editing_a_lock_file_adds_a_warning(self, tmp_path):
        (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
        proc = _run_hook(
            {"tool_name": "Edit", "tool_input": {"file_path": "package-lock.json"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert "lock file" in payload["additionalContext"]


class TestMainSnakeCaseGate:
    def test_write_of_new_kebab_file_is_denied(self, tmp_path):
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": "new-file.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "deny"

    def test_write_is_redirected_when_only_snake_variant_exists(self, tmp_path):
        (tmp_path / "both_here.py").write_text("", encoding="utf-8")
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": "both-here.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert payload["permissionDecision"] == "allow"
        assert payload["updatedInput"]["file_path"] == "both_here.py"

    def test_ambiguous_naming_requires_confirmation(self, tmp_path):
        (tmp_path / "both_here.py").write_text("", encoding="utf-8")
        (tmp_path / "both-here.py").write_text("", encoding="utf-8")
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": "both-here.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "ask"

    def test_edit_of_kebab_file_is_allowed_with_a_reminder(self, tmp_path):
        (tmp_path / "other-name.py").write_text("", encoding="utf-8")
        proc = _run_hook(
            {"tool_name": "Edit", "tool_input": {"file_path": "other-name.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        payload = _output(proc)
        assert payload is not None
        assert payload["permissionDecision"] == "allow"
        assert "kebab-case" in payload["additionalContext"]


class TestMainFailureHandling:
    def test_malformed_stdin_fails_open(self, tmp_path):
        env = dict(os.environ)
        env["HOME"] = str(tmp_path / "home")
        env["USERPROFILE"] = str(tmp_path / "home")
        proc = subprocess.run(
            [sys.executable, str(MODULE_PATH)],
            input="{not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        assert proc.returncode == 0
        assert not proc.stdout.strip()
        assert "Error in pre_tool_guard" in proc.stderr

    def test_payload_without_path_exits_silently(self, tmp_path):
        proc = _run_hook(
            {"tool_name": "Grep", "tool_input": {"pattern": "x"}, "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _output(proc) is None
        assert proc.returncode == 0
