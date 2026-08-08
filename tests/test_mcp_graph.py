"""Tests for index_mcp_graph.py — BFS, impact, and flow queries."""
import os
import sqlite3
import sys

import pytest

_REMY_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_REMY_ROOT, "remy-src"))
sys.path.insert(0, os.path.join(_REMY_ROOT, "skills", "remy-index"))

from struct_scan import SCHEMA_SQL


def _write_impact_db(tmp_path, fan_out_files, symbols_per_file):
    """Graph where root.py::entry calls symbols_per_file symbols in each of fan_out_files files."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    db = sqlite3.connect(str(claude_dir / "logic_index.db"))
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO files (path, struct_hash, language, layer) "
        "VALUES ('root.py','h','python','Core')"
    )
    db.execute(
        "INSERT INTO symbols (file_path,name,short_name,type,lineno,name_tokens) "
        "VALUES ('root.py','entry','entry','function',1,'entry')"
    )
    for file_index in range(fan_out_files):
        path = f"dep{file_index}.py"
        db.execute(
            "INSERT INTO files (path, struct_hash, language, layer) VALUES (?,?,?,?)",
            (path, f"h{file_index}", "python", "Util"),
        )
        for symbol_index in range(symbols_per_file):
            name = f"fn{symbol_index}"
            db.execute(
                "INSERT INTO symbols (file_path,name,short_name,type,lineno,name_tokens) "
                "VALUES (?,?,?,?,?,?)",
                (path, name, name, "function", symbol_index + 1, name),
            )
            db.execute(
                "INSERT INTO edges VALUES (NULL,'root.py','entry',?,?,?,?,'definite',NULL,NULL,'name')",
                (name, path, f"{path}::{name}", symbol_index + 1),
            )
    db.commit()
    db.close()


class TestQueryCallersImpl:
    def test_finds_direct_callers(self, db_dir):
        from index_mcp_graph import query_callers_impl
        result = query_callers_impl("b.py::process", 2, False, False)
        assert "a.py::main" in result

    def test_depth_clamped_to_max(self, db_dir, monkeypatch):
        monkeypatch.setenv("REMY_MCP_BFS_MAX_DEPTH", "1")
        from index_mcp_graph import query_callers_impl
        result = query_callers_impl("b.py::process", 99, False, False)
        assert "depth 1" in result or "1 levels" in result

    def test_static_only_excludes_heuristic(self, db_dir):
        from index_mcp_graph import query_callers_impl
        result = query_callers_impl("b.py::Util.run", 2, False, True)
        assert "a.py::helper" not in result

    def test_include_ambiguous_finds_candidates(self, db_dir):
        from index_mcp_graph import query_callers_impl
        result = query_callers_impl("c.py::process", 2, True, False)
        assert "a.py::main" in result

    def test_include_ambiguous_with_static_only(self, db_dir):
        from index_mcp_graph import query_callers_impl
        result = query_callers_impl("b.py::process", 2, True, True)
        assert "a.py::main" in result

    def test_include_ambiguous_with_static_only_excludes_heuristic(self, db_dir):
        from index_mcp_graph import query_callers_impl
        result = query_callers_impl("b.py::Util.run", 2, True, True)
        assert "a.py::helper" not in result

    def test_not_found_symbol(self, db_dir):
        from index_mcp_graph import query_callers_impl
        result = query_callers_impl("nonexistent", 2, False, False)
        assert "No symbols found" in result


class TestQueryCalleesImpl:
    def test_finds_direct_callees(self, db_dir):
        from index_mcp_graph import query_callees_impl
        result = query_callees_impl("a.py::main", 2, False, False)
        assert "b.py::process" in result

    def test_static_only_excludes_heuristic(self, db_dir):
        from index_mcp_graph import query_callees_impl
        result = query_callees_impl("a.py::helper", 1, False, True)
        assert "Util.run" not in result

    def test_include_ambiguous_expands_candidates(self, db_dir):
        from index_mcp_graph import query_callees_impl
        result = query_callees_impl("a.py::main", 1, True, False)
        assert "b.py::process" in result

    def test_include_ambiguous_with_static_only(self, db_dir):
        from index_mcp_graph import query_callees_impl
        result = query_callees_impl("a.py::main", 2, True, True)
        assert "b.py::process" in result


class TestQueryImpactImpl:
    def test_finds_downstream(self, db_dir):
        from index_mcp_graph import query_impact_impl
        result = query_impact_impl(["a.py"], 0, 2, False, False)
        assert "b.py" in result

    def test_file_not_in_index(self, db_dir):
        from index_mcp_graph import query_impact_impl
        result = query_impact_impl(["nonexistent.py"], 2, 2, False, False)
        assert "No indexed files" in result

    def test_no_db(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_graph import query_impact_impl
        result = query_impact_impl(["a.py"], 2, 2, False, False)
        assert "error" in result.lower() or "not found" in result.lower()


class TestQueryImpactRendering:
    """Depth lines label files; a file with many symbols must not repeat, and the
    file count must cover every symbol rather than a result-limit prefix."""

    def _run(self, root, monkeypatch, limit=None, files=3, symbols=20):
        _write_impact_db(root, files, symbols)
        monkeypatch.chdir(root)
        if limit is not None:
            monkeypatch.setenv("REMY_MCP_RESULT_LIMIT", str(limit))
        from index_mcp_graph import query_impact_impl
        return query_impact_impl(["root.py"], 0, 1, False, False)

    @staticmethod
    def _depth_line(result, depth=1):
        return [line for line in result.splitlines() if f"[depth {depth}]" in line][0]

    def test_repeated_file_appears_once_per_level(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch)
        assert self._depth_line(result).count("dep0.py") == 1

    def test_level_line_reports_file_and_symbol_counts(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch)
        assert "[depth 1] 3 file(s), 60 symbol(s):" in result

    def test_file_count_ignores_result_limit(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, limit=10)
        assert "summary: 3 files affected, 0 upstream + 60 downstream symbols" in result

    def test_output_identical_across_result_limits(self, tmp_path, monkeypatch):
        low = self._run(tmp_path / "low", monkeypatch, limit=10)
        high = self._run(tmp_path / "high", monkeypatch, limit=500)
        assert low == high

    def test_label_truncation_is_announced(self, tmp_path, monkeypatch):
        line = self._depth_line(self._run(tmp_path, monkeypatch, files=8, symbols=2))
        assert "8 file(s), 16 symbol(s):" in line
        assert line.endswith("... +3 more file(s)")
        labels = line.split(": ", 1)[1].split(" ...")[0].split(", ")
        assert labels == ["dep0.py", "dep1.py", "dep2.py", "dep3.py", "dep4.py"]

    def test_no_truncation_marker_when_every_file_is_shown(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, files=3, symbols=2)
        assert "more file(s)" not in result
        assert self._depth_line(result).endswith("dep0.py, dep1.py, dep2.py")


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
        from index_mcp_graph import _bfs_callers_ambiguous
        from index_mcp_common import _open_db
        db = _open_db()
        assert db is not None
        try:
            levels = _bfs_callers_ambiguous(db, {"target.py::target_fn"}, 1)
            assert 1 in levels
            assert len(levels[1]) == 500
        finally:
            db.close()

    def test_ambiguous_callees_over_400(self, large_db_dir):
        from index_mcp_graph import _bfs_callees_ambiguous
        from index_mcp_common import _open_db
        db = _open_db()
        assert db is not None
        try:
            all_callers = {f"f{i:04d}.py::caller_{i}" for i in range(500)}
            levels = _bfs_callees_ambiguous(db, all_callers, 1)
            assert 1 in levels
            assert "target.py::target_fn" in levels[1]
        finally:
            db.close()


class TestBfsAmbiguousDepth:
    """Multi-level ambiguous BFS expansion through the edge_candidates branch."""

    @pytest.fixture
    def chain_db_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        db = sqlite3.connect(str(claude_dir / "logic_index.db"))
        db.executescript(SCHEMA_SQL)
        for path, name in (
            ("target.py", "target_fn"),
            ("mid.py", "mid_fn"),
            ("outer.py", "outer_fn"),
        ):
            db.execute(
                "INSERT INTO files (path, struct_hash, language, layer) VALUES (?,?,'python','Core')",
                (path, f"h-{path}"),
            )
            db.execute(
                "INSERT INTO symbols (file_path,name,short_name,type,lineno,name_tokens) "
                "VALUES (?,?,?,'function',1,?)",
                (path, name, name, name),
            )
        db.execute(
            "INSERT INTO edges VALUES (NULL,'mid.py','mid_fn','target_fn','target.py','target.py::target_fn',2,'definite',NULL,NULL,'name')"
        )
        db.execute(
            "INSERT INTO edges VALUES (NULL,'outer.py','outer_fn','mid_fn',NULL,NULL,3,'speculative',NULL,NULL,'attribute')"
        )
        edge_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO edge_candidates (edge_id, candidate_qualified, score) VALUES (?,?,1)",
            (edge_id, "mid.py::mid_fn"),
        )
        db.commit()
        db.close()
        return tmp_path

    def test_callers_depth_two_reaches_candidate_edge(self, chain_db_dir):
        from index_mcp_graph import _bfs_callers_ambiguous
        from index_mcp_common import _open_db
        db = _open_db()
        assert db is not None
        try:
            levels = _bfs_callers_ambiguous(db, {"target.py::target_fn"}, 2)
            assert levels[1] == ["mid.py::mid_fn"]
            assert levels[2] == ["outer.py::outer_fn"]
        finally:
            db.close()

    def test_callees_depth_two_reaches_direct_edge(self, chain_db_dir):
        from index_mcp_graph import _bfs_callees_ambiguous
        from index_mcp_common import _open_db
        db = _open_db()
        assert db is not None
        try:
            levels = _bfs_callees_ambiguous(db, {"outer.py::outer_fn"}, 2)
            assert levels[1] == ["mid.py::mid_fn"]
            assert levels[2] == ["target.py::target_fn"]
        finally:
            db.close()


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
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/read.c','sys_read','vfs_read','src/fs/read.c','src/fs/read.c::vfs_read',15,'definite',NULL,NULL,'name')")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/read.c','vfs_read','new_sync_read','src/fs/vfs.c','src/fs/vfs.c::new_sync_read',60,'definite',NULL,NULL,'name')")
    db.execute("INSERT INTO edges VALUES (NULL,'train.py','train_epoch','forward','models/resnet.py','models/resnet.py::ResNet.forward',25,'definite',NULL,NULL,'name')")
    db.execute("INSERT INTO edges VALUES (NULL,'models/resnet.py','ResNet.forward','compute_loss',NULL,'losses.py::compute_loss',35,'inferred',NULL,'interface-impl','name')")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/vfs.c','new_sync_read','sys_write',NULL,NULL,20,NULL,NULL,NULL,'name')")
    db.execute("INSERT INTO edges VALUES (NULL,'src/fs/vfs.c','orphan_caller','sys_read','src/fs/read.c','src/fs/read.c::sys_read',99,'definite',NULL,NULL,'name')")
    db.commit()
    db.close()
    return tmp_path


class TestLoadGraph:
    def test_loads_edges_into_adjacency_lists(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph
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
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph
        db = _open_db()
        _, _, _, _, skipped = _load_graph(db)
        db.close()
        assert skipped >= 1

    def test_static_only_excludes_heuristic(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph
        db = _open_db()
        adj_fwd_all, _, _, _, _ = _load_graph(db, static_only=False)
        adj_fwd_static, _, _, _, _ = _load_graph(db, static_only=True)
        db.close()
        total_all = sum(len(v) for v in adj_fwd_all.values())
        total_static = sum(len(v) for v in adj_fwd_static.values())
        assert total_static < total_all

    def test_id_to_info_contains_all_symbols(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph
        db = _open_db()
        _, _, _, id_to_info, _ = _load_graph(db)
        db.close()
        assert len(id_to_info) == 8

    def test_bidirectional_consistency(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph
        db = _open_db()
        adj_fwd, adj_bwd, _, _, _ = _load_graph(db)
        db.close()
        for src, edges in adj_fwd.items():
            for tgt, prov, via in edges:
                back_sources = [s for s, _, _ in adj_bwd.get(tgt, [])]
                assert src in back_sources


class TestBidirBfs:
    def test_finds_direct_connection(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _bidir_bfs
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
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/vfs.c::new_sync_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is not None
        assert len(path) == 3

    def test_same_node_returns_single_element(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        nid = name_to_id["src/fs/read.c::sys_read"]
        path = _bidir_bfs(nid, nid, adj_fwd, adj_bwd, 15, 2000)
        assert path == [(nid, None, None)]

    def test_disconnected_returns_none(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["train.py::train_epoch"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 2000)
        assert path is None

    def test_max_visited_caps_expansion(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _bidir_bfs
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        db.close()
        src = name_to_id["src/fs/read.c::sys_read"]
        tgt = name_to_id["src/fs/vfs.c::new_sync_read"]
        path = _bidir_bfs(src, tgt, adj_fwd, adj_bwd, 15, 1)
        assert path is None

    def test_path_edges_are_valid(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _bidir_bfs
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
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _bidir_bfs
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
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _resolve_flow_symbol
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
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "ResNet.forward", db, name_to_id, adj_fwd, adj_bwd, set(), ["ResNet", "forward"]
        )
        db.close()
        assert sid is not None
        assert "resnet" in qualified.lower()

    def test_bare_name_unique_resolves(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "sys_read", db, name_to_id, adj_fwd, adj_bwd, set(), ["sys_read"]
        )
        db.close()
        assert sid is not None
        assert ambiguous is False

    def test_bare_name_ambiguous_uses_co_naming(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _resolve_flow_symbol
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
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _resolve_flow_symbol
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
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _resolve_flow_symbol
        db = _open_db()
        adj_fwd, adj_bwd, name_to_id, _, _ = _load_graph(db)
        sid, qualified, ambiguous = _resolve_flow_symbol(
            "nonexistent_func", db, name_to_id, adj_fwd, adj_bwd, set(), ["nonexistent_func"]
        )
        db.close()
        assert sid is None

    def test_connectivity_prefers_closer_candidate(self, flow_db_dir):
        from index_mcp_common import _open_db
        from index_mcp_graph import _load_graph, _resolve_flow_symbol
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
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl(["sys_read", "vfs_read", "new_sync_read"])
        assert "## Flow" in result
        assert "sys_read" in result
        assert "vfs_read" in result
        assert "new_sync_read" in result
        assert "↓ call" in result

    def test_partial_connectivity_shows_break(self, flow_db_dir):
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl(["sys_read", "train_epoch"])
        assert "Break" in result or "No connected" in result

    def test_synthesized_edge_annotated(self, flow_db_dir):
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl(["train_epoch", "ResNet.forward", "compute_loss"])
        assert "synthesized" in result
        assert "interface-impl" in result

    def test_static_only_excludes_synthesized(self, flow_db_dir):
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl(["ResNet.forward", "compute_loss"], static_only=True)
        assert "Break" in result or "No connected" in result

    def test_less_than_two_symbols_error(self, flow_db_dir):
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl(["sys_read"])
        assert "Error" in result
        assert "at least 2" in result

    def test_empty_list_error(self, flow_db_dir):
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl([])
        assert "Error" in result

    def test_no_db_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl(["a", "b"])
        assert "Error" in result or "logic_index.db" in result

    def test_all_unresolved_returns_message(self, flow_db_dir):
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl(["xxx_not_exist", "yyy_not_exist"])
        assert "No symbols resolved" in result

    def test_file_qualified_input(self, flow_db_dir):
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl(["src/fs/read.c:sys_read", "src/fs/read.c:vfs_read"])
        assert "## Flow" in result
        assert "sys_read" in result

    def test_max_depth_respected(self, flow_db_dir):
        from index_mcp_graph import query_flow_impl
        result = query_flow_impl(["sys_read", "new_sync_read"], max_depth=0)
        assert "Break" in result or "No connected" in result

    def test_flow_parameters_clamped_to_config(self, flow_db_dir, monkeypatch):
        import index_mcp_graph
        monkeypatch.setenv("REMY_FLOW_MAX_DEPTH", "1")
        monkeypatch.setenv("REMY_FLOW_MAX_VISITED", "100")
        observed = {}
        original = index_mcp_graph._bidir_bfs

        def capture(src_id, tgt_id, adj_fwd, adj_bwd, max_depth, max_visited):
            observed["limits"] = (max_depth, max_visited)
            return original(src_id, tgt_id, adj_fwd, adj_bwd, max_depth, max_visited)

        monkeypatch.setattr(index_mcp_graph, "_bidir_bfs", capture)
        index_mcp_graph.query_flow_impl(
            ["sys_read", "new_sync_read"], max_depth=20, max_visited=5000
        )
        assert observed["limits"] == (1, 100)

    def test_query_uses_one_config_snapshot(self, flow_db_dir, monkeypatch):
        import remy_config
        from index_mcp_graph import query_flow_impl
        calls = 0
        original = remy_config.load_config

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(remy_config, "load_config", counted)
        query_flow_impl(["sys_read", "new_sync_read"])
        assert calls == 1

    def test_format_flow_probable_label(self, flow_db_dir):
        from index_mcp_graph import _format_flow
        id_to_info = {
            1: ("a.py::foo", "a.py", "foo", 10, "function"),
            2: ("b.py::bar", "b.py", "bar", 20, "function"),
        }
        resolved = [(1, "a.py::foo", False), (2, "b.py::bar", False)]
        segments = [[(1, None, None), (2, "probable", None)]]
        result = _format_flow(resolved, segments, id_to_info, False, 15)
        assert "call [name-match]" in result

    def test_format_flow_speculative_label(self, flow_db_dir):
        from index_mcp_graph import _format_flow
        id_to_info = {
            1: ("a.py::foo", "a.py", "foo", 10, "function"),
            2: ("b.py::bar", "b.py", "bar", 20, "function"),
        }
        resolved = [(1, "a.py::foo", False), (2, "b.py::bar", False)]
        segments = [[(1, None, None), (2, "speculative", None)]]
        result = _format_flow(resolved, segments, id_to_info, False, 15)
        assert "call [speculative resolution]" in result
