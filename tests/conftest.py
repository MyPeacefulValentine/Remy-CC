"""Shared fixtures for MCP query tests."""
import os
import sqlite3
import sys

import pytest

_REMY_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _REMY_ROOT)
sys.path.insert(0, os.path.join(_REMY_ROOT, "remy-src"))
sys.path.insert(0, os.path.join(_REMY_ROOT, "skills", "remy-index"))

from struct_scan import SCHEMA_SQL
import retrieval_projection


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
    db.execute("INSERT INTO edges VALUES (NULL,'a.py','main','process','b.py','b.py::process',5,'definite',NULL,NULL,'name')")
    db.execute("INSERT INTO edges VALUES (NULL,'a.py','main','helper',NULL,'a.py::helper',3,'definite',NULL,NULL,'name')")
    db.execute("INSERT INTO edges VALUES (NULL,'a.py','helper','run','b.py','b.py::Util.run',14,'inferred',NULL,'interface-impl','name')")
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
    retrieval_projection.rebuild_projection(db)
    db.commit()
    db.close()
    return tmp_path
