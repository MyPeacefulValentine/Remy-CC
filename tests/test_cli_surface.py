"""Surface guard for the delegated Python CLI.

H.5 disposition: cli.py keeps only the config and summary families as the
delegation target of the remy-cc binary; every retired subcommand must be
rejected and the module must not depend on the retired install_runtime
package.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))

import cli

KEPT_INVOCATIONS = [
    ["config"],
    ["summary-rebuild"],
    ["summary-vacuum"],
    ["summary-audit", "file", "some/path.py"],
]

RETIRED_COMMANDS = ["daemon", "update", "uninstall", "verify", "version", "ui", "project"]


@pytest.mark.parametrize("argv", KEPT_INVOCATIONS, ids=lambda argv: argv[0])
def test_kept_commands_parse(argv):
    args = cli.build_parser().parse_args(argv)
    assert args.command == argv[0]


@pytest.mark.parametrize("command", RETIRED_COMMANDS)
def test_retired_commands_are_rejected(command, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args([command])
    assert excinfo.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_get_claude_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    assert cli.get_claude_home() == tmp_path / "claude-home"


def test_get_claude_home_defaults_under_user_home(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cli.get_claude_home() == Path.home() / ".claude"


def test_module_has_no_install_runtime_reference():
    source_path = Path(__file__).resolve().parent.parent / "remy-src" / "cli.py"
    assert "install_runtime" not in source_path.read_text(encoding="utf-8")
