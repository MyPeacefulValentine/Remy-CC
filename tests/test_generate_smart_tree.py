import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks" / "tree_system"))
from generate_smart_tree import TreeGenerator


@pytest.fixture
def tree_project(tmp_path):
    (tmp_path / ".claude").mkdir()
    config = tmp_path / ".claude" / "tree_config"
    config.write_text(". -depth 5\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "sub1").mkdir()
    (tmp_path / "src" / "sub1" / "deep").mkdir()
    (tmp_path / "src" / "sub2").mkdir()
    (tmp_path / "src" / "sub1" / "a.py").write_text("")
    (tmp_path / "src" / "sub1" / "deep" / "b.py").write_text("")
    (tmp_path / "src" / "sub2" / "c.py").write_text("")
    (tmp_path / "README.md").write_text("")
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    yield tmp_path
    os.chdir(original_cwd)


class TestBuildTreeMaxDepth:
    def test_max_depth_none_no_truncation(self, tree_project):
        gen = TreeGenerator(str(tree_project))
        result = gen.build_tree(max_depth=None)
        assert "deep/" in result
        assert "b.py" in result

    def test_max_depth_1_truncates(self, tree_project):
        gen = TreeGenerator(str(tree_project))
        result = gen.build_tree(max_depth=1)
        assert "src/" in result
        assert "sub1/" not in result
        assert "a.py" not in result

    def test_max_depth_2_shows_second_level(self, tree_project):
        gen = TreeGenerator(str(tree_project))
        result = gen.build_tree(max_depth=2)
        assert "sub1/" in result
        assert "sub2/" in result
        assert "deep/" not in result

    def test_max_depth_0_empty_tree(self, tree_project):
        gen = TreeGenerator(str(tree_project))
        result = gen.build_tree(max_depth=0)
        assert "src/" not in result
        assert "<project_tree>" in result

    def test_monotonic_depth_output(self, tree_project):
        gen = TreeGenerator(str(tree_project))
        lines_0 = gen.build_tree(max_depth=0).count("\n")
        gen2 = TreeGenerator(str(tree_project))
        lines_1 = gen2.build_tree(max_depth=1).count("\n")
        gen3 = TreeGenerator(str(tree_project))
        lines_2 = gen3.build_tree(max_depth=2).count("\n")
        assert lines_0 <= lines_1 <= lines_2
