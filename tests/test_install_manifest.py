"""Unit tests for v1.4.4 install.py manifest-based cleanup and third-party isolation."""
import hashlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))

import install
from install_runtime import CandidateFile, InstallRequest, InstallRuntime, InstallRuntimeError, RootPaths
from install_runtime.probes import DaemonProbe, probe_daemon
import install_runtime.probes as probes_module
from install_runtime.transaction import FileTransaction
import install_runtime.facade as install_facade
from install_runtime.facade import result_for_error


@pytest.fixture(autouse=True)
def _isolated_remy_cc_home(tmp_path, monkeypatch):
    """Point REMY_CC_HOME at a per-test directory so uninstall paths never
    probe the real ~/.remy-cc daemon (autouse runs first; daemon_env's own
    setenv still overrides it)."""
    monkeypatch.setenv("REMY_CC_HOME", str(tmp_path / "isolated-remy-cc-home"))


@pytest.fixture
def claude_home(tmp_path):
    home = tmp_path / "claude_home"
    home.mkdir()
    return home


def _seed_file(path, content="payload"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_path_roundtrip(claude_home):
    f = claude_home / "skills" / "remy-plan" / "SKILL.md"
    _seed_file(f, "x")
    rel = f.relative_to(claude_home).as_posix()
    assert rel == "skills/remy-plan/SKILL.md"
    resolved = install._resolve_record_path({"path": rel}, claude_home)
    assert resolved.resolve() == f.resolve()


def test_legacy_abs_manifest(claude_home):
    f = claude_home / "skills" / "old-foo" / "data.md"
    _seed_file(f, "old")
    resolved = install._resolve_record_path({"path": str(f)}, claude_home)
    assert resolved == f


def test_third_party_preserved(claude_home):
    remy_file = claude_home / "skills" / "remy-plan" / "SKILL.md"
    third_party = claude_home / "skills" / "vendor-tool" / "SKILL.md"
    h_remy = _seed_file(remy_file, "remy")
    _seed_file(third_party, "external")
    old_manifest = {
        "files": [{"path": "skills/remy-plan/SKILL.md", "sha256": h_remy}],
    }
    install.cleanup_from_manifest(old_manifest, claude_home)
    assert not remy_file.exists()
    assert third_party.exists()


def test_deprecated_skill_removed(claude_home):
    deprecated = claude_home / "skills" / "remy-foo" / "SKILL.md"
    h = _seed_file(deprecated, "deprecated")
    old_manifest = {
        "files": [{"path": "skills/remy-foo/SKILL.md", "sha256": h}],
    }
    install.cleanup_from_manifest(old_manifest, claude_home)
    assert not deprecated.exists()


def test_user_modified_backed_up(claude_home):
    f = claude_home / "skills" / "remy-plan" / "SKILL.md"
    _seed_file(f, "original")
    expected_hash = hashlib.sha256(b"original").hexdigest()
    f.write_text("modified-by-user", encoding="utf-8")
    old_manifest = {
        "files": [{"path": "skills/remy-plan/SKILL.md", "sha256": expected_hash}],
    }
    install.cleanup_from_manifest(old_manifest, claude_home)
    bak = f.with_suffix(f.suffix + install.BACKUP_SUFFIX)
    assert not f.exists()
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == "modified-by-user"


def test_empty_dirs_removed(claude_home):
    f = claude_home / "skills" / "remy-foo" / "deep" / "SKILL.md"
    h = _seed_file(f, "x")
    old_manifest = {"files": [{"path": "skills/remy-foo/deep/SKILL.md", "sha256": h}]}
    install.cleanup_from_manifest(old_manifest, claude_home)
    assert not (claude_home / "skills" / "remy-foo" / "deep").exists()
    assert not (claude_home / "skills" / "remy-foo").exists()
    assert not (claude_home / "skills").exists()
    assert claude_home.exists()


def test_empty_dir_stops_at_non_empty(claude_home):
    target = claude_home / "skills" / "remy-foo" / "a" / "remove.md"
    sibling = claude_home / "skills" / "remy-foo" / "b" / "keep.md"
    h = _seed_file(target, "x")
    _seed_file(sibling, "y")
    old_manifest = {"files": [{"path": "skills/remy-foo/a/remove.md", "sha256": h}]}
    install.cleanup_from_manifest(old_manifest, claude_home)
    assert not (claude_home / "skills" / "remy-foo" / "a").exists()
    assert sibling.exists()
    assert (claude_home / "skills" / "remy-foo").exists()


def test_no_manifest_glob_fallback(claude_home):
    (claude_home / "skills" / "remy-plan").mkdir(parents=True)
    (claude_home / "skills" / "remy-plan" / "SKILL.md").write_text("x", encoding="utf-8")
    (claude_home / "skills" / "vendor-tool").mkdir(parents=True)
    (claude_home / "skills" / "vendor-tool" / "SKILL.md").write_text("y", encoding="utf-8")
    (claude_home / "hooks" / "doc_manager").mkdir(parents=True)
    (claude_home / "hooks" / "doc_manager" / "injector.py").write_text("z", encoding="utf-8")
    (claude_home / "hooks").mkdir(exist_ok=True)
    (claude_home / "hooks" / "user-hook.py").write_text("u", encoding="utf-8")
    (claude_home / "output-styles").mkdir(parents=True, exist_ok=True)
    (claude_home / "output-styles" / "custom.md").write_text("c", encoding="utf-8")

    install.cleanup_fallback(claude_home)

    assert not (claude_home / "skills" / "remy-plan").exists()
    assert (claude_home / "skills" / "vendor-tool").exists()
    assert not (claude_home / "hooks" / "doc_manager").exists()
    assert (claude_home / "hooks" / "user-hook.py").exists()
    assert (claude_home / "output-styles" / "custom.md").exists()


def test_cleanup_idempotent(claude_home):
    f = claude_home / "skills" / "remy-foo" / "SKILL.md"
    h = _seed_file(f, "x")
    old_manifest = {"files": [{"path": "skills/remy-foo/SKILL.md", "sha256": h}]}
    install.cleanup_from_manifest(old_manifest, claude_home)
    install.cleanup_from_manifest(old_manifest, claude_home)
    assert not f.exists()


def test_copy_tree_records_relative_posix(tmp_path, claude_home):
    src_root = tmp_path / "src"
    (src_root / "subdir").mkdir(parents=True)
    (src_root / "a.md").write_text("a", encoding="utf-8")
    (src_root / "subdir" / "b.md").write_text("b", encoding="utf-8")
    dst = claude_home / "skills" / "remy-test"
    records = install.copy_tree(src_root, dst, claude_home)
    paths = sorted(r["path"] for r in records)
    assert paths == ["skills/remy-test/a.md", "skills/remy-test/subdir/b.md"]
    for r in records:
        assert "\\" not in r["path"]


def test_copy_tree_preserves_third_party_in_dst(tmp_path, claude_home):
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "remy.md").write_text("remy", encoding="utf-8")
    dst = claude_home / "skills"
    dst.mkdir(parents=True)
    (dst / "vendor-tool").mkdir()
    (dst / "vendor-tool" / "SKILL.md").write_text("external", encoding="utf-8")
    records = install.copy_tree(src_root, dst, claude_home)
    record_paths = {r["path"] for r in records}
    assert "skills/remy.md" in record_paths
    assert "skills/vendor-tool/SKILL.md" not in record_paths
    assert (dst / "vendor-tool" / "SKILL.md").exists()


