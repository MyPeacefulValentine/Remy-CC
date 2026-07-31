"""Tests for index_mcp_queries.py — MCP query implementations."""
import os
import sys
import sqlite3
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "remy-src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))

from struct_scan import SCHEMA_SQL


@pytest.fixture
def db_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db_path = claude_dir / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('a.py','h1','python','Core','[\"b.py\"]')")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('b.py','h2','python','Util',NULL)")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('a.py','main','main','function','args',1,10,NULL,NULL,'main')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('a.py','helper','helper','function','x',12,20,NULL,NULL,'helper')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('b.py','process','process','function','data',1,15,NULL,NULL,'process')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('b.py','Util.run','run','function','',17,25,NULL,NULL,'Util run')")
    _now = "2025-01-01T00:00:00"
    db.execute("INSERT INTO summary_versions (node_kind,node_ref,version,summary,status,created_at) VALUES ('symbol','a.py::main',1,'{\"short\":\"entry point\",\"full\":null}','ok',?)", (_now,))
    db.execute("INSERT INTO summary_versions (node_kind,node_ref,version,summary,status,created_at) VALUES ('symbol','a.py::helper',1,'{\"short\":\"does stuff\",\"full\":null}','ok',?)", (_now,))
    db.execute("INSERT INTO summary_versions (node_kind,node_ref,version,summary,status,created_at) VALUES ('symbol','b.py::process',1,'{\"short\":\"processes data\",\"full\":null}','ok',?)", (_now,))
    db.execute("INSERT INTO edges VALUES (NULL,'a.py','main','process','b.py','b.py::process',5,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'a.py','main','helper',NULL,'a.py::helper',3,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'a.py','helper','run','b.py','b.py::Util.run',14,'inferred',NULL,'interface-impl')")
    edge_id = db.execute("SELECT id FROM edges WHERE caller='main' AND callee='process'").fetchone()[0]
    db.execute("INSERT INTO edge_candidates VALUES (?,?,?)", (edge_id, "b.py::process", 1))
    db.execute("INSERT INTO edge_candidates VALUES (?,?,?)", (edge_id, "c.py::process", 0))
    db.execute("INSERT INTO patterns VALUES (NULL,'a.py','django_signal_connect','post_save','on_save',8,NULL)")
    db.execute("INSERT INTO patterns VALUES (NULL,'b.py','django_signal_send','post_save',NULL,3,NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('c.py','h3','python','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('d.py','h4','python','Util',NULL)")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('c.py','do_thing','do_thing','function','x',1,5,NULL,NULL,'do thing')")
    db.execute("INSERT INTO summary_versions (node_kind,node_ref,version,summary,status,created_at) VALUES ('file','c.py',1,'{\"short\":\"c module short\",\"full\":null}','ok',?)", (_now,))
    db.execute("INSERT INTO clusters (id,name,label,entry_symbols,file_count) VALUES (1,'test_cluster','My Cluster','[\"c.py::do_thing\"]',2)")
    db.execute("INSERT INTO clusters (id,name,label,entry_symbols,file_count) VALUES (2,'empty_cluster',NULL,'[]',0)")
    db.execute("INSERT INTO cluster_members (cluster_id,file_path) VALUES (1,'c.py')")
    db.execute("INSERT INTO cluster_members (cluster_id,file_path) VALUES (1,'d.py')")
    db.commit()
    db.close()
    return tmp_path


class TestOpenDb:
    def test_returns_connection_when_db_exists(self, db_dir, monkeypatch):
        from index_mcp_queries import _open_db
        db = _open_db()
        assert db is not None
        db.close()

    def test_returns_none_when_db_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import _open_db
        assert _open_db() is None


class TestResolveSymbol:
    def test_find_by_name(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "main")
        db.close()
        assert len(rows) == 1
        assert rows[0][0] == "a.py"
        assert rows[0][1] == "main"

    def test_find_by_qualified(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "b.py::process")
        db.close()
        assert len(rows) == 1
        assert rows[0][1] == "process"

    def test_find_by_short_name(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "run")
        db.close()
        assert len(rows) == 1
        assert rows[0][1] == "Util.run"

    def test_find_with_file_filter(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "main", file="a.py")
        db.close()
        assert len(rows) == 1

    def test_not_found(self, db_dir):
        from index_mcp_queries import _open_db, _resolve_symbol
        db = _open_db()
        rows = _resolve_symbol(db, "nonexistent")
        db.close()
        assert len(rows) == 0


