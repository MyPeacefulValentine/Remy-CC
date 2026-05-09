"""
C/C++ language parser.
Uses tree-sitter when available for high-precision parsing.
Falls back to regex-based extraction otherwise.
"""

import os
import re
from .base import LanguageParser, SymbolInfo, EdgeInfo

TREE_SITTER_AVAILABLE = False
_c_language = None
_cpp_language = None

try:
    from tree_sitter import Language, Parser as TSParser
    import tree_sitter_c
    import tree_sitter_cpp
    _c_language = Language(tree_sitter_c.language())
    _cpp_language = Language(tree_sitter_cpp.language())
    TREE_SITTER_AVAILABLE = True
except Exception:
    pass

# --- Regex Patterns (Fallback) ---

RE_INCLUDE_LOCAL = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.MULTILINE)

RE_DOXYGEN_BLOCK = re.compile(r'/\*\*(.+?)\*/', re.DOTALL)
RE_DOXYGEN_LINE = re.compile(r'^\s*///\s?(.*)', re.MULTILINE)

RE_FUNC = re.compile(
    r'^[ \t]*'
    r'(?:(?:static|inline|extern|const|volatile|unsigned|signed|long|short|register|__attribute__\s*\([^)]*\))\s+)*'
    r'(?:(?:struct|enum|union)\s+)?'
    r'([\w][\w\s\*&:<>]*?)\s+'
    r'(\*?\s*\w[\w:]*)\s*'
    r'\(([^)]*)\)\s*'
    r'(?:const\s*)?'
    r'(?:override\s*)?'
    r'(?:noexcept(?:\s*\([^)]*\))?\s*)?'
    r'\{',
    re.MULTILINE
)

RE_STRUCT = re.compile(r'^[ \t]*(?:typedef\s+)?struct\s+(\w+)\s*\{', re.MULTILINE)
RE_CLASS = re.compile(r'^[ \t]*(?:template\s*<[^>]*>\s*)?class\s+(\w+)\s*(?:final\s*)?(?::\s*[^{]+)?\{', re.MULTILINE)
RE_ENUM = re.compile(r'^[ \t]*(?:typedef\s+)?enum\s+(?:class\s+)?(\w+)\s*(?::\s*\w+\s*)?\{', re.MULTILINE)
RE_TYPEDEF = re.compile(r'^[ \t]*typedef\s+.+?\s+(\w+)\s*;', re.MULTILINE)
RE_NAMESPACE = re.compile(r'^[ \t]*namespace\s+(\w+)\s*\{', re.MULTILINE)
RE_FUNC_MACRO = re.compile(r'^[ \t]*#\s*define\s+(\w+)\s*\(([^)]*)\)', re.MULTILINE)


# --- Shared Utilities ---

def _find_matching_brace(source, start_pos):
    """Find the position of the closing brace matching the opening brace at start_pos."""
    depth = 0
    in_string = False
    in_char = False
    in_line_comment = False
    in_block_comment = False
    escape_next = False
    i = start_pos

    while i < len(source):
        ch = source[i]

        if escape_next:
            escape_next = False
            i += 1
            continue

        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == '*' and i + 1 < len(source) and source[i + 1] == '/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if ch == '\\' and (in_string or in_char):
            escape_next = True
            i += 1
            continue

        if ch == '"' and not in_char:
            in_string = not in_string
            i += 1
            continue

        if ch == "'" and not in_string:
            in_char = not in_char
            i += 1
            continue

        if in_string or in_char:
            i += 1
            continue

        if ch == '/' and i + 1 < len(source):
            next_ch = source[i + 1]
            if next_ch == '/':
                in_line_comment = True
                i += 2
                continue
            elif next_ch == '*':
                in_block_comment = True
                i += 2
                continue

        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i

        i += 1
    return -1


def _extract_doxygen_before(source, pos):
    """Extract Doxygen comment immediately preceding the declaration at pos."""
    prefix = source[:pos].rstrip()

    block_match = RE_DOXYGEN_BLOCK.search(prefix)
    if block_match and prefix.endswith("*/"):
        raw = block_match.group(1)
        lines = [line.strip().lstrip('* ').strip() for line in raw.splitlines()]
        lines = [l for l in lines if l]
        if lines:
            return " ".join(lines[:3])

    rev_lines = prefix.splitlines()
    doc_lines = []
    for line in reversed(rev_lines):
        m = RE_DOXYGEN_LINE.match(line)
        if m:
            doc_lines.insert(0, m.group(1).strip())
        else:
            break
    if doc_lines:
        return " ".join(doc_lines[:3])

    return None


