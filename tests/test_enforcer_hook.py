"""Behavioural tests for hooks/env_system/enforcer_hook.py (UserPromptSubmit reminder).

The hook is copied into a temporary directory so the reminder file set is fully
controlled (the repository copy always ships both languages). The subprocess
environment points ``HOME``/``USERPROFILE`` at a temporary directory and
supplies ``remy-src`` through ``PYTHONPATH``, so no test reads the developer's
real ``remy-config.json``.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_PATH = (
    Path(__file__).resolve().parent.parent / "hooks" / "env_system" / "enforcer_hook.py"
)
REMY_SRC = Path(__file__).resolve().parent.parent / "remy-src"

DEFAULT_TEXT = "Reminder prompt files missing. Please reinstall the suite."


@pytest.fixture()
def hook_dir(tmp_path):
    d = tmp_path / "hook"
    d.mkdir()
    shutil.copy(HOOK_PATH, d / "enforcer_hook.py")
    return d


def _run_hook(hook_dir, tmp_path, lang):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["PYTHONPATH"] = str(REMY_SRC)
    env.pop("REMY_LANG", None)
    if lang is not None:
        env["REMY_LANG"] = lang
    return subprocess.run(
        [sys.executable, str(hook_dir / "enforcer_hook.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _write_reminders(hook_dir, zh=None, en=None):
    if zh is not None:
        (hook_dir / "reminder_prompt_zh.md").write_text(zh, encoding="utf-8")
    if en is not None:
        (hook_dir / "reminder_prompt_en.md").write_text(en, encoding="utf-8")


class TestLanguageSelection:
    def test_zh_config_selects_the_zh_file(self, hook_dir, tmp_path):
        _write_reminders(hook_dir, zh="ZH_SENTINEL", en="EN_SENTINEL")
        proc = _run_hook(hook_dir, tmp_path, "zh-CN")
        assert proc.returncode == 0
        assert proc.stdout == "ZH_SENTINEL"

    def test_en_config_selects_the_en_file(self, hook_dir, tmp_path):
        _write_reminders(hook_dir, zh="ZH_SENTINEL", en="EN_SENTINEL")
        proc = _run_hook(hook_dir, tmp_path, "en")
        assert proc.returncode == 0
        assert proc.stdout == "EN_SENTINEL"

    def test_unset_language_defaults_to_en(self, hook_dir, tmp_path):
        _write_reminders(hook_dir, zh="ZH_SENTINEL", en="EN_SENTINEL")
        proc = _run_hook(hook_dir, tmp_path, None)
        assert proc.returncode == 0
        assert proc.stdout == "EN_SENTINEL"


class TestFallbackOrder:
    def test_missing_primary_falls_back_to_the_other_language(self, hook_dir, tmp_path):
        _write_reminders(hook_dir, zh="ZH_SENTINEL")
        proc = _run_hook(hook_dir, tmp_path, "en")
        assert proc.returncode == 0
        assert proc.stdout == "ZH_SENTINEL"

    def test_missing_both_files_returns_the_default_string(self, hook_dir, tmp_path):
        proc = _run_hook(hook_dir, tmp_path, "en")
        assert proc.returncode == 0
        assert proc.stdout == DEFAULT_TEXT

    def test_reminder_text_is_stripped(self, hook_dir, tmp_path):
        _write_reminders(hook_dir, en="  EN_SENTINEL  \n\n")
        proc = _run_hook(hook_dir, tmp_path, "en")
        assert proc.returncode == 0
        assert proc.stdout == "EN_SENTINEL"
