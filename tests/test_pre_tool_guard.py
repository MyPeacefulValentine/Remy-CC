"""Packet compatibility, input preservation, and selected permission decisions."""

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
    monkeypatch.setenv("REMY_LANG", "en")
    with guard.remy_config._CACHE_LOCK:
        guard.remy_config._FILE_CACHE.clear()
    yield
    with guard.remy_config._CACHE_LOCK:
        guard.remy_config._FILE_CACHE.clear()


def _write_packet(cwd, evidence, changes, marker="p.json", packet_name: Optional[str] = "p.json"):
    task_dir = Path(cwd) / ".claude" / "temp_task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / ".active_packet").write_text(marker, encoding="utf-8")
    if packet_name:
        (task_dir / packet_name).write_text(json.dumps({"evidence_packet": {
            "evidence": evidence, "proposed_changes": changes,
        }}), encoding="utf-8")
    return task_dir


def _run_hook(payload, home):
    env = {key: value for key, value in os.environ.items() if not key.startswith("REMY_")}
    env.update(REMY_LANG="en", HOME=str(home), USERPROFILE=str(home), PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(MODULE_PATH)], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)


def _output(proc):
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout.strip() else None


def _decision(proc):
    payload = _output(proc)
    return payload.get("permissionDecision") if payload else None


class TestPathIsContained:
    def test_child_and_root(self, tmp_path):
        assert guard.path_is_contained(str(tmp_path / "missing" / "f.py"), str(tmp_path))
        assert guard.path_is_contained(str(tmp_path), str(tmp_path))

    def test_sibling_prefix(self, tmp_path):
        assert not guard.path_is_contained(str(tmp_path / "proj_other"), str(tmp_path / "proj"))

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows drive and case handling")
    def test_case_and_cross_drive(self, tmp_path):
        assert guard.path_is_contained(str(tmp_path).upper(), str(tmp_path).lower())
        other_drive = "Z:" if tmp_path.drive.upper() != "Z:" else "Y:"
        assert not guard.path_is_contained(other_drive + "\\other", str(tmp_path))

    def test_absolute_detection(self, tmp_path):
        assert guard.is_absolute_path(str(tmp_path))
        assert not guard.is_absolute_path("sub/f.py")


class TestValidatePacket:
    _CONFIRMED = [{"id": "E-1", "status": "confirmed"}]
    _CHANGE = [{"id": "C-1", "evidence_refs": ["E-1"]}]

    def test_no_active_marker_passes(self, tmp_path):
        assert guard.validate_packet(str(tmp_path))[0]

    def test_marker_pointing_at_missing_packet_passes(self, tmp_path):
        _write_packet(tmp_path, [], [], marker="gone.json", packet_name=None)
        assert guard.validate_packet(str(tmp_path))[0]

    def test_malformed_json_is_rejected(self, tmp_path):
        task = _write_packet(tmp_path, [], [])
        (task / "p.json").write_text("{not json", encoding="utf-8")
        valid, message = guard.validate_packet(str(tmp_path))
        assert not valid and message
        assert "remove the active marker" in message
        assert "to bypass" in message

    @pytest.mark.parametrize("changes", [[], [{"id": "C-1"}], _CHANGE])
    def test_compatible_changes_pass(self, tmp_path, changes):
        _write_packet(tmp_path, self._CONFIRMED, changes)
        assert guard.validate_packet(str(tmp_path))[0]

    @pytest.mark.parametrize("status", ["suspected", "stale"])
    def test_blocked_status(self, tmp_path, status):
        _write_packet(tmp_path, [{"id": "E-1", "status": status}], self._CHANGE)
        valid, message = guard.validate_packet(str(tmp_path))
        assert not valid and message
        assert "do not relabel" in message

    def test_validation_is_not_scoped_to_a_file_path(self, tmp_path):
        _write_packet(tmp_path, self._CONFIRMED + [{"id": "E-2", "status": "suspected"}],
                      self._CHANGE + [{"id": "C-2", "evidence_refs": ["E-2"]}])
        assert not guard.validate_packet(str(tmp_path))[0]
        assert "file_path" not in guard.validate_packet.__code__.co_varnames

    @pytest.mark.parametrize("evidence,changes", [
        (_CONFIRMED, [{"id": "C-1", "evidence_refs": ["E-9"]}]),
        ([{"status": "confirmed"}], _CHANGE), (["bad"], _CHANGE),
        ([{"id": ["E-1"]}], _CHANGE), (_CONFIRMED, ["bad"]),
        (_CONFIRMED, [{"id": "C-1", "evidence_refs": "E-1"}]),
        (_CONFIRMED, [{"id": "C-1", "evidence_refs": [1]}]),
        ({"E-1": {}}, _CHANGE),
    ])
    def test_invalid_reference_structures(self, tmp_path, evidence, changes):
        _write_packet(tmp_path, evidence, changes)
        assert not guard.validate_packet(str(tmp_path))[0]

    @pytest.mark.parametrize("packet", [[], {"evidence_packet": []},
                                        {"evidence_packet": {"proposed_changes": {}}}])
    def test_invalid_packet_containers(self, tmp_path, packet):
        task = _write_packet(tmp_path, [], [])
        (task / "p.json").write_text(json.dumps(packet), encoding="utf-8")
        assert not guard.validate_packet(str(tmp_path))[0]

    def test_io_failure_preserves_no_decision_compatibility(self, tmp_path, monkeypatch):
        _write_packet(tmp_path, self._CONFIRMED, self._CHANGE)
        def fail(*args, **kwargs):
            raise OSError("test read failure")
        monkeypatch.setattr("builtins.open", fail)
        assert guard.validate_packet(str(tmp_path)) == (True, None)


