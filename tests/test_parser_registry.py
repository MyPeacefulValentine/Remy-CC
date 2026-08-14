"""Tests for ParserRegistry validation, resolution, and immutability."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))

from parsers.base import LanguageParser, ParserCacheIdentity
from parsers.registry import ParserRegistry
from parsers import build_default_registry, PythonParser, CCppParser, TSParser, RustParser


class _FakeParser(LanguageParser):
    language_id = "FakeParser"

    def get_extensions(self):
        return [".fake"]

    def parse_symbols(self, source, file_path):
        return []

    def resolve_imports(self, source, file_path, root_dir):
        return {}

    def get_prompt_template_path(self):
        return ""

    def cache_identity(self, source, file_path):
        return ParserCacheIdentity.create("1", "fake")

    def cache_identity_candidates(self, file_path):
        return (self.cache_identity("", file_path),)


class _NoIdParser(_FakeParser):
    language_id = ""


class _DuplicateIdParser(_FakeParser):
    language_id = "FakeParser"

    def get_extensions(self):
        return [".fake2"]


class _BadExtParser(_FakeParser):
    language_id = "BadExtParser"

    def get_extensions(self):
        return ["noprefix"]


class _DuplicateExtParser(_FakeParser):
    language_id = "DupExtParser"

    def get_extensions(self):
        return [".fake"]


class _LongerSuffixParser(_FakeParser):
    language_id = "LongerSuffix"

    def get_extensions(self):
        return [".d.ts"]


class TestConstruction:
    def test_empty_language_id_rejected(self):
        with pytest.raises(ValueError, match="no valid language_id"):
            ParserRegistry((_NoIdParser(),))

    def test_duplicate_language_id_rejected(self):
        with pytest.raises(ValueError, match="Duplicate language_id"):
            ParserRegistry((_FakeParser(), _DuplicateIdParser()))

    def test_extension_without_dot_rejected(self):
        with pytest.raises(ValueError, match="does not start with '.'"):
            ParserRegistry((_BadExtParser(),))

    def test_duplicate_extension_rejected(self):
        with pytest.raises(ValueError, match="registered by both"):
            ParserRegistry((_FakeParser(), _DuplicateExtParser()))

    def test_partial_failure_does_not_publish(self):
        with pytest.raises(ValueError):
            ParserRegistry((_FakeParser(), _DuplicateExtParser()))


class TestResolve:
    def test_resolve_returns_correct_parser(self):
        registry = ParserRegistry((_FakeParser(),))
        assert registry.resolve("module.fake") is not None
        assert registry.resolve("module.fake").language_id == "FakeParser"

    def test_resolve_returns_none_for_unknown(self):
        registry = ParserRegistry((_FakeParser(),))
        assert registry.resolve("module.unknown") is None

    def test_resolve_longest_suffix_preferred(self):
        short = _FakeParser()
        short.language_id = "ShortTS"
        short.get_extensions = lambda: [".ts"]

        long = _LongerSuffixParser()
        registry = ParserRegistry((short, long))
        assert registry.resolve("types.d.ts") is long
        assert registry.resolve("app.ts") is short

    def test_resolve_case_sensitive(self):
        registry = ParserRegistry((_FakeParser(),))
        assert registry.resolve("MODULE.FAKE") is None

    def test_resolve_returns_same_instance(self):
        parser = _FakeParser()
        registry = ParserRegistry((parser,))
        assert registry.resolve("x.fake") is parser
        assert registry.resolve("x.fake") is registry.resolve("y.fake")


class TestAll:
    def test_all_returns_tuple(self):
        registry = ParserRegistry((_FakeParser(),))
        result = registry.all()
        assert isinstance(result, tuple)
        assert len(result) == 1

    def test_all_is_immutable(self):
        registry = ParserRegistry((_FakeParser(),))
        t = registry.all()
        with pytest.raises(TypeError):
            t[0] = None


class TestDefaultRegistry:
    def test_build_default_contains_four_parsers(self):
        registry = build_default_registry()
        assert len(registry.all()) == 4

    def test_default_registry_language_ids(self):
        registry = build_default_registry()
        ids = {p.language_id for p in registry.all()}
        assert ids == {"PythonParser", "CCppParser", "TSParser", "RustParser"}

    def test_default_resolves_py(self):
        registry = build_default_registry()
        assert isinstance(registry.resolve("main.py"), PythonParser)

    def test_default_resolves_c(self):
        registry = build_default_registry()
        assert isinstance(registry.resolve("main.c"), CCppParser)

    def test_default_resolves_ts(self):
        registry = build_default_registry()
        assert isinstance(registry.resolve("app.ts"), TSParser)

    def test_default_resolves_tsx(self):
        registry = build_default_registry()
        assert isinstance(registry.resolve("view.tsx"), TSParser)

    def test_default_resolves_rs(self):
        registry = build_default_registry()
        assert isinstance(registry.resolve("state.rs"), RustParser)

    def test_fake_parser_extends_without_modifying_consumer(self):
        fake = _FakeParser()
        registry = ParserRegistry((*build_default_registry().all(), fake))
        assert len(registry.all()) == 5
        assert registry.resolve("test.fake") is fake
        assert isinstance(registry.resolve("main.py"), PythonParser)
