"""
TypeScript/TSX language parser.
Uses tree-sitter when available for high-precision parsing.
Falls back to regex-based extraction otherwise.
"""

import os
import re
from .base import (
    EdgeInfo,
    LanguageParser,
    ParserCacheIdentity,
    SymbolInfo,
    distribution_version,
)

TREE_SITTER_AVAILABLE = False
_ts_language = None
_tsx_language = None

try:
    from tree_sitter import Language, Parser as _TreeSitterParser
    import tree_sitter_typescript
    _ts_language = Language(tree_sitter_typescript.language_typescript())
    _tsx_language = Language(tree_sitter_typescript.language_tsx())
    TREE_SITTER_AVAILABLE = True
except Exception:
    pass

# --- Regex Patterns (Fallback) ---

RE_IMPORT_FROM = re.compile(
    r'''import\s+(?:type\s+)?(?:\*\s+as\s+\w+|\{[^}]*\}|\w+)(?:\s*,\s*(?:\{[^}]*\}|\w+))?\s+from\s+['"]([^'"]+)['"]''',
    re.MULTILINE,
)
RE_REQUIRE = re.compile(r'''require\s*\(\s*['"]([^'"]+)['"]\s*\)''', re.MULTILINE)

RE_TS_FUNC = re.compile(
    r'^[ \t]*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+(\w+)\s*(?:<[^>]*>)?\s*\(',
    re.MULTILINE,
)
RE_TS_CLASS = re.compile(
    r'^[ \t]*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+(\w+)',
    re.MULTILINE,
)
RE_TS_INTERFACE = re.compile(
    r'^[ \t]*(?:export\s+)?interface\s+(\w+)',
    re.MULTILINE,
)
RE_TS_ENUM = re.compile(
    r'^[ \t]*(?:export\s+)?(?:const\s+)?enum\s+(\w+)',
    re.MULTILINE,
)
RE_TS_TYPE = re.compile(
    r'^[ \t]*(?:export\s+)?type\s+(\w+)(?:\s*<[^>]*>)?\s*=',
    re.MULTILINE,
)
RE_TS_NAMESPACE = re.compile(
    r'^[ \t]*(?:export\s+)?(?:namespace|module)\s+(\w+)\s*\{',
    re.MULTILINE,
)
RE_JSDOC_BLOCK = re.compile(r'/\*\*(.+?)\*/', re.DOTALL)
RE_JSDOC_LINE = re.compile(r'^\s*///\s?(.*)', re.MULTILINE)

# Node types that can contain indexable symbols
_TS_DECLARATION_TYPES = frozenset({
    'function_declaration',
    'class_declaration',
    'abstract_class_declaration',
    'interface_declaration',
    'type_alias_declaration',
    'enum_declaration',
    'namespace_declaration',
    'internal_module',
    'module',
    'lexical_declaration',
    'variable_declaration',
})


# --- Shared Utilities ---

def _find_matching_brace(source, start_pos):
    depth = 0
    in_string = False
    in_tpl = False
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

        if ch == '\\' and (in_string or in_char or in_tpl):
            escape_next = True
            i += 1
            continue

        if ch == '"' and not in_char and not in_tpl:
            in_string = not in_string
            i += 1
            continue

        if ch == "'" and not in_string and not in_tpl:
            in_char = not in_char
            i += 1
            continue

        if ch == '`' and not in_string and not in_char:
            in_tpl = not in_tpl
            i += 1
            continue

        if in_string or in_char or in_tpl:
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


def _extract_jsdoc_before(source, pos):
    prefix = source[:pos].rstrip()

    block_match = RE_JSDOC_BLOCK.search(prefix)
    if block_match and prefix.endswith("*/"):
        raw = block_match.group(1)
        lines = [line.strip().lstrip('* ').strip() for line in raw.splitlines()]
        lines = [l for l in lines if l and not l.startswith('@')]
        if lines:
            return " ".join(lines[:3])

    rev_lines = prefix.splitlines()
    doc_lines = []
    for line in reversed(rev_lines):
        m = RE_JSDOC_LINE.match(line)
        if m:
            doc_lines.insert(0, m.group(1).strip())
        else:
            break
    if doc_lines:
        return " ".join(doc_lines[:3])

    return None


