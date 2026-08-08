"""Tests for eval/arms.py RemyTools dispatch (database_override routing)."""
import os
import sys

_REMY_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _REMY_ROOT)


class TestRemyToolsDispatch:
    def _tools(self, db_dir):
        from eval.arms import RemyTools
        db_path = db_dir / ".claude" / "logic_index.db"
        tools = RemyTools(db_path)
        tools.load()
        return tools

    def test_dispatch_routes_override_and_returns_query_text(self, db_dir):
        tools = self._tools(db_dir)
        out = tools.dispatch("query_symbol", {"name": "main"})
        assert not out.startswith("ERROR")
        assert "a.py::main" in out

    def test_dispatch_unknown_tool_returns_message(self, db_dir):
        tools = self._tools(db_dir)
        assert tools.dispatch("no_such_tool", {}) == "unknown tool: no_such_tool"

    def test_dispatch_before_load_reports_not_loaded(self, db_dir):
        from eval.arms import RemyTools
        tools = RemyTools(db_dir / ".claude" / "logic_index.db")
        tools._schemas["query_symbol"] = {}
        assert tools.dispatch("query_symbol", {"name": "main"}) == (
            "Remy MCP tools are not loaded"
        )
