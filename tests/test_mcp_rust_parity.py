"""H.4 differential suite: Rust MCP server vs the Python rendering oracle.

Long-lived regression asset (R4.1). The Python side runs in-process against
the *_impl functions (the same call surface the FastMCP wrappers use); the
Rust side is the release `remy-daemon mcp` binary spoken to over stdio
JSON-RPC. Skipped when the release binary is absent.

Comparison layers (docs/MCP_RUST_PARITY_BASELINE.md §4):
- byte-for-byte after stripping the freshness-warning prefix (10 tools);
- semantic layer for search/navigate: ordered node_ref sequence only.

RUST_ONLY_TOOLS (H.4 §4.2) lists tools with a single Rust implementation and
no oracle counterpart; they are excluded from the differential matrix and are
accepted by their own suites (query_dependencies: test_mcp_dependencies.py).
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest

_REMY_ROOT = os.path.join(os.path.dirname(__file__), "..")
RUST_BIN = os.path.abspath(
    os.path.join(
        _REMY_ROOT, "remy-daemon", "target", "release",
        "remy-daemon.exe" if os.name == "nt" else "remy-daemon",
    )
)

pytestmark = pytest.mark.skipif(
    not os.path.exists(RUST_BIN),
    reason="release remy-daemon binary not built (cargo build --release)",
)

from struct_scan import SCHEMA_SQL
import retrieval_projection

COMPARATOR_VERSION = "1.0.0"
RESULT_LIMIT = "10"

CONTROLLED_ENV = {
    "REMY_MCP_RESULT_LIMIT": RESULT_LIMIT,
    "REMY_FRESHNESS_SAMPLE_SEED": "0",
}


def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _build_corpus(tmp_path):
    """Fixture DB satisfying the H.4 §2 structural requirements R1-R7.

    R8 (source_commit == HEAD) is a frozen-real-corpus requirement; here the
    tmp dir has no git at all, so freshness falls to hash sampling — source
    files are written to disk with correct hashes so the probe stays quiet,
    and the comparator still strips any warning prefix.
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db_path = claude_dir / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(SCHEMA_SQL)
    now = "2025-01-01T00:00:00"

    def add_file(path, language, layer, body):
        (tmp_path / path).write_text(body, encoding="utf-8")
        digest = hashlib.md5(body.encode("utf-8")).hexdigest()
        db.execute(
            "INSERT INTO files (path, struct_hash, language, layer, imports) "
            "VALUES (?,?,?,?,NULL)",
            (path, digest, language, layer),
        )

    def add_symbol(fpath, name, short, stype, args, lineno, end, tokens):
        db.execute(
            "INSERT INTO symbols (file_path,name,short_name,type,args,lineno,"
            "end_lineno,hash,bases,name_tokens) VALUES (?,?,?,?,?,?,?,NULL,NULL,?)",
            (fpath, name, short, stype, args, lineno, end, tokens),
        )

    def add_summary(kind, ref, version, payload, status):
        db.execute(
            "INSERT INTO summary_versions (node_kind,node_ref,version,summary,"
            "status,created_at) VALUES (?,?,?,?,?,?)",
            (kind, ref, version, payload, status, now),
        )

    add_file("a.py", "PythonParser", "Core", "def main():\n    pass\n")
    add_file("b.py", "PythonParser", "Util", "def process(data):\n    pass\n")
    add_file("c.py", "PythonParser", "Core", "def process(x):\n    pass\n")
    add_file("empty.py", "PythonParser", "Core", "\n")
    add_file("big.py", "PythonParser", "Core", "x = 1\n")
    add_file("lib.rs", "RustParser", "Core", "fn rust_entry() {}\n")

    add_symbol("a.py", "main", "main", "function", "args", 1, 10, "main")
    add_symbol("a.py", "helper", "helper", "function", "x", 12, 20, "helper")
    # R1: multi-file same-short-name pair.
    add_symbol("b.py", "process", "process", "function", "data", 1, 15, "process")
    add_symbol("c.py", "process", "process", "function", "x", 1, 5, "process")
    add_symbol("b.py", "Util.run", "run", "function", "", 17, 25, "Util run")
    add_symbol("c.py", "do_thing", "do_thing", "function", "x", 7, 9, "do thing")
    # R6: BM25 near-tie pair (nearly identical summaries below).
    add_symbol("a.py", "parse_alpha", "parse_alpha", "function", "s", 22, 24, "parse alpha")
    add_symbol("a.py", "parse_beta", "parse_beta", "function", "s", 26, 28, "parse beta")
    add_symbol("lib.rs", "rust_entry", "rust_entry", "function", "", 1, 1, "rust entry")
    for i in range(12):
        add_symbol("big.py", f"bulk_{i:02d}", f"bulk_{i:02d}", "function", "", i + 1, i + 1, f"bulk {i:02d}")

    add_summary("symbol", "a.py::main", 1, '{"short":"entry point","full":"long entry description"}', "ok")
    add_summary("symbol", "a.py::helper", 1, '{"short":"does stuff","full":null}', "ok")
    add_summary("symbol", "b.py::process", 1, '{"short":"processes data quickly","full":null}', "ok")
    add_summary("symbol", "c.py::process", 1, '{"short":"processes data slowly","full":null}', "ok")
    add_summary("symbol", "a.py::parse_alpha", 1, '{"short":"parse tokens quickly","full":null}', "ok")
    add_summary("symbol", "a.py::parse_beta", 1, '{"short":"parse tokens quickly","full":null}', "ok")
    # R2: stale barrier and oversized_warn nodes.
    add_summary("symbol", "c.py::do_thing", 1, '{"short":"old text","full":null}', "stale")
    add_summary("symbol", "lib.rs::rust_entry", 1, '{"short":"rust entry point","full":null}', "oversized_warn")
    add_summary("file", "a.py", 1, '{"short":"module a","full":null}', "ok")
    add_summary("cluster", "alpha_cluster", 1, '{"short":"alpha subsystem","full":"alpha detail"}', "ok")

    edges = [
        ("a.py", "main", "process", "b.py", "b.py::process", 5, "definite", None, None, "name"),
        ("a.py", "main", "helper", None, "a.py::helper", 3, "definite", None, None, "name"),
        ("b.py", "process", "do_thing", "c.py", "c.py::do_thing", 8, "probable", None, None, "name"),
        # R4: one synthesized edge per family.
        ("a.py", "helper", "run", "b.py", "b.py::Util.run", 14, "inferred", None, "interface-impl", "name"),
        ("a.py", "helper", "rust_entry", "lib.rs", "lib.rs::rust_entry", 15, "inferred", None, "rust-trait-impl", "name"),
        ("b.py", "process", "parse_alpha", "a.py", "a.py::parse_alpha", 9, "inferred", None, "observer:post_save", "name"),
    ]
    # Unresolved edge whose candidates only surface via include_ambiguous:
    # bulk_00 -> process has no callee_qualified, so the plain BFS skips it.
    edges.append(
        ("big.py", "bulk_00", "process", None, None, 2, "definite", None, None, "name")
    )
    for edge in edges:
        db.execute("INSERT INTO edges VALUES (NULL,?,?,?,?,?,?,?,?,?,?)", edge)
    edge_id = db.execute(
        "SELECT id FROM edges WHERE caller='main' AND callee='process'"
    ).fetchone()[0]
    db.execute("INSERT INTO edge_candidates VALUES (?,?,?)", (edge_id, "b.py::process", 1))
    db.execute("INSERT INTO edge_candidates VALUES (?,?,?)", (edge_id, "c.py::process", 0))
    unresolved_id = db.execute(
        "SELECT id FROM edges WHERE caller='bulk_00' AND callee='process'"
    ).fetchone()[0]
    db.execute(
        "INSERT INTO edge_candidates VALUES (?,?,?)", (unresolved_id, "b.py::process", 1)
    )
    db.execute(
        "INSERT INTO edge_candidates VALUES (?,?,?)", (unresolved_id, "c.py::process", 1)
    )

    db.execute(
        "INSERT INTO patterns VALUES (NULL,'a.py','django_signal_connect','post_save','on_save',8,NULL)"
    )
    # R5: rust_trait_impl patterns row.
    db.execute(
        "INSERT INTO patterns VALUES (NULL,'lib.rs','rust_trait_impl','Display','rust_entry',1,NULL)"
    )

    # R3: cluster pair tied on file_count.
    db.execute(
        "INSERT INTO clusters (id,name,label,entry_symbols,file_count) "
        "VALUES (1,'alpha_cluster','Alpha','[\"a.py::main\"]',2)"
    )
    db.execute(
        "INSERT INTO clusters (id,name,label,entry_symbols,file_count) "
        "VALUES (2,'beta_cluster',NULL,'[]',2)"
    )
    db.execute("INSERT INTO cluster_members (cluster_id,file_path) VALUES (1,'a.py')")
    db.execute("INSERT INTO cluster_members (cluster_id,file_path) VALUES (1,'b.py')")
    db.execute("INSERT INTO cluster_members (cluster_id,file_path) VALUES (2,'c.py')")
    db.execute("INSERT INTO cluster_members (cluster_id,file_path) VALUES (2,'big.py')")
    db.execute("INSERT INTO meta (key,value) VALUES ('file_count','6')")
    db.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('version','12.0.0')")

    retrieval_projection.rebuild_projection(db)
    db.commit()
    db.close()
    return db_path