def _line_number_at(source, pos):
    return source[:pos].count('\n') + 1


# --- Tree-sitter Utilities ---

def _ts_node_text(node):
    return node.text.decode('utf-8') if node.text else ""


def _ts_extract_jsdoc(source_bytes, node):
    prev = node.prev_named_sibling
    if prev and prev.type == 'comment':
        text = _ts_node_text(prev)
        if text.startswith('/**'):
            raw = text[3:].rstrip('*/').strip()
            lines = [
                l.strip().lstrip('* ').strip()
                for l in raw.splitlines()
                if l.strip().lstrip('* ').strip() and not l.strip().lstrip('* ').startswith('@')
            ]
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


class TSParser(LanguageParser):
    """Parser for TypeScript and TSX source files. Uses tree-sitter when available, regex otherwise."""

    language_id = "TSParser"
    CACHE_CONTRACT_VERSION = "1"

    @staticmethod
    def _tree_sitter_environment():
        return {
            "tree-sitter": distribution_version("tree-sitter"),
            "tree-sitter-typescript": distribution_version("tree-sitter-typescript"),
        }

    def _tree_sitter_identity(self, is_tsx):
        return ParserCacheIdentity.create(
            self.CACHE_CONTRACT_VERSION,
            "tsx-tree-sitter" if is_tsx else "ts-tree-sitter",
            self._tree_sitter_environment(),
        )

    def cache_identity(self, source, file_path):
        if not TREE_SITTER_AVAILABLE:
            return ParserCacheIdentity.create(
                self.CACHE_CONTRACT_VERSION, "ts-regex"
            )
        return self._tree_sitter_identity(file_path.endswith(".tsx"))

    def cache_identity_candidates(self, file_path):
        return (self.cache_identity("", file_path),)

    def get_extensions(self):
        return [".ts", ".tsx"]

    def get_prompt_template_path(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts",
            "summarize_symbol_ts.md",
        )

    def symbol_hash_input(self, source_segment):
        try:
            result = re.sub(r'//[^\n]*', '', source_segment)
            result = re.sub(r'/\*[\s\S]*?\*/', '', result)
            return result
        except Exception:
            return source_segment

    def resolve_imports(self, source, file_path, root_dir):
        imports = {}
        current_dir = os.path.dirname(file_path)
        raw_paths = set()

        for m in RE_IMPORT_FROM.finditer(source):
            raw_paths.add(m.group(1))
        for m in RE_REQUIRE.finditer(source):
            raw_paths.add(m.group(1))

        for raw in raw_paths:
            if not (raw.startswith('./') or raw.startswith('../')):
                continue
            base = os.path.normpath(os.path.join(current_dir, raw))
            for candidate in [
                base + '.ts',
                base + '.tsx',
                os.path.join(base, 'index.ts'),
                os.path.join(base, 'index.tsx'),
            ]:
                if os.path.exists(candidate):
                    rel = os.path.relpath(candidate, root_dir).replace(os.sep, '/')
                    imports[rel] = False
                    break

        return imports

    def parse_symbols(self, source, file_path):
        if TREE_SITTER_AVAILABLE:
            return self._parse_with_tree_sitter(source, file_path)
        return self._parse_with_regex(source, file_path)

    # ========================================================================
    # Tree-sitter Path
    # ========================================================================

    def _parse_with_tree_sitter(self, source, file_path):
        lang = _tsx_language if file_path.endswith('.tsx') else _ts_language
        parser = _TreeSitterParser(lang)
        source_bytes = source.encode('utf-8')
        tree = parser.parse(source_bytes)
        symbols = []
        self._ts_walk_node(tree.root_node, source, source_bytes, symbols, parent_name=None)
        symbols.sort(key=lambda s: s.lineno)
        return symbols

    def _ts_walk_node(self, node, source, source_bytes, symbols, parent_name):
        for child in node.children:
            if child.type == 'export_statement':
                for subchild in child.named_children:
                    if subchild.type in _TS_DECLARATION_TYPES:
                        self._ts_dispatch(subchild, source, source_bytes, symbols, parent_name)
            elif child.type in _TS_DECLARATION_TYPES:
                self._ts_dispatch(child, source, source_bytes, symbols, parent_name)

    def _ts_dispatch(self, node, source, source_bytes, symbols, parent_name):
        t = node.type
        if t == 'function_declaration':
            self._ts_extract_function(node, source, source_bytes, symbols, parent_name)
        elif t in ('class_declaration', 'abstract_class_declaration'):
            self._ts_extract_class(node, source, source_bytes, symbols, parent_name)
        elif t == 'interface_declaration':
            self._ts_extract_interface(node, source, source_bytes, symbols, parent_name)
        elif t == 'type_alias_declaration':
            self._ts_extract_type_alias(node, source, source_bytes, symbols, parent_name)
        elif t == 'enum_declaration':
            self._ts_extract_enum(node, source, source_bytes, symbols, parent_name)
        elif t in ('namespace_declaration', 'internal_module', 'module'):
            self._ts_extract_namespace(node, source, source_bytes, symbols, parent_name)
        elif t in ('lexical_declaration', 'variable_declaration'):
            self._ts_extract_arrow_functions(node, source, source_bytes, symbols, parent_name)

    def _ts_name(self, node):
        name_node = node.child_by_field_name('name')
        return _ts_node_text(name_node) if name_node else '<default>'

    def _ts_params_str(self, node):
        params_node = node.child_by_field_name('parameters')
        if params_node:
            return _ts_node_text(params_node)
        param_node = node.child_by_field_name('parameter')
        if param_node:
            return f"({_ts_node_text(param_node)})"
        return "()"

    def _ts_extract_function(self, node, source, source_bytes, symbols, parent_name):
        name = self._ts_name(node)
        full_name = f"{parent_name}.{name}" if parent_name else name
        symbols.append(SymbolInfo(
            name=full_name,
            args=self._ts_params_str(node),
            type="function",
            lineno=node.start_point[0] + 1,
            source_segment=_ts_node_text(node),
            end_lineno=node.end_point[0] + 1,
            docstring=_ts_extract_jsdoc(source_bytes, node),
        ))

    def _ts_extract_class(self, node, source, source_bytes, symbols, parent_name):
        name_node = node.child_by_field_name('name')
        name = _ts_node_text(name_node) if name_node else '<default>'
        full_name = f"{parent_name}.{name}" if parent_name else name
        bases_list = []
        for child in node.children:
            if child.type in ('extends_clause', 'implements_clause'):
                for type_node in child.named_children:
                    bases_list.append(_ts_node_text(type_node))
        symbols.append(SymbolInfo(
            name=full_name,
            args="",
            type="class",
            lineno=node.start_point[0] + 1,
            source_segment=_ts_node_text(node),
            end_lineno=node.end_point[0] + 1,
            docstring=_ts_extract_jsdoc(source_bytes, node),
            bases=bases_list or None,
        ))
        body = node.child_by_field_name('body')
        if body:
            for member in body.children:
                if member.type == 'method_definition':
                    mname_node = member.child_by_field_name('name')
                    if mname_node:
                        symbols.append(SymbolInfo(
                            name=f"{full_name}.{_ts_node_text(mname_node)}",
                            args=self._ts_params_str(member),
                            type="function",
                            lineno=member.start_point[0] + 1,
                            source_segment=_ts_node_text(member),
                            end_lineno=member.end_point[0] + 1,
                            docstring=_ts_extract_jsdoc(source_bytes, member),
                        ))
                elif member.type == 'abstract_method_signature':
                    mname_node = member.child_by_field_name('name')
                    if mname_node:
                        symbols.append(SymbolInfo(
                            name=f"{full_name}.{_ts_node_text(mname_node)}",
                            args=self._ts_params_str(member),
                            type="function",
                            lineno=member.start_point[0] + 1,
                            source_segment=_ts_node_text(member),
                            end_lineno=member.end_point[0] + 1,
                            docstring=_ts_extract_jsdoc(source_bytes, member),
                        ))

    def _ts_extract_interface(self, node, source, source_bytes, symbols, parent_name):
        name_node = node.child_by_field_name('name')
        if not name_node:
            return
        name = _ts_node_text(name_node)
        full_name = f"{parent_name}.{name}" if parent_name else name
        symbols.append(SymbolInfo(
            name=full_name,
            args="",
            type="interface",
            lineno=node.start_point[0] + 1,
            source_segment=_ts_node_text(node),
            end_lineno=node.end_point[0] + 1,
            docstring=_ts_extract_jsdoc(source_bytes, node),
        ))
        body = node.child_by_field_name('body')
        if body:
            for member in body.children:
                if member.type == 'method_signature':
                    mname_node = member.child_by_field_name('name')
                    if mname_node:
                        symbols.append(SymbolInfo(
                            name=f"{full_name}.{_ts_node_text(mname_node)}",
                            args=self._ts_params_str(member),
                            type="function",
                            lineno=member.start_point[0] + 1,
                            source_segment=_ts_node_text(member),
                            end_lineno=member.end_point[0] + 1,
                            docstring=_ts_extract_jsdoc(source_bytes, member),
                        ))

    def _ts_extract_type_alias(self, node, source, source_bytes, symbols, parent_name):
        name_node = node.child_by_field_name('name')
        if not name_node:
            return
        name = _ts_node_text(name_node)
        full_name = f"{parent_name}.{name}" if parent_name else name
        symbols.append(SymbolInfo(
            name=full_name,
            args="",
            type="type_alias",
            lineno=node.start_point[0] + 1,
            source_segment=_ts_node_text(node),
            end_lineno=node.end_point[0] + 1,
            docstring=_ts_extract_jsdoc(source_bytes, node),
        ))

    def _ts_extract_enum(self, node, source, source_bytes, symbols, parent_name):
        name_node = node.child_by_field_name('name')
        if not name_node:
            return
        name = _ts_node_text(name_node)
        full_name = f"{parent_name}.{name}" if parent_name else name
        symbols.append(SymbolInfo(
            name=full_name,
            args="",
            type="enum",
            lineno=node.start_point[0] + 1,
            source_segment=_ts_node_text(node),
            end_lineno=node.end_point[0] + 1,
            docstring=_ts_extract_jsdoc(source_bytes, node),
        ))

    def _ts_extract_namespace(self, node, source, source_bytes, symbols, parent_name):
        name_node = node.child_by_field_name('name')
        if not name_node:
            return
        name = _ts_node_text(name_node)
        full_name = f"{parent_name}.{name}" if parent_name else name
        symbols.append(SymbolInfo(
            name=full_name,
            args="",
            type="namespace",
            lineno=node.start_point[0] + 1,
            source_segment=_ts_node_text(node),
            end_lineno=node.end_point[0] + 1,
            docstring=_ts_extract_jsdoc(source_bytes, node),
        ))
        body = node.child_by_field_name('body')
        if body:
            self._ts_walk_node(body, source, source_bytes, symbols, parent_name=full_name)

    def _ts_extract_arrow_functions(self, node, source, source_bytes, symbols, parent_name):
        for child in node.children:
            if child.type != 'variable_declarator':
                continue
            value_node = child.child_by_field_name('value')
            if not value_node or value_node.type != 'arrow_function':
                continue
            name_node = child.child_by_field_name('name')
            if not name_node:
                continue
            name = _ts_node_text(name_node)
            full_name = f"{parent_name}.{name}" if parent_name else name
            symbols.append(SymbolInfo(
                name=full_name,
                args=self._ts_params_str(value_node),
                type="function",
                lineno=child.start_point[0] + 1,
                source_segment=_ts_node_text(child),
                end_lineno=child.end_point[0] + 1,
                docstring=_ts_extract_jsdoc(source_bytes, node),
            ))

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
            try:
                brace_pos = source.index('{', match.start())
            except ValueError:
                return None
            end_pos = _find_matching_brace(source, brace_pos)
            if end_pos == -1:
                end_pos = min(brace_pos + 500, len(source) - 1)
            if _overlaps(match.start(), end_pos + 1):
                return None
            lineno = _line_number_at(source, match.start())
            docstring = _extract_jsdoc_before(source, match.start())
            seen_ranges.append((match.start(), end_pos + 1))
            sym = SymbolInfo(
                name=name,
                args=args_str,
                type=sym_type,
                lineno=lineno,
                source_segment=source[match.start():end_pos + 1],
                end_lineno=_line_number_at(source, end_pos),
                docstring=docstring,
            )
            symbols.append(sym)
            return sym

        for m in RE_TS_NAMESPACE.finditer(source):
            _add_braced_symbol(m, m.group(1), "namespace")

        for m in RE_TS_CLASS.finditer(source):
            if not _overlaps(m.start(), m.start() + len(m.group(0))):
                _add_braced_symbol(m, m.group(1), "class")

        for m in RE_TS_INTERFACE.finditer(source):
            if not _overlaps(m.start(), m.start() + len(m.group(0))):
                _add_braced_symbol(m, m.group(1), "interface")

        for m in RE_TS_ENUM.finditer(source):
            if not _overlaps(m.start(), m.start() + len(m.group(0))):
                _add_braced_symbol(m, m.group(1), "enum")

        for m in RE_TS_FUNC.finditer(source):
            paren_start = m.end() - 1
            depth = 0
            paren_end = paren_start
            for j in range(paren_start, min(paren_start + 2000, len(source))):
                if source[j] == '(':
                    depth += 1
                elif source[j] == ')':
                    depth -= 1
                    if depth == 0:
                        paren_end = j
                        break
            args_str = source[paren_start:paren_end + 1] if depth == 0 else "()"
            # Known limitation (regex path only): if the return type is an object literal
            # (e.g., ): { k: V } {), the first '{' found below may be the return type
            # annotation rather than the function body, truncating source_segment.
            # The tree-sitter path handles this correctly.
            try:
                brace_pos = source.index('{', paren_end)
            except ValueError:
                continue
            end_pos = _find_matching_brace(source, brace_pos)
            if end_pos == -1:
                continue
            if _overlaps(m.start(), end_pos + 1):
                continue
            lineno = _line_number_at(source, m.start())
            docstring = _extract_jsdoc_before(source, m.start())
            seen_ranges.append((m.start(), end_pos + 1))
            symbols.append(SymbolInfo(
                name=m.group(1),
                args=args_str,
                type="function",
                lineno=lineno,
                source_segment=source[m.start():end_pos + 1],
                end_lineno=_line_number_at(source, end_pos),
                docstring=docstring,
            ))

        for m in RE_TS_TYPE.finditer(source):
            if _overlaps(m.start(), m.end()):
                continue
            semicolon = source.find(';', m.end())
            end = (semicolon + 1) if semicolon != -1 else len(source)
            lineno = _line_number_at(source, m.start())
            docstring = _extract_jsdoc_before(source, m.start())
            seen_ranges.append((m.start(), end))
            symbols.append(SymbolInfo(
                name=m.group(1),
                args="",
                type="type_alias",
                lineno=lineno,
                source_segment=source[m.start():end],
                end_lineno=_line_number_at(source, end - 1),
                docstring=docstring,
            ))

        symbols.sort(key=lambda s: s.lineno)
        return symbols

    def extract_call_graph(self, source, file_path):
        if not TREE_SITTER_AVAILABLE:
            return []

        lang = _tsx_language if file_path.endswith('.tsx') else _ts_language
        parser = _TreeSitterParser(lang)
        source_bytes = source.encode('utf-8')
        tree = parser.parse(source_bytes)

        edges = []
        function_stack = []

        def _walk_calls(node):
            pushed = False

            if node.type in ('function_declaration', 'method_definition'):
                name_node = node.child_by_field_name('name')
                if name_node:
                    function_stack.append(_ts_node_text(name_node))
                    pushed = True
            elif node.type == 'arrow_function' and node.parent and node.parent.type == 'variable_declarator':
                name_node = node.parent.child_by_field_name('name')
                if name_node:
                    function_stack.append(_ts_node_text(name_node))
                    pushed = True

            if node.type == 'call_expression' and function_stack:
                func_node = node.child_by_field_name('function')
                if func_node:
                    text = _ts_node_text(func_node)
                    callee = text.rsplit('.', 1)[-1] if '.' in text else text
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
