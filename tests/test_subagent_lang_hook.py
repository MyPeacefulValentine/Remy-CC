"""Tests for subagent_lang_hook.py: language directive injection and fail-open IO."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "hooks" / "env_system" / "subagent_lang_hook.py"
spec = importlib.util.spec_from_file_location("subagent_lang_hook_tested", MODULE_PATH)
assert spec and spec.loader
hook = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hook
spec.loader.exec_module(hook)

REMY_SRC = Path(__file__).resolve().parent.parent / "remy-src"
if str(REMY_SRC) not in sys.path:
    sys.path.insert(0, str(REMY_SRC))
import remy_config


@pytest.fixture(autouse=True)
def isolated_remy_user_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(remy_config.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.delenv("REMY_LANG", raising=False)
    with remy_config._CACHE_LOCK:
        remy_config._FILE_CACHE.clear()
    yield
    with remy_config._CACHE_LOCK:
        remy_config._FILE_CACHE.clear()


def _run_hook(stdin_bytes, home, extra_env=None):
    env = dict(os.environ)
    env.pop("REMY_LANG", None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH)],
        input=stdin_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, timeout=60,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()


def _payload(cwd):
    return json.dumps({
        "hook_event_name": "SubagentStart",
        "cwd": str(cwd),
        "agent_id": "agent-test",
        "agent_type": "Explore",
    }).encode("utf-8")


class TestBuildDirective:
    def test_zh_directive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REMY_LANG", "zh-CN")
        assert "Chinese-simplified" in hook.build_directive(str(tmp_path))

    def test_default_en_directive(self, tmp_path, monkeypatch):
        monkeypatch.delenv("REMY_LANG", raising=False)
        assert "English" in hook.build_directive(str(tmp_path))

    def test_unknown_lang_falls_back_to_en(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REMY_LANG", "fr")
        assert "English" in hook.build_directive(str(tmp_path))


class TestMainIO:
    def test_emits_subagent_additional_context(self, tmp_path):
        rc, out = _run_hook(_payload(tmp_path), tmp_path / "home",
                            extra_env={"REMY_LANG": "zh-CN"})
        assert rc == 0
        decoded = json.loads(out)
        specific = decoded["hookSpecificOutput"]
        assert specific["hookEventName"] == "SubagentStart"
        assert "Chinese-simplified" in specific["additionalContext"]

    @pytest.mark.parametrize("stdin_bytes", [b"", b"not json at all"])
    def test_malformed_stdin_fails_open(self, tmp_path, stdin_bytes):
        rc, out = _run_hook(stdin_bytes, tmp_path / "home")
        assert rc == 0
        assert out == ""