def test_refresh_record_hashes_after_post_copy_patch(claude_home):
    skill = claude_home / "skills" / "remy-plan" / "SKILL.md"
    original_hash = _seed_file(skill, "description: English\n")
    records = [{"path": "skills/remy-plan/SKILL.md", "sha256": original_hash}]
    skill.write_text("description: 中文\n", encoding="utf-8")

    install.refresh_record_hashes(records, claude_home)

    assert records[0]["sha256"] == install.compute_sha256(skill)
    assert records[0]["sha256"] != original_hash
    install.cleanup_from_manifest({"files": records}, claude_home)
    assert not skill.exists()
    assert not skill.with_suffix(".md.bak").exists()


def test_refresh_record_hashes_ignores_untracked_and_missing(claude_home):
    tracked = claude_home / "skills" / "remy-plan" / "SKILL.md"
    _seed_file(tracked, "tracked")
    third_party = claude_home / "skills" / "vendor" / "SKILL.md"
    _seed_file(third_party, "external")
    records = [
        {"path": "skills/remy-plan/SKILL.md", "sha256": "stale"},
        {"path": "skills/missing/SKILL.md", "sha256": "missing"},
    ]

    install.refresh_record_hashes(records, claude_home)

    assert records[0]["sha256"] == install.compute_sha256(tracked)
    assert records[1]["sha256"] == "missing"
    assert third_party.read_text(encoding="utf-8") == "external"


def test_resolve_path_rejects_traversal(claude_home):
    rel_rec = {"path": "skills/../skills/remy-plan/SKILL.md"}
    f = claude_home / "skills" / "remy-plan" / "SKILL.md"
    _seed_file(f, "x")
    resolved = install._resolve_record_path(rel_rec, claude_home)
    assert resolved.resolve() == f.resolve()


def test_e2e_upgrade_from_v143_legacy_manifest(tmp_path, claude_home):
    """v1.4.3 -> v1.4.4 upgrade replay: legacy absolute-path manifest cleanup
    removes managed files, third-party files survive, the deprecated skill is
    auto-cleaned, and fresh copy_tree records are POSIX-relative."""
    remy_plan = claude_home / "skills" / "remy-plan" / "SKILL.md"
    h_plan = _seed_file(remy_plan, "v1.4.3 remy-plan")
    remy_foo = claude_home / "skills" / "remy-foo" / "SKILL.md"
    h_foo = _seed_file(remy_foo, "v1.4.3 remy-foo (deprecated in v1.4.4)")
    hook = claude_home / "hooks" / "pre_tool_guard.py"
    h_hook = _seed_file(hook, "v1.4.3 hook")
    style = claude_home / "output-styles" / "system-architect.md"
    h_style = _seed_file(style, "v1.4.3 style")

    legacy_manifest = {
        "version": "1.4.3",
        "files": [
            {"path": str(remy_plan), "sha256": h_plan},
            {"path": str(remy_foo), "sha256": h_foo},
            {"path": str(hook), "sha256": h_hook},
            {"path": str(style), "sha256": h_style},
        ],
    }
    assert "schema_version" not in legacy_manifest

    third_party_skill = claude_home / "skills" / "vendor-tool" / "SKILL.md"
    _seed_file(third_party_skill, "external skill")
    third_party_hook = claude_home / "hooks" / "user-extension.py"
    _seed_file(third_party_hook, "external hook")

    install.cleanup_from_manifest(legacy_manifest, claude_home)

    assert not remy_plan.exists()
    assert not remy_foo.exists()
    assert not hook.exists()
    assert not style.exists()
    assert third_party_skill.exists()
    assert third_party_hook.exists()

    src_root = tmp_path / "src-v144"
    (src_root / "skills" / "remy-plan").mkdir(parents=True)
    (src_root / "skills" / "remy-plan" / "SKILL.md").write_text("v1.4.4 remy-plan", encoding="utf-8")
    (src_root / "hooks").mkdir(parents=True)
    (src_root / "hooks" / "pre_tool_guard.py").write_text("v1.4.4 hook", encoding="utf-8")

    records = []
    records.extend(install.copy_tree(src_root / "skills", claude_home / "skills", claude_home))
    records.extend(install.copy_tree(src_root / "hooks", claude_home / "hooks", claude_home))

    record_paths = {entry["path"] for entry in records}
    assert "skills/remy-plan/SKILL.md" in record_paths
    assert "hooks/pre_tool_guard.py" in record_paths
    for p in record_paths:
        assert not p.startswith("/")
        assert "\\" not in p
        assert not (len(p) >= 2 and p[1] == ":")
    assert all("vendor-tool" not in p for p in record_paths)
    assert all("user-extension" not in p for p in record_paths)

    assert third_party_skill.read_text(encoding="utf-8") == "external skill"
    assert third_party_hook.read_text(encoding="utf-8") == "external hook"
    assert not (claude_home / "skills" / "remy-foo").exists()
    assert (claude_home / "skills" / "remy-plan" / "SKILL.md").read_text(encoding="utf-8") == "v1.4.4 remy-plan"
    assert (claude_home / "hooks" / "pre_tool_guard.py").read_text(encoding="utf-8") == "v1.4.4 hook"


def test_language_md_sha256_consistency_no_spurious_bak(claude_home, monkeypatch):
    """R1 regression: language.md is written once according to _ui_lang and its sha256
    matches the manifest record byte-for-byte. A subsequent cleanup_from_manifest pass
    over that manifest must NOT produce language.md.bak."""
    monkeypatch.setattr(install, "_ui_lang", "en")
    lang_directives = {"zh-CN": "Always respond in Chinese-simplified", "en": "Always respond in English"}
    lang_md_path = claude_home / "language.md"
    lang_md_path.write_text(lang_directives[install._ui_lang] + "\n", encoding="utf-8")
    record = {
        "path": "language.md",
        "sha256": install.compute_sha256(lang_md_path),
    }
    manifest = {"files": [record]}

    install.cleanup_from_manifest(manifest, claude_home)

    bak = lang_md_path.with_suffix(lang_md_path.suffix + install.BACKUP_SUFFIX)
    assert not bak.exists()
    assert not lang_md_path.exists()


