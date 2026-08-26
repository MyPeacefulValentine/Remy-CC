"""
Rust language parser.

Requires tree-sitter with the pinned tree-sitter-rust grammar. There is no
regex fallback: when the grammar is unavailable every parse entry point
raises, scan_file records a StageError, and previously indexed rows are
preserved (R3.0b rejection semantics). The cache identity marks the missing
backend so installing the grammar triggers a rescan through the existing
identity-invalidation channel.
"""

import os

from .base import (
    EdgeInfo,
    LanguageParser,
    ParserCacheIdentity,
    SymbolInfo,
    distribution_version,
)

RUST_TREE_SITTER_AVAILABLE = False
_rust_language = None

try:
    from tree_sitter import Language, Parser as TSParser
    import tree_sitter_rust
    _rust_language = Language(tree_sitter_rust.language())
    RUST_TREE_SITTER_AVAILABLE = True
except Exception:
    pass


_CRATE_ROOT_FILES = ("lib.rs", "main.rs")
_MODULE_FILE_BASENAMES = ("mod.rs", "lib.rs", "main.rs")
_COMMENT_NODE_TYPES = ("line_comment", "block_comment")


def _node_text(node):
    """Get text content of a tree-sitter node as str."""
    return node.text.decode("utf-8") if node.text else ""


def _type_name(node):
    """Extract the base type identifier from an impl/trait type node.

    Handles plain, scoped (``fmt::Display``), and generic (``Foo<T>``)
    type references by descending to the trailing ``type_identifier`` leaf.
    """
    if node is None:
        return None
    if node.type in ("type_identifier", "identifier"):
        return _node_text(node)
    if node.type == "scoped_type_identifier":
        name_node = node.child_by_field_name("name")
        return _type_name(name_node) if name_node else None
    if node.type == "generic_type":
        inner = node.child_by_field_name("type")
        return _type_name(inner) if inner else None
    for child in reversed(node.named_children):
        name = _type_name(child)
        if name:
            return name
    return None


def _skip_attributes_backward(node):
    """Return the first contiguous preceding sibling that is not an attribute."""
    cursor = node.prev_named_sibling
    while cursor is not None and cursor.type == "attribute_item":
        cursor = cursor.prev_named_sibling
    return cursor


def _extract_rust_doc(node):
    """Extract ``///`` (or ``/** */``) doc comment preceding a tree-sitter node.

    Contiguous ``attribute_item`` siblings between the docs and the item are
    skipped, matching Rust source layout ``/// doc`` -> ``#[attr]`` -> item.
    """
    prev = _skip_attributes_backward(node)
    if prev is None or prev.type not in _COMMENT_NODE_TYPES:
        return None
    text = _node_text(prev)
    if prev.type == "block_comment" and text.startswith("/**"):
        raw = text[3:]
        if raw.endswith("*/"):
            raw = raw[:-2]
        lines = [l.strip().lstrip("* ").strip() for l in raw.splitlines()]
        lines = [l for l in lines if l]
        return " ".join(lines[:3]) if lines else None
    if prev.type == "line_comment" and text.startswith("///"):
        doc_lines = [text[3:].strip()]
        cursor = prev.prev_named_sibling
        while (
            cursor is not None
            and cursor.type == "line_comment"
            and _node_text(cursor).startswith("///")
        ):
            doc_lines.insert(0, _node_text(cursor)[3:].strip())
            cursor = cursor.prev_named_sibling
        return " ".join(doc_lines[:3])
    return None


def _segment_with_attributes(node, source_bytes):
    """Item text including contiguous immediately-preceding attribute items.

    ``#[cfg(...)]`` and friends are siblings of the item in tree-sitter-rust,
    so plain ``node.text`` would drop them and cfg-gated same-name duplicates
    would collapse to identical hashes.
    """
    start = node.start_byte
    cursor = node.prev_named_sibling
    while cursor is not None and cursor.type == "attribute_item":
        start = cursor.start_byte
        cursor = cursor.prev_named_sibling
    return source_bytes[start:node.end_byte].decode("utf-8", errors="replace")