class TestQuerySymbolImpl:
    def test_returns_formatted_output(self, db_dir):
        from index_mcp_queries import query_symbol_impl
        result = query_symbol_impl("main", None)
        assert "a.py::main" in result
        assert "[function]" in result
        assert "entry point" in result

    def test_no_db_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_symbol_impl
        result = query_symbol_impl("main", None)
        assert "not found" in result.lower() or "error" in result.lower()

    def test_no_match_returns_message(self, db_dir):
        from index_mcp_queries import query_symbol_impl
        result = query_symbol_impl("zzz_missing", None)
        assert "No symbols found" in result


class TestQueryCallersImpl:
    def test_finds_direct_callers(self, db_dir):
        from index_mcp_queries import query_callers_impl
        result = query_callers_impl("b.py::process", 2, False, False)
        assert "a.py::main" in result

    def test_depth_clamped_to_max(self, db_dir, monkeypatch):
        monkeypatch.setenv("MCP_BFS_MAX_DEPTH", "1")
        import importlib
        import index_mcp_queries
        importlib.reload(index_mcp_queries)
        result = index_mcp_queries.query_callers_impl("b.py::process", 99, False, False)
        assert "depth 1" in result or "1 levels" in result

    def test_static_only_excludes_heuristic(self, db_dir):
        from index_mcp_queries import query_callers_impl
        result = query_callers_impl("b.py::Util.run", 2, False, True)
        assert "a.py::helper" not in result

    def test_include_ambiguous_finds_candidates(self, db_dir):
        from index_mcp_queries import query_callers_impl
        result = query_callers_impl("c.py::process", 2, True, False)
        assert "a.py::main" in result

    def test_not_found_symbol(self, db_dir):
        from index_mcp_queries import query_callers_impl
        result = query_callers_impl("nonexistent", 2, False, False)
        assert "No symbols found" in result


class TestQueryCalleesImpl:
    def test_finds_direct_callees(self, db_dir):
        from index_mcp_queries import query_callees_impl
        result = query_callees_impl("a.py::main", 2, False, False)
        assert "b.py::process" in result

    def test_static_only_excludes_heuristic(self, db_dir):
        from index_mcp_queries import query_callees_impl
        result = query_callees_impl("a.py::helper", 1, False, True)
        assert "Util.run" not in result

    def test_include_ambiguous_expands_candidates(self, db_dir):
        from index_mcp_queries import query_callees_impl
        result = query_callees_impl("a.py::main", 1, True, False)
        assert "b.py::process" in result


