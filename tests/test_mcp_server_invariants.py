"""Transport-boundary invariant tests for index_mcp_server (A1.3).

Pins four of the five invariants declared in plans/remy-index-evolution-plan.md
section A1.3. Invariant (d) — results starting with "Error:" bypass the
freshness prefix — is covered by tests/test_freshness.py::TestWithFreshness.

(a) No pipe-creating call (subprocess usage, os.popen, os.pipe) outside the
    startup-only functions of the server, and none at all in the query owner
    modules on the tool-handler call path.
(b) _init_freshness() runs before mcp.run() in the __main__ block.
(c) All 12 @mcp.tool handlers are synchronous ``def``.
(e) Missing MCP SDK and REMY_MCP_SERVER_ENABLED=false both exit 0 with a
    diagnostic on stderr.
"""
import ast
import os
import subprocess
import sys

_TESTS_DIR = os.path.dirname(__file__)
_REMY_SRC = os.path.join(_TESTS_DIR, "..", "remy-src")
_SERVER_PATH = os.path.join(_REMY_SRC, "index_mcp_server.py")
_IMPACT_PATH = os.path.join(_TESTS_DIR, "..", "skills", "remy-index", "impact.py")

_OWNER_MODULE_FILES = (
    "index_mcp_common.py",
    "index_mcp_facts.py",
    "index_mcp_graph.py",
    "index_mcp_search.py",
    "index_mcp_navigate.py",
)

_SUBPROCESS_ALLOWED_FUNCS = {"_resolve_git_head", "_init_freshness"}
_HANDLER_COUNT = 12


def _parse(path):
    with open(path, "r", encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


def _pipe_creating_refs(tree):
    """Nodes referencing the subprocess module or os.popen/os.pipe.

    A bare ``import subprocess`` statement is not flagged; expression
    references (calls, attribute access, exception tuples) and
    ``from subprocess/os import ...`` bindings are.
    """
    refs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "subprocess":
            refs.append(node)
        elif (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in ("popen", "pipe")):
            refs.append(node)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                refs.append(node)
            elif node.module == "os" and any(
                    alias.name in ("popen", "pipe") for alias in node.names):
                refs.append(node)
    return refs


class TestNoPipeCreationInHandlerPath:
    def test_server_subprocess_refs_confined_to_startup_functions(self):
        tree = _parse(_SERVER_PATH)
        spans = [
            (node.name, node.lineno, node.end_lineno or node.lineno)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        offenders = []
        for ref in _pipe_creating_refs(tree):
            owners = {name for name, start, end in spans if start <= ref.lineno <= end}
            if not owners or not owners <= _SUBPROCESS_ALLOWED_FUNCS:
                offenders.append((ref.lineno, sorted(owners)))
        assert offenders == []

    def test_owner_modules_and_impact_have_no_pipe_creating_refs(self):
        paths = [os.path.join(_REMY_SRC, name) for name in _OWNER_MODULE_FILES]
        paths.append(_IMPACT_PATH)
        for path in paths:
            refs = _pipe_creating_refs(_parse(path))
            assert not refs, (path, [ref.lineno for ref in refs])


class TestStartupOrder:
    def test_init_freshness_precedes_mcp_run_in_main_block(self):
        tree = _parse(_SERVER_PATH)
        main_block = None
        for node in tree.body:
            if (isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Name)
                    and node.test.left.id == "__name__"):
                main_block = node
                break
        assert main_block is not None
        calls = []
        for stmt in main_block.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func = stmt.value.func
                if isinstance(func, ast.Name):
                    calls.append(func.id)
                elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    calls.append(f"{func.value.id}.{func.attr}")
        assert "_init_freshness" in calls
        assert "mcp.run" in calls
        assert calls.index("_init_freshness") < calls.index("mcp.run")


class TestHandlersAreSynchronous:
    def test_twelve_sync_handlers_zero_async(self):
        tree = _parse(_SERVER_PATH)
        sync_handlers = []
        async_handlers = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if (isinstance(target, ast.Attribute)
                        and target.attr == "tool"
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "mcp"):
                    if isinstance(node, ast.AsyncFunctionDef):
                        async_handlers.append(node.name)
                    else:
                        sync_handlers.append(node.name)
        assert async_handlers == []
        assert len(sync_handlers) == _HANDLER_COUNT


class TestExitBehavior:
    def _run_server(self, cwd, env_extra):
        env = {**os.environ, **env_extra}
        return subprocess.run(
            [sys.executable, os.path.abspath(_SERVER_PATH)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
            timeout=30,
        )

    def test_missing_sdk_exits_zero_with_message(self, tmp_path):
        stub_pkg = tmp_path / "stub" / "mcp"
        stub_pkg.mkdir(parents=True)
        (stub_pkg / "__init__.py").write_text("", encoding="utf-8")
        result = self._run_server(tmp_path, {"PYTHONPATH": str(tmp_path / "stub")})
        assert result.returncode == 0
        assert "not installed" in result.stderr

    def test_disabled_flag_exits_zero_with_message(self, tmp_path):
        result = self._run_server(tmp_path, {"REMY_MCP_SERVER_ENABLED": "false"})
        assert result.returncode == 0
        assert "disabled" in result.stderr