NAVIGATE_INTENT = "process data"


def _prewarm_judge_cache(db_path):
    """R7: seed a judge_cache row for the cache-hit navigate group, keyed by
    the Python oracle's own candidate pipeline."""
    import index_mcp_common
    import index_mcp_navigate

    with index_mcp_common.database_override(db_path):
        db = index_mcp_common._open_db()
        try:
            candidates = index_mcp_navigate._navigate_candidates(db, NAVIGATE_INTENT)
            assert candidates, "fixture must produce navigate candidates"
            cache_key = index_mcp_navigate._navigate_cache_key(
                NAVIGATE_INTENT, 5, candidates
            )
            ranked = [
                {
                    "cluster": "alpha_cluster",
                    "file": "b.py",
                    "symbol": "process",
                    "relevance_score": 0.9,
                    "rationale": "direct match",
                },
                {
                    "cluster": "beta_cluster",
                    "file": None,
                    "symbol": None,
                    "relevance_score": 0.4,
                    "rationale": "related subsystem",
                },
            ]
            db.execute(
                "INSERT OR REPLACE INTO judge_cache (payload_hash, result, created_at) "
                "VALUES (?,?,?)",
                (cache_key, json.dumps(ranked, ensure_ascii=False), "2025-01-01T00:00:00"),
            )
            db.commit()
        finally:
            db.close()


