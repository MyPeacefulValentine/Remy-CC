"""Tests for index_mcp_facts.py — symbol/file/cluster/pattern fact queries."""
import os
import sys

_REMY_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_REMY_ROOT, "remy-src"))
sys.path.insert(0, os.path.join(_REMY_ROOT, "skills", "remy-index"))


class TestResolveSymbol:
    def test_find_by_name(self, db_dir):
        from index_mcp_common import _open_db
        from index_mcp_facts import _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "main")
        db.close()
        assert len(rows) == 1
        assert rows[0][0] == "a.py"
        assert rows[0][1] == "main"

    def test_find_by_qualified(self, db_dir):
        from index_mcp_common import _open_db
        from index_mcp_facts import _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "b.py::process")
        db.close()
        assert len(rows) == 1
        assert rows[0][1] == "process"

    def test_find_by_short_name(self, db_dir):
        from index_mcp_common import _open_db
        from index_mcp_facts import _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "run")
        db.close()
        assert len(rows) == 1
        assert rows[0][1] == "Util.run"

    def test_find_with_file_filter(self, db_dir):
        from index_mcp_common import _open_db
        from index_mcp_facts import _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "main", file="a.py")
        db.close()
        assert len(rows) == 1

    def test_not_found(self, db_dir):
        from index_mcp_common import _open_db
        from index_mcp_facts import _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "nonexistent")
        db.close()
        assert len(rows) == 0


class TestQuerySymbolImpl:
    def test_returns_formatted_output(self, db_dir):
        from index_mcp_facts import query_symbol_impl
        result = query_symbol_impl("main", None)
        assert "a.py::main" in result
        assert "[function]" in result
        assert "entry point" in result

    def test_no_db_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_facts import query_symbol_impl
        result = query_symbol_impl("main", None)
        assert "not found" in result.lower() or "error" in result.lower()

    def test_no_match_returns_message(self, db_dir):
        from index_mcp_facts import query_symbol_impl
        result = query_symbol_impl("zzz_missing", None)
        assert "No symbols found" in result


class TestQueryPatternsImpl:
    def test_find_by_type(self, db_dir):
        from index_mcp_facts import query_patterns_impl
        result = query_patterns_impl("django_signal_connect", None, None)
        assert "post_save" in result
        assert "on_save" in result

    def test_find_by_signal_name(self, db_dir):
        from index_mcp_facts import query_patterns_impl
        result = query_patterns_impl(None, "post_save", None)
        assert "django_signal" in result

    def test_find_by_file(self, db_dir):
        from index_mcp_facts import query_patterns_impl
        result = query_patterns_impl(None, None, "a.py")
        assert "on_save" in result
        assert "1 results" in result

    def test_no_match(self, db_dir):
        from index_mcp_facts import query_patterns_impl
        result = query_patterns_impl("nonexistent_type", None, None)
        assert "No patterns found" in result


class TestQuerySymbolSummaryImpl:
    def test_returns_summary_text(self, db_dir):
        from index_mcp_facts import query_symbol_summary_impl
        result = query_symbol_summary_impl("process", None)
        assert "processes data" in result

    def test_no_summary_shows_placeholder(self, db_dir):
        from index_mcp_facts import query_symbol_summary_impl
        result = query_symbol_summary_impl("run", None)
        assert "no summary available" in result


class TestQueryFileSummaryImpl:
    def test_returns_file_metadata_with_placeholder(self, db_dir):
        from index_mcp_facts import query_file_summary_impl
        result = query_file_summary_impl("a.py")
        assert "## a.py" in result
        assert "2 symbols" in result
        assert "layer=Core" in result
        assert "no summary available" in result

    def test_unknown_path_returns_error(self, db_dir):
        from index_mcp_facts import query_file_summary_impl
        result = query_file_summary_impl("nonexistent.py")
        assert "No file" in result
        assert "nonexistent.py" in result

    def test_empty_path_returns_error(self, db_dir):
        from index_mcp_facts import query_file_summary_impl
        result = query_file_summary_impl("")
        assert result.startswith("Error:")

    def test_normalizes_backslash_in_path(self, db_dir):
        from index_mcp_facts import query_file_summary_impl
        result = query_file_summary_impl("dir\\sub\\nonexistent.py")
        assert "dir/sub/nonexistent.py" in result


class TestQueryClusterFilesImpl:
    def test_empty_cluster_name_returns_error(self, db_dir):
        from index_mcp_facts import query_cluster_files_impl
        result = query_cluster_files_impl("")
        assert result.startswith("Error:")

    def test_unknown_cluster_returns_error(self, db_dir):
        from index_mcp_facts import query_cluster_files_impl
        result = query_cluster_files_impl("nonexistent_cluster")
        assert "No cluster" in result
        assert "nonexistent_cluster" in result

    def test_cluster_with_no_members_returns_message(self, db_dir):
        from index_mcp_facts import query_cluster_files_impl
        result = query_cluster_files_impl("empty_cluster")
        assert "no member files" in result

    def test_lists_files_with_layer(self, db_dir):
        from index_mcp_facts import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster")
        assert "## test_cluster" in result
        assert "2 files" in result
        assert "c.py" in result
        assert "d.py" in result
        assert "layer=Core" in result
        assert "layer=Util" in result

    def test_alias_shown_in_header(self, db_dir):
        from index_mcp_facts import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster")
        assert "[alias: My Cluster]" in result

    def test_with_summary_includes_short(self, db_dir):
        from index_mcp_facts import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster", with_summary=True)
        assert "c module short" in result

    def test_with_summary_placeholder_when_missing(self, db_dir):
        from index_mcp_facts import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster", with_summary=True)
        assert "(no summary available)" in result

    def test_alphabetical_ordering(self, db_dir):
        from index_mcp_facts import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster")
        c_idx = result.index("c.py")
        d_idx = result.index("d.py")
        assert c_idx < d_idx


