"""Tests for pre_tool_guard.py: pure helpers, packet validation, and the main() decision matrix.

Assertions record the CURRENT behaviour. Four cases assert the expected
post-fix behaviour and carry ``xfail(strict=True)`` so that fixing the
corresponding defect turns them into XPASS failures, prompting removal of the
marker. See docs/TESTING.md "PreToolUse guard" for the defect list.
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

    @pytest.mark.xfail(
        strict=True,
        reason="Defect: the skip condition requires BOTH PYTHONIOENCODING and "
               "miniforge3, so a command that already sets the encoding is "
               "injected a second time.",
    )
    def test_does_not_reinject_when_encoding_already_set(self):
        result = guard.inject_bash_env('export PYTHONIOENCODING=utf-8; python x.py')
        assert result is None or result.count("PYTHONIOENCODING") == 1


class TestValidatePacket:
    _CONFIRMED = [{"id": "E-1", "status": "confirmed"}]
    _CHANGE = [{"id": "C-1", "evidence_refs": ["E-1"]}]

    def test_no_active_marker_passes(self, tmp_path):
        assert guard.validate_packet(str(tmp_path))[0] is True

    def test_marker_pointing_at_missing_packet_passes(self, tmp_path):
        _write_packet(tmp_path, [], [], marker="gone.json", packet_name=None)
        assert guard.validate_packet(str(tmp_path))[0] is True

    def test_malformed_json_fails_open(self, tmp_path):
        task_dir = tmp_path / ".claude" / "temp_task"
        task_dir.mkdir(parents=True)
        (task_dir / ".active_packet").write_text("p.json", encoding="utf-8")
        (task_dir / "p.json").write_text("{not json", encoding="utf-8")
        assert guard.validate_packet(str(tmp_path))[0] is True

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

    @pytest.mark.xfail(
        strict=True,
        reason="Defect: an evidence entry without an 'id' key raises KeyError in "
               "the dict comprehension, which the broad except clause swallows, "
               "so a malformed packet silently permits every write.",
    )
    def test_evidence_entry_missing_id_is_rejected(self, tmp_path):
        _write_packet(tmp_path, [{"status": "confirmed"}], self._CHANGE)
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

    @pytest.mark.xfail(
        strict=True,
        reason="Defect: the Bash branch exits before validate_packet runs, so a "
               "redirection such as 'echo x > f' writes files while evidence is "
               "still unconfirmed.",
    )
    def test_bash_is_subject_to_packet_validation(self, tmp_path):
        _write_packet(tmp_path, self._SUSPECTED, self._CHANGE)
        proc = _run_hook(
            {"tool_name": "Bash", "tool_input": {"command": "echo x > a.py"},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) == "deny"

    @pytest.mark.xfail(
        strict=True,
        reason="Defect: the guard denies writes to the packet itself, so the "
               "remediation it demands (promote evidence to confirmed) cannot be "
               "performed with Edit/Write.",
    )
    def test_writing_to_the_active_packet_is_permitted(self, tmp_path):
        _write_packet(tmp_path, self._SUSPECTED, self._CHANGE)
        packet_rel = os.path.join(".claude", "temp_task", "p.json")
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": packet_rel},
             "cwd": str(tmp_path)},
            tmp_path / "home",
        )
        assert _decision(proc) != "deny"


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