class RustMcpClient:
    def __init__(self, cwd, db_path, home):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("REMY_")
        }
        env.update(CONTROLLED_ENV)
        env["REMY_LOGIC_INDEX_DB_PATH"] = str(db_path)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        self.proc = subprocess.Popen(
            [RUST_BIN, "mcp"],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._id = 0
        response = self._request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "parity-driver", "version": "0.0.0"},
            },
        )
        self.server_info = response["result"]["serverInfo"]
        self._notify("notifications/initialized")

    def _send(self, payload):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _request(self, method, params):
        self._id += 1
        request_id = self._id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            line = self.proc.stdout.readline()
            if not line:
                stderr = self.proc.stderr.read()
                raise AssertionError(f"rust mcp closed stdout; stderr: {stderr}")
            message = json.loads(line)
            if message.get("id") == request_id:
                assert "error" not in message, message
                return message

    def _notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def list_tools(self):
        return self._request("tools/list", {})["result"]["tools"]

    def call(self, name, arguments):
        response = self._request("tools/call", {"name": name, "arguments": arguments})
        result = response["result"]
        assert not result.get("isError"), result
        return result["content"][0]["text"]

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def _strip_warning(text):
    if text.startswith("[Warning:"):
        head, sep, rest = text.partition("\n\n")
        if sep:
            return rest
    return text


def _node_ref_sequence(text):
    refs = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and "] " in stripped and "::" in stripped:
            refs.append(stripped.split("] ", 1)[1].split("  ")[0])
    return refs


def _navigate_sequence(text):
    entries = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped[:1].isdigit() and "] " in stripped:
            entries.append(stripped.split("] ", 1)[1])
    return entries