def _write_facts_db(tmp_path, files, symbols=(), patterns=(), clusters=()):
    import sqlite3
    from struct_scan import SCHEMA_SQL

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db = sqlite3.connect(str(claude_dir / "logic_index.db"))
    db.executescript(SCHEMA_SQL)
    for index, path in enumerate(files):
        db.execute(
            "INSERT INTO files (path, struct_hash, language, layer) "
            "VALUES (?,?,'python','Core')",
            (path, f"h{index}"),
        )
    for path, name, lineno in symbols:
        db.execute(
            "INSERT INTO symbols (file_path,name,short_name,type,lineno,end_lineno,name_tokens) "
            "VALUES (?,?,?,'function',?,?,?)",
            (path, name, name.split(".")[-1], lineno, lineno + 4, name),
        )
    for path, ptype, signal, handler, line in patterns:
        db.execute(
            "INSERT INTO patterns VALUES (NULL,?,?,?,?,?,NULL)",
            (path, ptype, signal, handler, line),
        )
    for cid, name, file_count in clusters:
        db.execute(
            "INSERT INTO clusters (id,name,label,entry_symbols,file_count) "
            "VALUES (?,?,NULL,'[]',?)",
            (cid, name, file_count),
        )
    db.commit()
    db.close()


class TestDeterministicOrdering:
    def test_symbol_output_ignores_insertion_order(self, tmp_path, monkeypatch):
        from index_mcp_facts import query_symbol_impl

        outputs = []
        for order in (("z.py", "a.py"), ("a.py", "z.py")):
            root = tmp_path / f"case_{order[0][0]}"
            root.mkdir()
            _write_facts_db(
                root, order, symbols=[(path, "shared_fn", 5) for path in order]
            )
            monkeypatch.chdir(root)
            outputs.append(query_symbol_impl("shared_fn", None))
        assert outputs[0] == outputs[1]
        assert outputs[0].index("a.py::shared_fn") < outputs[0].index("z.py::shared_fn")

    def test_patterns_output_ignores_insertion_order(self, tmp_path, monkeypatch):
        from index_mcp_facts import query_patterns_impl

        rows = [
            ("z.py", "observer_register", "sig_b", "on_b", 9),
            ("a.py", "observer_register", "sig_a", "on_a", 3),
        ]
        outputs = []
        for label, ordered in (("fwd", rows), ("rev", list(reversed(rows)))):
            root = tmp_path / label
            root.mkdir()
            _write_facts_db(root, ("a.py", "z.py"), patterns=ordered)
            monkeypatch.chdir(root)
            outputs.append(query_patterns_impl(None, None, None))
        assert outputs[0] == outputs[1]
        assert outputs[0].index("a.py:L3") < outputs[0].index("z.py:L9")

    def test_cluster_summary_ties_break_by_name(self, tmp_path, monkeypatch):
        from index_mcp_facts import query_cluster_summary_impl

        _write_facts_db(
            tmp_path, (), clusters=[(1, "zeta", 3), (2, "alpha", 3), (3, "big", 9)]
        )
        monkeypatch.chdir(tmp_path)
        result = query_cluster_summary_impl(None)
        assert result.index("## big") < result.index("## alpha")
        assert result.index("## alpha") < result.index("## zeta")


class TestFileSummaryKeySymbols:
    def test_lists_bounded_symbols_and_remainder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("REMY_MCP_RESULT_LIMIT", "10")
        _write_facts_db(
            tmp_path,
            ("wide.py",),
            symbols=[("wide.py", f"fn_{index:02d}", index * 10 + 1)
                     for index in range(12)],
        )
        monkeypatch.chdir(tmp_path)
        from index_mcp_facts import query_file_summary_impl
        result = query_file_summary_impl("wide.py")
        assert "12 symbols" in result
        section = result.split("key symbols:")[1]
        shown = [line for line in section.splitlines()
                 if line.strip().startswith("- [")]
        assert len(shown) == 10
        assert "fn_00" in shown[0]
        assert "... (+2 more)" in section

    def test_lists_all_symbols_within_limit(self, db_dir):
        from index_mcp_facts import query_file_summary_impl
        result = query_file_summary_impl("a.py")
        section = result.split("key symbols:")[1]
        assert "- [function] helper  L12-L20" in section
        assert "- [function] main  L1-L10" in section
        assert section.index("helper") < section.index("main")
        assert "more)" not in section

    def test_zero_symbol_file_reports_none(self, db_dir):
        import sqlite3
        from index_mcp_facts import query_file_summary_impl
        db = sqlite3.connect(".claude/logic_index.db")
        db.execute(
            "INSERT INTO files (path, struct_hash, language, layer) "
            "VALUES ('empty.py','he','python','Core')"
        )
        db.commit()
        db.close()
        result = query_file_summary_impl("empty.py")
        assert "0 symbols" in result
        assert "key symbols: (none)" in result
