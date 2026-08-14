"""Tests for RustParser: symbols, hash contract, call graph, imports, rejection, scan integration."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))

from struct_scan import StructScanner
from symbol_selection import DUPLICATE_DEFINITION
import parsers.rust_parser as rust_parser_module
from parsers.rust_parser import RustParser, RUST_TREE_SITTER_AVAILABLE

requires_rust_grammar = pytest.mark.skipif(
    not RUST_TREE_SITTER_AVAILABLE, reason="tree-sitter-rust not installed"
)


SUPPORT_MATRIX_SOURCE = """\
//! Crate-level docs.
use std::collections::HashMap;

/// Point docs line 1.
/// Point docs line 2.
#[derive(Debug)]
pub struct Point {
    x: i32,
}

pub enum Shape {
    Circle,
}

/** Block doc for trait. */
pub trait Drawable {
    fn draw(&self);
    fn hint(&self) -> u8 {
        0
    }
}

impl Drawable for Point {
    fn draw(&self) {
        helper();
    }
}

impl Point {
    pub fn new() -> Self {
        Point { x: 0 }
    }
}

pub type Alias = Point;

macro_rules! my_macro {
    () => {};
}

pub mod inner {
    pub fn nested_fn(v: u32) -> u32 {
        v
    }
}

pub async unsafe fn helper<T: Clone>(v: T) -> T
where
    T: Send,
{
    v
}
"""


@requires_rust_grammar
class TestRustSymbols:
    @pytest.fixture
    def symbols(self):
        return RustParser().parse_symbols(SUPPORT_MATRIX_SOURCE, "demo.rs")

    def _by_name(self, symbols):
        return {s.name: s for s in symbols}

    def test_support_matrix_types(self, symbols):
        types = {s.name: s.type for s in symbols}
        assert types["Point"] == "struct"
        assert types["Shape"] == "enum"
        assert types["Drawable"] == "interface"
        assert types["Alias"] == "type_alias"
        assert types["my_macro"] == "macro"
        assert types["inner"] == "namespace"
        assert types["helper"] == "function"

    def test_impl_method_uses_dot_qualified_name(self, symbols):
        names = {s.name for s in symbols}
        assert "Point.draw" in names
        assert "Point.new" in names
        assert self._by_name(symbols)["Point.draw"].type == "function"

    def test_trait_methods_emitted_under_trait_name(self, symbols):
        by_name = self._by_name(symbols)
        assert by_name["Drawable.draw"].type == "function"
        assert by_name["Drawable.hint"].type == "function"

    def test_inline_mod_prefixes_nested_items(self, symbols):
        assert "inner.nested_fn" in {s.name for s in symbols}

    def test_trait_impl_recorded_in_bases(self, symbols):
        assert self._by_name(symbols)["Point"].bases == ["Drawable"]

    def test_line_doc_comment_extracted(self, symbols):
        doc = self._by_name(symbols)["Point"].docstring
        assert doc == "Point docs line 1. Point docs line 2."

    def test_block_doc_comment_extracted(self, symbols):
        assert self._by_name(symbols)["Drawable"].docstring == "Block doc for trait."

    def test_signature_modifiers_preserved_in_segment(self, symbols):
        segment = self._by_name(symbols)["helper"].source_segment
        for token in ("async", "unsafe", "<T: Clone>", "where", "T: Send"):
            assert token in segment

    def test_attributes_preserved_in_segment(self, symbols):
        assert "#[derive(Debug)]" in self._by_name(symbols)["Point"].source_segment

    def test_cfg_duplicates_have_distinct_segments(self):
        source = (
            "impl Point {\n"
            "    #[cfg(unix)]\n"
            "    pub fn new() -> Self { Point { x: 0 } }\n"
            "    #[cfg(windows)]\n"
            "    pub fn new() -> Self { Point { x: 0 } }\n"
            "}\n"
        )
        parser = RustParser()
        duplicates = [s for s in parser.parse_symbols(source, "cfg.rs") if s.name == "Point.new"]
        assert len(duplicates) == 2
        segments = {parser.symbol_hash_input(s.source_segment).replace(" ", "") for s in duplicates}
        assert len(segments) == 2

    def test_unique_short_name_fallback_for_mod_scoped_impl(self):
        source = (
            "pub struct Store;\n"
            "pub trait Persist { fn save(&self); }\n"
            "mod glue {\n"
            "    impl super::Persist for super::Store {\n"
            "        fn save(&self) {}\n"
            "    }\n"
            "}\n"
        )
        symbols = RustParser().parse_symbols(source, "fallback.rs")
        store = next(s for s in symbols if s.name == "Store")
        assert store.bases == ["Persist"]


@requires_rust_grammar
class TestRustHashInput:
    def test_nested_block_comment_stripped(self):
        parser = RustParser()
        with_comment = "fn f() { /* outer /* inner */ still comment */ let x = 1; }"
        without_comment = "fn f() {  let x = 1; }"
        normalize = lambda text: "".join(text.split())
        assert normalize(parser.symbol_hash_input(with_comment)) == normalize(without_comment)

    def test_doc_comments_do_not_participate(self):
        parser = RustParser()
        documented = "/// Docs here.\nfn f() { 1; }"
        bare = "fn f() { 1; }"
        normalize = lambda text: "".join(text.split())
        assert normalize(parser.symbol_hash_input(documented)) == normalize(bare)

    def test_string_literal_with_comment_markers_preserved(self):
        parser = RustParser()
        source = 'fn f() { let y = "/* not a comment */"; }'
        assert '"/* not a comment */"' in parser.symbol_hash_input(source)

    def test_comment_free_segment_unchanged(self):
        parser = RustParser()
        source = "fn f() { let x = 1; }"
        assert parser.symbol_hash_input(source) == source


@requires_rust_grammar
class TestRustCallGraph:
    def test_call_forms_resolved_to_bare_callee(self):
        source = (
            "struct Point;\n"
            "impl Point {\n"
            "    fn draw(&self) {\n"
            "        helper();\n"
            "        self.render();\n"
            "        state::persist::<u8>();\n"
            "        println!(\"skip macros\");\n"
            "    }\n"
            "}\n"
            "fn helper() { save(); }\n"
        )
        edges = RustParser().extract_call_graph(source, "calls.rs")
        pairs = {(e.caller, e.callee) for e in edges}
        assert ("Point.draw", "helper") in pairs
        assert ("Point.draw", "render") in pairs
        assert ("Point.draw", "persist") in pairs
        assert ("helper", "save") in pairs
        forms = {e.callee: e.call_form for e in edges}
        assert forms["render"] == "attribute"
        assert forms["helper"] == "name"
        assert forms["persist"] == "name"
        assert forms["save"] == "name"
        assert not any(callee == "println" for _, callee in pairs)

    def test_mod_prefix_in_caller(self):
        source = "mod inner { fn worker() { helper(); } }\nfn helper() {}\n"
        edges = RustParser().extract_call_graph(source, "calls.rs")
        assert ("inner.worker", "helper") in {(e.caller, e.callee) for e in edges}


@requires_rust_grammar
class TestRustImports:
    @pytest.fixture
    def crate(self, tmp_path):
        src = tmp_path / "crate1" / "src"
        (src / "utils").mkdir(parents=True)
        (src / "hook_client").mkdir()
        for rel in ("main.rs", "state.rs", "hook_client.rs",
                    "hook_client/codec.rs", "utils/mod.rs", "utils/fs.rs"):
            (src / rel).write_text("", encoding="utf-8")
        return tmp_path, src

    def test_mod_declarations_and_crate_use(self, crate):
        root, src = crate
        source = (
            "mod state;\nmod hook_client;\nmod utils;\n"
            "use crate::state::save;\nuse std::collections::HashMap;\n"
        )
        imports = RustParser().resolve_imports(source, str(src / "main.rs"), str(root))
        assert set(imports) == {
            "crate1/src/state.rs", "crate1/src/hook_client.rs", "crate1/src/utils/mod.rs",
        }

    def test_self_super_and_child_module(self, crate):
        root, src = crate
        source = (
            "mod codec;\nuse crate::state::StateStore;\n"
            "use super::utils::fs;\nuse self::codec::encode;\n"
        )
        imports = RustParser().resolve_imports(source, str(src / "hook_client.rs"), str(root))
        assert set(imports) == {
            "crate1/src/hook_client/codec.rs", "crate1/src/state.rs", "crate1/src/utils/fs.rs",
        }

    def test_grouped_use_list(self, crate):
        root, src = crate
        source = "use crate::{state::save, utils::fs::read_all};\n"
        imports = RustParser().resolve_imports(source, str(src / "main.rs"), str(root))
        assert set(imports) == {"crate1/src/state.rs", "crate1/src/utils/fs.rs"}

    def test_unresolved_use_produces_no_bindings(self, crate):
        root, src = crate
        source = "use serde::Serialize;\nuse crate::missing::thing;\n"
        parser = RustParser()
        assert parser.resolve_imports(source, str(src / "main.rs"), str(root)) == {}
        assert parser.collect_import_bindings(source, str(src / "main.rs"), str(root)) == []

    def test_bare_use_without_mod_declaration_is_external(self, crate):
        root, src = crate
        (src / "log.rs").write_text("", encoding="utf-8")
        source = "use log::info;\n"
        assert RustParser().resolve_imports(source, str(src / "main.rs"), str(root)) == {}

    def test_bare_use_maps_when_mod_declared_in_same_file(self, crate):
        root, src = crate
        source = "mod utils;\nuse utils::fs::read_all;\n"
        imports = RustParser().resolve_imports(source, str(src / "main.rs"), str(root))
        assert set(imports) == {"crate1/src/utils/mod.rs", "crate1/src/utils/fs.rs"}


class TestRustRejectionSemantics:
    @pytest.fixture
    def no_grammar(self, monkeypatch):
        monkeypatch.setattr(rust_parser_module, "RUST_TREE_SITTER_AVAILABLE", False)
        return RustParser()

    def test_parse_entry_points_raise(self, no_grammar):
        with pytest.raises(RuntimeError):
            no_grammar.parse_symbols("fn f() {}", "a.rs")
        with pytest.raises(RuntimeError):
            no_grammar.resolve_imports("mod x;", "a.rs", ".")
        with pytest.raises(RuntimeError):
            no_grammar.extract_call_graph("fn f() {}", "a.rs")

    def test_cache_identity_marks_missing_backend(self, no_grammar):
        identity = no_grammar.cache_identity("fn f() {}", "a.rs")
        assert identity.backend == "rust-unavailable"
        assert no_grammar.cache_identity_candidates("a.rs") == (identity,)

    def test_symbol_hash_input_passthrough(self, no_grammar):
        segment = "fn f() { /* comment */ }"
        assert no_grammar.symbol_hash_input(segment) == segment


def _rust_project(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.rs").write_text(
        "mod state;\n\n"
        "/// Entry point.\n"
        "fn main() {\n"
        "    state::save();\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "state.rs").write_text(
        "pub trait Persist {\n"
        "    fn persist(&self);\n"
        "}\n\n"
        "pub struct Store;\n\n"
        "impl Persist for Store {\n"
        "    fn persist(&self) {\n"
        "        write_disk();\n"
        "    }\n"
        "}\n\n"
        "pub fn save() {\n"
        "    write_disk();\n"
        "}\n\n"
        "fn write_disk() {}\n",
        encoding="utf-8",
    )
    return tmp_path


def _current_state(db):
    return {
        "files": db.execute(
            "SELECT path,struct_hash,language,layer,imports,"
            "parser_contract_version,parser_backend,parser_environment,import_bindings "
            "FROM files ORDER BY path"
        ).fetchall(),
        "symbols": db.execute(
            "SELECT file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens "
            "FROM symbols ORDER BY file_path,name"
        ).fetchall(),
        "edges": db.execute(
            "SELECT source_file,caller,callee,callee_file,callee_qualified,line,provenance,via,call_form "
            "FROM edges ORDER BY source_file,caller,callee,COALESCE(via,'')"
        ).fetchall(),
    }


@requires_rust_grammar
class TestRustScanIntegration:
    def test_scan_all_indexes_rust_facts(self, tmp_path):
        project = _rust_project(tmp_path)
        scanner = StructScanner(str(project))
        try:
            scanner.scan_all()
            language = scanner.db.execute(
                "SELECT language FROM files WHERE path='src/state.rs'"
            ).fetchone()
            assert language == ("RustParser",)
            imports = scanner.db.execute(
                "SELECT imports FROM files WHERE path='src/main.rs'"
            ).fetchone()[0]
            assert "src/state.rs" in imports
            names = {r[0] for r in scanner.db.execute(
                "SELECT name FROM symbols WHERE file_path='src/state.rs'"
            )}
            assert {"Persist", "Persist.persist", "Store", "Store.persist", "save", "write_disk"} <= names
            resolved = scanner.db.execute(
                "SELECT callee_qualified, provenance FROM edges "
                "WHERE source_file='src/main.rs' AND caller='main' AND callee='save'"
            ).fetchone()
            assert resolved == ("src/state.rs::save", "definite")
            projection = scanner.db.execute(
                "SELECT COUNT(*) FROM retrieval_documents "
                "WHERE node_kind='symbol' AND node_ref LIKE 'src/state.rs::%'"
            ).fetchone()[0]
            assert projection > 0
        finally:
            scanner.db.close()

    def test_trait_impl_edge_synthesized(self, tmp_path):
        project = _rust_project(tmp_path)
        scanner = StructScanner(str(project))
        try:
            scanner.scan_all()
            row = scanner.db.execute(
                "SELECT caller, callee_qualified, provenance FROM edges WHERE via='trait-impl'"
            ).fetchone()
            assert row == ("Persist.persist", "src/state.rs::Store.persist", "inferred")
        finally:
            scanner.db.close()

    def test_full_and_incremental_scans_produce_same_projection(self, tmp_path):
        project = _rust_project(tmp_path)
        incremental = StructScanner(str(project))
        try:
            incremental.scan_all()
            (project / "src" / "state.rs").write_text(
                "pub fn save() {\n    write_disk();\n}\n\nfn write_disk() {}\n",
                encoding="utf-8",
            )
            result = incremental.scan_files(["src/state.rs"])
            assert result.status.value == "success"
            incremental_state = _current_state(incremental.db)
        finally:
            incremental.db.close()

        (project / ".claude" / "logic_index.db").unlink()
        fresh = StructScanner(str(project))
        try:
            fresh.scan_all()
            fresh_state = _current_state(fresh.db)
        finally:
            fresh.db.close()

        assert incremental_state == fresh_state

    def test_cfg_duplicates_persist_as_occurrences(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
        (tmp_path / "conditional.rs").write_text(
            "#[cfg(unix)]\npub fn pick() -> i32 { 1 }\n"
            "#[cfg(windows)]\npub fn pick() -> i32 { 2 }\n",
            encoding="utf-8",
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            rows = scanner.db.execute(
                "SELECT occurrence_index, is_canonical, conflict_kind, hash "
                "FROM symbol_occurrences WHERE file_path='conditional.rs' "
                "ORDER BY occurrence_index"
            ).fetchall()
            assert [r[2] for r in rows] == [DUPLICATE_DEFINITION, DUPLICATE_DEFINITION]
            assert sum(r[1] for r in rows) == 1
            assert rows[0][3] != rows[1][3]
        finally:
            scanner.db.close()

    def test_import_layer_method_call_downgrades_to_speculative(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "logic_index_config").write_text("!.claude/\n", encoding="utf-8")
        src = tmp_path / "src"
        src.mkdir()
        (src / "main.rs").write_text(
            "mod state;\n\nfn main() {\n    let s = state::Store;\n    s.render();\n}\n",
            encoding="utf-8",
        )
        (src / "state.rs").write_text(
            "pub struct Store;\n\nimpl Store {\n    pub fn render(&self) {}\n}\n",
            encoding="utf-8",
        )
        scanner = StructScanner(str(tmp_path))
        try:
            scanner.scan_all()
            row = scanner.db.execute(
                "SELECT callee_qualified, provenance, call_form FROM edges "
                "WHERE source_file='src/main.rs' AND caller='main' AND callee='render'"
            ).fetchone()
            assert row == ("src/state.rs::Store.render", "speculative", "attribute")
        finally:
            scanner.db.close()

    def test_grammar_loss_preserves_existing_rows(self, tmp_path, monkeypatch):
        project = _rust_project(tmp_path)
        scanner = StructScanner(str(project))
        try:
            scanner.scan_all()
            before_symbols = scanner.db.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_path='src/state.rs'"
            ).fetchone()[0]
            assert before_symbols > 0

            monkeypatch.setattr(rust_parser_module, "RUST_TREE_SITTER_AVAILABLE", False)
            (project / "src" / "state.rs").write_text(
                "pub fn save() {}\n", encoding="utf-8"
            )
            result = scanner.scan_files(["src/state.rs"])
            assert result.status.value != "success"
            assert "src/state.rs" in result.failed_paths
            after = scanner.db.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_path='src/state.rs'"
            ).fetchone()[0]
            assert after == before_symbols
        finally:
            scanner.db.close()

    def test_backend_identity_change_invalidates_path(self, tmp_path):
        project = _rust_project(tmp_path)
        scanner = StructScanner(str(project))
        try:
            scanner.scan_all()
            scanner.db.execute(
                "UPDATE files SET parser_backend='rust-unavailable', "
                "parser_environment='{}' WHERE path='src/state.rs'"
            )
            scanner.db.commit()
            assert "src/state.rs" in scanner._identity_invalid_paths()
        finally:
            scanner.db.close()
