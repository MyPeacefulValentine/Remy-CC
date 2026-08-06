"""Unit tests for cli.py manifest path resolution (schema v1 / v2 compatibility).

Regression target: cmd_verify and cmd_uninstall previously called Path(entry["path"]) directly,
which resolved schema v2 POSIX-relative records against CWD instead of claude_home.
"""
import hashlib
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))

import cli


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claude_home"
    home.mkdir()
    (home / "settings.json").write_text("{}", encoding="utf-8")
    (home / "remy-config.json").write_text(
        json.dumps({"schema_version": "1.0.0", "values": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "get_claude_home", lambda: home)
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    return home


def _seed_file(path, content="payload"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _write_manifest(home, files, version="1.4.4", schema_version=2):
    manifest = {
        "schema_version": schema_version,
        "version": version,
        "files": files,
    }
    (home / ".installer_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_resolve_relative_path(claude_home):
    f = claude_home / "skills" / "remy-plan" / "SKILL.md"
    _seed_file(f, "x")
    resolved = cli._resolve_record_path({"path": "skills/remy-plan/SKILL.md"}, claude_home)
    assert resolved.resolve() == f.resolve()


def test_resolve_absolute_path_legacy(claude_home):
    f = claude_home / "skills" / "old-foo" / "data.md"
    _seed_file(f, "old")
    resolved = cli._resolve_record_path({"path": str(f)}, claude_home)
    assert resolved == f


def test_verify_schema_v2_all_present(claude_home, capsys):
    h1 = _seed_file(claude_home / "language.md", "lang")
    h2 = _seed_file(claude_home / "skills" / "remy-plan" / "SKILL.md", "skill")
    _write_manifest(claude_home, [
        {"path": "language.md", "sha256": h1},
        {"path": "skills/remy-plan/SKILL.md", "sha256": h2},
    ])
    cli.cmd_verify(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert "files missing from manifest" not in out
    assert "Verification passed" in out


def test_verify_reports_missing_mcp_as_error(claude_home, capsys, monkeypatch):
    """The MCP SDK is a required install component, so `remy-cc verify` must fail
    without it — matching install.py --verify and the installer's abort-on-failure."""
    h1 = _seed_file(claude_home / "language.md", "lang")
    _write_manifest(claude_home, [{"path": "language.md", "sha256": h1}])
    monkeypatch.setitem(sys.modules, "mcp", None)

    with pytest.raises(SystemExit) as exc:
        cli.cmd_verify(types.SimpleNamespace())

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "MCP SDK (mcp) is a required component" in out
    assert "MCP SDK: not installed (required)" in out


def test_verify_prints_dependency_status_lines(claude_home, capsys):
    h1 = _seed_file(claude_home / "language.md", "lang")
    _write_manifest(claude_home, [{"path": "language.md", "sha256": h1}])
    cli.cmd_verify(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert "MCP SDK: installed" in out
    for label in ("tree-sitter:", "Jinja2:", "GitHub CLI (gh):"):
        assert label in out


def test_verify_schema_v2_missing_detected(claude_home, capsys):
    h1 = _seed_file(claude_home / "language.md", "lang")
    _write_manifest(claude_home, [
        {"path": "language.md", "sha256": h1},
        {"path": "skills/nonexistent/SKILL.md", "sha256": "deadbeef"},
        {"path": "hooks/missing.py", "sha256": "deadbeef"},
    ])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_verify(types.SimpleNamespace())
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "2 files missing from manifest" in out


def test_verify_schema_v2_hash_mismatch_detected(claude_home, capsys):
    file_path = claude_home / "skills" / "remy-plan" / "SKILL.md"
    original_hash = _seed_file(file_path, "original")
    _write_manifest(claude_home, [
        {"path": "skills/remy-plan/SKILL.md", "sha256": original_hash},
    ])
    file_path.write_text("changed", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        cli.cmd_verify(types.SimpleNamespace())

    assert exc.value.code == 1
    assert "1 files differ from manifest" in capsys.readouterr().out


def test_verify_cwd_independent(claude_home, capsys, tmp_path, monkeypatch):
    h = _seed_file(claude_home / "language.md", "lang")
    _write_manifest(claude_home, [{"path": "language.md", "sha256": h}])
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)
    cli.cmd_verify(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert "files missing from manifest" not in out


def test_verify_schema_v1_absolute_still_works(claude_home, capsys):
    f = claude_home / "language.md"
    h = _seed_file(f, "lang")
    _write_manifest(claude_home, [{"path": str(f), "sha256": h}], schema_version=1)
    cli.cmd_verify(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert "files missing from manifest" not in out


def test_verify_missing_remy_config_detected(claude_home, capsys):
    (claude_home / "remy-config.json").unlink()
    _write_manifest(claude_home, [])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_verify(types.SimpleNamespace())
    assert exc.value.code == 1
    assert "Remy configuration not found" in capsys.readouterr().out


def test_verify_invalid_remy_config_is_redacted(claude_home, capsys):
    fake_secret = "fake-secret-not-for-output"
    (claude_home / "remy-config.json").write_text(json.dumps({
        "schema_version": "1.0.0",
        "values": {"REMY_LLM_API_KEY": fake_secret, "REMY_LLM_MAX_WORKERS": "zero"},
    }), encoding="utf-8")
    _write_manifest(claude_home, [])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_verify(types.SimpleNamespace())
    output = capsys.readouterr().out
    assert exc.value.code == 1
    assert "Remy configuration invalid" in output
    assert fake_secret not in output


def test_uninstall_schema_v2_removes_files(claude_home, capsys):
    f1 = claude_home / "skills" / "remy-plan" / "SKILL.md"
    f2 = claude_home / "hooks" / "logic_dirty_tracker.py"
    h1 = _seed_file(f1, "skill")
    h2 = _seed_file(f2, "hook")
    _write_manifest(claude_home, [
        {"path": "skills/remy-plan/SKILL.md", "sha256": h1},
        {"path": "hooks/logic_dirty_tracker.py", "sha256": h2},
    ])
    args = types.SimpleNamespace(yes=True)
    cli.cmd_uninstall(args)
    assert not f1.exists()
    assert not f2.exists()


def test_uninstall_skips_modified_files(claude_home, capsys):
    f = claude_home / "skills" / "remy-plan" / "SKILL.md"
    original_hash = _seed_file(f, "original")
    f.write_text("user-edited", encoding="utf-8")
    _write_manifest(claude_home, [
        {"path": "skills/remy-plan/SKILL.md", "sha256": original_hash},
    ])
    args = types.SimpleNamespace(yes=True)
    cli.cmd_uninstall(args)
    assert f.exists()
    assert f.read_text(encoding="utf-8") == "user-edited"