@pytest.fixture(scope="module")
def parity(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("parity")
    home = tmp_path / "home"
    home.mkdir()
    db_path = _build_corpus(tmp_path)

    saved_env = {}
    for key in list(os.environ):
        if key.startswith("REMY_"):
            saved_env[key] = os.environ.pop(key)
    saved_extra = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
    os.environ.update(CONTROLLED_ENV)
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    saved_cwd = os.getcwd()
    os.chdir(tmp_path)

    _prewarm_judge_cache(db_path)
    # Checkpoint pending WAL frames into the main file so the snapshot hash
    # cannot drift when a later read connection triggers a passive checkpoint.
    checkpoint = sqlite3.connect(str(db_path))
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    snapshot_hash = _sha256(db_path)

    client = RustMcpClient(tmp_path, db_path, home)
    import index_mcp_common

    override = index_mcp_common.database_override(db_path)
    override.__enter__()
    try:
        yield {
            "client": client,
            "db_path": db_path,
            "snapshot_hash": snapshot_hash,
        }
    finally:
        override.__exit__(None, None, None)
        client.close()
        os.chdir(saved_cwd)
        for key in ("HOME", "USERPROFILE"):
            if saved_extra[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved_extra[key]
        for key in CONTROLLED_ENV:
            os.environ.pop(key, None)
        os.environ.update(saved_env)


def _python_call(tool, arguments):
    import index_mcp_facts as facts
    import index_mcp_graph as graph
    import index_mcp_navigate as navigate
    import index_mcp_search as search

    a = arguments
    if tool == "query_symbol":
        return facts.query_symbol_impl(a["name"], a.get("file") or None)
    if tool == "query_symbol_summary":
        return facts.query_symbol_summary_impl(a["name"], a.get("file") or None)
    if tool == "query_file_summary":
        return facts.query_file_summary_impl(a["file"])
    if tool == "query_callers":
        return graph.query_callers_impl(
            a["symbol"], a.get("depth", 2), a.get("include_ambiguous", False),
            a.get("static_only", False))
    if tool == "query_callees":
        return graph.query_callees_impl(
            a["symbol"], a.get("depth", 2), a.get("include_ambiguous", False),
            a.get("static_only", False))
    if tool == "query_impact":
        return graph.query_impact_impl(
            a["files"], a.get("depth_up", 3), a.get("depth_down", 3),
            a.get("include_ambiguous", False), a.get("static_only", False))
    if tool == "query_patterns":
        return facts.query_patterns_impl(
            a.get("pattern_type") or None, a.get("signal_name") or None,
            a.get("file") or None)
    if tool == "query_search":
        return search.query_search_impl(
            a["text"], a.get("limit", 10), a.get("file_hint", ""),
            match=a.get("match", "all"), language=a.get("language", ""),
            symbol_type=a.get("symbol_type", ""), path_hint=a.get("path_hint", ""))
    if tool == "query_flow":
        return graph.query_flow_impl(
            a["symbols"], a.get("max_depth", 15), a.get("max_visited", 2000),
            a.get("static_only", False))
    if tool == "query_cluster_summary":
        return facts.query_cluster_summary_impl(a.get("name") or None)
    if tool == "query_cluster_files":
        return facts.query_cluster_files_impl(a["cluster"], a.get("with_summary", False))
    if tool == "query_navigate":
        return navigate.query_navigate_impl(a["intent"], a.get("top_k", 5))
    raise AssertionError(f"unknown tool {tool}")


BYTE_GROUPS = [
    ("query_symbol", {"name": "b.py::process"}),
    ("query_symbol", {"name": "no_such_symbol"}),
    ("query_symbol", {"name": "process"}),
    ("query_symbol", {"name": "process", "file": "b.py"}),
    ("query_symbol_summary", {"name": "main"}),
    ("query_symbol_summary", {"name": "no_such_symbol"}),
    ("query_symbol_summary", {"name": "run"}),
    ("query_symbol_summary", {"name": "do_thing"}),
    ("query_symbol_summary", {"name": "rust_entry"}),
    ("query_file_summary", {"file": "a.py"}),
    ("query_file_summary", {"file": "no_such.py"}),
    ("query_file_summary", {"file": "empty.py"}),
    ("query_file_summary", {"file": "big.py"}),
    ("query_callers", {"symbol": "process"}),
    ("query_callers", {"symbol": "main"}),
    ("query_callers", {"symbol": "process", "static_only": True}),
    ("query_callers", {"symbol": "process", "include_ambiguous": True}),
    ("query_callers", {"symbol": "process", "depth": 1}),
    ("query_callees", {"symbol": "main"}),
    ("query_callees", {"symbol": "run"}),
    ("query_callees", {"symbol": "main", "static_only": True}),
    ("query_callees", {"symbol": "main", "include_ambiguous": True}),
    ("query_impact", {"files": ["b.py"]}),
    ("query_impact", {"files": ["no_such.py"]}),
    ("query_impact", {"files": ["a.py", "b.py"]}),
    ("query_patterns", {}),
    ("query_patterns", {"signal_name": "no_signal"}),
    ("query_patterns", {"pattern_type": "rust_trait_impl"}),
    ("query_patterns", {"file": "a.py"}),
    ("query_flow", {"symbols": ["main", "do_thing"]}),
    ("query_flow", {"symbols": ["main", "bulk_00"]}),
    ("query_flow", {"symbols": ["main", "process", "do_thing"]}),
    ("query_flow", {"symbols": ["b.py:process", "do_thing"]}),
    ("query_flow", {"symbols": ["main", "do_thing"], "static_only": True}),
    ("query_flow", {"symbols": ["helper", "run"]}),
    ("query_cluster_summary", {"name": ""}),
    ("query_cluster_summary", {"name": "no_such_cluster"}),
    ("query_cluster_summary", {"name": "alpha_cluster"}),
    ("query_cluster_files", {"cluster": "alpha_cluster"}),
    ("query_cluster_files", {"cluster": "no_such_cluster"}),
    ("query_cluster_files", {"cluster": "alpha_cluster", "with_summary": True}),
]

SEARCH_GROUPS = [
    ("query_search", {"text": "process"}),
    ("query_search", {"text": "zzyqx_none"}),
    ("query_search", {"text": "process data", "match": "any"}),
    ("query_search", {"text": "do thing", "match": "phrase"}),
    ("query_search", {"text": "proces"}),
    ("query_search", {"text": "procss"}),
    ("query_search", {"text": "parse tokens"}),
    ("query_search", {"text": "rust entry", "language": "rust", "match": "any"}),
    ("query_search", {"text": "process", "path_hint": "b.py"}),
    ("query_search", {"text": "process", "limit": 1}),
    ("query_search", {"text": "process", "language": "go"}),
]


@pytest.mark.parametrize("tool,arguments", BYTE_GROUPS)
def test_byte_parity(parity, tool, arguments):
    python_out = _python_call(tool, arguments)
    rust_out = _strip_warning(parity["client"].call(tool, arguments))
    assert rust_out == python_out


@pytest.mark.parametrize("tool,arguments", SEARCH_GROUPS)
def test_search_semantic_parity(parity, tool, arguments):
    python_out = _python_call(tool, arguments)
    rust_out = _strip_warning(parity["client"].call(tool, arguments))
    if python_out.startswith(("Error:", "No symbols")):
        assert rust_out == python_out
        return
    assert _node_ref_sequence(rust_out) == _node_ref_sequence(python_out)
    assert _node_ref_sequence(rust_out), "expected non-empty result sequence"


def test_navigate_cache_hit_parity(parity):
    arguments = {"intent": NAVIGATE_INTENT, "top_k": 5}
    python_out = _python_call("query_navigate", arguments)
    rust_out = _strip_warning(parity["client"].call("query_navigate", arguments))
    assert "source=cache" in python_out
    assert "source=cache" in rust_out
    assert _navigate_sequence(rust_out) == _navigate_sequence(python_out)


def test_navigate_heuristic_parity(parity):
    arguments = {"intent": NAVIGATE_INTENT, "top_k": 1}
    python_out = _python_call("query_navigate", arguments)
    rust_out = _strip_warning(parity["client"].call("query_navigate", arguments))
    assert "source=heuristic" in python_out
    assert _navigate_sequence(rust_out) == _navigate_sequence(python_out)


def test_navigate_empty_intent(parity):
    arguments = {"intent": "   "}
    python_out = _python_call("query_navigate", arguments)
    rust_out = _strip_warning(parity["client"].call("query_navigate", arguments))
    assert rust_out == python_out == "Error: intent must not be empty."


def test_snapshot_identity_recorded(parity):
    identity = {
        "db_snapshot_sha256": parity["snapshot_hash"],
        "schema_version": "12.0.0",
        "comparator_version": COMPARATOR_VERSION,
        "config_snapshot": CONTROLLED_ENV,
        "server_info": parity["client"].server_info,
    }
    assert identity["db_snapshot_sha256"]
    assert identity["server_info"]["name"] == "remy-index"
    db = sqlite3.connect(str(parity["db_path"]))
    try:
        version = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        assert version and version[0] == identity["schema_version"]
    finally:
        db.close()


# Rust single-implementation tools: no Python oracle arm, excluded from the
# differential matrix (docs/MCP_RUST_PARITY_BASELINE.md §4.2). Their
# acceptance surface is a dedicated suite (test_mcp_dependencies.py).
ORACLE_TOOL_NAMES = [
    "query_symbol", "query_symbol_summary", "query_file_summary",
    "query_callers", "query_callees", "query_impact", "query_patterns",
    "query_search", "query_flow", "query_cluster_summary",
    "query_cluster_files", "query_navigate",
]
RUST_ONLY_TOOLS = {"query_dependencies"}


def test_tool_listing_matches_oracle_names(parity):
    tools = parity["client"].list_tools()
    names = sorted(tool["name"] for tool in tools)
    assert names == sorted(ORACLE_TOOL_NAMES + sorted(RUST_ONLY_TOOLS))
    for tool in tools:
        schema = tool["inputSchema"]
        assert schema.get("type") == "object"


def test_byte_groups_leave_db_unchanged(parity):
    assert _sha256(parity["db_path"]) == parity["snapshot_hash"]