class TestQueryImpactImpl:
    def test_finds_downstream(self, db_dir):
        from index_mcp_queries import query_impact_impl
        result = query_impact_impl(["a.py"], 0, 2, False, False)
        assert "b.py" in result

    def test_file_not_in_index(self, db_dir):
        from index_mcp_queries import query_impact_impl
        result = query_impact_impl(["nonexistent.py"], 2, 2, False, False)
        assert "No indexed files" in result

    def test_no_db(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_impact_impl
        result = query_impact_impl(["a.py"], 2, 2, False, False)
        assert "error" in result.lower() or "not found" in result.lower()


class TestQueryPatternsImpl:
    def test_find_by_type(self, db_dir):
        from index_mcp_queries import query_patterns_impl
        result = query_patterns_impl("django_signal_connect", None, None)
        assert "post_save" in result
        assert "on_save" in result

    def test_find_by_signal_name(self, db_dir):
        from index_mcp_queries import query_patterns_impl
        result = query_patterns_impl(None, "post_save", None)
        assert "django_signal" in result

    def test_find_by_file(self, db_dir):
        from index_mcp_queries import query_patterns_impl
        result = query_patterns_impl(None, None, "a.py")
        assert "on_save" in result
        assert "1 results" in result

    def test_no_match(self, db_dir):
        from index_mcp_queries import query_patterns_impl
        result = query_patterns_impl("nonexistent_type", None, None)
        assert "No patterns found" in result


class TestQuerySymbolSummaryImpl:
    def test_returns_summary_text(self, db_dir):
        from index_mcp_queries import query_symbol_summary_impl
        result = query_symbol_summary_impl("process", None)
        assert "processes data" in result

    def test_no_summary_shows_placeholder(self, db_dir):
        from index_mcp_queries import query_symbol_summary_impl
        result = query_symbol_summary_impl("run", None)
        assert "no summary available" in result


class TestQueryFileSummaryImpl:
    def test_returns_file_metadata_with_placeholder(self, db_dir):
        from index_mcp_queries import query_file_summary_impl
        result = query_file_summary_impl("a.py")
        assert "## a.py" in result
        assert "2 symbols" in result
        assert "layer=Core" in result
        assert "no summary available" in result

    def test_unknown_path_returns_error(self, db_dir):
        from index_mcp_queries import query_file_summary_impl
        result = query_file_summary_impl("nonexistent.py")
        assert "No file" in result
        assert "nonexistent.py" in result

    def test_empty_path_returns_error(self, db_dir):
        from index_mcp_queries import query_file_summary_impl
        result = query_file_summary_impl("")
        assert result.startswith("Error:")

    def test_normalizes_backslash_in_path(self, db_dir):
        from index_mcp_queries import query_file_summary_impl
        result = query_file_summary_impl("dir\\sub\\nonexistent.py")
        assert "dir/sub/nonexistent.py" in result


class TestQueryClusterFilesImpl:
    def test_empty_cluster_name_returns_error(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("")
        assert result.startswith("Error:")

    def test_unknown_cluster_returns_error(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("nonexistent_cluster")
        assert "No cluster" in result
        assert "nonexistent_cluster" in result

    def test_cluster_with_no_members_returns_message(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("empty_cluster")
        assert "no member files" in result

    def test_lists_files_with_layer(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster")
        assert "## test_cluster" in result
        assert "2 files" in result
        assert "c.py" in result
        assert "d.py" in result
        assert "layer=Core" in result
        assert "layer=Util" in result

    def test_alias_shown_in_header(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster")
        assert "[alias: My Cluster]" in result

    def test_with_summary_includes_short(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster", with_summary=True)
        assert "c module short" in result

    def test_with_summary_placeholder_when_missing(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster", with_summary=True)
        assert "(no summary available)" in result

    def test_alphabetical_ordering(self, db_dir):
        from index_mcp_queries import query_cluster_files_impl
        result = query_cluster_files_impl("test_cluster")
        c_idx = result.index("c.py")
        d_idx = result.index("d.py")
        assert c_idx < d_idx


class TestBfsChunking:
    """Verify ambiguous BFS works when current set exceeds chunk size (400)."""

    @pytest.fixture
    def large_db_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "logic_index.db"
        db = sqlite3.connect(str(db_path))
        db.executescript(SCHEMA_SQL)

        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('target.py','h0','python','Core','[]')")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('target.py','target_fn','target_fn','function',NULL,1,10,NULL,NULL,'target fn')")

        for i in range(500):
            fname = f"f{i:04d}.py"
            db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES (?,?,'python','Core','[]')", (fname, f"h{i}"))
            db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES (?,?,?,'function',NULL,1,5,NULL,NULL,?)", (fname, f"caller_{i}", f"caller_{i}", f"caller {i}"))
            db.execute(
                "INSERT INTO edges (source_file,caller,callee,callee_file,callee_qualified,line) VALUES (?,?,?,?,?,?)",
                (fname, f"caller_{i}", "target_fn", "target.py", "target.py::target_fn", 1)
            )

        db.commit()
        db.close()
        return tmp_path

    def test_ambiguous_callers_over_400(self, large_db_dir):
        from index_mcp_queries import _bfs_callers_ambiguous, _open_db
        db = _open_db()
        assert db is not None
        try:
            levels = _bfs_callers_ambiguous(db, {"target.py::target_fn"}, 1)
            assert 1 in levels
            assert len(levels[1]) == 500
        finally:
            db.close()

    def test_ambiguous_callees_over_400(self, large_db_dir):
        from index_mcp_queries import _bfs_callees_ambiguous, _open_db
        db = _open_db()
        assert db is not None
        try:
            all_callers = {f"f{i:04d}.py::caller_{i}" for i in range(500)}
            levels = _bfs_callees_ambiguous(db, all_callers, 1)
            assert 1 in levels
            assert "target.py::target_fn" in levels[1]
        finally:
            db.close()


class TestQuerySearch:
    def test_fts_prefix_match(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("proc", limit=5)
        assert "process" in result
        assert "FTS5" in result

    def test_fts_exact_name_ranked_first(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("main", limit=5)
        lines = [l for l in result.splitlines() if "a.py::main" in l]
        assert len(lines) == 1

    def test_like_fallback_on_substring(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("elpe", limit=5)
        assert "helper" in result
        assert "LIKE" in result

    def test_fuzzy_fallback_on_typo(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("processs", limit=5)
        assert "process" in result
        assert "fuzzy" in result

    def test_file_hint_filters(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("main", limit=5, file_hint="b.py")
        assert "a.py" not in result

    def test_no_match_returns_message(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("zzzznonexistent", limit=5)
        assert "No symbols found" in result

    def test_fts_not_available(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "logic_index.db"
        db = sqlite3.connect(str(db_path))
        db.execute("CREATE TABLE files (path TEXT PRIMARY KEY, struct_hash TEXT NOT NULL, language TEXT, layer TEXT DEFAULT 'Core', imports TEXT)")
        db.execute("CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_path TEXT, name TEXT, short_name TEXT, type TEXT, args TEXT, lineno INTEGER, end_lineno INTEGER, hash TEXT, summary TEXT, bases TEXT, name_tokens TEXT DEFAULT '')")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.commit()
        db.close()
        from index_mcp_queries import query_search_impl
        result = query_search_impl("test", limit=5)
        assert "FTS index not available" in result

    def test_limit_respected(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("main", limit=1)
        content_lines = [l for l in result.splitlines() if l.strip().startswith("[")]
        assert len(content_lines) <= 1

    def test_exact_name_ranks_above_prefix(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("helper", limit=5)
        lines = [l.strip() for l in result.splitlines() if l.strip().startswith("[")]
        assert len(lines) >= 1
        assert "helper" in lines[0]

    def test_multiterm_fts_query(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db_path = claude_dir / "logic_index.db"
        db = sqlite3.connect(str(db_path))
        db.executescript(SCHEMA_SQL)
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('m.py','h1','python','Core',NULL)")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('m.py','getUserById','getUserById','function',NULL,1,5,NULL,NULL,'get User By Id')")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('m.py','getItem','getItem','function',NULL,7,10,NULL,NULL,'get Item')")
        db.commit()
        db.close()
        from index_mcp_queries import query_search_impl
        result = query_search_impl("get User", limit=5)
        assert "getUserById" in result

    def test_empty_string_input(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("", limit=5)
        assert isinstance(result, str)

    def test_special_characters_input(self, db_dir):
        from index_mcp_queries import query_search_impl
        result = query_search_impl("proc*", limit=5)
        assert isinstance(result, str)
        assert "error" not in result.lower() or "No symbols" in result


@pytest.fixture
def flow_db_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db_path = claude_dir / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('src/fs/read.c','h1','c','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('src/fs/write.c','h2','c','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('src/fs/vfs.c','h3','c','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('models/resnet.py','h4','python','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('models/vgg.py','h5','python','Core',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('train.py','h6','python','Util',NULL)")
    db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('losses.py','h7','python','Util',NULL)")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('src/fs/read.c','sys_read','sys_read','function','fd,buf,count',10,50,NULL,NULL,'sys read')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('src/fs/read.c','vfs_read','vfs_read','function','file,buf,count',55,100,NULL,NULL,'vfs read')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('src/fs/vfs.c','new_sync_read','new_sync_read','function','filp,buf',5,30,NULL,NULL,'new sync read')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('src/fs/write.c','sys_write','sys_write','function','fd,buf,count',10,50,NULL,NULL,'sys write')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('models/resnet.py','ResNet.forward','forward','function','self,x',20,40,NULL,NULL,'Res Net forward')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('models/vgg.py','VGG.forward','forward','function','self,x',15,35,NULL,NULL,'VGG forward')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('train.py','train_epoch','train_epoch','function','model,loader',5,50,NULL,NULL,'train epoch')")
    db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno,hash,bases,name_tokens) VALUES ('losses.py','compute_loss','compute_loss','function','pred,target',3,20,NULL,NULL,'compute loss')")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/read.c','sys_read','vfs_read','src/fs/read.c','src/fs/read.c::vfs_read',15,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/read.c','vfs_read','new_sync_read','src/fs/vfs.c','src/fs/vfs.c::new_sync_read',60,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'train.py','train_epoch','forward','models/resnet.py','models/resnet.py::ResNet.forward',25,'definite',NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'models/resnet.py','ResNet.forward','compute_loss',NULL,'losses.py::compute_loss',35,'inferred',NULL,'interface-impl')")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/vfs.c','new_sync_read','sys_write',NULL,NULL,20,NULL,NULL,NULL)")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/vfs.c','orphan_caller','sys_read','src/fs/read.c','src/fs/read.c::sys_read',99,'definite',NULL,NULL)")
    db.commit()
    db.close()
    return tmp_path


class TestLoadGraph:
    def test_loads_edges_into_adjacency_lists(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, id_to_info, skipped = _load_graph(db)
        db.close()
        sys_read_id = name_to_id.get("src/fs/read.c::sys_read")
        vfs_read_id = name_to_id.get("src/fs/read.c::vfs_read")
        assert sys_read_id is not None
        assert vfs_read_id is not None
        targets = [t for t, _, _ in adj_fwd[sys_read_id]]
        assert vfs_read_id in targets

    def test_skips_orphan_source_edges(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        _, _, _, _, skipped = _load_graph(db)
        db.close()
        assert skipped >= 1

    def test_static_only_excludes_heuristic(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        adj_fwd_all, _, _, _, _ = _load_graph(db, static_only=False)
        adj_fwd_static, _, _, _, _ = _load_graph(db, static_only=True)
        db.close()
        total_all = sum(len(v) for v in adj_fwd_all.values())
        total_static = sum(len(v) for v in adj_fwd_static.values())
        assert total_static < total_all

    def test_id_to_info_contains_all_symbols(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        _, _, _, id_to_info, _ = _load_graph(db)
        db.close()
        assert len(id_to_info) == 8

    def test_bidirectional_consistency(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph
        db = _open_db()
        adj_fwd, adj_bwd, _, _, _ = _load_graph(db)
        db.close()
        for src, edges in adj_fwd.items():
            for tgt, prov, via in edges:
                back_sources = [s for s, _, _ in adj_bwd.get(tgt, [])]
                assert src in back_sources


class TestBidirBfs:
    def test_finds_direct_connection(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/read.c::vfs_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is not None
        assert len(path) == 2
        assert path[0][0] == src
        assert path[1][0] == tgt

    def test_finds_two_hop_path(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/vfs.c::new_sync_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is not None
        assert len(path) == 3

    def test_same_node_returns_single_element(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        nid = name_to_id["src/fs/read.c::sys_read"]
        path = _bidir_bfs(nid, nid, adj_fwd, adj_bwd, 15, 2000)
        assert path == [(nid, None, None)]

    def test_disconnected_returns_none(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["train.py::train_epoch"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is None

    def test_max_visited_caps_expansion(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/vfs.c::new_sync_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 1)
        assert path is None

    def test_path_edges_are_valid(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/vfs.c::new_sync_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is not None
        for i in range(len(path) - 1):
            cur_id = path[i][0]
            nxt_id = path[i + 1][0]
            targets = [t for t, _, _ in adj_fwd.get(cur_id, [])]
            assert nxt_id in targets

    def test_symmetry_both_directions_find_path(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        a = name_to_id["train.py::train_epoch"]
        b = name_to_id["models/resnet.py::ResNet.forward"]
        path_ab = _bidir_bfs(a, b, adj_fwd, adj_bwd, 15, 2000)
        path_ba = _bidir_bfs(b, a, adj_fwd, adj_bwd, 15, 2000)
        assert path_ab is not None
        assert path_ba is None


class TestResolveFlowSymbol:
    def test_file_qualified_syntax(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "src/fs/read.c:sys_read", db, name_to_id, adj_fwd, adj_bwd, set(), ["sys_read"]
        )
        db.close()
        assert sid is not None
        assert "sys_read" in qualified
        assert ambiguous is False

    def test_class_qualified_dot_syntax(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "ResNet.forward", db, name_to_id, adj_fwd, adj_bwd, set(), ["ResNet", "forward"]
        )
        db.close()
        assert sid is not None
        assert "resnet" in qualified.lower()

    def test_bare_name_unique_resolves(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "sys_read", db, name_to_id, adj_fwd, adj_bwd, set(), ["sys_read"]
        )
        db.close()
        assert sid is not None
        assert ambiguous is False

    def test_bare_name_ambiguous_uses_co_naming(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "forward", db, name_to_id, adj_fwd, adj_bwd, set(), ["ResNet", "forward", "compute_loss"]
        )
        db.close()
        assert sid is not None
        assert "resnet" in qualified.lower()
        assert ambiguous is False

    def test_bare_name_ambiguous_connectivity(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        train_id = name_to_id["train.py::train_epoch"]
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "forward", db, name_to_id, adj_fwd, adj_bwd, {train_id}, ["forward"]
        )
        db.close()
        assert sid is not None
        assert "resnet" in qualified.lower()

    def test_not_found_returns_none(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "nonexistent_func", db, name_to_id, adj_fwd, adj_bwd, set(), ["nonexistent_func"]
        )
        db.close()
        assert sid is None

    def test_connectivity_prefers_closer_candidate(self, flow_db_dir):
        from index_mcp_queries import _open_db, _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        resnet_id = name_to_id["models/resnet.py::ResNet.forward"]
        loss_id = name_to_id["losses.py::compute_loss"]
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "forward", db, name_to_id, adj_fwd, adj_bwd, {loss_id}, ["forward"]
        )
        db.close()
        assert sid == resnet_id
        assert "resnet" in qualified.lower()


class TestQueryFlowImpl:
    def test_linear_chain_connected(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["sys_read", "vfs_read", "new_sync_read"])
        assert "## Flow" in result
        assert "sys_read" in result
        assert "vfs_read" in result
        assert "new_sync_read" in result
        assert "↓ call" in result

    def test_partial_connectivity_shows_break(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["sys_read", "train_epoch"])
        assert "Break" in result or "No connected" in result

    def test_synthesized_edge_annotated(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["train_epoch", "ResNet.forward", "compute_loss"])
        assert "synthesized" in result
        assert "interface-impl" in result

    def test_static_only_excludes_synthesized(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["ResNet.forward", "compute_loss"], static_only=True)
        assert "Break" in result or "No connected" in result

    def test_less_than_two_symbols_error(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["sys_read"])
        assert "Error" in result
        assert "at least 2" in result

    def test_empty_list_error(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl([])
        assert "Error" in result

    def test_no_db_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["a", "b"])
        assert "Error" in result or "logic_index.db" in result

    def test_all_unresolved_returns_message(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["xxx_not_exist", "yyy_not_exist"])
        assert "No symbols resolved" in result

    def test_file_qualified_input(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["src/fs/read.c:sys_read", "src/fs/read.c:vfs_read"])
        assert "## Flow" in result
        assert "sys_read" in result

    def test_max_depth_respected(self, flow_db_dir):
        from index_mcp_queries import query_flow_impl
        result = query_flow_impl(["sys_read", "new_sync_read"], max_depth=0)
        assert "Break" in result or "No connected" in result

    def test_format_flow_probable_label(self, flow_db_dir):
        from index_mcp_queries import _format_flow
        id_to_info = {
            1: ("a.py::foo", "a.py", "foo", 10, "function"),
            2: ("b.py::bar", "b.py", "bar", 20, "function"),
        }
        resolved = [(1, "a.py::foo", False), (2, "b.py::bar", False)]
        segments = [[(1, None, None), (2, "probable", None)]]
        result = _format_flow(resolved, segments, id_to_info, False, 15)
        assert "call [name-match]" in result

    def test_format_flow_speculative_label(self, flow_db_dir):
        from index_mcp_queries import _format_flow
        id_to_info = {
            1: ("a.py::foo", "a.py", "foo", 10, "function"),
            2: ("b.py::bar", "b.py", "bar", 20, "function"),
        }
        resolved = [(1, "a.py::foo", False), (2, "b.py::bar", False)]
        segments = [[(1, None, None), (2, "speculative", None)]]
        result = _format_flow(resolved, segments, id_to_info, False, 15)
        assert "call [speculative resolution]" in result


class TestSearchFtsNodeKindFilter:
    """_search_fts must return symbol-layer rows only (P1-8)."""

    def test_only_symbol_layer_returned(self, db_dir, monkeypatch):
        db_path = str(db_dir / ".claude" / "logic_index.db")
        conn = sqlite3.connect(db_path)
        _now = "2025-01-01T00:00:00"
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('file', 'a.py', 1, "
            "'{\"short\":\"entry shared keyword\",\"full\":null}', 'ok', ?)",
            (_now,),
        )
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('cluster', 'mycluster', 1, "
            "'{\"short\":\"entry shared keyword\",\"full\":null}', 'ok', ?)",
            (_now,),
        )
        conn.commit()
        conn.close()

        monkeypatch.chdir(db_dir)
        from index_mcp_queries import _open_db, _search_fts
        db = _open_db()
        results = _search_fts(db, "entry", limit=10, file_hint="")
        db.close()
        for r in results:
            name, fpath, lineno, stype, _rank = r
            assert name is not None
            assert fpath in ("a.py", "b.py")

    def test_zero_results_when_only_non_symbol_match(self, db_dir, monkeypatch):
        db_path = str(db_dir / ".claude" / "logic_index.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('cluster', 'mycluster', 1, "
            "'{\"short\":\"uniqueclusterkeyword\",\"full\":null}', 'ok', "
            "'2025-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

        monkeypatch.chdir(db_dir)
        from index_mcp_queries import _open_db, _search_fts
        db = _open_db()
        results = _search_fts(db, "uniqueclusterkeyword", limit=10, file_hint="")
        db.close()
        assert results == []

    def test_file_layer_matches_excluded(self, db_dir, monkeypatch):
        db_path = str(db_dir / ".claude" / "logic_index.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('file', 'a.py', 1, "
            "'{\"short\":\"uniquefilekeyword\",\"full\":null}', 'ok', "
            "'2025-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()

        monkeypatch.chdir(db_dir)
        from index_mcp_queries import _open_db, _search_fts
        db = _open_db()
        results = _search_fts(db, "uniquefilekeyword", limit=10, file_hint="")
        db.close()
        assert results == []

    def test_symbol_match_still_returned_alongside_non_symbol(self, db_dir, monkeypatch):
        db_path = str(db_dir / ".claude" / "logic_index.db")
        conn = sqlite3.connect(db_path)
        _now = "2025-01-01T00:00:00"
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('symbol', 'a.py::main', 2, "
            "'{\"short\":\"shared keyword indicator\",\"full\":null}', 'ok', ?)",
            (_now,),
        )
        conn.execute(
            "INSERT INTO summary_versions "
            "(node_kind, node_ref, version, summary, status, created_at) "
            "VALUES ('cluster', 'mycluster', 1, "
            "'{\"short\":\"shared keyword indicator\",\"full\":null}', 'ok', ?)",
            (_now,),
        )
        conn.commit()
        conn.close()

        monkeypatch.chdir(db_dir)
        from index_mcp_queries import _open_db, _search_fts
        db = _open_db()
        results = _search_fts(db, "indicator", limit=10, file_hint="")
        db.close()
        assert len(results) >= 1
        for r in results:
            name, fpath, _lineno, _stype, _rank = r
            assert fpath in ("a.py", "b.py")