def _case_exact_on_disk(base_dir, rel_parts):
    """True when every path segment matches the on-disk entry name exactly.

    ``os.path.isfile`` matches case-insensitively on Windows/macOS
    filesystems; source-derived segments (module names from ``use``/``mod``)
    must match the real entry name byte-for-byte or the index diverges
    across platforms (e.g. ``use super::Clock`` probing ``Clock.rs`` must
    not match ``clock.rs``).
    """
    cursor = base_dir
    for part in rel_parts:
        try:
            if part not in os.listdir(cursor):
                return False
        except OSError:
            return False
        cursor = os.path.join(cursor, part)
    return True


class RustParser(LanguageParser):
    """Parser for Rust source files. Requires tree-sitter-rust; no fallback."""

    language_id = "RustParser"
    CACHE_CONTRACT_VERSION = "5"

    def get_extensions(self):
        return [".rs"]

    def get_prompt_template_path(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "prompts", "summarize_symbol_rust.md",
        )

    # ------------------------------------------------------------------
    # Cache identity / backend gating
    # ------------------------------------------------------------------

    def cache_identity(self, source, file_path):
        if not RUST_TREE_SITTER_AVAILABLE:
            return ParserCacheIdentity.create(
                self.CACHE_CONTRACT_VERSION, "rust-unavailable"
            )
        return ParserCacheIdentity.create(
            self.CACHE_CONTRACT_VERSION,
            "rust-tree-sitter",
            {
                "tree-sitter": distribution_version("tree-sitter"),
                "tree-sitter-rust": distribution_version("tree-sitter-rust"),
            },
        )

    def cache_identity_candidates(self, file_path):
        return (self.cache_identity("", file_path),)

    @staticmethod
    def _require_backend():
        if not RUST_TREE_SITTER_AVAILABLE:
            raise RuntimeError(
                "tree-sitter-rust grammar is not installed; "
                "Rust sources cannot be parsed (no fallback)"
            )

    def _parse(self, source_bytes):
        parser = TSParser(_rust_language)
        return parser.parse(source_bytes)

    # ------------------------------------------------------------------
    # Symbol hash input (hash contract, mirrored by the R3.2+ Rust scanner)
    # ------------------------------------------------------------------

    def symbol_hash_input(self, source_segment):
        """Strip all comments (incl. ``///``/``//!`` docs) via comment tokens.

        Contract: parse the segment with tree-sitter-rust, drop the byte
        ranges of every ``line_comment``/``block_comment`` node (nested block
        comments are single tokens), concatenate the remaining bytes in
        order. Regex is insufficient because Rust block comments nest.
        """
        if not RUST_TREE_SITTER_AVAILABLE:
            return source_segment
        try:
            segment_bytes = source_segment.encode("utf-8")
            tree = self._parse(segment_bytes)
            ranges = []

            def _collect(node):
                if node.type in _COMMENT_NODE_TYPES:
                    ranges.append((node.start_byte, node.end_byte))
                    return
                for child in node.children:
                    _collect(child)

            _collect(tree.root_node)
            if not ranges:
                return source_segment
            pieces = []
            pos = 0
            for start, end in sorted(ranges):
                pieces.append(segment_bytes[pos:start])
                pos = end
            pieces.append(segment_bytes[pos:])
            return b"".join(pieces).decode("utf-8", errors="replace")
        except Exception:
            return source_segment

    # ------------------------------------------------------------------
    # Symbols
    # ------------------------------------------------------------------

    def parse_symbols(self, source, file_path):
        self._require_backend()
        source_bytes = source.encode("utf-8")
        tree = self._parse(source_bytes)

        symbols = []
        trait_impls = {}
        self._walk_items(tree.root_node, source_bytes, symbols, None, trait_impls)
        self._merge_trait_bases(symbols, trait_impls)
        symbols.sort(key=lambda s: s.lineno)
        return symbols

    def _emit(self, symbols, node, source_bytes, name, sym_type, args="", bases=None):
        symbols.append(SymbolInfo(
            name=name,
            args=args,
            type=sym_type,
            lineno=node.start_point[0] + 1,
            source_segment=_segment_with_attributes(node, source_bytes),
            end_lineno=node.end_point[0] + 1,
            docstring=_extract_rust_doc(node),
            bases=bases,
        ))

    def _emit_function(self, symbols, node, source_bytes, prefix):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _node_text(name_node)
        full_name = f"{prefix}.{name}" if prefix else name
        params_node = node.child_by_field_name("parameters")
        params = _node_text(params_node) if params_node is not None else "()"
        self._emit(symbols, node, source_bytes, full_name, "function", args=params)

    def _walk_items(self, node, source_bytes, symbols, prefix, trait_impls):
        for child in node.children:
            t = child.type

            if t == "function_item":
                self._emit_function(symbols, child, source_bytes, prefix)

            elif t in ("struct_item", "enum_item", "trait_item", "type_item"):
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                name = _node_text(name_node)
                full_name = f"{prefix}.{name}" if prefix else name
                sym_type = {
                    "struct_item": "struct",
                    "enum_item": "enum",
                    "trait_item": "interface",
                    "type_item": "type_alias",
                }[t]
                self._emit(symbols, child, source_bytes, full_name, sym_type)
                if t == "trait_item":
                    body = child.child_by_field_name("body")
                    if body is not None:
                        for member in body.children:
                            if member.type in ("function_item", "function_signature_item"):
                                self._emit_function(symbols, member, source_bytes, full_name)

            elif t == "macro_definition":
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    name = _node_text(name_node)
                    full_name = f"{prefix}.{name}" if prefix else name
                    self._emit(symbols, child, source_bytes, full_name, "macro")

            elif t == "mod_item":
                name_node = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if name_node is None or body is None:
                    continue
                name = _node_text(name_node)
                full_mod = f"{prefix}.{name}" if prefix else name
                self._emit(symbols, child, source_bytes, full_mod, "namespace")
                self._walk_items(body, source_bytes, symbols, full_mod, trait_impls)

            elif t == "impl_item":
                type_name = _type_name(child.child_by_field_name("type"))
                if not type_name:
                    continue
                full_type = f"{prefix}.{type_name}" if prefix else type_name
                trait_name = _type_name(child.child_by_field_name("trait"))
                if trait_name:
                    trait_impls.setdefault(full_type, []).append(trait_name)
                body = child.child_by_field_name("body")
                if body is not None:
                    for member in body.children:
                        if member.type == "function_item":
                            self._emit_function(symbols, member, source_bytes, full_type)

    @staticmethod
    def _merge_trait_bases(symbols, trait_impls):
        """Attach same-file ``impl Trait for Type`` traits to the type's bases.

        Exact full-name match first; otherwise a unique short-name match
        among this file's struct/enum symbols. Cross-file impl blocks are a
        documented R3.0b limitation.
        """
        if not trait_impls:
            return
        by_name = {}
        by_short = {}
        for sym in symbols:
            if sym.type not in ("struct", "enum"):
                continue
            by_name[sym.name] = sym
            by_short.setdefault(sym.name.split(".")[-1], []).append(sym)

        for full_type, traits in trait_impls.items():
            target = by_name.get(full_type)
            if target is None:
                candidates = by_short.get(full_type.split(".")[-1], [])
                if len(candidates) != 1:
                    continue
                target = candidates[0]
            merged = list(target.bases or [])
            for trait in traits:
                if trait not in merged:
                    merged.append(trait)
            target.bases = merged or None

    # ------------------------------------------------------------------
    # Patterns (trait-impl facts)
    # ------------------------------------------------------------------

    def extract_patterns(self, source, file_path):
        """Emit one ``rust_trait_impl`` fact per ``impl Trait for Type`` block.

        Carries the impl site (file + impl-site qualified type name) so the
        global postprocess can overwrite struct/enum bases and the
        rust_trait_dispatch synthesizer can resolve methods in the impl
        file — closing the cross-file impl gap a per-file bases merge
        cannot see.
        """
        self._require_backend()
        source_bytes = source.encode("utf-8")
        tree = self._parse(source_bytes)
        patterns = []
        self._walk_trait_impls(tree.root_node, None, patterns)
        return patterns

    def _walk_trait_impls(self, node, prefix, patterns):
        for child in node.children:
            t = child.type
            if t == "impl_item":
                type_name = _type_name(child.child_by_field_name("type"))
                trait_node = child.child_by_field_name("trait")
                trait_name = _type_name(trait_node)
                if not type_name or not trait_name:
                    continue
                full_type = f"{prefix}.{type_name}" if prefix else type_name
                patterns.append({
                    "pattern_type": "rust_trait_impl",
                    "signal_name": trait_name,
                    "handler": full_type,
                    "line": child.start_point[0] + 1,
                    "metadata": {"trait_path": _node_text(trait_node)},
                })
            elif t == "mod_item":
                name_node = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if name_node is None or body is None:
                    continue
                name = _node_text(name_node)
                full_mod = f"{prefix}.{name}" if prefix else name
                self._walk_trait_impls(body, full_mod, patterns)

    # ------------------------------------------------------------------
    # Call graph
    # ------------------------------------------------------------------

    def extract_call_graph(self, source, file_path):
        self._require_backend()
        source_bytes = source.encode("utf-8")
        tree = self._parse(source_bytes)
        edges = []

        def _callee_name(func_node):
            if func_node is None:
                return None
            if func_node.type == "identifier":
                return _node_text(func_node)
            if func_node.type == "field_expression":
                field = func_node.child_by_field_name("field")
                return _node_text(field) if field is not None else None
            if func_node.type == "scoped_identifier":
                name = func_node.child_by_field_name("name")
                return _node_text(name) if name is not None else None
            if func_node.type == "generic_function":
                return _callee_name(func_node.child_by_field_name("function"))
            return None

        def _walk(node, prefix, current_fn):
            t = node.type
            if t == "function_item":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    name = _node_text(name_node)
                    qualified = f"{prefix}.{name}" if prefix else name
                    for child in node.children:
                        _walk(child, prefix, qualified)
                    return
            elif t == "impl_item":
                type_name = _type_name(node.child_by_field_name("type"))
                if type_name:
                    new_prefix = f"{prefix}.{type_name}" if prefix else type_name
                    for child in node.children:
                        _walk(child, new_prefix, current_fn)
                    return
            elif t in ("mod_item", "trait_item"):
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    name = _node_text(name_node)
                    new_prefix = f"{prefix}.{name}" if prefix else name
                    for child in node.children:
                        _walk(child, new_prefix, current_fn)
                    return
            elif t == "call_expression" and current_fn:
                func_node = node.child_by_field_name("function")
                callee = _callee_name(func_node)
                if callee:
                    edges.append(EdgeInfo(
                        caller=current_fn,
                        callee=callee,
                        line=node.start_point[0] + 1,
                        call_form=(
                            "attribute"
                            if func_node is not None and func_node.type == "field_expression"
                            else "name"
                        ),
                    ))

            for child in node.children:
                _walk(child, prefix, current_fn)

        _walk(tree.root_node, None, None)
        return edges

    # ------------------------------------------------------------------
    # Imports (`mod x;` and `use` file-existence mapping)
    # ------------------------------------------------------------------

    def resolve_imports(self, source, file_path, root_dir):
        self._require_backend()
        source_bytes = source.encode("utf-8")
        tree = self._parse(source_bytes)

        current_dir = os.path.dirname(os.path.abspath(file_path))
        basename = os.path.basename(file_path)
        module_dir = (
            current_dir
            if basename in _MODULE_FILE_BASENAMES
            else os.path.join(current_dir, os.path.splitext(basename)[0])
        )
        crate_root = self._find_crate_root(current_dir, root_dir)

        imports = {}

        def _record(base_dir, rel_parts):
            candidate = os.path.join(base_dir, *rel_parts)
            if os.path.isfile(candidate) and _case_exact_on_disk(base_dir, rel_parts):
                rel = os.path.relpath(candidate, root_dir).replace(os.sep, "/")
                if not rel.startswith(".."):
                    imports[rel] = False
                    return True
            return False

        def _try_module(base_dir, segments):
            if not base_dir or not segments:
                return False
            for k in range(len(segments), 0, -1):
                parts = list(segments[:k])
                file_parts = parts[:-1] + [parts[-1] + ".rs"]
                if _record(base_dir, file_parts) or _record(base_dir, parts + ["mod.rs"]):
                    return True
            return False

        declared_mods = set()
        for child in tree.root_node.children:
            if child.type == "mod_item":
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                declared_mods.add(_node_text(name_node))
                if child.child_by_field_name("body") is None:
                    _try_module(module_dir, [_node_text(name_node)])

        def _walk_uses(node):
            if node.type == "use_declaration":
                argument = node.child_by_field_name("argument")
                for segments in self._use_paths(argument):
                    if not segments:
                        continue
                    head, rest = segments[0], segments[1:]
                    if head == "crate":
                        _try_module(crate_root, rest)
                    elif head == "self":
                        _try_module(module_dir, rest)
                    elif head == "super":
                        base = os.path.dirname(module_dir)
                        while rest and rest[0] == "super":
                            base = os.path.dirname(base)
                            rest = rest[1:]
                        _try_module(base, rest)
                    else:
                        # Bare heads are external crates in edition 2018+
                        # unless this file declares the module itself.
                        if head in declared_mods:
                            _try_module(module_dir, segments)
                return
            for child in node.children:
                _walk_uses(child)

        _walk_uses(tree.root_node)
        return imports

    @classmethod
    def _use_paths(cls, node):
        """Flatten a use-tree into a list of segment lists (aliases dropped)."""
        if node is None:
            return []
        t = node.type
        if t in ("identifier", "crate", "super", "self", "metavariable"):
            return [[_node_text(node) if t not in ("crate", "super", "self") else t]]
        if t == "scoped_identifier":
            name = node.child_by_field_name("name")
            path = node.child_by_field_name("path")
            prefixes = cls._use_paths(path) if path is not None else [[]]
            suffix = [_node_text(name)] if name is not None else []
            return [p + suffix for p in prefixes]
        if t == "use_as_clause":
            return cls._use_paths(node.child_by_field_name("path"))
        if t == "scoped_use_list":
            path = node.child_by_field_name("path")
            use_list = node.child_by_field_name("list")
            prefixes = cls._use_paths(path) if path is not None else [[]]
            results = []
            if use_list is not None:
                for child in use_list.named_children:
                    for tail in cls._use_paths(child):
                        for p in prefixes:
                            results.append(p + tail)
            return results
        if t == "use_list":
            results = []
            for child in node.named_children:
                results.extend(cls._use_paths(child))
            return results
        if t == "use_wildcard":
            for child in node.named_children:
                return cls._use_paths(child)
            return []
        return []

    @staticmethod
    def _find_crate_root(start_dir, root_dir):
        """Nearest ancestor directory (within root_dir) holding lib.rs/main.rs."""
        root_abs = os.path.abspath(root_dir)
        cursor = start_dir
        while True:
            if any(
                os.path.isfile(os.path.join(cursor, marker))
                for marker in _CRATE_ROOT_FILES
            ):
                return cursor
            if os.path.normcase(cursor) == os.path.normcase(root_abs):
                return None
            parent = os.path.dirname(cursor)
            if parent == cursor:
                return None
            cursor = parent
