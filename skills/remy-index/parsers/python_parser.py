"""
Python language parser using the standard library ast module.
Extracted from the original run.py Logic Indexer.
"""

import ast
import hashlib
import os
import re
import sys
from .base import LanguageParser, SymbolInfo, EdgeInfo


class ImportVisitor(ast.NodeVisitor):
    """AST visitor to collect internal imports."""

    def __init__(self, root_dir, current_file_path):
        self.root_dir = root_dir
        self.current_dir = os.path.dirname(current_file_path)
        self.internal_imports = {}

    def visit_Import(self, node):
        for alias in node.names:
            has_alias = alias.asname is not None
            self._add_import(alias.name, has_alias)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        for alias in node.names:
            has_alias = alias.asname is not None
            if module:
                full_name = f"{module}.{alias.name}"
            else:
                full_name = alias.name
            if self._add_import(full_name, has_alias, node.level):
                continue
            if module:
                self._add_import(module, has_alias, node.level)

    def _add_import(self, module_name, has_alias, level=0):
        if module_name:
            parts = module_name.split('.')
        else:
            parts = []

        if level > 0:
            base = self.current_dir
            for _ in range(level - 1):
                base = os.path.dirname(base)
            potential_path = os.path.join(base, *parts)
        else:
            potential_path = os.path.join(self.root_dir, *parts)

        py_file = potential_path + ".py"
        init_file = os.path.join(potential_path, "__init__.py")

        found_path = None
        if os.path.exists(py_file):
            found_path = os.path.relpath(py_file, self.root_dir).replace(os.sep, '/')
        elif os.path.exists(init_file):
            found_path = os.path.relpath(init_file, self.root_dir).replace(os.sep, '/')

        if found_path:
            current_alias = self.internal_imports.get(found_path, False)
            self.internal_imports[found_path] = current_alias or has_alias
            return True
        return False


class UsageVisitor(ast.NodeVisitor):
    """AST visitor to collect used identifiers (names and attributes)."""

    def __init__(self):
        self.used_names = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.used_names.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        self.used_names.add(node.attr)
        self.generic_visit(node)


