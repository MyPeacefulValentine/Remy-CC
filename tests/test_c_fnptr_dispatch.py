"""Tests for the C function-pointer dispatch synthesizer (parser facts + SQL join).

Validates the TEE-dominant shape: fn-pointer typedef + struct layout in a header,
positional registration table + subscript-field dispatch in a source file, joined
cross-file into dispatcher->handler edges.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import SCHEMA_SQL
from parsers.c_cpp_parser import CCppParser
from synthesizers.c_fnptr_dispatch import synthesize_c_fnptr_dispatch_edges


HEADER = """\
typedef int (*sync_func)(const void *cmd);

struct ns_cmd_t {
    unsigned int cmd_id;
    sync_func func;
};
"""

SOURCE = """\
#include "disp.h"

static const struct ns_cmd_t g_cmd_table[] = {
    { CMD_A, handle_a },
    { CMD_B, handle_b },
#ifdef CONFIG_EXTRA
    { CMD_C, handle_c },
#endif
};

static int dispatch(const void *cmd, unsigned int id)
{
    unsigned int i;
    for (i = 0; i < 3; i++) {
        if (g_cmd_table[i].cmd_id == id)
            return g_cmd_table[i].func(cmd);
    }
    return -1;
}
"""

SOURCE_TWO_SITES = """\
#include "disp.h"

static const struct ns_cmd_t g_cmd_table[] = {
    { CMD_A, handle_a },
    { CMD_B, handle_b },
};

