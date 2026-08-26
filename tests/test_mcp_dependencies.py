"""Dedicated acceptance suite for query_dependencies (Rust single-implementation).

The tool has no Python oracle arm and is excluded from the H.4 differential
matrix (docs/MCP_RUST_PARITY_BASELINE.md §4.2); this suite is its acceptance
surface. It drives the release `remy-daemon mcp` binary over stdio against a
purpose-built corpus exercising: stored resolved imports, unique-suffix
derivation from import_bindings, stdlib short-circuit, multi-hit ambiguity
drop, dangling entries, import cycles, depth clamping, and read-only access.
"""
import hashlib
import json
import sqlite3

import pytest

from struct_scan import SCHEMA_SQL
from test_mcp_rust_parity import (
    RUST_BIN,
    RustMcpClient,
    _sha256,
    _strip_warning,
)
import os

pytestmark = pytest.mark.skipif(
    not os.path.exists(RUST_BIN),
    reason="release remy-daemon binary not built (cargo build --release)",
)

# Chain length exceeds the default REMY_MCP_BFS_MAX_DEPTH (5) so the clamp
# is observable: chain/c7.py is reachable only at depth 6.
CHAIN_LEN = 7


def _build_corpus(tmp_path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    db_path = claude_dir / "logic_index.db"
    db = sqlite3.connect(str(db_path))
    # Persist WAL in the file header up front so the Rust client's idempotent
    # WAL pragma cannot rewrite the header after the snapshot hash is taken.
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA_SQL)

    def add_file(path, imports, bindings):
        body = f"# {path}\n"
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        digest = hashlib.md5(body.encode("utf-8")).hexdigest()
        db.execute(
            "INSERT INTO files (path, struct_hash, language, layer, imports, "
            "import_bindings) VALUES (?,?,'PythonParser','Core',?,?)",
            (
                path,
                digest,
                json.dumps(imports) if imports else None,
                json.dumps(bindings),
            ),
        )

    add_file(
        "app/main.py",
        ["app/util.py"],
        [
            {"module": "helpers", "names": ["calc"]},
            {"module": "json", "names": ["json"]},
            {"module": "dual", "names": ["thing"]},
        ],
    )
    # vendor/missing.py is deliberately absent from the files table: a
    # dangling stored entry (the clock.rs -> Clock.rs class of data).
    add_file("app/util.py", ["vendor/missing.py"], [])
    add_file("libs/helpers.py", [], [])
    add_file("pkg_a/dual.py", [], [])
    add_file("pkg_b/dual.py", [], [])
    add_file("cyc/a.py", ["cyc/b.py"], [])
    add_file("cyc/b.py", ["cyc/a.py"], [])
    for i in range(1, CHAIN_LEN + 1):
        nxt = [f"chain/c{i + 1}.py"] if i < CHAIN_LEN else []
        add_file(f"chain/c{i}.py", nxt, [])

    file_count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    db.execute("INSERT INTO meta (key,value) VALUES ('file_count',?)", (str(file_count),))
    db.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('version','12.0.0')")
    db.commit()
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    return db_path


@pytest.fixture(scope="module")
def deps(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("deps")
    home = tmp_path / "home"
    home.mkdir()
    db_path = _build_corpus(tmp_path)
    snapshot_hash = _sha256(db_path)
    client = RustMcpClient(tmp_path, db_path, home)
    try:
        yield {
            "client": client,
            "db_path": db_path,
            "snapshot_hash": snapshot_hash,
        }
    finally:
        client.close()


def _call(deps, arguments):
    return _strip_warning(deps["client"].call("query_dependencies", arguments))


def test_down_merges_stored_and_derived_with_dangling_marker(deps):
    out = _call(deps, {"files": ["app/main.py"], "direction": "down", "depth": 2})
    assert out == (
        "dependency analysis for: app/main.py\n"
        "\n"
        "imports (downstream dependencies):\n"
        "  [depth 1] 2 file(s): app/util.py, libs/helpers.py\n"
        "  [depth 2] 1 file(s): vendor/missing.py (not indexed)\n"
        "\n"
        "summary: 3 downstream file(s)"
    )


def test_multi_hit_and_stdlib_bindings_produce_no_edges(deps):
    out = _call(deps, {"files": ["app/main.py"], "direction": "down", "depth": 3})
    assert "dual.py" not in out
    assert "json" not in out


def test_up_direction_via_stored_and_derived_edges(deps):
    stored = _call(deps, {"files": ["app/util.py"], "direction": "up", "depth": 1})
    assert "[depth 1] 1 file(s): app/main.py" in stored
    derived = _call(deps, {"files": ["libs/helpers.py"], "direction": "up", "depth": 1})
    assert "[depth 1] 1 file(s): app/main.py" in derived


def test_direction_duality(deps):
    down = _call(deps, {"files": ["app/main.py"], "direction": "down", "depth": 1})
    assert "app/util.py" in down
    up = _call(deps, {"files": ["app/util.py"], "direction": "up", "depth": 1})
    assert "app/main.py" in up


def test_both_renders_two_sections_with_combined_summary(deps):
    out = _call(deps, {"files": ["app/main.py"]})
    assert "imported by (upstream importers):\n  (none)" in out
    assert "imports (downstream dependencies):" in out
    assert out.endswith("summary: 0 upstream file(s), 3 downstream file(s)")


def test_cycle_terminates_and_lists_first_reach_only(deps):
    out = _call(deps, {"files": ["cyc/a.py"], "direction": "down", "depth": 5})
    assert "[depth 1] 1 file(s): cyc/b.py" in out
    assert "[depth 2]" not in out
    assert out.endswith("summary: 1 downstream file(s)")


def test_depth_clamped_to_bfs_max_depth(deps):
    out = _call(deps, {"files": ["chain/c1.py"], "direction": "down", "depth": 100})
    assert "[depth 5] 1 file(s): chain/c6.py" in out
    assert "chain/c7.py" not in out
    assert out.endswith("summary: 5 downstream file(s)")


def test_depth_zero_and_negative_render_none(deps):
    for depth in (0, -1):
        out = _call(deps, {"files": ["app/main.py"], "direction": "down", "depth": depth})
        assert "imports (downstream dependencies):\n  (none)" in out
        assert out.endswith("summary: 0 downstream file(s)")


def test_invalid_direction_error_skips_freshness_prefix(deps):
    raw = deps["client"].call(
        "query_dependencies", {"files": ["app/main.py"], "direction": "sideways"}
    )
    assert raw == "Error: direction must be one of up/down/both."


def test_no_indexed_files_matching(deps):
    out = _call(deps, {"files": ["nope.py", "also_missing.py"], "direction": "down"})
    assert out == "No indexed files found matching: nope.py, also_missing.py"


def test_backslash_paths_normalized(deps):
    out = _call(deps, {"files": ["app\\main.py"], "direction": "down", "depth": 1})
    assert out.startswith("dependency analysis for: app/main.py")


def test_repeated_calls_byte_identical(deps):
    arguments = {"files": ["app/main.py", "cyc/a.py"], "direction": "both", "depth": 3}
    first = _call(deps, arguments)
    second = _call(deps, arguments)
    assert first == second


def test_queries_leave_db_unchanged(deps):
    assert _sha256(deps["db_path"]) == deps["snapshot_hash"]