class PythonParser(LanguageParser):
    """Parser for Python source files using the standard library ast module."""

    def __init__(self):
        self._cached_hash = None
        self._cached_tree = None

    def _get_tree(self, source):
        """Return cached AST tree, re-parsing only if source changed."""
        h = hashlib.md5(source.encode('utf-8')).hexdigest()
        if h != self._cached_hash:
            self._cached_hash = h
            self._cached_tree = ast.parse(source)
        return self._cached_tree

    def get_extensions(self):
        return [".py"]

    def get_complexity_indicators(self):
        return [
            "yield", "__metaclass__", "getattr", "setattr", "eval", "exec",
            "ast.", "compile(", "locals(", "globals(", "importlib", "__import__",
            "sys.modules", "pickle", "dill"
        ]

    def get_prompt_template_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompt.md")

    def resolve_imports(self, source, file_path, root_dir):
        try:
            tree = self._get_tree(source)
        except SyntaxError:
            return {}
        visitor = ImportVisitor(root_dir, file_path)
        visitor.visit(tree)
        return visitor.internal_imports

    def collect_used_names(self, source):
        try:
            tree = self._get_tree(source)
        except SyntaxError:
            return set()
        visitor = UsageVisitor()
        visitor.visit(tree)
        return visitor.used_names

    def parse_symbols(self, source, file_path):
        try:
            tree = self._get_tree(source)
        except SyntaxError:
            return []

        symbols = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                sym = self._extract_symbol(node, source)
                if sym:
                    symbols.append(sym)

                if isinstance(node, ast.ClassDef):
                    for subnode in node.body:
                        if isinstance(subnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            child_sym = self._extract_symbol(subnode, source, parent_name=node.name)
                            if child_sym:
                                symbols.append(child_sym)
        return symbols

    def _extract_symbol(self, node, source, parent_name=None):
        symbol_name = f"{parent_name}.{node.name}" if parent_name else node.name
        symbol_type = "class" if isinstance(node, ast.ClassDef) else "function"

        try:
            segment = ast.get_source_segment(source, node)
        except Exception:
            segment = None
        if not segment:
            return None

        args_str = ""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            try:
                if sys.version_info >= (3, 9):
                    args_str = f"({ast.unparse(node.args)})"
                else:
                    args_str = "(...)"
            except Exception:
                pass

        docstring = ast.get_docstring(node)

        bases_list = None
        if isinstance(node, ast.ClassDef) and node.bases:
            bases_list = []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases_list.append(base.id)
                elif isinstance(base, ast.Attribute):
                    bases_list.append(base.attr)

        return SymbolInfo(
            name=symbol_name,
            args=args_str,
            type=symbol_type,
            lineno=node.lineno,
            source_segment=segment,
            end_lineno=node.end_lineno,
            docstring=docstring,
            bases=bases_list,
        )

    def extract_call_graph(self, source, file_path):
        try:
            tree = self._get_tree(source)
        except SyntaxError:
            return []

        edges = []
        function_stack = []

        def _walk(node, parent_class=None):
            pushed = False

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fname = f"{parent_class}.{node.name}" if parent_class else node.name
                function_stack.append(fname)
                pushed = True

            if isinstance(node, ast.Call) and function_stack:
                callee = None
                if isinstance(node.func, ast.Name):
                    callee = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    callee = node.func.attr
                if callee:
                    edges.append(EdgeInfo(
                        caller=function_stack[-1],
                        callee=callee,
                        line=node.lineno,
                    ))

            child_class = None
            if isinstance(node, ast.ClassDef):
                child_class = node.name

            for child in ast.iter_child_nodes(node):
                _walk(child, parent_class=child_class if isinstance(node, ast.ClassDef) else parent_class)

            if pushed:
                function_stack.pop()

        _walk(tree)
        return edges

    _DJANGO_CONNECT_RE = re.compile(r'(\w+)\.connect\(\s*(\w+)')
    _DJANGO_SEND_RE = re.compile(r'(\w+)\.send\(')
    _PYQT_CONNECT_RE = re.compile(r'(\w+)\.connect\(\s*(?:self\.)?(\w+)')
    _PYQT_EMIT_RE = re.compile(r'(\w+)\.emit\(')
    _OBSERVER_APPEND_RE = re.compile(r'self\.(\w+)\.(?:append|add|insert)\(')
    _OBSERVER_ITER_RE = re.compile(r'for\s+(\w+)\s+in\s+self\.(\w+)\s*:')
    _OBSERVER_INVOKE_RE = re.compile(r'\b(\w+)\s*\(')

    def extract_patterns(self, source: str, file_path: str) -> list:
        results = []
        symbols = self.parse_symbols(source, file_path)

        def _line_at(pos):
            return source[:pos].count('\n') + 1

        def _enclosing_func(line):
            best = None
            for sym in symbols:
                if sym.type != "function":
                    continue
                end = sym.end_lineno or sym.lineno
                if sym.lineno <= line <= end:
                    if best is None or sym.lineno >= best.lineno:
                        best = sym
            return best.name if best else None

        has_django = '.connect(' in source or '.send(' in source
        has_pyqt = ('from PyQt' in source or 'from PySide' in source) and (
            '.connect(' in source or '.emit(' in source
        )

        if has_django:
            for m in self._DJANGO_SEND_RE.finditer(source):
                line = _line_at(m.start())
                handler = _enclosing_func(line)
                results.append({
                    "pattern_type": "django_signal_send",
                    "signal_name": m.group(1),
                    "handler": handler,
                    "line": line,
                    "metadata": None,
                })
            for m in self._DJANGO_CONNECT_RE.finditer(source):
                line = _line_at(m.start())
                results.append({
                    "pattern_type": "django_signal_connect",
                    "signal_name": m.group(1),
                    "handler": m.group(2),
                    "line": line,
                    "metadata": None,
                })

        if has_pyqt:
            for m in self._PYQT_EMIT_RE.finditer(source):
                line = _line_at(m.start())
                handler = _enclosing_func(line)
                results.append({
                    "pattern_type": "pyqt_signal_emit",
                    "signal_name": m.group(1),
                    "handler": handler,
                    "line": line,
                    "metadata": None,
                })
            for m in self._PYQT_CONNECT_RE.finditer(source):
                line = _line_at(m.start())
                results.append({
                    "pattern_type": "pyqt_signal_connect",
                    "signal_name": m.group(1),
                    "handler": m.group(2),
                    "line": line,
                    "metadata": None,
                })

        for m in self._OBSERVER_ITER_RE.finditer(source):
            loop_var = m.group(1)
            field_name = m.group(2)
            after_colon = source[m.end():]
            invoke_match = self._OBSERVER_INVOKE_RE.match(after_colon.lstrip())
            if invoke_match and invoke_match.group(1) == loop_var:
                line = _line_at(m.start())
                handler = _enclosing_func(line)
                results.append({
                    "pattern_type": "observer_emit",
                    "signal_name": field_name,
                    "handler": handler,
                    "line": line,
                    "metadata": None,
                })

        for m in self._OBSERVER_APPEND_RE.finditer(source):
            field_name = m.group(1)
            line = _line_at(m.start())
            handler = _enclosing_func(line)
            results.append({
                "pattern_type": "observer_register",
                "signal_name": field_name,
                "handler": handler,
                "line": line,
                "metadata": None,
            })

        return results
