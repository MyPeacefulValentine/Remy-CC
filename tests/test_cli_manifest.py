"""CLI tests for the transactional dual-root install runtime."""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))

import cli
from install_runtime import CandidateFile, InstallRequest, InstallRuntime, RootPaths


def _install_v3_for_cli(tmp_path, monkeypatch):
    roots = RootPaths(tmp_path / "claude-v3", tmp_path / "remy-v3")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(roots.claude))
    monkeypatch.setenv("REMY_CC_HOME", str(roots.remy))
    source = tmp_path / "managed.txt"
    source.write_text("managed", encoding="utf-8")
    template = {
        "hooks": {
            "PreToolUse": [{
                "matcher": "Read|Glob|Grep",
                "hooks": [{"type": "command", "command": "__REMY_ENRICH_COMMAND__"}],
            }],
            "PostToolUse": [{
                "matcher": "Edit|Write",
                "hooks": [{"type": "command", "command": "__REMY_DIRTY_COMMAND__"}],
            }],
        },
        "permissions": {"allow": []},
        "env": {},
    }
    runtime = InstallRuntime(roots)
    runtime.install(InstallRequest(
        suite_version="1.7.3",
        candidates=[CandidateFile("claude", "managed.txt", source, "test")],
        settings_template=template,
        python_executable=sys.executable,
    ))
    roots.claude.mkdir(parents=True, exist_ok=True)
    (roots.claude / "remy-config.json").write_text(
        json.dumps({"schema_version": "1.0.0", "values": {}}), encoding="utf-8"
    )
    return roots


def test_cli_v3_version_and_verify_json(tmp_path, monkeypatch, capsys):
    _install_v3_for_cli(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))

    assert cli.get_version() == "1.7.3"
    cli.cmd_verify_runtime(types.SimpleNamespace(json=True))

    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    payload = json.loads(output[0])
    assert payload["status"] == "ok"
    assert payload["exit_code"] == 0


def test_cli_v3_uninstall_json(tmp_path, monkeypatch, capsys):
    roots = _install_v3_for_cli(tmp_path, monkeypatch)

    cli.cmd_uninstall_runtime(types.SimpleNamespace(
        yes=True, non_interactive=True, json=True, purge_state=False
    ))

    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    payload = json.loads(output[0])
    assert payload["operation"] == "uninstall"
    assert payload["exit_code"] == 0
    assert not (roots.remy / "install" / "manifest.json").exists()


def test_cli_v3_uninstall_aborts_without_confirmation(tmp_path, monkeypatch, capsys):
    roots = _install_v3_for_cli(tmp_path, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    cli.cmd_uninstall_runtime(types.SimpleNamespace(
        yes=False, non_interactive=False, json=False, purge_state=False
    ))

    assert (roots.remy / "install" / "manifest.json").exists()
    assert cli._um("aborted") in capsys.readouterr().out