@pytest.mark.parametrize("tool,command", [("Bash", "ls"), ("Bash", "python x.py"),
                                          ("PowerShell", "Get-ChildItem"), ("PowerShell", "python x.py")])
def test_shell_is_not_rewritten_or_approved(tmp_path, tool, command):
    marker = tmp_path / "executed"
    (tmp_path / ".env_setup.sh").write_text(f'touch "{marker}"', encoding="utf-8")
    proc = _run_hook({"tool_name": tool, "tool_input": {"command": command}, "cwd": str(tmp_path)}, tmp_path / "home")
    assert proc.returncode == 0 and not proc.stderr
    assert _output(proc) is None
    assert not marker.exists()


@pytest.mark.parametrize("subagent,decision", [("Plan", None), ("Explore", None),
                                               ("general-purpose", "ask"), ("statusline-setup", None)])
def test_agent_decisions(tmp_path, subagent, decision):
    proc = _run_hook({"tool_name": "Agent", "tool_input": {"subagent_type": subagent}, "cwd": str(tmp_path)}, tmp_path / "home")
    assert _decision(proc) == decision
    output = _output(proc)
    assert output and "search scope" in output["additionalContext"]
    assert "Level 4" not in output["additionalContext"]


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")])
@pytest.mark.parametrize("absolute", [False, True])
def test_write_packet_gate_and_input_preservation(tmp_path, tool, key, absolute):
    _write_packet(tmp_path, [{"id": "E-1", "status": "suspected"}], [{"id": "C-1", "evidence_refs": ["E-1"]}])
    target = str(tmp_path / "new-file.py") if absolute else "new-file.py"
    proc = _run_hook({"tool_name": tool, "tool_input": {key: target, "other": 7}, "cwd": str(tmp_path)}, tmp_path / "home")
    assert _decision(proc) == "deny"
    assert "updatedInput" not in _output(proc)


@pytest.mark.parametrize("target", ["a.py", "new-file.py", "both-here.py", "package-lock.json", "sub/f.py"])
def test_project_target_is_not_redirected_or_approved(tmp_path, target):
    (tmp_path / "both_here.py").write_text("unchanged", encoding="utf-8")
    for path in (target, str(tmp_path / target)):
        proc = _run_hook({"tool_name": "Write", "tool_input": {"file_path": path}, "cwd": str(tmp_path)}, tmp_path / "home")
        output = _output(proc)
        assert output and _decision(proc) is None and "updatedInput" not in output
    assert (tmp_path / "both_here.py").read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("absolute", [False, True])