# ── daemon binary deployment (R1.3) ─────────────────────────────


@pytest.fixture
def daemon_env(tmp_path, monkeypatch):
    """Fake built daemon binary + isolated REMY_CC_HOME."""
    src_dir = tmp_path / "release"
    src_dir.mkdir()
    exe_name = "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"
    exe = src_dir / exe_name
    exe.write_bytes(b"fake-daemon-binary")
    remy_home = tmp_path / "remy-cc-home"
    monkeypatch.setenv("REMY_CC_HOME", str(remy_home))
    monkeypatch.setattr(install, "DAEMON_SOURCE_DIR", src_dir)
    return types.SimpleNamespace(exe=exe, remy_home=remy_home, exe_name=exe_name)


class _FakeDaemonRun:
    """Stands in for subprocess.run: --version returns version_rc, status
    returns status_rc (exit 0 = a daemon holds the lock)."""

    def __init__(self, version_rc=0, status_rc=1):
        self.version_rc = version_rc
        self.status_rc = status_rc
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        rc = self.version_rc if cmd[-1] == "--version" else self.status_rc
        return types.SimpleNamespace(returncode=rc, stdout=b"", stderr=b"")


def test_daemon_deploy_records_absolute_path(daemon_env, monkeypatch):
    monkeypatch.setattr(install.subprocess, "run", _FakeDaemonRun())
    records = []
    install.deploy_daemon_binary(records)

    dst = daemon_env.remy_home / "bin" / daemon_env.exe_name
    assert dst.exists()
    assert dst.read_bytes() == b"fake-daemon-binary"
    assert len(records) == 1
    assert os.path.isabs(records[0]["path"])
    assert install._resolve_record_path(records[0], daemon_env.remy_home) == dst
    assert records[0]["sha256"] == install.compute_sha256(dst)
    if os.name == "posix":
        assert os.access(dst, os.X_OK)


def test_daemon_deploy_skipped_when_daemon_running(daemon_env, monkeypatch, capsys):
    monkeypatch.setattr(install.subprocess, "run", _FakeDaemonRun(status_rc=0))
    records = []
    install.deploy_daemon_binary(records)

    assert not (daemon_env.remy_home / "bin" / daemon_env.exe_name).exists()
    assert records == []
    assert install._t("daemon_running") in capsys.readouterr().out


def test_daemon_deploy_skipped_when_version_check_fails(daemon_env, monkeypatch, capsys):
    fake = _FakeDaemonRun(version_rc=1)
    monkeypatch.setattr(install.subprocess, "run", fake)
    records = []
    install.deploy_daemon_binary(records)

    assert not (daemon_env.remy_home / "bin" / daemon_env.exe_name).exists()
    assert records == []
    assert "--version" in fake.calls[0]
    assert install._t("daemon_verify_failed", err="exit code 1") in capsys.readouterr().out


def test_daemon_deploy_skipped_when_source_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(install, "DAEMON_SOURCE_DIR", tmp_path / "absent")
    monkeypatch.setenv("REMY_CC_HOME", str(tmp_path / "remy-home"))
    records = []
    install.deploy_daemon_binary(records)

    assert records == []
    assert not (tmp_path / "remy-home").exists()
    assert install._t("daemon_src_missing") in capsys.readouterr().out


def test_daemon_deploy_rerun_overwrites(daemon_env, monkeypatch):
    monkeypatch.setattr(install.subprocess, "run", _FakeDaemonRun())
    install.deploy_daemon_binary([])

    daemon_env.exe.write_bytes(b"updated-daemon-binary")
    records = []
    install.deploy_daemon_binary(records)

    dst = daemon_env.remy_home / "bin" / daemon_env.exe_name
    assert dst.read_bytes() == b"updated-daemon-binary"
    assert len(records) == 1
    assert records[0]["sha256"] == install.compute_sha256(dst)


class _RaisingVersionRun:
    """subprocess.run stand-in whose --version call raises exc."""

    def __init__(self, exc):
        self.exc = exc

    def __call__(self, cmd, **kwargs):
        raise self.exc


class _StatusRaisesRun:
    """subprocess.run stand-in: --version succeeds, status probe raises OSError."""

    def __call__(self, cmd, **kwargs):
        if cmd[-1] == "--version":
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        raise OSError("probe failed")


def test_daemon_deploy_skipped_when_version_check_times_out(daemon_env, monkeypatch, capsys):
    exc = install.subprocess.TimeoutExpired(cmd="remy-daemon --version", timeout=install.DAEMON_PROBE_TIMEOUT)
    monkeypatch.setattr(install.subprocess, "run", _RaisingVersionRun(exc))
    records = []
    install.deploy_daemon_binary(records)

    assert records == []
    assert not (daemon_env.remy_home / "bin" / daemon_env.exe_name).exists()
    assert install._t("daemon_verify_failed", err="timeout") in capsys.readouterr().out


def test_daemon_deploy_skipped_when_binary_not_runnable(daemon_env, monkeypatch, capsys):
    monkeypatch.setattr(install.subprocess, "run", _RaisingVersionRun(OSError("exec format error")))
    records = []
    install.deploy_daemon_binary(records)

    assert records == []
    assert not (daemon_env.remy_home / "bin" / daemon_env.exe_name).exists()
    assert "exec format error" in capsys.readouterr().out


def test_daemon_deploy_proceeds_when_status_probe_errors(daemon_env, monkeypatch):
    monkeypatch.setattr(install.subprocess, "run", _StatusRaisesRun())
    records = []
    install.deploy_daemon_binary(records)

    dst = daemon_env.remy_home / "bin" / daemon_env.exe_name
    assert dst.exists()
    assert len(records) == 1


# ── reinstall scoping: cleanup vs. deploy (R1.3 follow-up) ──────


def test_cleanup_skips_records_outside_claude_home(claude_home, tmp_path):
    """Reinstall cleanup must leave the daemon binary under ~/.remy-cc/ in place;
    deploy_daemon_binary overwrites it instead of cleanup deleting and rebuilding it."""
    inside = claude_home / "hooks" / "pre_tool_guard.py"
    h_inside = _seed_file(inside, "hook")
    outside = tmp_path / "remy-cc-home" / "bin" / "remy-daemon"
    h_outside = _seed_file(outside, "daemon")

    install.cleanup_from_manifest(
        {
            "schema_version": 2,
            "files": [
                {"path": "hooks/pre_tool_guard.py", "sha256": h_inside},
                {"path": str(outside), "sha256": h_outside},
            ],
        },
        claude_home,
    )

    assert not inside.exists()
    assert outside.exists()
    assert outside.read_text(encoding="utf-8") == "daemon"