def _line_number_at(source, pos):
    """Return the 1-based line number for position pos in source."""
    return source[:pos].count('\n') + 1


# --- Tree-sitter Utilities ---

def _ts_node_text(node):
    """Get text content of a tree-sitter node as str."""
    return node.text.decode('utf-8') if node.text else ""


def _ts_extract_doxygen(source_bytes, node):
    """Extract Doxygen comment preceding a tree-sitter node."""
    prev = node.prev_named_sibling
    if prev and prev.type == 'comment':
        text = _ts_node_text(prev)
        if text.startswith('/**'):
            raw = text[3:].rstrip('*/').strip()
            lines = [l.strip().lstrip('* ').strip() for l in raw.splitlines() if l.strip().lstrip('* ').strip()]
            if lines:
                return " ".join(lines[:3])
        elif text.startswith('///'):
            doc_lines = [text[3:].strip()]
            cursor = prev.prev_named_sibling
            while cursor and cursor.type == 'comment' and _ts_node_text(cursor).startswith('///'):
                doc_lines.insert(0, _ts_node_text(cursor)[3:].strip())
                cursor = cursor.prev_named_sibling
            return " ".join(doc_lines[:3])
    return None


def _ts_func_name(node):
    """Extract function name from a function_definition or declaration node."""
    decl = node.child_by_field_name('declarator')
    if not decl:
        return None
    if decl.type == 'function_declarator':
        name_node = decl.child_by_field_name('declarator')
        if name_node:
            return _ts_node_text(name_node)
    elif decl.type == 'pointer_declarator':
        inner = decl.child_by_field_name('declarator')
        if inner and inner.type == 'function_declarator':
            name_node = inner.child_by_field_name('declarator')
            if name_node:
                return _ts_node_text(name_node)
    return None


def _ts_func_params(node):
    """Extract function parameters string."""
    decl = node.child_by_field_name('declarator')
    if not decl:
        return "()"
    if decl.type == 'pointer_declarator':
        decl = decl.child_by_field_name('declarator')
        if not decl:
            return "()"
    if decl.type == 'function_declarator':
        params = decl.child_by_field_name('parameters')
        if params:
            return _ts_node_text(params)
    return "()"


