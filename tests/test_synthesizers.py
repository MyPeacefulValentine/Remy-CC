"""Tests for synthesizers (interface_dispatch + event_emitter) SQL implementations."""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import StructScanner, SCHEMA_SQL


@pytest.fixture
def db_with_schema(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    return db


class TestInterfaceDispatch:
    def test_override_edge_created(self, db_with_schema):
        db = db_with_schema
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('base.py','h1','PythonParser','Core','[]')")
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('impl.py','h2','PythonParser','Core','[]')")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,bases) VALUES ('base.py','Animal','Animal','class',NULL)")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno) VALUES ('base.py','Animal.speak','speak','function','(self)',10,12)")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,bases) VALUES ('impl.py','Dog','Dog','class',?)", (json.dumps(["Animal"]),))
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno,end_lineno) VALUES ('impl.py','Dog.speak','speak','function','(self)',5,7)")
        db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
        db.commit()

        from synthesizers.interface_dispatch import synthesize_interface_override_edges
        synthesize_interface_override_edges(db)

        edges = db.execute("SELECT caller, callee, via FROM edges WHERE provenance='inferred'").fetchall()
        assert len(edges) == 1
        assert edges[0][0] == "Animal.speak"
        assert edges[0][2] == "interface-impl"

    def test_fanout_cap_respected(self, db_with_schema):
        db = db_with_schema
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('base.py','h1','P','Core','[]')")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,bases) VALUES ('base.py','Base','Base','class',NULL)")
        for i in range(15):
            db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno) VALUES ('base.py',?,?,'function','(self)',?)",
                       (f"Base.method_{i}", f"method_{i}", i+1))

        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('impl.py','h2','P','Core','[]')")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,bases) VALUES ('impl.py','Child','Child','class',?)", (json.dumps(["Base"]),))
        for i in range(15):
            db.execute("INSERT INTO symbols (file_path,name,short_name,type,args,lineno) VALUES ('impl.py',?,?,'function','(self)',?)",
                       (f"Child.method_{i}", f"method_{i}", i+1))
        db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
        db.commit()

        os.environ["SYNTH_INTERFACE_FANOUT_CAP"] = "5"
        try:
            from synthesizers.interface_dispatch import synthesize_interface_override_edges
            synthesize_interface_override_edges(db)
        finally:
            del os.environ["SYNTH_INTERFACE_FANOUT_CAP"]

        count = db.execute("SELECT COUNT(*) FROM edges WHERE via='interface-impl'").fetchone()[0]
        assert count == 5

    def test_no_bases_no_edges(self, db_with_schema):
        db = db_with_schema
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('solo.py','h1','P','Core','[]')")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,bases) VALUES ('solo.py','Solo','Solo','class',NULL)")
        db.execute("INSERT INTO symbols (file_path,name,short_name,type,args) VALUES ('solo.py','Solo.run','run','function','(self)')")
        db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
        db.commit()

        from synthesizers.interface_dispatch import synthesize_interface_override_edges
        synthesize_interface_override_edges(db)

        count = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert count == 0


class TestEventEmitter:
    def test_django_signal_edge(self, db_with_schema):
        db = db_with_schema
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('sender.py','h1','P','Core','[]')")
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('receiver.py','h2','P','Core','[]')")
        db.execute("INSERT INTO patterns (file_path,pattern_type,signal_name,handler,line) VALUES ('sender.py','django_signal_send','post_save','on_save',10)")
        db.execute("INSERT INTO patterns (file_path,pattern_type,signal_name,handler,line) VALUES ('receiver.py','django_signal_connect','post_save','handle_save',5)")
        db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
        db.commit()

        from synthesizers.event_emitter import synthesize_event_emitter_edges
        synthesize_event_emitter_edges(db)

        edges = db.execute("SELECT caller, callee, via FROM edges WHERE provenance='inferred'").fetchall()
        assert len(edges) == 1
        assert edges[0][0] == "on_save"
        assert edges[0][1] == "handle_save"
        assert edges[0][2] == "django-signal"

    def test_no_matching_signal_no_edge(self, db_with_schema):
        db = db_with_schema
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('a.py','h1','P','Core','[]')")
        db.execute("INSERT INTO patterns (file_path,pattern_type,signal_name,handler,line) VALUES ('a.py','django_signal_send','sig_a','emitter',1)")
        db.execute("INSERT INTO patterns (file_path,pattern_type,signal_name,handler,line) VALUES ('a.py','django_signal_connect','sig_b','handler',2)")
        db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
        db.commit()

        from synthesizers.event_emitter import synthesize_event_emitter_edges
        synthesize_event_emitter_edges(db)

        count = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert count == 0

    def test_observer_pattern_edge(self, db_with_schema):
        db = db_with_schema
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('pub.py','h1','P','Core','[]')")
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('sub.py','h2','P','Core','[]')")
        db.execute("INSERT INTO patterns (file_path,pattern_type,signal_name,handler,line) VALUES ('pub.py','observer_emit','listeners','dispatch',20)")
        db.execute("INSERT INTO patterns (file_path,pattern_type,signal_name,handler,line) VALUES ('sub.py','observer_register','listeners','subscribe',5)")
        db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
        db.commit()

        from synthesizers.event_emitter import synthesize_event_emitter_edges
        synthesize_event_emitter_edges(db)

        edges = db.execute("SELECT via FROM edges WHERE provenance='inferred'").fetchall()
        assert len(edges) == 1
        assert edges[0][0] == "observer"

    def test_self_loop_excluded(self, db_with_schema):
        db = db_with_schema
        db.execute("INSERT INTO files (path, struct_hash, language, layer, imports) VALUES ('loop.py','h1','P','Core','[]')")
        db.execute("INSERT INTO patterns (file_path,pattern_type,signal_name,handler,line) VALUES ('loop.py','pyqt_signal_emit','clicked','on_click',10)")
        db.execute("INSERT INTO patterns (file_path,pattern_type,signal_name,handler,line) VALUES ('loop.py','pyqt_signal_connect','clicked','on_click',5)")
        db.execute("INSERT INTO meta VALUES ('version','4.0.0')")
        db.commit()

        from synthesizers.event_emitter import synthesize_event_emitter_edges
        synthesize_event_emitter_edges(db)

        count = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        assert count == 0