def test_cleanup_still_removes_legacy_absolute_inside_home(claude_home):
    """The _within_root guard keys on containment, not absoluteness: v1 manifests
    recorded absolute paths inside claude_home and those must still be cleaned."""
    legacy = claude_home / "skills" / "remy-plan" / "SKILL.md"
    h = _seed_file(legacy, "v1.4.3")

    install.cleanup_from_manifest({"files": [{"path": str(legacy), "sha256": h}]}, claude_home)

    assert not legacy.exists()


def test_daemon_deploy_reclaims_existing_when_source_missing(daemon_env, monkeypatch, capsys):
    """A prior deployment must stay under manifest hash claim across a run that
    cannot rebuild it, otherwise uninstall would strand the binary."""
    monkeypatch.setattr(install.subprocess, "run", _FakeDaemonRun())
    install.deploy_daemon_binary([])
    dst = daemon_env.remy_home / "bin" / daemon_env.exe_name
    deployed_hash = install.compute_sha256(dst)
    capsys.readouterr()

    daemon_env.exe.unlink()
    records = []
    install.deploy_daemon_binary(records)

    assert dst.exists()
    assert records == [{"path": str(dst), "sha256": deployed_hash}]
    assert install._t("daemon_src_missing") in capsys.readouterr().out


def test_daemon_deploy_reclaims_existing_when_daemon_running(daemon_env, monkeypatch):
    monkeypatch.setattr(install.subprocess, "run", _FakeDaemonRun())
    install.deploy_daemon_binary([])
    dst = daemon_env.remy_home / "bin" / daemon_env.exe_name
    deployed_hash = install.compute_sha256(dst)

    monkeypatch.setattr(install.subprocess, "run", _FakeDaemonRun(status_rc=0))
    daemon_env.exe.write_bytes(b"newer-daemon-binary")
    records = []
    install.deploy_daemon_binary(records)

    assert dst.read_bytes() == b"fake-daemon-binary"
    assert records == [{"path": str(dst), "sha256": deployed_hash}]


def test_daemon_deploy_survives_copy_permission_error(daemon_env, monkeypatch, capsys):
    """Windows raises PermissionError (winerror 32) when the target exe is running.
    Install must continue so the remaining transaction still runs."""
    monkeypatch.setattr(install.subprocess, "run", _FakeDaemonRun())
    install.deploy_daemon_binary([])
    dst = daemon_env.remy_home / "bin" / daemon_env.exe_name
    deployed_hash = install.compute_sha256(dst)
    capsys.readouterr()

    def _refuse(src, target):
        raise PermissionError(13, "used by another process")

    monkeypatch.setattr(install.shutil, "copy2", _refuse)
    records = []
    install.deploy_daemon_binary(records)

    assert dst.exists()
    assert records == [{"path": str(dst), "sha256": deployed_hash}]
    assert "used by another process" in capsys.readouterr().out


def test_daemon_record_survives_reinstall_without_source(daemon_env, monkeypatch, claude_home):
    """Deploy, then replay a legacy manifest cleanup with no buildable source:
    the deployed binary lives outside the claude root, so cleanup must skip it
    and a re-deploy without source must keep it recorded."""
    monkeypatch.setattr(install.subprocess, "run", _FakeDaemonRun())
    first = []
    install.deploy_daemon_binary(first)

    dst = daemon_env.remy_home / "bin" / daemon_env.exe_name
    legacy_manifest = {"version": "test", "files": first}

    daemon_env.exe.unlink()
    install.cleanup_from_manifest(legacy_manifest, claude_home)
    assert dst.exists()

    second = []
    install.deploy_daemon_binary(second)
    assert {entry["path"] for entry in second} == {str(dst)}
    assert dst.exists()


def _raise_no_home():
    raise RuntimeError("no home")


def test_remy_cc_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("REMY_CC_HOME", str(tmp_path / "custom"))
    assert install._remy_cc_home() == tmp_path / "custom"


def test_remy_cc_home_falls_back_when_path_home_raises(monkeypatch, tmp_path):
    """Mirrors the cli.py _remy_cc_home contract (tests/test_cli_daemon.py)."""
    monkeypatch.delenv("REMY_CC_HOME", raising=False)
    monkeypatch.setattr(install.Path, "home", _raise_no_home)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert install._remy_cc_home() == tmp_path / ".remy-cc"




@pytest.fixture
def v3_runtime(tmp_path):
    roots = RootPaths(tmp_path / "claude root", tmp_path / "remy root")
    return InstallRuntime(roots), roots


def _v3_template():
    return {
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
        "permissions": {"allow": ["Skill(remy-index)"]},
        "env": {},
    }


def _v3_request(tmp_path, content="payload"):
    source = tmp_path / "candidate.txt"
    source.write_text(content, encoding="utf-8")
    return InstallRequest(
        suite_version="1.7.3",
        candidates=[CandidateFile("claude", "skills/remy-test/data.txt", source, "claude_skill")],
        settings_template=_v3_template(),
        python_executable=sys.executable,
    )