class CCppParser(LanguageParser):
    """Parser for C and C++ source files. Uses tree-sitter when available, regex otherwise."""

    def get_extensions(self):
        return [".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh", ".hxx"]

    def get_complexity_indicators(self):
        return [
            "template<", "template <",
            "#define", "##",
            "reinterpret_cast", "dynamic_cast",
            "decltype", "constexpr if",
            "va_list", "va_start",
            "__attribute__", "__declspec",
            "asm(", "__asm",
        ]

    def get_prompt_template_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompt_c.md")

    def resolve_imports(self, source, file_path, root_dir):
        imports = {}
        current_dir = os.path.dirname(file_path)

        for match in RE_INCLUDE_LOCAL.finditer(source):
            include_path = match.group(1)

            candidate = os.path.normpath(os.path.join(current_dir, include_path))
            if os.path.exists(candidate):
                rel = os.path.relpath(candidate, root_dir).replace(os.sep, '/')
                imports[rel] = False
                continue

            candidate = os.path.normpath(os.path.join(root_dir, include_path))
            if os.path.exists(candidate):
                rel = os.path.relpath(candidate, root_dir).replace(os.sep, '/')
                imports[rel] = False

        return imports

    def collect_used_names(self, source):
        names = set()
        cleaned = re.sub(r'//[^\n]*', '', source)
        cleaned = re.sub(r'/\*.*?\*/', '', cleaned, flags=re.DOTALL)
        cleaned = re.sub(r'"(?:[^"\\]|\\.)*"', '""', cleaned)
        cleaned = re.sub(r"'(?:[^'\\]|\\.)*'", "''", cleaned)

        for m in re.finditer(r'\b([a-zA-Z_]\w*)\b', cleaned):
            names.add(m.group(1))
        return names

    def parse_symbols(self, source, file_path):
        if TREE_SITTER_AVAILABLE:
            return self._parse_with_tree_sitter(source, file_path)
        return self._parse_with_regex(source, file_path)

    # ========================================================================
    # Tree-sitter Path
    # ========================================================================

    def _parse_with_tree_sitter(self, source, file_path):
        is_cpp = any(file_path.endswith(ext) for ext in [".cpp", ".hpp", ".cc", ".cxx", ".hh", ".hxx"])
        if not is_cpp and file_path.endswith('.h'):
            cpp_indicators = ['class ', 'namespace ', 'template<', 'template <',
                              'public:', 'private:', 'protected:', '::']
            is_cpp = any(ind in source for ind in cpp_indicators)
        lang = _cpp_language if is_cpp else _c_language
        parser = TSParser(lang)

        source_bytes = source.encode('utf-8')
        tree = parser.parse(source_bytes)

        symbols = []
        self._ts_walk_node(tree.root_node, source, source_bytes, symbols, parent_name=None)
        symbols.sort(key=lambda s: s.lineno)
        return symbols

    _TS_PREPROC_CONTAINERS = frozenset({
        'preproc_ifdef', 'preproc_if', 'preproc_else', 'preproc_elif',
    })

    def _ts_walk_node(self, node, source, source_bytes, symbols, parent_name):
        """Recursively walk tree-sitter AST and extract symbols."""
        for child in node.children:
            if child.type in self._TS_PREPROC_CONTAINERS:
                self._ts_walk_node(child, source, source_bytes, symbols, parent_name)

            elif child.type == 'function_definition':
                self._ts_extract_function(child, source, source_bytes, symbols, parent_name)

            elif child.type in ('struct_specifier', 'class_specifier'):
                self._ts_extract_class_or_struct(child, source, source_bytes, symbols, parent_name)

            elif child.type == 'enum_specifier':
                name_node = child.child_by_field_name('name')
                if name_node:
                    name = _ts_node_text(name_node)
                    full_name = f"{parent_name}.{name}" if parent_name else name
                    symbols.append(SymbolInfo(
                        name=full_name,
                        args="",
                        type="enum",
                        lineno=child.start_point[0] + 1,
                        source_segment=_ts_node_text(child),
                        docstring=_ts_extract_doxygen(source_bytes, child),
                    ))

            elif child.type == 'type_definition':
                decl = child.child_by_field_name('declarator')
                if decl:
                    name = _ts_node_text(decl)
                    full_name = f"{parent_name}.{name}" if parent_name else name
                    symbols.append(SymbolInfo(
                        name=full_name,
                        args="",
                        type="typedef",
                        lineno=child.start_point[0] + 1,
                        source_segment=_ts_node_text(child),
                        docstring=_ts_extract_doxygen(source_bytes, child),
                    ))

            elif child.type == 'namespace_definition':
                ns_name_node = child.child_by_field_name('name')
                ns_name = _ts_node_text(ns_name_node) if ns_name_node else None
                if ns_name:
                    full_ns = f"{parent_name}.{ns_name}" if parent_name else ns_name
                    symbols.append(SymbolInfo(
                        name=full_ns,
                        args="",
                        type="namespace",
                        lineno=child.start_point[0] + 1,
                        source_segment=_ts_node_text(child),
                        docstring=_ts_extract_doxygen(source_bytes, child),
                    ))
                    body = child.child_by_field_name('body')
                    if body:
                        self._ts_walk_node(body, source, source_bytes, symbols, parent_name=full_ns)

            elif child.type == 'template_declaration':
                for tc in child.children:
                    if tc.type in ('class_specifier', 'struct_specifier'):
                        self._ts_extract_class_or_struct(tc, source, source_bytes, symbols, parent_name)
                    elif tc.type == 'function_definition':
                        self._ts_extract_function(tc, source, source_bytes, symbols, parent_name)

            elif child.type == 'preproc_function_def':
                name_node = child.child_by_field_name('name')
                params_node = child.child_by_field_name('parameters')
                if name_node:
                    macro_name = _ts_node_text(name_node)
                    params = _ts_node_text(params_node) if params_node else "()"
                    symbols.append(SymbolInfo(
                        name=macro_name,
                        args=params,
                        type="macro",
                        lineno=child.start_point[0] + 1,
                        source_segment=_ts_node_text(child),
                        docstring=None,
                    ))

    def _ts_extract_function(self, node, source, source_bytes, symbols, parent_name):
        func_name = _ts_func_name(node)
        if not func_name:
            return
        full_name = f"{parent_name}.{func_name}" if parent_name else func_name
        params = _ts_func_params(node)
        symbols.append(SymbolInfo(
            name=full_name,
            args=params,
            type="function",
            lineno=node.start_point[0] + 1,
            source_segment=_ts_node_text(node),
            docstring=_ts_extract_doxygen(source_bytes, node),
        ))

    def _ts_extract_class_or_struct(self, node, source, source_bytes, symbols, parent_name):
        name_node = node.child_by_field_name('name')
        if not name_node:
            return
        name = _ts_node_text(name_node)
        full_name = f"{parent_name}.{name}" if parent_name else name
        sym_type = "class" if node.type == 'class_specifier' else "struct"

        symbols.append(SymbolInfo(
            name=full_name,
            args="",
            type=sym_type,
            lineno=node.start_point[0] + 1,
            source_segment=_ts_node_text(node),
            docstring=_ts_extract_doxygen(source_bytes, node),
        ))

        body = node.child_by_field_name('body')
        if body:
            for member in body.children:
                if member.type == 'function_definition':
                    self._ts_extract_function(member, source, source_bytes, symbols, parent_name=full_name)

    # ========================================================================
    # Regex Fallback Path
    # ========================================================================

    def _parse_with_regex(self, source, file_path):
        symbols = []
        seen_ranges = []

        def _overlaps(start, end):
            for s, e in seen_ranges:
                if start < e and end > s:
                    return True
            return False

        def _add_braced_symbol(match, name, sym_type, args_str=""):
            brace_pos = source.index('{', match.start())
            end_pos = _find_matching_brace(source, brace_pos)
            if end_pos == -1:
                end_pos = min(brace_pos + 500, len(source) - 1)

            if _overlaps(match.start(), end_pos + 1):
                return None

            segment = source[match.start():end_pos + 1]
            lineno = _line_number_at(source, match.start())
            docstring = _extract_doxygen_before(source, match.start())
            seen_ranges.append((match.start(), end_pos + 1))

            sym = SymbolInfo(
                name=name,
                args=args_str,
                type=sym_type,
                lineno=lineno,
                source_segment=segment,
                docstring=docstring,
            )
            symbols.append(sym)
            return sym

        ns_ranges = []
        for m in RE_NAMESPACE.finditer(source):
            brace_pos = source.index('{', m.start())
            end_pos = _find_matching_brace(source, brace_pos)
            if end_pos == -1:
                end_pos = min(brace_pos + 500, len(source) - 1)
            segment = source[m.start():end_pos + 1]
            lineno = _line_number_at(source, m.start())
            docstring = _extract_doxygen_before(source, m.start())
            ns_name = m.group(1)
            symbols.append(SymbolInfo(
                name=ns_name,
                args="",
                type="namespace",
                lineno=lineno,
                source_segment=segment,
                docstring=docstring,
            ))
            ns_ranges.append((brace_pos + 1, end_pos, ns_name))

        def _ns_prefix_for(pos):
            for ns_start, ns_end, ns_name in ns_ranges:
                if ns_start <= pos < ns_end:
                    return ns_name
            return None

        for m in RE_CLASS.finditer(source):
            prefix = _ns_prefix_for(m.start())
            name = f"{prefix}.{m.group(1)}" if prefix else m.group(1)
            class_sym = _add_braced_symbol(m, name, "class")
            if class_sym:
                self._regex_extract_class_methods(source, m, class_sym.name, symbols, seen_ranges)

        for m in RE_STRUCT.finditer(source):
            prefix = _ns_prefix_for(m.start())
            name = f"{prefix}.{m.group(1)}" if prefix else m.group(1)
            struct_sym = _add_braced_symbol(m, name, "struct")
            if struct_sym:
                self._regex_extract_class_methods(source, m, struct_sym.name, symbols, seen_ranges)

        for m in RE_ENUM.finditer(source):
            prefix = _ns_prefix_for(m.start())
            name = f"{prefix}.{m.group(1)}" if prefix else m.group(1)
            _add_braced_symbol(m, name, "enum")

        for m in RE_FUNC.finditer(source):
            brace_pos = source.index('{', m.start())
            end_pos = _find_matching_brace(source, brace_pos)
            if end_pos == -1:
                end_pos = min(brace_pos + 500, len(source) - 1)

            if _overlaps(m.start(), end_pos + 1):
                continue

            func_name = m.group(2).strip().lstrip('*').strip()
            prefix = _ns_prefix_for(m.start())
            if prefix:
                func_name = f"{prefix}.{func_name}"
            params = m.group(3).strip()
            args_str = f"({params})" if params else "()"
            segment = source[m.start():end_pos + 1]
            lineno = _line_number_at(source, m.start())
            docstring = _extract_doxygen_before(source, m.start())
            seen_ranges.append((m.start(), end_pos + 1))

            symbols.append(SymbolInfo(
                name=func_name,
                args=args_str,
                type="function",
                lineno=lineno,
                source_segment=segment,
                docstring=docstring,
            ))

        for m in RE_TYPEDEF.finditer(source):
            if not _overlaps(m.start(), m.end()):
                prefix = _ns_prefix_for(m.start())
                name = f"{prefix}.{m.group(1)}" if prefix else m.group(1)
                lineno = _line_number_at(source, m.start())
                docstring = _extract_doxygen_before(source, m.start())
                seen_ranges.append((m.start(), m.end()))
                symbols.append(SymbolInfo(
                    name=name,
                    args="",
                    type="typedef",
                    lineno=lineno,
                    source_segment=source[m.start():m.end()],
                    docstring=docstring,
                ))

        for m in RE_FUNC_MACRO.finditer(source):
            if not _overlaps(m.start(), m.end()):
                macro_name = m.group(1)
                prefix = _ns_prefix_for(m.start())
                if prefix:
                    macro_name = f"{prefix}.{macro_name}"
                macro_params = m.group(2).strip()
                lineno = _line_number_at(source, m.start())

                end = source.find('\n', m.end())
                while end > 0 and source[end - 1] == '\\':
                    end = source.find('\n', end + 1)
                if end == -1:
                    end = len(source)

                segment = source[m.start():end]
                seen_ranges.append((m.start(), end))
                symbols.append(SymbolInfo(
                    name=macro_name,
                    args=f"({macro_params})" if macro_params else "()",
                    type="macro",
                    lineno=lineno,
                    source_segment=segment,
                    docstring=None,
                ))

        symbols.sort(key=lambda s: s.lineno)
        return symbols

    def _regex_extract_class_methods(self, source, class_match, class_name, symbols, seen_ranges):
        """Extract method definitions inside a class/struct body (regex path)."""
        brace_pos = source.index('{', class_match.start())
        end_pos = _find_matching_brace(source, brace_pos)
        if end_pos == -1:
            return

        body = source[brace_pos + 1:end_pos]
        body_offset = brace_pos + 1

        for m in RE_FUNC.finditer(body):
            inner_brace = body.index('{', m.start())
            abs_start = body_offset + m.start()
            abs_brace = body_offset + inner_brace
            inner_end = _find_matching_brace(source, abs_brace)
            if inner_end == -1:
                continue

            abs_end = inner_end + 1

            func_name = m.group(2).strip().lstrip('*').strip()
            params = m.group(3).strip()
            segment = source[abs_start:abs_end]
            lineno = _line_number_at(source, abs_start)
            docstring = _extract_doxygen_before(source, abs_start)
            seen_ranges.append((abs_start, abs_end))

            symbols.append(SymbolInfo(
                name=f"{class_name}.{func_name}",
                args=f"({params})" if params else "()",
                type="function",
                lineno=lineno,
                source_segment=segment,
                docstring=docstring,
            ))

    def extract_call_graph(self, source, file_path):
        if not TREE_SITTER_AVAILABLE:
            return []

        is_cpp = any(file_path.endswith(ext) for ext in [".cpp", ".hpp", ".cc", ".cxx", ".hh", ".hxx"])
        if not is_cpp and file_path.endswith('.h'):
            cpp_indicators = ['class ', 'namespace ', 'template<', 'template <',
                              'public:', 'private:', 'protected:', '::']
            is_cpp = any(ind in source for ind in cpp_indicators)
        lang = _cpp_language if is_cpp else _c_language
        parser = TSParser(lang)

        source_bytes = source.encode('utf-8')
        tree = parser.parse(source_bytes)

        edges = []
        function_stack = []

        def _walk_calls(node):
            pushed = False
            if node.type == 'function_definition':
                name = _ts_func_name(node)
                if name:
                    function_stack.append(name)
                    pushed = True

            if node.type == 'call_expression' and function_stack:
                func_node = node.child_by_field_name('function')
                if func_node:
                    callee = _ts_node_text(func_node).split('(')[0].strip()
                    if '.' in callee:
                        callee = callee.rsplit('.', 1)[-1]
                    elif '::' in callee:
                        callee = callee.rsplit('::', 1)[-1]
                    if callee:
                        edges.append(EdgeInfo(
                            caller=function_stack[-1],
                            callee=callee,
                            line=node.start_point[0] + 1,
                        ))

            for child in node.children:
                _walk_calls(child)

            if pushed:
                function_stack.pop()

        _walk_calls(tree.root_node)
        return edges
