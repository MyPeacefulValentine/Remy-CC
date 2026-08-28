import importlib.util
import json
import multiprocessing
import os
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "remy-src" / "remy_config.py"
spec = importlib.util.spec_from_file_location("remy_config", MODULE_PATH)
assert spec and spec.loader
remy_config = importlib.util.module_from_spec(spec)
sys.modules["remy_config"] = remy_config
spec.loader.exec_module(remy_config)


def _write(path, values, schema="1.0.0"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": schema, "values": values}), encoding="utf-8")


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for key in remy_config.FIELD_SPECS:
        monkeypatch.delenv(key, raising=False)
    return home


def test_registry_owns_llm_defaults(config_home):
    snapshot = remy_config.load_config(strict=True)
    assert snapshot.get("REMY_LLM_MODEL") == "deepseek-v4-flash"
    assert snapshot.get("REMY_LLM_BASE_URL") == "https://api.deepseek.com/v1/chat/completions"
    assert snapshot.get_int("REMY_LLM_MAX_WORKERS") == 8
    assert snapshot.get_int("REMY_LLM_MAX_TOKENS") == 32768
    assert snapshot.get_int("REMY_LLM_RETRY_LIMIT") == 8
    assert snapshot.get_int("REMY_LLM_TIMEOUT") == 300
    assert remy_config.FIELD_SPECS["REMY_LLM_MAX_WORKERS"].minimum == 1
    assert remy_config.FIELD_SPECS["REMY_LLM_MAX_WORKERS"].maximum == 64
    assert remy_config.FIELD_SPECS["REMY_LLM_RETRY_LIMIT"].minimum == 0
    assert remy_config.FIELD_SPECS["REMY_LLM_RETRY_LIMIT"].maximum == 32
    assert remy_config.FIELD_SPECS["REMY_LLM_TIMEOUT"].minimum == 30
    assert remy_config.FIELD_SPECS["REMY_LLM_TIMEOUT"].maximum == 3600
    assert remy_config.FIELD_SPECS["REMY_LLM_MAX_TOKENS"].minimum == 1024
    assert remy_config.FIELD_SPECS["REMY_LLM_MAX_TOKENS"].maximum == 1048576


def test_registry_ui_metadata_contract(config_home):
    _ = config_home
    registry = remy_config.registry_for_ui()
    assert len(registry) == 57
    group_ids = [group["id"] for group in remy_config.GROUPS]
    assert group_ids == ["llm_api", "index_generation", "injection", "mcp", "summary", "timeline", "system"]
    counts = {group_id: 0 for group_id in group_ids}
    for row in registry:
        counts[row["group"]] += 1
    assert counts == {
        "llm_api": 8,
        "index_generation": 12,
        "injection": 8,
        "mcp": 9,
        "summary": 11,
        "timeline": 2,
        "system": 7,
    }
    for row in registry:
        assert row["label_en"] and row["label_zh"]
        assert row["restart_scope"] in remy_config.RESTART_SCOPES
        assert isinstance(row["advanced"], bool)
        assert ("unit_en" in row) == ("unit_zh" in row)
    common = sorted(row["key"] for row in registry if not row["advanced"])
    assert common == [
        "REMY_LANG",
        "REMY_LLM_API_KEY",
        "REMY_LLM_BASE_URL",
        "REMY_LLM_MAX_WORKERS",
        "REMY_LLM_MODEL",
        "REMY_LOGIC_INDEX_AUTO_INJECT",
        "REMY_MCP_SERVER_ENABLED",
        "REMY_PERMISSION_GATE",
    ]
    keys_by_group = {group_id: [] for group_id in group_ids}
    for row in registry:
        keys_by_group[row["group"]].append(row["key"])
    assert keys_by_group == {
        "llm_api": [
            "REMY_LLM_API_KEY", "REMY_LLM_BASE_URL", "REMY_LLM_MODEL",
            "REMY_LLM_MAX_WORKERS", "REMY_LLM_RETRY_LIMIT", "REMY_LLM_TIMEOUT",
            "REMY_LLM_MAX_TOKENS", "REMY_LLM_TLS_INSECURE",
        ],
        "index_generation": [
            "REMY_LOGIC_INDEX_FILTER_SMALL", "REMY_LOGIC_INDEX_DB_PATH",
            "REMY_SCAN_COMMIT_BATCH_SIZE", "REMY_CLUSTER_DENSITY_THRESHOLD",
            "REMY_CLUSTER_MAX_SIZE", "REMY_CLUSTER_ENTRY_COUNT",
            "REMY_SYNTH_INTERFACE_FANOUT_CAP", "REMY_SYNTH_EVENT_FANOUT_CAP",
            "REMY_RESOLVE_FANOUT_CAP", "REMY_RESOLVE_SCORE_SAME_FILE",
            "REMY_RESOLVE_SCORE_DIRECT_IMPORT", "REMY_RESOLVE_SCORE_GLOBAL",
        ],
        "injection": [
            "REMY_LOGIC_INDEX_AUTO_INJECT",
            "REMY_PROJECT_TREE_AUTO_INJECT", "REMY_TIMELINE_AUTO_INJECT",
            "REMY_ENRICHMENT_TIER_FULL_MAX", "REMY_ENRICHMENT_TIER_MID_MAX",
            "REMY_ENRICHMENT_CAP", "REMY_ENRICHMENT_CAP_LARGE",
            "REMY_ENRICHMENT_SIG_MAX_CHARS",
        ],
        "mcp": [
            "REMY_MCP_SERVER_ENABLED", "REMY_MCP_BFS_MAX_DEPTH",
            "REMY_MCP_RESULT_LIMIT", "REMY_MCP_STATIC_ONLY_DEFAULT",
            "REMY_FLOW_MAX_DEPTH", "REMY_FLOW_MAX_VISITED",
            "REMY_NAVIGATE_CANDIDATE_CLUSTERS", "REMY_NAVIGATE_CANDIDATE_FILES",
            "REMY_NAVIGATE_CANDIDATE_SYMBOLS",
        ],
        "summary": [
            "REMY_SUMMARY_CHAR_LIMIT_SYMBOL", "REMY_SUMMARY_CHAR_LIMIT_FILE_COHESIVE",
            "REMY_SUMMARY_CHAR_LIMIT_FILE_UTILITY", "REMY_SUMMARY_CHAR_LIMIT_CLUSTER",
            "REMY_FILE_KIND_MIN_SYMBOLS",
            "REMY_FILE_KIND_LOW_COHESION_THRESHOLD",
            "REMY_FORCE_RECOMPUTE_THRESHOLD_PRIMARY",
            "REMY_FORCE_RECOMPUTE_THRESHOLD_BACKUP",
            "REMY_FORCE_RECOMPUTE_INTERVAL_DAYS", "REMY_SUMMARY_BOOTSTRAP_MODE",
            "REMY_BOOTSTRAP_AUTO_SIZE_GUARD",
        ],
        "timeline": ["REMY_TIMELINE_INJECT_MODE", "REMY_TIMELINE_INJECT_VALUE"],
        "system": [
            "REMY_LANG", "REMY_BANNER_ENABLED", "REMY_PERMISSION_GATE",
            "REMY_REPO_AUDIT_ROOT",
            "REMY_STRUCT_SCAN_TIMEOUT", "REMY_FULL_SCAN_TIMEOUT",
            "REMY_INDEX_SCAN_LOCK_TIMEOUT",
        ],
    }
    scopes = {row["key"]: row["restart_scope"] for row in registry}
    assert scopes["REMY_MCP_SERVER_ENABLED"] == "next_mcp_launch"
    assert scopes["REMY_MCP_RESULT_LIMIT"] == "immediate"
    assert scopes["REMY_ENRICHMENT_CAP"] == "immediate"
    assert scopes["REMY_LLM_MODEL"] == "next_index"
    assert scopes["REMY_SUMMARY_BOOTSTRAP_MODE"] == "next_index"
    assert scopes["REMY_LANG"] == "next_session"
    assert scopes["REMY_LOGIC_INDEX_AUTO_INJECT"] == "next_session"
    assert scopes["REMY_PERMISSION_GATE"] == "immediate"
    assert scopes["REMY_FULL_SCAN_TIMEOUT"] == "next_session"


def test_field_spec_metadata_validation(config_home):
    _ = config_home
    with pytest.raises(ValueError):
        remy_config._field("X", None, "text", "", "system", "d", "d", label_en="X", label_zh="X", restart_scope="bogus")
    with pytest.raises(ValueError):
        remy_config._field("X", None, "text", "", "system", "d", "d", label_en="X", label_zh="X", restart_scope="immediate", unit_en="s")
    with pytest.raises(ValueError):
        remy_config._field("X", None, "text", "", "system", "d", "d", label_en="", label_zh="X", restart_scope="immediate")
    for spec in remy_config.FIELD_SPECS.values():
        assert spec.restart_scope in remy_config.RESTART_SCOPES
        assert (spec.unit_en is None) == (spec.unit_zh is None)
        assert spec.label_en and spec.label_zh


def test_precedence_environment_project_user_default(config_home, tmp_path, monkeypatch):
    project = tmp_path / "project"
    user = config_home / ".claude" / "remy-config.json"
    local = project / ".claude" / "remy-config.json"
    _write(user, {"REMY_LLM_MAX_WORKERS": "2"})
    _write(local, {"REMY_LLM_MAX_WORKERS": "3"})
    assert remy_config.load_config(project, strict=True).get_int("REMY_LLM_MAX_WORKERS") == 3
    monkeypatch.setenv("REMY_LLM_MAX_WORKERS", "4")
    snapshot = remy_config.load_config(project, strict=True)
    assert snapshot.get_int("REMY_LLM_MAX_WORKERS") == 4
    assert snapshot.source_of("REMY_LLM_MAX_WORKERS") == "environment"


def test_unknown_fields_round_trip_but_do_not_activate(config_home):
    path = remy_config.user_config_path()
    _write(path, {"FUTURE_FIELD": "x", "REMY_LANG": "zh-CN"})
    remy_config.save_config(path, {"REMY_LLM_MAX_WORKERS": "4"})
    document = remy_config.read_document(path)
    assert document["values"]["FUTURE_FIELD"] == "x"
    assert "FUTURE_FIELD" not in remy_config.load_config(strict=True).values


def test_strict_schema_and_type_validation(config_home):
    path = remy_config.user_config_path()
    _write(path, {}, schema="2.0.0")
    with pytest.raises(remy_config.ConfigError):
        remy_config.load_config(strict=True)
    _write(path, {"REMY_LLM_MAX_WORKERS": "zero"})
    with pytest.raises(remy_config.ConfigError):
        remy_config.load_config(strict=True)


def test_project_secret_rejected_strict_and_ignored_non_strict(config_home, tmp_path):
    project = tmp_path / "project"
    path = project / ".claude" / "remy-config.json"
    _write(path, {"REMY_LLM_API_KEY": "fake-secret"})
    with pytest.raises(remy_config.ConfigError):
        remy_config.load_config(project, strict=True)
    snapshot = remy_config.load_config(project, strict=False)
    assert snapshot.get("REMY_LLM_API_KEY") == ""
    assert snapshot.diagnostics


def test_secret_redaction(config_home, monkeypatch):
    monkeypatch.setenv("REMY_LLM_API_KEY", "fake-secret")
    snapshot = remy_config.load_config(strict=True)
    assert snapshot.redacted_view()["REMY_LLM_API_KEY"] == "<configured>"
    assert "fake-secret" not in json.dumps(snapshot.redacted_view())


def test_secret_save_preserve_replace_clear(config_home):
    path = remy_config.user_config_path()
    remy_config.save_config(path, {"REMY_LLM_API_KEY": "first"})
    remy_config.save_config(path, {"REMY_LANG": "zh-CN"})
    assert remy_config.read_document(path)["values"]["REMY_LLM_API_KEY"] == "first"
    remy_config.save_config(path, {"REMY_LLM_API_KEY": "second"})
    assert remy_config.read_document(path)["values"]["REMY_LLM_API_KEY"] == "second"
    remy_config.save_config(path, clear_secrets=["REMY_LLM_API_KEY"])
    assert "REMY_LLM_API_KEY" not in remy_config.read_document(path)["values"]


def test_named_resets_preserve_unknown_and_apply_secret_boundary(config_home):
    path = remy_config.user_config_path()
    original = {
        "FUTURE_FIELD": "keep",
        "REMY_LLM_API_KEY": "fake-secret",
        "REMY_LANG": "zh-CN",
        "REMY_LLM_MAX_WORKERS": "4",
    }
    _write(path, original)
    first = remy_config.reset_non_secret_values(path)
    assert first["values"] == {"FUTURE_FIELD": "keep", "REMY_LLM_API_KEY": "fake-secret"}
    second = remy_config.reset_non_secret_values(path)
    assert second == first
    cleared = remy_config.reset_known_values(path)
    assert cleared["values"] == {"FUTURE_FIELD": "keep"}
    assert remy_config.reset_known_values(path) == cleared


def test_migration_rejects_sentinel_and_only_fills_missing(config_home):
    settings = config_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"env": {
        "OPENAI_API_KEY": "PROXY_MANAGED",
        "OPENAI_MAX_WORKERS": "2",
        "OPENAI_MAX_TOKENS": "8192",
        "OTHER": "keep",
    }}), encoding="utf-8")
    config = remy_config.user_config_path()
    remy_config.save_config(config, {"REMY_LLM_MAX_WORKERS": "5"})
    result = remy_config.migrate_settings_file(settings, config)
    values = remy_config.read_document(config)["values"]
    assert values["REMY_LLM_MAX_WORKERS"] == "5"
    assert values["REMY_LLM_MAX_TOKENS"] == "8192"
    assert "REMY_LLM_API_KEY" not in values
    remaining = json.loads(settings.read_text(encoding="utf-8"))["env"]
    assert remaining == {"OTHER": "keep"}
    assert result["backup"].exists()
    backup_bytes = result["backup"].read_bytes()
    remy_config.migrate_settings_file(settings, config)
    assert result["backup"].read_bytes() == backup_bytes


