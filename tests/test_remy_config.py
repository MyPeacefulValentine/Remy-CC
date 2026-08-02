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
    assert snapshot.get_int("REMY_LLM_MAX_WORKERS") == 5
    assert snapshot.get_int("REMY_LLM_MAX_TOKENS") == 32768
    assert snapshot.get_int("REMY_LLM_RETRY_LIMIT") == 3
    assert snapshot.get_int("REMY_LLM_TIMEOUT") == 300


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