def test_packet_remediation_exemption_has_no_approval(tmp_path, absolute):
    _write_packet(tmp_path, [{"id": "E-1", "status": "suspected"}], [{"evidence_refs": ["E-1"]}])
    rel = os.path.join(".claude", "temp_task", "p.json")
    path = str(tmp_path / rel) if absolute else rel
    proc = _run_hook({"tool_name": "Write", "tool_input": {"file_path": path}, "cwd": str(tmp_path)}, tmp_path / "home")
    assert _decision(proc) is None


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")])
@pytest.mark.parametrize("absolute", [False, True])
def test_outside_paths_require_confirmation(tmp_path, tool, key, absolute):
    project = tmp_path / "project"
    project.mkdir()
    target = str(tmp_path / "outside.py") if absolute else os.path.join("..", "outside.py")
    proc = _run_hook({"tool_name": tool, "tool_input": {key: target}, "cwd": str(project)}, tmp_path / "home")
    assert _decision(proc) == "ask"
    assert "updatedInput" not in _output(proc)


@pytest.mark.parametrize("tool,key", [("Read", "file_path"), ("Glob", "path"), ("Grep", "path")])
def test_read_only_outside_has_no_decision(tmp_path, tool, key):
    proc = _run_hook({"tool_name": tool, "tool_input": {key: "../outside"}, "cwd": str(tmp_path)}, tmp_path / "home")
    assert _output(proc) is None


def test_symlink_outside_requires_confirmation(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    link = project / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    proc = _run_hook({"tool_name": "Write", "tool_input": {"file_path": "linked/f.py"}, "cwd": str(project)}, tmp_path / "home")
    assert _decision(proc) == "ask"


def test_shell_does_not_claim_packet_validation(tmp_path):
    _write_packet(tmp_path, [{"id": "E-1", "status": "suspected"}], [{"evidence_refs": ["E-1"]}])
    proc = _run_hook({"tool_name": "Bash", "tool_input": {"command": "echo x > a.py"}, "cwd": str(tmp_path)}, tmp_path / "home")
    assert _output(proc) is None


@pytest.mark.parametrize("payload", [[], {"tool_input": []}, {"cwd": ""}])
def test_invalid_input_has_diagnostic_not_approval(tmp_path, payload):
    proc = _run_hook(payload, tmp_path / "home")
    assert proc.returncode == 0 and not proc.stdout.strip()
    assert "pre_tool_guard:" in proc.stderr


def test_cwd_drift_is_reported_even_without_target(tmp_path, monkeypatch, capsys):
    import io
    monkeypatch.setattr(guard.session_anchor, "drift", lambda *args: str(tmp_path / "anchor"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(tmp_path)})))
    guard.main()
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert "session root" in output["additionalContext"]
    assert "permissionDecision" not in output


@pytest.mark.parametrize("tool,key", [("Write", "file_path"), ("Edit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_packet_denial_precedes_outside_ask(tmp_path, tool, key):
    _write_packet(tmp_path, [{"id": "E-1", "status": "suspected"}], [{"evidence_refs": ["E-1"]}])
    proc = _run_hook({"tool_name": tool, "tool_input": {key: "../outside.py"}, "cwd": str(tmp_path)}, tmp_path / "home")
    output = _output(proc)
    assert output and output["permissionDecision"] == "deny"
    assert "suspected" in output["permissionDecisionReason"]


def test_language_follows_event_project_from_subdirectory(tmp_path, monkeypatch):
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / "remy-config.json").write_text(json.dumps({"schema_version": "1.0.0", "values": {"REMY_LANG": "zh-CN"}}), encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    monkeypatch.delenv("REMY_LANG", raising=False)
    assert "计划" in guard._msg("plan_agent_lang", str(nested))
