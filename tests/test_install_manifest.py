"""Unit tests for v1.4.4 install.py manifest-based cleanup and third-party isolation."""
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import install


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


def test_schema_version_persisted(claude_home):
    records = [{"path": "skills/remy-plan/SKILL.md", "sha256": "deadbeef"}]
    install.write_manifest(claude_home, records, None,
                           injected_hooks={}, injected_permissions=[])
    manifest_path = claude_home / install.MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == install.MANIFEST_SCHEMA_VERSION
    assert manifest["schema_version"] == 2
    assert manifest["files"] == records


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


def test_resolve_path_rejects_traversal(claude_home):
    rel_rec = {"path": "skills/../skills/remy-plan/SKILL.md"}
    f = claude_home / "skills" / "remy-plan" / "SKILL.md"
    _seed_file(f, "x")
    resolved = install._resolve_record_path(rel_rec, claude_home)
    assert resolved.resolve() == f.resolve()


def test_e2e_upgrade_from_v143_legacy_manifest(tmp_path, claude_home):
    """End-to-end v1.4.3 -> v1.4.4 upgrade: legacy absolute-path manifest is replayed,
    third-party files survive, deprecated skill is auto-cleaned, and the new manifest is
    written with schema_version=2 + POSIX-relative paths."""
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

    install.write_manifest(claude_home, records, None,
                           injected_hooks={}, injected_permissions=[])

    new_manifest_path = claude_home / install.MANIFEST_FILE
    assert new_manifest_path.exists()
    new_manifest = json.loads(new_manifest_path.read_text(encoding="utf-8"))

    assert new_manifest["schema_version"] == 2
    for entry in new_manifest["files"]:
        p = entry["path"]
        assert not p.startswith("/")
        assert "\\" not in p
        assert not (len(p) >= 2 and p[1] == ":")

    record_paths = {e["path"] for e in new_manifest["files"]}
    assert "skills/remy-plan/SKILL.md" in record_paths
    assert "hooks/pre_tool_guard.py" in record_paths
    assert all("vendor-tool" not in p for p in record_paths)
    assert all("user-extension" not in p for p in record_paths)

    assert third_party_skill.exists()
    assert third_party_skill.read_text(encoding="utf-8") == "external skill"
    assert third_party_hook.exists()
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


def test_uninstall_handles_missing_sha256_field(claude_home, monkeypatch):
    """R2 regression: do_uninstall must tolerate manifest entries lacking the sha256 field
    (semantic alignment with cleanup_from_manifest)."""
    f = claude_home / "skills" / "remy-plan" / "SKILL.md"
    f.parent.mkdir(parents=True)
    f.write_text("x", encoding="utf-8")
    manifest = {
        "version": "test",
        "schema_version": 2,
        "files": [{"path": "skills/remy-plan/SKILL.md"}],
        "injected_hooks": {},
        "injected_permissions": [],
    }
    (claude_home / install.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(install, "get_claude_home", lambda: claude_home)

    install.do_uninstall()

    assert not f.exists()
    assert not (claude_home / install.MANIFEST_FILE).exists()


def test_uninstall_with_sha256_match_still_removes(claude_home, monkeypatch):
    """R2 regression complement: when sha256 is present and matches, removal proceeds as before."""
    f = claude_home / "skills" / "remy-plan" / "SKILL.md"
    f.parent.mkdir(parents=True)
    f.write_text("x", encoding="utf-8")
    expected = install.compute_sha256(f)
    manifest = {
        "version": "test",
        "schema_version": 2,
        "files": [{"path": "skills/remy-plan/SKILL.md", "sha256": expected}],
        "injected_hooks": {},
        "injected_permissions": [],
    }
    (claude_home / install.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(install, "get_claude_home", lambda: claude_home)

    install.do_uninstall()

    assert not f.exists()


def test_uninstall_skips_user_modified_file(claude_home, monkeypatch):
    """R2 regression complement: user-modified file (sha256 mismatch) is preserved."""
    f = claude_home / "skills" / "remy-plan" / "SKILL.md"
    f.parent.mkdir(parents=True)
    f.write_text("original", encoding="utf-8")
    recorded_hash = install.compute_sha256(f)
    f.write_text("user-modified", encoding="utf-8")
    manifest = {
        "version": "test",
        "schema_version": 2,
        "files": [{"path": "skills/remy-plan/SKILL.md", "sha256": recorded_hash}],
        "injected_hooks": {},
        "injected_permissions": [],
    }
    (claude_home / install.MANIFEST_FILE).write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(install, "get_claude_home", lambda: claude_home)

    install.do_uninstall()

    assert f.exists()
    assert f.read_text(encoding="utf-8") == "user-modified"