def test_cc_switch_rewrite_does_not_change_effective_config(config_home):
    config = remy_config.user_config_path()
    remy_config.save_config(config, {
        "REMY_LLM_MAX_TOKENS": "32768",
        "REMY_LLM_API_KEY": "fake-secret",
    })
    before = remy_config.load_config(strict=True)
    settings = config_home / ".claude" / "settings.json"
    settings.write_text(json.dumps({"env": {
        "OPENAI_MAX_TOKENS": "8192",
        "OPENAI_API_KEY": "PROXY_MANAGED",
    }}), encoding="utf-8")
    after = remy_config.load_config(strict=True)
    assert after.get_int("REMY_LLM_MAX_TOKENS") == before.get_int("REMY_LLM_MAX_TOKENS") == 32768
    assert after.get("REMY_LLM_API_KEY") == before.get("REMY_LLM_API_KEY") == "fake-secret"


def test_project_discovery_from_descendant(config_home, tmp_path):
    project = tmp_path / "project"
    nested = project / "a" / "b"
    nested.mkdir(parents=True)
    _write(project / ".claude" / "remy-config.json", {"REMY_LANG": "zh-CN"})
    assert remy_config.discover_project_root(nested) == project
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.chdir(nested)
    try:
        assert remy_config.load_config(strict=True).get("REMY_LANG") == "zh-CN"
    finally:
        monkeypatch.undo()


