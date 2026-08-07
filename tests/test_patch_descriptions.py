"""Tests for remy-src/patch_descriptions.py (SKILL.md frontmatter description patching).

``patch()`` is exercised in-process against a synthetic ``claude_home`` tree.
The unchanged-line short circuit is proven with a read-only target file: a
write attempt would raise ``PermissionError``.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "remy-src" / "patch_descriptions.py"
spec = importlib.util.spec_from_file_location("patch_descriptions_tested", MODULE_PATH)
assert spec and spec.loader
pd = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pd
spec.loader.exec_module(pd)

FRONTMATTER = ["---\n", "name: demo\n", "description: old text\n", "---\n", "# Body\n"]


def _make_home(tmp_path, descriptions, skill_lines=None, skill_name="demo"):
    home = tmp_path / "claude_home"
    skills = home / "skills"
    skills.mkdir(parents=True)
    if descriptions is not None:
        payload = descriptions if isinstance(descriptions, str) else json.dumps(descriptions)
        (skills / "skill_descriptions.json").write_text(payload, encoding="utf-8")
    if skill_lines is not None:
        d = skills / skill_name
        d.mkdir()
        (d / "SKILL.md").write_text("".join(skill_lines), encoding="utf-8")
    return home


def _skill_md(home, skill_name="demo"):
    return home / "skills" / skill_name / "SKILL.md"


class TestRewrite:
    def test_description_line_is_rewritten_and_body_preserved(self, tmp_path):
        home = _make_home(tmp_path, {"demo": {"en": "new text"}}, FRONTMATTER)
        pd.patch(home, "en")
        content = _skill_md(home).read_text(encoding="utf-8")
        assert "description: new text\n" in content
        assert "old text" not in content
        assert "# Body\n" in content

    def test_description_beyond_the_window_is_untouched(self, tmp_path):
        lines = ["---\n"] + [f"k{i}: v\n" for i in range(pd.MAX_FRONTMATTER_LINES - 1)] \
            + ["description: old text\n", "---\n"]
        home = _make_home(tmp_path, {"demo": {"en": "new text"}}, lines)
        pd.patch(home, "en")
        content = _skill_md(home).read_text(encoding="utf-8")
        assert "old text" in content
        assert "new text" not in content

    def test_missing_skill_md_is_skipped(self, tmp_path):
        home = _make_home(tmp_path, {"ghost": {"en": "text"}})
        pd.patch(home, "en")


class TestLanguageFallback:
    def test_missing_lang_falls_back_to_en(self, tmp_path):
        home = _make_home(tmp_path, {"demo": {"en": "english text"}}, FRONTMATTER)
        pd.patch(home, "zh-CN")
        assert "description: english text\n" in _skill_md(home).read_text(encoding="utf-8")

    def test_requested_lang_wins_over_en(self, tmp_path):
        home = _make_home(
            tmp_path, {"demo": {"zh-CN": "中文描述", "en": "english text"}}, FRONTMATTER
        )
        pd.patch(home, "zh-CN")
        assert "description: 中文描述\n" in _skill_md(home).read_text(encoding="utf-8")

    def test_no_usable_language_leaves_the_file_untouched(self, tmp_path):
        home = _make_home(tmp_path, {"demo": {"fr": "texte"}}, FRONTMATTER)
        pd.patch(home, "zh-CN")
        assert "description: old text\n" in _skill_md(home).read_text(encoding="utf-8")


class TestShortCircuit:
    def test_identical_description_does_not_write_the_file(self, tmp_path):
        home = _make_home(
            tmp_path, {"demo": {"en": "same text"}},
            ["---\n", "description: same text\n", "---\n"],
        )
        target = _skill_md(home)
        os.chmod(target, 0o444)
        try:
            pd.patch(home, "en")
        finally:
            os.chmod(target, 0o666)
        assert "description: same text\n" in target.read_text(encoding="utf-8")


class TestWarnings:
    def test_missing_descriptions_file_warns_on_stderr(self, tmp_path, capsys):
        home = _make_home(tmp_path, None, FRONTMATTER)
        pd.patch(home, "en")
        assert "not found" in capsys.readouterr().err
        assert "description: old text\n" in _skill_md(home).read_text(encoding="utf-8")

    def test_malformed_descriptions_json_warns_on_stderr(self, tmp_path, capsys):
        home = _make_home(tmp_path, "{not json", FRONTMATTER)
        pd.patch(home, "en")
        assert "failed to read" in capsys.readouterr().err
        assert "description: old text\n" in _skill_md(home).read_text(encoding="utf-8")