def test_v3_python_install_writes_dual_root_manifest_and_runtime(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    result = runtime.install(_v3_request(tmp_path))

    assert result.exit_code == 0
    assert result.hook_mode == "python"
    manifest = json.loads((roots.remy / "install" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 3
    assert manifest["hook_mode"] == "python"
    assert {entry["root"] for entry in manifest["files"]} == {"claude", "remy"}
    assert all(not Path(entry["path"]).is_absolute() for entry in manifest["files"])
    descriptor = json.loads((roots.remy / "runtime" / "python.json").read_text(encoding="utf-8"))
    assert descriptor["schema_version"] == 1
    assert Path(descriptor["executable"]).is_absolute()
    settings = json.loads((roots.claude / "settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entries in settings["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]
    assert any(str(Path(sys.executable)) in command and "logic_enrichment_hook.py" in command for command in commands)
    assert any(str(Path(sys.executable)) in command and "logic_dirty_tracker.py" in command for command in commands)


def test_v3_repeat_install_is_content_idempotent(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    request = _v3_request(tmp_path)
    runtime.install(request)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.name.endswith("manifest.json")
    }

    runtime.install(request)

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.name.endswith("manifest.json")
    }
    assert after == before
    assert not (roots.remy / "install" / "transaction.json").exists()


def test_v3_rejects_unmanaged_different_target(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    target = roots.claude / "skills" / "remy-test" / "data.txt"
    target.parent.mkdir(parents=True)
    target.write_text("user data", encoding="utf-8")

    with pytest.raises(InstallRuntimeError, match="unmanaged target"):
        runtime.install(_v3_request(tmp_path, "candidate data"))

    assert target.read_text(encoding="utf-8") == "user data"
    assert not (roots.remy / "install" / "manifest.json").exists()


@pytest.mark.parametrize("root,path", [("project", "data.txt"), ("claude", "../project/data.txt"), ("claude", "/absolute")])
def test_v3_rejects_paths_outside_managed_roots(v3_runtime, tmp_path, root, path):
    runtime, _roots = v3_runtime
    source = tmp_path / "candidate-outside.txt"
    source.write_text("candidate", encoding="utf-8")
    request = InstallRequest(
        suite_version="1.7.3",
        candidates=[CandidateFile(root, path, source, "test")],
        settings_template=_v3_template(),
        python_executable=sys.executable,
    )

    with pytest.raises(InstallRuntimeError):
        runtime.install(request)

    assert not (tmp_path / "project" / "data.txt").exists()


def test_v3_reinstall_rejects_modified_owned_file(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    request = _v3_request(tmp_path)
    runtime.install(request)
    target = roots.claude / "skills" / "remy-test" / "data.txt"
    target.write_text("user modified", encoding="utf-8")

    with pytest.raises(InstallRuntimeError, match="missing or modified"):
        runtime.install(request)

    assert target.read_text(encoding="utf-8") == "user modified"


def test_v3_uninstall_preserves_engine_and_project_state(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    runtime.install(_v3_request(tmp_path))
    state = roots.remy / "state.db"
    state.write_bytes(b"state")
    project_db = tmp_path / "project" / ".claude" / "logic_index.db"
    project_db.parent.mkdir(parents=True)
    project_db.write_bytes(b"project")
    user_config = roots.claude / "remy-config.json"
    user_config.write_text(
        json.dumps({"schema_version": "1.0.0", "values": {"REMY_LANG": "zh-CN"}}),
        encoding="utf-8",
    )

    result = runtime.uninstall()

    assert result.exit_code == 0
    assert state.read_bytes() == b"state"
    assert project_db.read_bytes() == b"project"
    assert json.loads(user_config.read_text(encoding="utf-8"))["values"]["REMY_LANG"] == "zh-CN"
    assert not (roots.remy / "install" / "manifest.json").exists()
    assert not (roots.claude / "skills" / "remy-test" / "data.txt").exists()


def test_v3_uninstall_purge_removes_only_remy_root(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    runtime.install(_v3_request(tmp_path))
    (roots.remy / "state.db").write_bytes(b"state")
    project_db = tmp_path / "project" / ".claude" / "logic_index.db"
    project_db.parent.mkdir(parents=True)
    project_db.write_bytes(b"project")

    runtime.uninstall(purge_state=True)

    assert not roots.remy.exists()
    assert project_db.read_bytes() == b"project"


def test_v3_precommit_failure_rolls_back_all_targets(v3_runtime, tmp_path, monkeypatch):
    runtime, roots = v3_runtime
    original_apply = FileTransaction._apply
    calls = 0

    def fail_after_first(self, action):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected failure")
        return original_apply(self, action)

    monkeypatch.setattr(FileTransaction, "_apply", fail_after_first)
    with pytest.raises(InstallRuntimeError) as exc:
        runtime.install(_v3_request(tmp_path))

    assert exc.value.category == "rollback"
    assert not (roots.remy / "install" / "manifest.json").exists()
    assert not (roots.claude / "skills" / "remy-test" / "data.txt").exists()
    assert not (roots.remy / "install" / "transaction.json").exists()


def test_v3_committed_cleanup_is_recovered(v3_runtime, tmp_path, monkeypatch):
    runtime, roots = v3_runtime
    request = _v3_request(tmp_path)
    original_cleanup = FileTransaction._cleanup
    failed = False

    def fail_once(self, record):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected cleanup failure")
        return original_cleanup(self, record)

    monkeypatch.setattr(FileTransaction, "_cleanup", fail_once)
    with pytest.raises(InstallRuntimeError) as exc:
        runtime.install(request)
    assert exc.value.category == "cleanup"
    assert (roots.remy / "install" / "manifest.json").is_file()
    journal_path = roots.remy / "install" / "transaction.json"
    assert journal_path.is_file()
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert set(journal) == {
        "schema_version", "transaction_id", "operation", "phase",
        "old_manifest_hash", "new_manifest_hash", "actions",
    }
    assert set(journal["actions"][0]) == {
        "root", "path", "operation", "old_hash", "new_hash",
        "staged_path", "backup_path", "executable", "applied",
    }
    assert str(tmp_path) not in journal_path.read_text(encoding="utf-8")

    result = runtime.install(request)

    assert result.recovery == "completed_committed_cleanup"
    assert not (roots.remy / "install" / "transaction.json").exists()


def test_v3_crash_window_after_manifest_publish_recovers_as_committed(v3_runtime, tmp_path, monkeypatch):
    runtime, roots = v3_runtime
    request = _v3_request(tmp_path)
    original_write = FileTransaction._write_record
    failed = False

    def fail_committed_record(self, record):
        nonlocal failed
        if record.phase == "committed" and not failed:
            failed = True
            raise OSError("injected post-publish failure")
        return original_write(self, record)

    monkeypatch.setattr(FileTransaction, "_write_record", fail_committed_record)
    with pytest.raises(InstallRuntimeError) as exc:
        runtime.install(request)
    assert exc.value.category == "cleanup"
    journal_path = roots.remy / "install" / "transaction.json"
    assert json.loads(journal_path.read_text(encoding="utf-8"))["phase"] == "publishing_manifest"

    result = runtime.install(request)

    assert result.recovery == "completed_committed_cleanup"
    assert runtime.verify().exit_code == 0


def test_v3_uninstall_crash_after_manifest_delete_resumes_without_manifest(v3_runtime, tmp_path, monkeypatch):
    runtime, roots = v3_runtime
    runtime.install(_v3_request(tmp_path))
    original_write = FileTransaction._write_record
    failed = False

    def fail_committed_record(self, record):
        nonlocal failed
        if record.operation == "uninstall" and record.phase == "committed" and not failed:
            failed = True
            raise OSError("injected post-delete failure")
        return original_write(self, record)

    monkeypatch.setattr(FileTransaction, "_write_record", fail_committed_record)
    with pytest.raises(InstallRuntimeError) as exc:
        runtime.uninstall()
    assert exc.value.category == "cleanup"
    assert not (roots.remy / "install" / "manifest.json").exists()
    assert (roots.remy / "install" / "transaction.json").exists()

    result = runtime.uninstall()

    assert result.exit_code == 0
    assert result.recovery == "completed_committed_cleanup"
    assert not (roots.remy / "install" / "transaction.json").exists()


def test_v3_running_daemon_rejects_before_write(v3_runtime, tmp_path, monkeypatch):
    runtime, roots = v3_runtime
    monkeypatch.setattr(install_facade, "probe_daemon", lambda _path: DaemonProbe("running", "0.2.0"))

    with pytest.raises(InstallRuntimeError, match="must be stopped") as exc:
        runtime.install(_v3_request(tmp_path))

    assert "remy-cc daemon stop" in str(exc.value)
    assert not roots.claude.exists()
    assert not (roots.remy / "install" / "manifest.json").exists()


def test_v3_running_daemon_rejects_uninstall_with_stop_command(v3_runtime, tmp_path, monkeypatch):
    runtime, roots = v3_runtime
    runtime.install(_v3_request(tmp_path))
    monkeypatch.setattr(install_facade, "probe_daemon", lambda _path: DaemonProbe("running", "0.2.0"))

    with pytest.raises(InstallRuntimeError, match="must be stopped") as exc:
        runtime.uninstall()

    assert "remy-cc daemon stop" in str(exc.value)
    assert (roots.remy / "install" / "manifest.json").exists()


def test_install_entry_uninstall_requires_interactive_confirmation(v3_runtime, tmp_path, monkeypatch):
    runtime, roots = v3_runtime
    runtime.install(_v3_request(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(roots.claude))
    monkeypatch.setenv("REMY_CC_HOME", str(roots.remy))
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    install.do_uninstall_v3(types.SimpleNamespace(
        non_interactive=False,
        json=False,
        purge_state=False,
    ))

    assert (roots.remy / "install" / "manifest.json").exists()


def test_install_entry_json_uses_v3_facade(tmp_path, monkeypatch, capsys):
    claude_home = tmp_path / "用户 配置"
    remy_home = tmp_path / "引擎 状态"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("REMY_CC_HOME", str(remy_home))
    monkeypatch.setattr(install, "DAEMON_SOURCE_DIR", tmp_path / "missing-release")
    monkeypatch.setattr(install, "_prepare_dependencies", lambda _non_interactive: True)
    monkeypatch.setattr(install, "register_mcp_server", lambda _home: None)
    monkeypatch.setattr(install, "migrate_permissions", lambda _path: None)
    monkeypatch.setattr(install, "_ui_lang", "zh-CN")
    args = types.SimpleNamespace(non_interactive=True, json=True)

    install.do_install_v3(args)

    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    result = json.loads(output[0])
    assert result["schema_version"] == 1
    assert result["status"] == "ok"
    assert result["hook_mode"] == "python"
    assert str(tmp_path) not in output[0]
    manifest = json.loads((remy_home / "install" / "manifest.json").read_text(encoding="utf-8"))
    paths = {(item["root"], item["path"]) for item in manifest["files"]}
    assert ("claude", "remy-src/install_runtime/facade.py") in paths
    expected_shim = "bin/remy-cc.cmd" if sys.platform == "win32" else "bin/remy-cc"
    assert ("claude", expected_shim) in paths
    settings_text = (claude_home / "settings.json").read_text(encoding="utf-8")
    assert "__REMY_" not in settings_text


def test_v3_migrates_legacy_manifest_and_hook_claims(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    target = roots.claude / "skills" / "remy-test" / "data.txt"
    old_hash = _seed_file(target, "old")
    settings = {
        "hooks": {
            "PreToolUse": [{
                "matcher": "Read|Glob|Grep",
                "hooks": [{
                    "type": "command",
                    "command": 'python "{}"'.format(roots.claude / "hooks" / "logic_enrichment_hook.py"),
                }],
            }],
            "PostToolUse": [{
                "matcher": "Edit|Write",
                "hooks": [{
                    "type": "command",
                    "command": 'python "{}"'.format(roots.claude / "hooks" / "logic_dirty_tracker.py"),
                }],
            }],
        },
        "permissions": {"allow": ["Skill(remy-index)"]},
    }
    roots.claude.mkdir(parents=True, exist_ok=True)
    (roots.claude / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    legacy = {
        "version": "1.7.3",
        "schema_version": 2,
        "files": [{"path": "skills/remy-test/data.txt", "sha256": old_hash}],
        "injected_hooks": settings["hooks"],
        "injected_permissions": ["Skill(remy-index)"],
    }
    legacy_path = roots.claude / ".installer_manifest.json"
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")

    runtime.install(_v3_request(tmp_path, "new"))

    assert not legacy_path.exists()
    assert (roots.remy / "install" / "manifest.json").is_file()
    assert target.read_text(encoding="utf-8") == "new"
    migrated = json.loads((roots.claude / "settings.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entries in migrated["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]
    assert not any(command.startswith('python "') for command in commands)


def test_v3_adopts_unrecorded_legacy_cli_shim(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    shim = roots.claude / "bin" / "remy-cc.cmd"
    _seed_file(shim, "old shim")
    legacy = {
        "version": "1.7.3",
        "files": [],
        "injected_hooks": {},
        "injected_permissions": [],
    }
    roots.claude.mkdir(parents=True, exist_ok=True)
    (roots.claude / ".installer_manifest.json").write_text(json.dumps(legacy), encoding="utf-8")

    shim_source = tmp_path / "shim-candidate.cmd"
    shim_source.write_text("new shim", encoding="utf-8")
    request = InstallRequest(
        suite_version="1.7.3",
        candidates=[
            CandidateFile("claude", "skills/remy-test/data.txt", tmp_path / "candidate.txt", "claude_skill"),
            CandidateFile("claude", "bin/remy-cc.cmd", shim_source, "claude_protocol"),
        ],
        settings_template=_v3_template(),
        python_executable=sys.executable,
    )
    (tmp_path / "candidate.txt").write_text("payload", encoding="utf-8")

    result = runtime.install(request)

    assert result.exit_code == 0
    assert shim.read_text(encoding="utf-8") == "new shim"
    manifest = json.loads((roots.remy / "install" / "manifest.json").read_text(encoding="utf-8"))
    assert ("claude", "bin/remy-cc.cmd") in {(f["root"], f["path"]) for f in manifest["files"]}


def test_v3_corrupt_manifest_rejects_without_writes(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    manifest = roots.remy / "install" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{invalid", encoding="utf-8")

    with pytest.raises(InstallRuntimeError):
        runtime.install(_v3_request(tmp_path))

    assert manifest.read_text(encoding="utf-8") == "{invalid"
    assert not roots.claude.exists()


def test_v3_corrupt_transaction_rejects_without_writes(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    journal = roots.remy / "install" / "transaction.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{invalid", encoding="utf-8")

    with pytest.raises(InstallRuntimeError):
        runtime.install(_v3_request(tmp_path))

    assert journal.read_text(encoding="utf-8") == "{invalid"
    assert not roots.claude.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document.update(phase="unknown"),
        lambda document: document.update(operation="unknown"),
        lambda document: document["actions"].append({
            "root": "project",
            "path": "../data",
            "operation": "delete",
            "old_hash": None,
            "new_hash": None,
            "staged_path": None,
            "backup_path": "backup",
            "executable": False,
            "applied": False,
        }),
    ],
)
def test_v3_transaction_rejects_invalid_state_fields(v3_runtime, tmp_path, mutation):
    runtime, roots = v3_runtime
    journal = roots.remy / "install" / "transaction.json"
    journal.parent.mkdir(parents=True)
    document = {
        "schema_version": 1,
        "transaction_id": "test",
        "operation": "install",
        "phase": "prepared",
        "old_manifest_hash": None,
        "new_manifest_hash": None,
        "actions": [],
    }
    mutation(document)
    original = json.dumps(document, sort_keys=True)
    journal.write_text(original, encoding="utf-8")

    with pytest.raises(InstallRuntimeError):
        runtime.install(_v3_request(tmp_path))

    assert journal.read_text(encoding="utf-8") == original
    assert not roots.claude.exists()


def test_v3_modified_settings_claim_rejects_reinstall(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    request = _v3_request(tmp_path)
    runtime.install(request)
    settings_path = roots.claude / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] += " --user-change"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(InstallRuntimeError, match="settings Hook"):
        runtime.install(request)


def test_v3_structurally_invalid_settings_are_rejected(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    request = _v3_request(tmp_path)
    runtime.install(request)
    settings_path = roots.claude / "settings.json"
    settings_path.write_text(json.dumps({"hooks": []}), encoding="utf-8")

    assert runtime.verify().exit_code == 1
    with pytest.raises(InstallRuntimeError, match="settings hooks"):
        runtime.install(request)


def test_v3_runtime_descriptor_rejects_boolean_version(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    runtime.install(_v3_request(tmp_path))
    descriptor_path = roots.remy / "runtime" / "python.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor["version"] = [True, 10, 0]
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    manifest_path = roots.remy / "install" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for record in manifest["files"]:
        if record["root"] == "remy" and record["path"] == "runtime/python.json":
            record["sha256"] = install.compute_sha256(descriptor_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert runtime.verify().exit_code == 1


def test_v3_unknown_daemon_state_rejects_before_write(v3_runtime, tmp_path, monkeypatch):
    runtime, roots = v3_runtime
    monkeypatch.setattr(install_facade, "probe_daemon", lambda _path: DaemonProbe("unknown"))

    with pytest.raises(InstallRuntimeError, match="must be stopped") as exc:
        runtime.install(_v3_request(tmp_path))

    assert "remy-cc daemon stop" in str(exc.value)
    assert not roots.claude.exists()


def test_v3_missing_binary_with_endpoint_residue_is_unknown(tmp_path):
    executable = tmp_path / "remy" / "bin" / "remy-daemon.exe"
    run_dir = executable.parent.parent / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "daemon.port").write_text("1234", encoding="ascii")

    assert probe_daemon(executable).state == "unknown"


def test_v3_missing_binary_with_unheld_lock_is_stopped(tmp_path):
    executable = tmp_path / "remy" / "bin" / "remy-daemon.exe"
    run_dir = executable.parent.parent / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "daemon.lock").write_bytes(b"")

    assert probe_daemon(executable).state == "stopped"


def test_v3_same_version_different_daemon_hash_is_rejected(v3_runtime, tmp_path, monkeypatch):
    runtime, roots = v3_runtime
    deployed = roots.remy / "bin" / ("remy-daemon.exe" if sys.platform == "win32" else "remy-daemon")
    deployed.parent.mkdir(parents=True)
    deployed.write_bytes(b"deployed")
    candidate = tmp_path / deployed.name
    candidate.write_bytes(b"candidate")
    monkeypatch.setattr(install_facade, "probe_daemon", lambda _path: DaemonProbe("stopped"))
    monkeypatch.setattr(install_facade, "probe_daemon_version", lambda _path: "0.2.0")
    request = _v3_request(tmp_path)
    request = InstallRequest(
        suite_version=request.suite_version,
        candidates=request.candidates,
        settings_template=request.settings_template,
        python_executable=request.python_executable,
        daemon_candidate=candidate,
    )

    with pytest.raises(InstallRuntimeError, match="different hashes"):
        runtime.install(request)

    assert deployed.read_bytes() == b"deployed"


@pytest.mark.parametrize(
    ("category", "exit_code", "status"),
    [
        ("preflight", 1, "preflight_rejected"),
        ("rollback", 2, "rolled_back"),
        ("cleanup", 3, "committed_cleanup_pending"),
        ("recovery", 4, "recovery_incomplete"),
    ],
)
def test_v3_error_categories_have_stable_machine_results(category, exit_code, status):
    result = result_for_error("install", InstallRuntimeError("redacted", category=category))

    assert result.exit_code == exit_code
    assert result.status == status
    assert result.to_dict()["warnings"] == ["redacted"]


def test_v3_uninstall_preserves_preexisting_permission(v3_runtime, tmp_path):
    runtime, roots = v3_runtime
    settings_path = roots.claude / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"permissions": {"allow": ["Skill(remy-index)"]}}),
        encoding="utf-8",
    )

    runtime.install(_v3_request(tmp_path))
    manifest = runtime.load_manifest()
    assert manifest["settings_claim"]["permissions"] == []

    runtime.uninstall()

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["permissions"]["allow"] == ["Skill(remy-index)"]


def test_install_candidates_exclude_python_cache_files(tmp_path):
    install_runtime, _ = install._load_install_runtime_module()
    roots = RootPaths(tmp_path / "claude", tmp_path / "remy")

    candidates = install._build_install_candidates(
        tmp_path / "stage", "en", install_runtime, roots
    )

    paths = [candidate.path for candidate in candidates]
    assert not any("__pycache__" in path for path in paths)
    assert not any(path.endswith((".pyc", ".pyo")) for path in paths)


def test_install_candidates_exclude_claude_dir_and_db_artifacts(tmp_path):
    install_runtime, _ = install._load_install_runtime_module()
    roots = RootPaths(tmp_path / "claude", tmp_path / "remy")

    stage = tmp_path / "stage"
    skill_claude = stage / "skills" / "remy-index" / ".claude"
    skill_claude.mkdir(parents=True)
    (skill_claude / "logic_index_dirty").write_text("", encoding="utf-8")
    (skill_claude / "logic_index_dirty.lock").write_text("", encoding="utf-8")
    (stage / "skills" / "remy-index" / "test.db").write_text("", encoding="utf-8")
    (stage / "skills" / "remy-index" / "test.db-wal").write_text("", encoding="utf-8")
    (stage / "skills" / "remy-index" / "test.db-shm").write_text("", encoding="utf-8")
    (stage / "skills" / "remy-index" / "test.lock").write_text("", encoding="utf-8")

    candidates = install._build_install_candidates(stage, "en", install_runtime, roots)

    paths = [candidate.path for candidate in candidates]
    assert not any(".claude" in path for path in paths)
    assert not any(path.endswith(".db") for path in paths)
    assert not any(path.endswith(".db-wal") for path in paths)
    assert not any(path.endswith(".db-shm") for path in paths)
    assert not any(path.endswith(".lock") for path in paths)


def test_mcp_registration_points_to_daemon_binary(tmp_path, monkeypatch):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    monkeypatch.setenv("REMY_CC_HOME", str(tmp_path / ".remy-cc"))

    install.register_mcp_server(claude_home)

    document = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    entry = document["mcpServers"]["remy-index"]
    expected = str(tmp_path / ".remy-cc" / "bin" / install._daemon_exe_name()).replace("\\", "/")
    assert entry["command"] == expected
    assert entry["args"] == ["mcp"]
    assert "~" not in entry["command"]


def test_mcp_registration_rejects_corrupt_user_document(tmp_path):
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{invalid", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid"):
        install.register_mcp_server(claude_home)

    assert claude_json.read_text(encoding="utf-8") == "{invalid"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_v3_settings_file_preserves_private_mode(v3_runtime, tmp_path):
    runtime, roots = v3_runtime

    runtime.install(_v3_request(tmp_path))

    assert roots.claude.joinpath("settings.json").stat().st_mode & 0o777 == 0o600


class _LegacyDaemonRun:
    """Stands in for subprocess.run against a daemon binary that predates
    `status --json`: the flag is rejected with exit 2 and usage text, plain
    `status` returns plain_rc, and `--version` prints a version line."""

    def __init__(self, plain_rc):
        self.plain_rc = plain_rc
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[-1] == "--json":
            return types.SimpleNamespace(
                returncode=2, stdout="", stderr="error: unexpected argument '--json'"
            )
        if cmd[-1] == "--version":
            return types.SimpleNamespace(returncode=0, stdout="remy-daemon 0.1.0\n", stderr="")
        return types.SimpleNamespace(returncode=self.plain_rc, stdout="", stderr="")


def _legacy_probe(tmp_path, monkeypatch, plain_rc):
    executable = tmp_path / "remy-daemon.exe"
    executable.write_bytes(b"legacy-daemon")
    fake = _LegacyDaemonRun(plain_rc)
    monkeypatch.setattr(probes_module.subprocess, "run", fake)
    return probe_daemon(executable), fake


def test_probe_daemon_legacy_binary_running_via_plain_fallback(tmp_path, monkeypatch):
    probe, fake = _legacy_probe(tmp_path, monkeypatch, plain_rc=0)
    assert probe.state == "running"
    assert probe.version == "0.1.0"
    assert fake.calls[0][-1] == "--json"
    assert fake.calls[1][-1] == "status"


def test_probe_daemon_legacy_binary_stopped_via_plain_fallback(tmp_path, monkeypatch):
    probe, _fake = _legacy_probe(tmp_path, monkeypatch, plain_rc=1)
    assert probe.state == "stopped"


def test_probe_daemon_legacy_binary_odd_exit_stays_unknown(tmp_path, monkeypatch):
    probe, _fake = _legacy_probe(tmp_path, monkeypatch, plain_rc=3)
    assert probe.state == "unknown"


def test_probe_daemon_non_json_without_exit_two_stays_unknown(tmp_path, monkeypatch):
    executable = tmp_path / "remy-daemon.exe"
    executable.write_bytes(b"broken-daemon")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(probes_module.subprocess, "run", fake_run)
    assert probe_daemon(executable).state == "unknown"
    assert len(calls) == 1


def _write_lang_config(claude_home, lang):
    (claude_home / "remy-config.json").write_text(
        json.dumps({"schema_version": "1.0.0", "values": {"REMY_LANG": lang}}),
        encoding="utf-8",
    )


def test_existing_config_lang_reads_deployed_value(claude_home, monkeypatch):
    monkeypatch.setattr(install, "get_claude_home", lambda: claude_home)
    _write_lang_config(claude_home, "zh-CN")
    assert install.existing_config_lang() == "zh-CN"


def test_existing_config_lang_missing_or_corrupt_falls_back_en(claude_home, monkeypatch):
    monkeypatch.setattr(install, "get_claude_home", lambda: claude_home)
    assert install.existing_config_lang() == "en"
    (claude_home / "remy-config.json").write_text("{invalid", encoding="utf-8")
    assert install.existing_config_lang() == "en"
    _write_lang_config(claude_home, "fr")
    assert install.existing_config_lang() == "en"


def test_non_interactive_install_preserves_language(claude_home, monkeypatch):
    monkeypatch.setattr(install, "get_claude_home", lambda: claude_home)
    monkeypatch.setattr(install, "_ui_lang", install._ui_lang)
    _write_lang_config(claude_home, "zh-CN")
    captured = []
    monkeypatch.setattr(install, "do_install_v3", lambda args: captured.append(install._ui_lang))
    monkeypatch.setattr(sys, "argv", ["install.py", "--non-interactive"])
    install.main()
    assert captured == ["zh-CN"]


def test_non_interactive_install_explicit_lang_overrides_config(claude_home, monkeypatch):
    monkeypatch.setattr(install, "get_claude_home", lambda: claude_home)
    monkeypatch.setattr(install, "_ui_lang", install._ui_lang)
    _write_lang_config(claude_home, "zh-CN")
    captured = []
    monkeypatch.setattr(install, "do_install_v3", lambda args: captured.append(install._ui_lang))
    monkeypatch.setattr(sys, "argv", ["install.py", "--non-interactive", "--lang", "en"])
    install.main()
    assert captured == ["en"]


def test_interactive_install_non_tty_stdin_preserves_language(claude_home, monkeypatch):
    monkeypatch.setattr(install, "get_claude_home", lambda: claude_home)
    monkeypatch.setattr(install, "_ui_lang", install._ui_lang)
    _write_lang_config(claude_home, "zh-CN")
    captured = []
    monkeypatch.setattr(install, "do_install_v3", lambda args: captured.append(install._ui_lang))
    monkeypatch.setattr(
        install, "prompt_language",
        lambda: pytest.fail("prompt_language must not be called without a tty"),
    )
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(sys, "argv", ["install.py"])
    install.main()
    assert captured == ["zh-CN"]


def test_prompt_language_eof_falls_back_to_existing_config(claude_home, monkeypatch):
    monkeypatch.setattr(install, "get_claude_home", lambda: claude_home)
    _write_lang_config(claude_home, "zh-CN")

    def raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert install.prompt_language() == "zh-CN"