def test_home_is_not_a_project_root(config_home):
    _write(config_home / ".claude" / "remy-config.json", {"REMY_LANG": "zh-CN"})
    nested = config_home / "docs" / "notes"
    nested.mkdir(parents=True)
    assert remy_config.discover_project_root(nested) is None
    assert remy_config.discover_project_root(config_home) is None


def test_project_under_home_is_still_discovered(config_home):
    project = config_home / "work" / "repo"
    nested = project / "src"
    nested.mkdir(parents=True)
    _write(project / ".claude" / "remy-config.json", {"REMY_LANG": "en"})
    assert remy_config.discover_project_root(nested) == project


def _save_worker(module_path, home, key, value, ready):
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home
    spec = importlib.util.spec_from_file_location("remy_config_worker", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    ready.wait()
    module.save_config(Path(home) / ".claude" / "remy-config.json", {key: value})


def test_concurrent_non_conflicting_updates_are_preserved(config_home):
    ready = multiprocessing.Event()
    args = (str(MODULE_PATH), str(config_home))
    first = multiprocessing.Process(target=_save_worker, args=(*args, "REMY_LANG", "zh-CN", ready))
    second = multiprocessing.Process(target=_save_worker, args=(*args, "REMY_LLM_MAX_WORKERS", "4", ready))
    first.start()
    second.start()
    ready.set()
    first.join(10)
    second.join(10)
    assert first.exitcode == second.exitcode == 0
    values = remy_config.read_document(remy_config.user_config_path())["values"]
    assert values["REMY_LANG"] == "zh-CN"
    assert values["REMY_LLM_MAX_WORKERS"] == "4"


@pytest.mark.parametrize("failure_point", ["fsync", "replace"])
def test_atomic_write_failure_preserves_old_bytes(config_home, monkeypatch, failure_point):
    path = remy_config.user_config_path()
    remy_config.save_config(path, {"REMY_LANG": "en"})
    before = path.read_bytes()
    if failure_point == "fsync":
        monkeypatch.setattr(remy_config.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync failed")))
    else:
        monkeypatch.setattr(remy_config.os, "replace", lambda _src, _dst: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError):
        remy_config.save_config(path, {"REMY_LANG": "zh-CN"})
    assert path.read_bytes() == before
    assert list(path.parent.glob(".remy-config.json.tmp.*")) == []


def test_cache_reused_until_fingerprint_changes(config_home, monkeypatch):
    path = remy_config.user_config_path()
    _write(path, {"REMY_LANG": "en"})
    calls = 0
    original = remy_config._parse_document

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(remy_config, "_parse_document", counted)
    assert remy_config.load_config(strict=False).get("REMY_LANG") == "en"
    first_calls = calls
    assert remy_config.load_config(strict=False).get("REMY_LANG") == "en"
    assert calls == first_calls
    _write(path, {"REMY_LANG": "zh-CN", "REMY_LLM_MAX_WORKERS": "4"})
    assert remy_config.load_config(strict=False).get("REMY_LANG") == "zh-CN"
    assert calls > first_calls


def test_user_config_platform_permission_contract(config_home, monkeypatch):
    path = remy_config.user_config_path()
    if sys.platform == "win32":
        chmod_calls = []
        monkeypatch.setattr(remy_config.os, "chmod", lambda *args: chmod_calls.append(args))
        remy_config.save_config(path, {"REMY_LANG": "en"})
        assert chmod_calls == []
    else:
        remy_config.save_config(path, {"REMY_LANG": "en"})
        assert path.stat().st_mode & 0o777 == 0o600
