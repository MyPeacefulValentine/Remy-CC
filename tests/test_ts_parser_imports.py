"""Regression tests for TSParser.resolve_imports ordering determinism.

Before the R3.3 fix the raw specifier collection used a set, so the
files.imports column order followed the process's string-hash
randomization (different across PYTHONHASHSEED values). Deduplication now
preserves source match order: import-from matches first, require matches
second.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from parsers.ts_parser import TSParser


@pytest.fixture
def parser():
    return TSParser()


def _write_modules(root, names):
    sub = root / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    for name in names:
        (sub / f"{name}.ts").write_text("export const x = 1;\n", encoding="utf-8")
    return sub


class TestImportOrderDeterminism:
    def test_imports_follow_source_order(self, parser, tmp_path):
        sub = _write_modules(tmp_path, ["alpha", "beta", "gamma", "delta"])
        source = (
            "import {d} from './delta';\n"
            "import {a} from './alpha';\n"
            "const g = require('./gamma');\n"
            "import {b} from './beta';\n"
        )
        imports = parser.resolve_imports(
            source, str(sub / "main.ts"), str(tmp_path)
        )
        assert list(imports.keys()) == [
            "sub/delta.ts",
            "sub/alpha.ts",
            "sub/beta.ts",
            "sub/gamma.ts",
        ]

    def test_duplicate_specifiers_resolve_once(self, parser, tmp_path):
        sub = _write_modules(tmp_path, ["alpha"])
        source = (
            "import {a} from './alpha';\n"
            "import {b} from './alpha';\n"
            "const c = require('./alpha');\n"
        )
        imports = parser.resolve_imports(
            source, str(sub / "main.ts"), str(tmp_path)
        )
        assert list(imports.keys()) == ["sub/alpha.ts"]
