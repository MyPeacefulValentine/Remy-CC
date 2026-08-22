"""Tests for permission_gate.py: whitelist decisions, deny precedence, and fail-open IO."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "hooks" / "permission_gate.py"
spec = importlib.util.spec_from_file_location("permission_gate_tested", MODULE_PATH)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)

REMY_SRC = Path(__file__).resolve().parent.parent / "remy-src"
if str(REMY_SRC) not in sys.path:
    sys.path.insert(0, str(REMY_SRC))
import remy_config


@pytest.fixture(autouse=True)
def isolated_temp_root(tmp_path, monkeypatch):
    """Pin the gate's temp-dir rule to a fake root: pytest tmp_path lives
    inside the real system temp, so without this every skip:outside case
    would match the temp rule instead."""
    fake = tmp_path / "fake_system_temp"
    fake.mkdir()
    monkeypatch.setattr(gate.tempfile, "gettempdir", lambda: str(fake))
    yield fake


@pytest.fixture(autouse=True)
def isolated_remy_user_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(remy_config.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.delenv("REMY_PERMISSION_GATE", raising=False)
    with remy_config._CACHE_LOCK:
        remy_config._FILE_CACHE.clear()
    yield
    with remy_config._CACHE_LOCK:
        remy_config._FILE_CACHE.clear()


def _run_hook(stdin_bytes, home, *args):
    env = dict(os.environ)
    env.pop("REMY_PERMISSION_GATE", None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH), *args],
        input=stdin_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, timeout=60,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def _payload(cwd, tool="Write", path=".claude/temp_task/x.json"):
    return json.dumps(
        {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": str(cwd)}
    ).encode("utf-8")


def _write_project_config(cwd, values):
    claude_dir = Path(cwd) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "remy-config.json").write_text(
        json.dumps({"schema_version": "1.0.0", "values": values}), encoding="utf-8"
    )


class TestDecide:
    @pytest.mark.parametrize("path", [
        ".claude/temp_task/x.json",
        ".claude/temp_task/sub/deep.json",
        ".claude/temp_decisions/d.md",
        ".claude/history/reports/r.md",
        ".claude/project_tree.md",
        ".claude/logic_tree_view.md",
        ".claude/logic_index.db",
        ".claude/logic_index_dirty.lock",
        ".claude/tree_config",
    ])
    def test_system_artifacts_allowed(self, tmp_path, path):
        assert gate.decide(str(tmp_path), "Write", path) == "allow"

    def test_absolute_path_allowed(self, tmp_path):
        target = tmp_path / ".claude" / "temp_task" / "x.json"
        assert gate.decide(str(tmp_path), "Edit", str(target)) == "allow"

    def test_artifact_read_allowed(self, tmp_path):
        assert gate.decide(str(tmp_path), "Read", ".claude/temp_task/x.json") == "allow"

    @pytest.mark.parametrize("tool", ["Grep", "Glob"])
    def test_artifact_search_allowed(self, tmp_path, tool):
        assert gate.decide(str(tmp_path), tool, ".claude/temp_task") == "allow"

    @pytest.mark.parametrize("path", [
        ".claude/temp_task/x.json",
        ".claude/settings.json",
        ".claude/unknown.md",
        "src/main.py",
    ])
    def test_read_write_symmetry(self, tmp_path, path):
        assert gate.decide(str(tmp_path), "Read", path) == gate.decide(str(tmp_path), "Write", path)

    @pytest.mark.parametrize("tool", ["Grep", "Glob"])
    @pytest.mark.parametrize("path", [
        ".claude/temp_task/x.json",
        ".claude/settings.json",
        ".claude/unknown.md",
        "src/main.py",
    ])
    def test_search_file_target_matches_read(self, tmp_path, tool, path):
        assert gate.decide(str(tmp_path), tool, path) == gate.decide(str(tmp_path), "Read", path)

    @pytest.mark.parametrize("tool", ["Grep", "Glob"])
    def test_claude_root_search_still_prompts(self, tmp_path, tool):
        assert gate.decide(str(tmp_path), tool, ".claude") == "skip:root"

    @pytest.mark.parametrize("path", [
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/SETTINGS.LOCAL.JSON",
        ".claude/remy-config.json",
        ".claude/temp_task/../settings.json",
    ])
    def test_settings_denied(self, tmp_path, path):
        assert gate.decide(str(tmp_path), "Edit", path) == "skip:denied"

    @pytest.mark.parametrize("path", [
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/remy-config.json",
    ])
    @pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
    def test_settings_read_denied(self, tmp_path, tool, path):
        assert gate.decide(str(tmp_path), tool, path) == "skip:denied"

    @pytest.mark.parametrize("path", [
        "src/main.py",
        ".claude/temp_task/../../outside.txt",
    ])
    def test_outside_claude_skipped(self, tmp_path, path):
        assert gate.decide(str(tmp_path), "Edit", path) == "skip:outside"

    def test_foreign_project_claude_skipped(self, tmp_path):
        other = tmp_path / "other" / ".claude" / "temp_task"
        other.mkdir(parents=True)
        cwd = tmp_path / "proj"
        cwd.mkdir()
        assert gate.decide(str(cwd), "Write", str(other / "x.json")) == "skip:outside"

    def test_unlisted_claude_file_skipped(self, tmp_path):
        assert gate.decide(str(tmp_path), "Edit", ".claude/unknown.md") == "skip:unlisted"

    def test_claude_root_skipped(self, tmp_path):
        assert gate.decide(str(tmp_path), "Edit", ".claude") == "skip:root"

    def test_other_tools_skipped(self, tmp_path):
        assert gate.decide(str(tmp_path), "Bash", ".claude/temp_task/x.json") == "skip:tool"

    def test_empty_path_skipped(self, tmp_path):
        assert gate.decide(str(tmp_path), "Edit", "") == "skip:no_path"

    @pytest.mark.skipif(sys.platform != "win32", reason="drive-letter semantics are Windows-only")
    def test_cross_drive_skipped(self):
        assert gate.decide("Z:\\proj", "Edit", "C:\\other\\.claude\\temp_task\\x.json") == "skip:outside"

    def test_repeated_calls_are_stable(self, tmp_path):
        results = {gate.decide(str(tmp_path), "Write", ".claude/temp_task/x.json") for _ in range(3)}
        assert results == {"allow"}


class TestMemoryDecide:
    @pytest.fixture(autouse=True)
    def fake_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        self.projects = home / ".claude" / "projects"
        yield

    def test_memory_file_allowed(self, tmp_path):
        target = self.projects / "D--some-project" / "memory" / "MEMORY.md"
        assert gate.decide(str(tmp_path), "Write", str(target)) == "allow"

    def test_memory_nested_file_allowed(self, tmp_path):
        target = self.projects / "D--some-project" / "memory" / "sub" / "note.md"
        assert gate.decide(str(tmp_path), "Edit", str(target)) == "allow"

    def test_any_project_slug_allowed(self, tmp_path):
        target = self.projects / "C--another-project" / "memory" / "fact.md"
        assert gate.decide(str(tmp_path), "Write", str(target)) == "allow"

    def test_memory_read_allowed(self, tmp_path):
        target = self.projects / "D--some-project" / "memory" / "MEMORY.md"
        assert gate.decide(str(tmp_path), "Read", str(target)) == "allow"

    def test_non_memory_sibling_skipped(self, tmp_path):
        target = self.projects / "D--some-project" / "other" / "x.json"
        assert gate.decide(str(tmp_path), "Write", str(target)) == "skip:outside"

    def test_project_slug_root_file_skipped(self, tmp_path):
        target = self.projects / "D--some-project" / "memory.md"
        assert gate.decide(str(tmp_path), "Write", str(target)) == "skip:outside"

    def test_memory_dir_itself_skipped(self, tmp_path):
        target = self.projects / "D--some-project" / "memory"
        assert gate.decide(str(tmp_path), "Write", str(target)) == "skip:outside"

    @pytest.mark.parametrize("tool", ["Grep", "Glob"])
    def test_memory_dir_search_allowed(self, tmp_path, tool):
        target = self.projects / "D--some-project" / "memory"
        assert gate.decide(str(tmp_path), tool, str(target)) == "allow"

    @pytest.mark.parametrize("tool", ["Grep", "Glob"])
    def test_memory_file_search_allowed(self, tmp_path, tool):
        target = self.projects / "D--some-project" / "memory" / "MEMORY.md"
        assert gate.decide(str(tmp_path), tool, str(target)) == "allow"

    @pytest.mark.parametrize("tool", ["Grep", "Glob"])
    def test_project_slug_root_search_skipped(self, tmp_path, tool):
        target = self.projects / "D--some-project"
        assert gate.decide(str(tmp_path), tool, str(target)) == "skip:outside"

    def test_projects_root_file_skipped(self, tmp_path):
        target = self.projects / "stray.md"
        assert gate.decide(str(tmp_path), "Write", str(target)) == "skip:outside"

    def test_traversal_out_of_memory_skipped(self, tmp_path):
        target = self.projects / "D--some-project" / "memory" / ".." / ".." / "escape.md"
        assert gate.decide(str(tmp_path), "Write", str(target)) == "skip:outside"

    def test_other_tools_still_skipped(self, tmp_path):
        target = self.projects / "D--some-project" / "memory" / "MEMORY.md"
        assert gate.decide(str(tmp_path), "Bash", str(target)) == "skip:tool"


class TestTempDecide:
    def test_temp_file_allowed(self, tmp_path, isolated_temp_root):
        target = isolated_temp_root / "probe" / "script.py"
        assert gate.decide(str(tmp_path), "Write", str(target)) == "allow"

    def test_temp_nested_edit_allowed(self, tmp_path, isolated_temp_root):
        target = isolated_temp_root / "a" / "b" / "data.json"
        assert gate.decide(str(tmp_path), "Edit", str(target)) == "allow"

    def test_temp_read_allowed(self, tmp_path, isolated_temp_root):
        target = isolated_temp_root / "tasks" / "agent.output"
        assert gate.decide(str(tmp_path), "Read", str(target)) == "allow"

    @pytest.mark.parametrize("tool", ["Grep", "Glob"])
    def test_temp_dir_search_allowed(self, tmp_path, isolated_temp_root, tool):
        target = isolated_temp_root / "tasks"
        assert gate.decide(str(tmp_path), tool, str(target)) == "allow"

    @pytest.mark.parametrize("tool", ["Grep", "Glob"])
    def test_temp_root_search_allowed(self, tmp_path, isolated_temp_root, tool):
        assert gate.decide(str(tmp_path), tool, str(isolated_temp_root)) == "allow"

    def test_traversal_out_of_temp_skipped(self, tmp_path, isolated_temp_root):
        target = isolated_temp_root / ".." / "escape.py"
        assert gate.decide(str(tmp_path), "Write", str(target)) == "skip:outside"

    def test_other_tools_still_skipped(self, tmp_path, isolated_temp_root):
        target = isolated_temp_root / "script.py"
        assert gate.decide(str(tmp_path), "Bash", str(target)) == "skip:tool"

    def test_project_claude_inside_temp_keeps_deny(self, isolated_temp_root):
        cwd = isolated_temp_root / "proj"
        (cwd / ".claude").mkdir(parents=True)
        assert gate.decide(str(cwd), "Edit", ".claude/settings.json") == "skip:denied"


class TestSuiteDecide:
    @pytest.fixture(autouse=True)
    def fake_home(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        self.claude_home = home / ".claude"
        yield

    @pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
    @pytest.mark.parametrize("sub", ["skills", "output-styles", "hooks"])
    def test_suite_file_readonly_allowed(self, tmp_path, tool, sub):
        target = self.claude_home / sub / "remy-plan" / "halt_protocol.md"
        assert gate.decide(str(tmp_path), tool, str(target)) == "allow"

    @pytest.mark.parametrize("tool", ["Grep", "Glob"])
    def test_suite_dir_search_allowed(self, tmp_path, tool):
        target = self.claude_home / "skills" / "remy-plan"
        assert gate.decide(str(tmp_path), tool, str(target)) == "allow"

    @pytest.mark.parametrize("tool", ["Edit", "Write"])
    def test_suite_file_write_skipped(self, tmp_path, tool):
        target = self.claude_home / "skills" / "remy-plan" / "SKILL.md"
        assert gate.decide(str(tmp_path), tool, str(target)) == "skip:outside"

    @pytest.mark.parametrize("name", ["settings.json", "settings.local.json", "CLAUDE.md", ".credentials.json"])
    def test_claude_home_root_files_skipped(self, tmp_path, name):
        target = self.claude_home / name
        assert gate.decide(str(tmp_path), "Read", str(target)) == "skip:outside"

    def test_traversal_out_of_suite_dir_skipped(self, tmp_path):
        target = self.claude_home / "skills" / ".." / "settings.json"
        assert gate.decide(str(tmp_path), "Read", str(target)) == "skip:outside"

    def test_other_tools_still_skipped(self, tmp_path):
        target = self.claude_home / "skills" / "remy-plan" / "SKILL.md"
        assert gate.decide(str(tmp_path), "Bash", str(target)) == "skip:tool"


class TestGateEnabled:
    def test_default_is_enabled(self, tmp_path):
        assert gate.gate_enabled(str(tmp_path)) is True

    def test_env_off_disables(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REMY_PERMISSION_GATE", "false")
        assert gate.gate_enabled(str(tmp_path)) is False

    def test_project_config_off_disables(self, tmp_path):
        _write_project_config(tmp_path, {"REMY_PERMISSION_GATE": "false"})
        assert gate.gate_enabled(str(tmp_path)) is False


class TestMainIO:
    def test_allow_emits_documented_decision(self, tmp_path):
        rc, out = _run_hook(_payload(tmp_path), tmp_path / "home")
        assert rc == 0
        assert json.loads(out) == gate.ALLOW_DECISION

    def test_read_allow_emits_documented_decision(self, tmp_path):
        rc, out = _run_hook(_payload(tmp_path, "Read"), tmp_path / "home")
        assert rc == 0
        assert json.loads(out) == gate.ALLOW_DECISION

    def test_grep_path_field_emits_documented_decision(self, tmp_path):
        stdin_bytes = json.dumps(
            {"tool_name": "Grep",
             "tool_input": {"pattern": "x", "path": ".claude/temp_task"},
             "cwd": str(tmp_path)}
        ).encode("utf-8")
        rc, out = _run_hook(stdin_bytes, tmp_path / "home")
        assert rc == 0
        assert json.loads(out) == gate.ALLOW_DECISION

    def test_grep_without_path_emits_nothing(self, tmp_path):
        stdin_bytes = json.dumps(
            {"tool_name": "Grep", "tool_input": {"pattern": "x"}, "cwd": str(tmp_path)}
        ).encode("utf-8")
        rc, out = _run_hook(stdin_bytes, tmp_path / "home")
        assert rc == 0
        assert out == ""

    @pytest.mark.parametrize("stdin_bytes", [b"", b"not json at all", b"[1, 2]"])
    def test_malformed_stdin_fails_open(self, tmp_path, stdin_bytes):
        rc, out = _run_hook(stdin_bytes, tmp_path / "home")
        assert rc == 0
        assert out == ""

    def test_denied_path_emits_nothing(self, tmp_path):
        rc, out = _run_hook(_payload(tmp_path, "Edit", ".claude/settings.json"), tmp_path / "home")
        assert rc == 0
        assert out == ""

    def test_gate_off_via_project_config_emits_nothing(self, tmp_path):
        _write_project_config(tmp_path, {"REMY_PERMISSION_GATE": "false"})
        rc, out = _run_hook(_payload(tmp_path), tmp_path / "home")
        assert rc == 0
        assert out == ""

    def test_trace_flag_writes_log(self, tmp_path):
        rc, out = _run_hook(_payload(tmp_path), tmp_path / "home", "--trace")
        assert rc == 0
        assert json.loads(out) == gate.ALLOW_DECISION
        log = tmp_path / ".claude" / "temp_log" / "permission_gate_trace.log"
        assert log.is_file()
        assert "outcome=allow" in log.read_text(encoding="utf-8")