static int dispatch(const void *cmd)
{
    if (g_cmd_table[0].func != 0)
        g_cmd_table[0].func(cmd);
    return g_cmd_table[1].func(cmd);
}
"""


def _insert_file(db, path):
    db.execute("INSERT OR IGNORE INTO files (path, struct_hash, language, layer, imports) "
               "VALUES (?, 'h', 'CCppParser', 'Core', '[]')", (path,))


def _insert_symbol(db, path, name, sym_type="function"):
    _insert_file(db, path)
    short = name.split(".")[-1]
    db.execute("INSERT OR IGNORE INTO symbols (file_path, name, short_name, type) VALUES (?,?,?,?)",
               (path, name, short, sym_type))


def _insert_patterns(db, path, patterns):
    _insert_file(db, path)
    for pat in patterns:
        db.execute(
            "INSERT INTO patterns (file_path, pattern_type, signal_name, handler, line, metadata) "
            "VALUES (?,?,?,?,?,?)",
            (path, pat["pattern_type"], pat.get("signal_name"), pat.get("handler"),
             pat.get("line"), json.dumps(pat["metadata"]) if pat.get("metadata") else None)
        )


@pytest.fixture
def db_with_schema(tmp_path):
    db = sqlite3.connect(str(tmp_path / "test.db"))
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    return db


class TestExtractPatterns:
    def test_emits_four_fact_families(self):
        parser = CCppParser()
        header_pats = parser.extract_patterns(HEADER, "disp.h")
        source_pats = parser.extract_patterns(SOURCE, "disp.c")

        typedefs = [p for p in header_pats if p["pattern_type"] == "c_fnptr_typedef"]
        assert any(p["signal_name"] == "sync_func" for p in typedefs)

        layouts = [p for p in header_pats if p["pattern_type"] == "c_struct_layout"]
        ns = next(p for p in layouts if p["signal_name"] == "ns_cmd_t")
        fields = {f["name"]: f for f in ns["metadata"]["fields"]}
        assert fields["func"]["type"] == "sync_func"
        assert fields["func"]["index"] == 1

        regs = [p for p in source_pats if p["pattern_type"] == "c_fnptr_register"]
        handlers = {p["handler"] for p in regs}
        assert {"handle_a", "handle_b", "handle_c"}.issubset(handlers)

        disp = [p for p in source_pats if p["pattern_type"] == "c_fnptr_dispatch"]
        assert any(p["signal_name"] == "func" and p["handler"] == "dispatch" for p in disp)


class TestSynthesizer:
    def _seed(self, db, handler_file="handlers.c"):
        parser = CCppParser()
        _insert_patterns(db, "disp.h", parser.extract_patterns(HEADER, "disp.h"))
        _insert_patterns(db, "disp.c", parser.extract_patterns(SOURCE, "disp.c"))
        _insert_symbol(db, "disp.c", "dispatch")
        for h in ("handle_a", "handle_b", "handle_c"):
            _insert_symbol(db, handler_file, h)
        db.commit()

    def test_dispatcher_to_handlers_edges(self, db_with_schema):
        db = db_with_schema
        self._seed(db)
        synthesize_c_fnptr_dispatch_edges(db)

        rows = db.execute(
            "SELECT caller, callee, provenance, via FROM edges WHERE via = 'c-fnptr-dispatch'"
        ).fetchall()
        callees = {r[1] for r in rows}
        assert callees == {"handle_a", "handle_b", "handle_c"}
        assert all(r[0] == "dispatch" for r in rows)
        assert all(r[2] == "inferred" for r in rows)

    def test_data_field_not_linked(self, db_with_schema):
        db = db_with_schema
        self._seed(db)
        # CMD_A/B/C are the data (cmd_id) slot; register them as symbols to prove
        # the synthesizer never links a non-fn-pointer field's slot.
        for c in ("CMD_A", "CMD_B", "CMD_C"):
            _insert_symbol(db, "cmds.c", c, sym_type="macro")
        db.commit()
        synthesize_c_fnptr_dispatch_edges(db)

        callees = {r[0] for r in db.execute(
            "SELECT callee FROM edges WHERE via = 'c-fnptr-dispatch'").fetchall()}
        assert callees == {"handle_a", "handle_b", "handle_c"}
        assert not callees & {"CMD_A", "CMD_B", "CMD_C"}

    def test_intra_run_dedup(self, db_with_schema):
        db = db_with_schema
        parser = CCppParser()
        _insert_patterns(db, "disp.h", parser.extract_patterns(HEADER, "disp.h"))
        _insert_patterns(db, "two.c", parser.extract_patterns(SOURCE_TWO_SITES, "two.c"))
        _insert_symbol(db, "two.c", "dispatch")
        for h in ("handle_a", "handle_b"):
            _insert_symbol(db, "handlers.c", h)
        db.commit()
        synthesize_c_fnptr_dispatch_edges(db)
        rows = db.execute(
            "SELECT caller, callee FROM edges WHERE via='c-fnptr-dispatch'").fetchall()
        # two dispatch sites in one function must not double the edges (seen-set dedup)
        assert sorted(r[1] for r in rows) == ["handle_a", "handle_b"]
        assert all(r[0] == "dispatch" for r in rows)

    def test_rerun_returns_zero_and_keeps_single_identity(self, db_with_schema):
        db = db_with_schema
        self._seed(db)
        assert synthesize_c_fnptr_dispatch_edges(db) == 3
        assert synthesize_c_fnptr_dispatch_edges(db) == 0
        assert db.execute(
            "SELECT COUNT(*) FROM edges WHERE via='c-fnptr-dispatch'"
        ).fetchone()[0] == 3

    def test_handler_rename_replaces_edge_after_global_rebuild(self, db_with_schema):
        db = db_with_schema
        self._seed(db)
        synthesize_c_fnptr_dispatch_edges(db)
        db.execute(
            "UPDATE patterns SET handler='handle_renamed' "
            "WHERE pattern_type='c_fnptr_register' AND handler='handle_a'"
        )
        db.execute(
            "UPDATE symbols SET name='handle_renamed', short_name='handle_renamed' "
            "WHERE file_path='handlers.c' AND name='handle_a'"
        )
        db.execute("DELETE FROM edges WHERE provenance='inferred'")

        assert synthesize_c_fnptr_dispatch_edges(db) == 3
        callees = {
            row[0] for row in db.execute(
                "SELECT callee FROM edges WHERE via='c-fnptr-dispatch'"
            ).fetchall()
        }
        assert callees == {"handle_renamed", "handle_b", "handle_c"}

    def test_handler_delete_removes_edge_after_global_rebuild(self, db_with_schema):
        db = db_with_schema
        self._seed(db)
        synthesize_c_fnptr_dispatch_edges(db)
        db.execute(
            "DELETE FROM symbols WHERE file_path='handlers.c' AND name='handle_a'"
        )
        db.execute("DELETE FROM edges WHERE provenance='inferred'")

        assert synthesize_c_fnptr_dispatch_edges(db) == 2
        callees = {
            row[0] for row in db.execute(
                "SELECT callee FROM edges WHERE via='c-fnptr-dispatch'"
            ).fetchall()
        }
        assert callees == {"handle_b", "handle_c"}

    def test_registration_delete_removes_edge_after_global_rebuild(self, db_with_schema):
        db = db_with_schema
        self._seed(db)
        synthesize_c_fnptr_dispatch_edges(db)
        db.execute(
            "DELETE FROM patterns WHERE pattern_type='c_fnptr_register' "
            "AND handler='handle_a'"
        )
        db.execute("DELETE FROM edges WHERE provenance='inferred'")

        assert synthesize_c_fnptr_dispatch_edges(db) == 2
        callees = {
            row[0] for row in db.execute(
                "SELECT callee FROM edges WHERE via='c-fnptr-dispatch'"
            ).fetchall()
        }
        assert callees == {"handle_b", "handle_c"}

    def test_dispatch_delete_removes_all_edges_after_global_rebuild(self, db_with_schema):
        db = db_with_schema
        self._seed(db)
        synthesize_c_fnptr_dispatch_edges(db)
        db.execute(
            "DELETE FROM patterns WHERE pattern_type='c_fnptr_dispatch'"
        )
        db.execute("DELETE FROM edges WHERE provenance='inferred'")

        assert synthesize_c_fnptr_dispatch_edges(db) == 0
        assert db.execute(
            "SELECT COUNT(*) FROM edges WHERE via='c-fnptr-dispatch'"
        ).fetchone()[0] == 0

    def test_unresolved_handler_skipped(self, db_with_schema):
        db = db_with_schema
        parser = CCppParser()
        _insert_patterns(db, "disp.h", parser.extract_patterns(HEADER, "disp.h"))
        _insert_patterns(db, "disp.c", parser.extract_patterns(SOURCE, "disp.c"))
        _insert_symbol(db, "disp.c", "dispatch")
        # only handle_a exists as a symbol; b/c unresolved -> no phantom edges
        _insert_symbol(db, "handlers.c", "handle_a")
        db.commit()
        synthesize_c_fnptr_dispatch_edges(db)
        callees = {r[0] for r in db.execute(
            "SELECT callee FROM edges WHERE via='c-fnptr-dispatch'").fetchall()}
        assert callees == {"handle_a"}
