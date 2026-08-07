"""Behavioural tests for hooks/logic_dirty_tracker.py (PostToolUse dirty recording).

``main()`` is exercised through subprocess stdin injection. The subprocess
environment points ``HOME``/``USERPROFILE`` at a temporary directory, so
``_load_index_state`` falls back to the repository's ``skills/remy-index`` copy
and no test touches the developer's installed suite. Assertions target the
on-disk dirty queue file only.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "hooks" / "logic_dirty_tracker.py"


def _run_hook(payload, home):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(MODULE_PATH)],
        input=data,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _dirty_file(project):
    return Path(project) / ".claude" / "logic_index_dirty"


@pytest.fixture()
def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    return proj


@pytest.fixture()
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


class TestDispatch:
    @pytest.mark.parametrize("tool_name", ["Write", "Edit"])
    def test_write_tools_record_the_source_path(self, project, home, tool_name):
        proc = _run_hook(
            {"tool_name": tool_name, "tool_input": {"file_path": "a.py"},
             "cwd": str(project)},
            home,
        )
        assert proc.returncode == 0
        assert _dirty_file(project).read_text(encoding="utf-8").split() == ["a.py"]

    def test_non_write_tool_records_nothing(self, project, home):
        proc = _run_hook(
            {"tool_name": "Read", "tool_input": {"file_path": "a.py"},
             "cwd": str(project)},
            home,
        )
        assert proc.returncode == 0
        assert not _dirty_file(project).exists()

    def test_missing_file_path_records_nothing(self, project, home):
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {}, "cwd": str(project)},
            home,
        )
        assert proc.returncode == 0
        assert not _dirty_file(project).exists()


class TestPathNormalization:
    def test_absolute_inside_path_is_recorded_relative(self, project, home):
        target = project / "sub" / "b.py"
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(target)},
             "cwd": str(project)},
            home,
        )
        assert proc.returncode == 0
        assert _dirty_file(project).read_text(encoding="utf-8").split() == ["sub/b.py"]

    def test_non_source_extension_records_nothing(self, project, home):
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": "notes.txt"},
             "cwd": str(project)},
            home,
        )
        assert proc.returncode == 0
        assert not _dirty_file(project).exists()

    def test_path_outside_project_records_nothing(self, project, home, tmp_path):
        outside = tmp_path / "other.py"
        outside.write_text("y = 2\n", encoding="utf-8")
        proc = _run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(outside)},
             "cwd": str(project)},
            home,
        )
        assert proc.returncode == 0
        assert not _dirty_file(project).exists()


class TestFailureHandling:
    def test_malformed_stdin_exits_zero_silently(self, home):
        proc = _run_hook("{not json", home)
        assert proc.returncode == 0
        assert not proc.stdout.strip()
