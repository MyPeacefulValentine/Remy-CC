//! `PythonParser` replication.
//!
//! The Python oracle is the only parser that runs on CPython's `ast` module
//! rather than tree-sitter, so this is the highest-equivalence-risk language
//! of R3.3. Contracts replicated here:
//!
//! - **Symbol scope**: module-level functions/classes plus one level of class
//!   methods (`Class.method`); nested and conditionally defined functions are
//!   not symbols, matching the oracle's `tree.body` walk.
//! - **Source extent**: `ast.get_source_segment` starts at `def`/`async`/
//!   `class` (decorators excluded) and ends at the last statement. tree-sitter
//!   includes trailing comments inside the block, so the effective end skips
//!   them.
//! - **Failure mapping**: `ast.parse` raises `SyntaxError` for broken
//!   grammar, a leading BOM, and NUL bytes (CPython 3.12). Every extraction
//!   channel catches it, so the `files` row stays in place with empty
//!   symbols/imports/bindings/edges, while patterns are still extracted (the
//!   oracle's `extract_patterns` runs its regexes on the raw source
//!   regardless). Deterministic only since the parser's R3.3 cache fix
//!   (contract version 2): the frozen v1 tree cache handed later channels a
//!   stale tree from the previously parsed file.
//! - **Hash input**: the docstring literal is spliced out first (contract
//!   v3, C2 ruling), then `re.sub(r'#[^\n]*', '', segment)` — the `#` strip
//!   inside string literals is frozen oracle behaviour, reproduced verbatim.

use crate::facts::{CacheIdentity, EdgeInfo, PatternFact, SymbolInfo};
use crate::py_unparse;
use crate::pyjson;
use regex::Regex;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use tree_sitter::{Node, Parser, Tree};

pub const LANGUAGE_ID: &str = "PythonParser";
pub const CACHE_CONTRACT_VERSION: &str = "3";
pub const EXTENSIONS: &[&str] = &[".py"];

/// Crate versions pinned in Cargo.toml, recorded in `parser_environment`
/// (an ALLOWED_DIFF column under classification v2).
pub const TREE_SITTER_CRATE_VERSION: &str = "0.25";
pub const GRAMMAR_PYTHON_CRATE_VERSION: &str = "0.25.0";

/// Everything one Python source file contributes to the fact tables.
#[derive(Debug)]
pub struct PythonFacts {
    pub imports: Vec<String>,
    pub import_bindings_json: String,
    pub symbols: Vec<SymbolInfo>,
    pub edges: Vec<EdgeInfo>,
    pub patterns: Vec<PatternFact>,
}

pub fn handles(filename: &str) -> bool {
    EXTENSIONS.iter().any(|ext| filename.ends_with(ext))
}

/// The Rust producer names its own backend: the oracle's `python-ast` would
/// misdescribe who produced the row, and the column allows the difference.
pub fn cache_identity() -> CacheIdentity {
    let environment = pyjson::dumps_identity(&json!({
        "tree-sitter": TREE_SITTER_CRATE_VERSION,
        "tree-sitter-python": GRAMMAR_PYTHON_CRATE_VERSION,
    }));
    CacheIdentity {
        contract_version: CACHE_CONTRACT_VERSION.to_string(),
        backend: "python-tree-sitter".to_string(),
        environment,
    }
}

/// `PythonParser.symbol_hash_input`: strip `#` to end of line. A `#` inside a
/// string literal is stripped too — the frozen oracle behaviour.
pub fn symbol_hash_input(source_segment: &str) -> String {
    static COMMENT: OnceLock<Regex> = OnceLock::new();
    let comment = COMMENT.get_or_init(|| Regex::new(r"#[^\n]*").unwrap());
    comment.replace_all(source_segment, "").into_owned()
}

fn parse_tree(source: &str) -> Tree {
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .expect("bundled grammar must be ABI-compatible with the linked tree-sitter core");
    parser
        .parse(source.as_bytes(), None)
        .expect("parsing without a cancellation flag always yields a tree")
}

/// Extract every per-file fact. Never fails: files CPython would reject
/// (grammar error / BOM / NUL) keep their `files` row and yield empty
/// parsed facts, matching the oracle's per-channel `except SyntaxError`.
pub fn parse_file(source: &str, full_path: &Path, root_dir: &Path) -> PythonFacts {
    let tree = parse_tree(source);
    let root = tree.root_node();
    // A leading BOM and NUL bytes are SyntaxErrors for CPython while
    // tree-sitter accepts them, so both are checked explicitly.
    let syntax_ok = !root.has_error() && !source.starts_with('\u{feff}') && !source.contains('\0');

    let symbols = if syntax_ok {
        collect_symbols(root, source)
    } else {
        Vec::new()
    };
    let (imports, bindings) = if syntax_ok {
        collect_imports(root, source, full_path, root_dir)
    } else {
        (Vec::new(), Vec::new())
    };
    let edges = if syntax_ok {
        collect_edges(root, source)
    } else {
        Vec::new()
    };
    let patterns = extract_patterns(source, &symbols);

    let bindings_json = pyjson::dumps_default(&Value::Array(
        bindings
            .into_iter()
            .map(|(module, names)| {
                json!({
                    "module": module,
                    "names": names,
                })
            })
            .collect(),
    ));

    PythonFacts {
        imports,
        import_bindings_json: bindings_json,
        symbols,
        edges,
        patterns,
    }
}

fn text<'a>(node: Node, source: &'a str) -> &'a str {
    source.get(node.byte_range()).unwrap_or("")
}

fn named_children<'a>(node: Node<'a>) -> Vec<Node<'a>> {
    let mut cursor = node.walk();
    node.named_children(&mut cursor).collect()
}

fn all_children<'a>(node: Node<'a>) -> Vec<Node<'a>> {
    let mut cursor = node.walk();
    node.children(&mut cursor).collect()
}

/// `decorated_definition` wraps the decorators the AST keeps in
/// `decorator_list`; the definition itself is where `lineno` and the source
/// segment start.
fn undecorated<'a>(node: Node<'a>) -> Node<'a> {
    if node.kind() == "decorated_definition" {
        if let Some(definition) = node.child_by_field_name("definition") {
            return definition;
        }
    }
    node
}

// ---------------------------------------------------------------------------
// Symbols
// ---------------------------------------------------------------------------

fn collect_symbols(root: Node, source: &str) -> Vec<SymbolInfo> {
    let mut symbols = Vec::new();
    for child in named_children(root) {
        let node = undecorated(child);
        match node.kind() {
            "function_definition" => {
                if let Some(symbol) = function_symbol(node, source, None) {
                    symbols.push(symbol);
                }
            }
            "class_definition" => {
                let Some(name_node) = node.child_by_field_name("name") else {
                    continue;
                };
                let class_name = text(name_node, source).to_string();
                if let Some(symbol) = class_symbol(node, source, &class_name) {
                    symbols.push(symbol);
                }
                if let Some(body) = node.child_by_field_name("body") {
                    for member in named_children(body) {
                        let member = undecorated(member);
                        if member.kind() != "function_definition" {
                            continue;
                        }
                        if let Some(symbol) = function_symbol(member, source, Some(&class_name)) {
                            symbols.push(symbol);
                        }
                    }
                }
            }
            _ => {}
        }
    }
    symbols
}

fn function_symbol(node: Node, source: &str, parent: Option<&str>) -> Option<SymbolInfo> {
    let name_node = node.child_by_field_name("name")?;
    let name = text(name_node, source);
    let full_name = match parent {
        Some(parent) => format!("{parent}.{name}"),
        None => name.to_string(),
    };
    let args = node
        .child_by_field_name("parameters")
        .map(|params| format!("({})", py_unparse::unparse_parameters(params, source)))
        .unwrap_or_default();
    let (segment, end_row) = source_extent(node, source)?;
    let hash_source_segment = hash_segment_without_docstring(node, source, &segment);
    Some(SymbolInfo {
        name: full_name,
        args,
        sym_type: "function".to_string(),
        lineno: node.start_position().row as i64 + 1,
        source_segment: segment,
        end_lineno: Some(end_row as i64 + 1),
        docstring: docstring(node, source),
        bases: None,
        hash_source_segment,
    })
}

fn class_symbol(node: Node, source: &str, name: &str) -> Option<SymbolInfo> {
    let (segment, end_row) = source_extent(node, source)?;
    let hash_source_segment = hash_segment_without_docstring(node, source, &segment);
    Some(SymbolInfo {
        name: name.to_string(),
        args: String::new(),
        sym_type: "class".to_string(),
        lineno: node.start_position().row as i64 + 1,
        source_segment: segment,
        end_lineno: Some(end_row as i64 + 1),
        docstring: docstring(node, source),
        bases: class_bases(node, source),
        hash_source_segment,
    })
}

/// `ClassDef.bases` holds only positional superclasses. The oracle records
/// plain names and attribute tails and silently drops anything else
/// (subscripts, starred bases), so an all-dropped base list stays an empty
/// list — distinct from "no bases at all", which is `None`.
fn class_bases(node: Node, source: &str) -> Option<Vec<String>> {
    let superclasses = node.child_by_field_name("superclasses")?;
    let positional: Vec<Node> = named_children(superclasses)
        .into_iter()
        .filter(|child| {
            !matches!(
                child.kind(),
                "keyword_argument" | "dictionary_splat" | "comment"
            )
        })
        .collect();
    if positional.is_empty() {
        return None;
    }
    Some(
        positional
            .into_iter()
            .filter_map(|child| match child.kind() {
                "identifier" => Some(text(child, source).to_string()),
                "attribute" => child
                    .child_by_field_name("attribute")
                    .map(|attr| text(attr, source).to_string()),
                _ => None,
            })
            .collect(),
    )
}

/// The source text and end row `ast.get_source_segment` /
/// `node.end_lineno` would report: the node's own start through its last
/// non-comment descendant.
fn source_extent(node: Node, source: &str) -> Option<(String, usize)> {
    let (end_byte, end_row) = effective_end(node)?;
    let segment = source.get(node.start_byte()..end_byte)?.to_string();
    Some((segment, end_row))
}

fn effective_end(node: Node) -> Option<(usize, usize)> {
    if node.kind() == "comment" {
        return None;
    }
    let children = all_children(node);
    for child in children.iter().rev() {
        if let Some(end) = effective_end(*child) {
            return Some(end);
        }
    }
    if children.is_empty() {
        Some((node.end_byte(), node.end_position().row))
    } else {
        None
    }
}

/// `ast.get_docstring`: the first statement's string literal, cleaned with
/// `inspect.cleandoc`.
/// Splice the docstring literal out of `segment` for the hash input
/// (contract v3), byte-identical to python_parser._segment_without_docstring.
fn hash_segment_without_docstring(node: Node, source: &str, segment: &str) -> Option<String> {
    let (start, end) = docstring_extent(node, source)?;
    let seg_start = node.start_byte();
    let rel_start = start.checked_sub(seg_start)?;
    let rel_end = end.checked_sub(seg_start)?;
    if rel_start >= rel_end || rel_end > segment.len() {
        return None;
    }
    if !segment.is_char_boundary(rel_start) || !segment.is_char_boundary(rel_end) {
        return None;
    }
    Some(format!("{}{}", &segment[..rel_start], &segment[rel_end..]))
}

/// Docstring extent per `ast.get_docstring`'s predicate: unlike
/// `docstring()` below (frozen column behaviour), `concatenated_string` is
/// accepted because CPython folds adjacent plain literals into one Constant.
fn docstring_extent(node: Node, source: &str) -> Option<(usize, usize)> {
    let body = node.child_by_field_name("body")?;
    let first = named_children(body)
        .into_iter()
        .find(|child| child.kind() != "comment")?;
    if first.kind() != "expression_statement" {
        return None;
    }
    let literal = named_children(first).into_iter().next()?;
    let plain = |raw: &str| -> Option<bool> {
        let (prefix, _quote, _body) = crate::py_repr::split_literal(raw)?;
        Some(!prefix.bytes && !prefix.format)
    };
    match literal.kind() {
        "string" => {
            if !plain(text(literal, source))? {
                return None;
            }
        }
        "concatenated_string" => {
            for part in named_children(literal) {
                if part.kind() != "string" || !plain(text(part, source))? {
                    return None;
                }
            }
        }
        _ => return None,
    }
    Some((literal.start_byte(), literal.end_byte()))
}

fn docstring(node: Node, source: &str) -> Option<String> {
    let body = node.child_by_field_name("body")?;
    let first = named_children(body)
        .into_iter()
        .find(|child| child.kind() != "comment")?;
    if first.kind() != "expression_statement" {
        return None;
    }
    let literal = named_children(first).into_iter().next()?;
    let raw = match literal.kind() {
        "string" => text(literal, source),
        _ => return None,
    };
    let (prefix, _quote, body_text) = crate::py_repr::split_literal(raw)?;
    if prefix.bytes || prefix.format {
        return None;
    }
    match crate::py_repr::decode_literal_body(prefix, body_text)? {
        crate::py_repr::LiteralValue::Str(value) => Some(cleandoc(&value)),
        crate::py_repr::LiteralValue::Bytes(_) => None,
    }
}

/// `inspect.cleandoc`: expand tabs, drop the common indentation of every line
/// after the first, then strip leading and trailing blank lines.
fn cleandoc(doc: &str) -> String {
    let expanded = expandtabs(doc, 8);
    let mut lines: Vec<String> = expanded.split('\n').map(|line| line.to_string()).collect();
    let mut margin = usize::MAX;
    for line in lines.iter().skip(1) {
        let content = line.trim_start().chars().count();
        if content > 0 {
            let indent = line.chars().count() - content;
            margin = margin.min(indent);
        }
    }
    if let Some(first) = lines.first_mut() {
        *first = first.trim_start().to_string();
    }
    if margin < usize::MAX {
        for line in lines.iter_mut().skip(1) {
            *line = line.chars().skip(margin).collect();
        }
    }
    while lines.last().is_some_and(|line| line.is_empty()) {
        lines.pop();
    }
    while lines.first().is_some_and(|line| line.is_empty()) {
        lines.remove(0);
    }
    lines.join("\n")
}

/// `str.expandtabs`: advance to the next multiple of `tabsize`, with the
/// column counter reset by `\n` and `\r`.
fn expandtabs(value: &str, tabsize: usize) -> String {
    let mut out = String::with_capacity(value.len());
    let mut column = 0usize;
    for c in value.chars() {
        match c {
            '\t' => {
                if tabsize > 0 {
                    let spaces = tabsize - (column % tabsize);
                    out.push_str(&" ".repeat(spaces));
                    column += spaces;
                }
            }
            '\n' | '\r' => {
                out.push(c);
                column = 0;
            }
            _ => {
                out.push(c);
                column += 1;
            }
        }
    }
    out
}

// ---------------------------------------------------------------------------
// Imports
// ---------------------------------------------------------------------------

/// `ImportVisitor`: resolve dotted module names against the project tree and
/// record the bindings that stayed unresolved. Both walks visit the whole
/// tree, so imports inside functions count as well.
fn collect_imports(
    root: Node,
    source: &str,
    full_path: &Path,
    root_dir: &Path,
) -> (Vec<String>, Vec<(String, Vec<String>)>) {
    let current_dir = full_path
        .parent()
        .map(|parent| parent.to_path_buf())
        .unwrap_or_else(|| PathBuf::from(""));
    let mut visitor = ImportVisitor {
        root_dir: root_dir.to_path_buf(),
        current_dir,
        imports: Vec::new(),
        seen: std::collections::HashSet::new(),
        unresolved: BTreeMap::new(),
    };
    visitor.walk(root, source);
    let unresolved = visitor.unresolved.into_iter().collect();
    (visitor.imports, unresolved)
}

struct ImportVisitor {
    root_dir: PathBuf,
    current_dir: PathBuf,
    imports: Vec<String>,
    seen: std::collections::HashSet<String>,
    unresolved: BTreeMap<String, Vec<String>>,
}

impl ImportVisitor {
    fn walk(&mut self, node: Node, source: &str) {
        match node.kind() {
            "import_statement" => self.visit_import(node, source),
            "import_from_statement" => {
                let (module, level) = match node.child_by_field_name("module_name") {
                    Some(module_node) => relative_module(module_node, source),
                    None => (String::new(), 0),
                };
                self.visit_import_from(node, source, &module, level);
            }
            // `from __future__ import ...` is its own node type but the AST
            // builds a plain `ImportFrom` with module `__future__`.
            "future_import_statement" => self.visit_import_from(node, source, "__future__", 0),
            _ => {
                for child in named_children(node) {
                    self.walk(child, source);
                }
            }
        }
    }

    fn visit_import(&mut self, node: Node, source: &str) {
        for name_node in field_nodes(node, "name") {
            let (module, alias) = split_aliased(name_node, source);
            if !self.add_import(&module, 0) {
                let bound = alias
                    .unwrap_or_else(|| module.split('.').next().unwrap_or(&module).to_string());
                self.record_unresolved(&module, &bound);
            }
        }
    }

    fn visit_import_from(&mut self, node: Node, source: &str, module: &str, level: usize) {
        let mut names: Vec<(String, Option<String>)> = field_nodes(node, "name")
            .into_iter()
            .map(|name_node| {
                let (name, alias) = split_aliased(name_node, source);
                (name, alias)
            })
            .collect();
        if names.is_empty()
            && named_children(node)
                .iter()
                .any(|child| child.kind() == "wildcard_import")
        {
            names.push(("*".to_string(), None));
        }

        for (name, alias) in names {
            let full_name = if module.is_empty() {
                name.clone()
            } else {
                format!("{module}.{name}")
            };
            if self.add_import(&full_name, level) {
                continue;
            }
            if !module.is_empty() && self.add_import(module, level) {
                continue;
            }
            if level == 0 && !module.is_empty() {
                let bound = alias.unwrap_or(name);
                self.record_unresolved(module, &bound);
            }
        }
    }

    /// `ImportVisitor._add_import`: `level == 0` resolves against the project
    /// root, `level > 0` walks up from the importing file's directory.
    fn add_import(&mut self, module_name: &str, level: usize) -> bool {
        let parts: Vec<&str> = if module_name.is_empty() {
            Vec::new()
        } else {
            module_name.split('.').collect()
        };
        let mut base = if level > 0 {
            let mut base = self.current_dir.clone();
            for _ in 0..level.saturating_sub(1) {
                base = base.parent().map(|p| p.to_path_buf()).unwrap_or_default();
            }
            base
        } else {
            self.root_dir.clone()
        };
        for part in &parts {
            base.push(part);
        }

        // The oracle appends ".py" to the joined path textually, so a
        // directory component containing a dot must not be treated as an
        // extension to replace.
        let mut module_file = base.clone().into_os_string();
        module_file.push(".py");
        let module_file = PathBuf::from(module_file);
        let package_file = base.join("__init__.py");
        let found = if module_file.is_file() {
            Some(module_file)
        } else if package_file.is_file() {
            Some(package_file)
        } else {
            None
        };
        match found {
            Some(path) => {
                let rel = crate::parse_c_cpp::relpath_slash(&path, &self.root_dir);
                if self.seen.insert(rel.clone()) {
                    self.imports.push(rel);
                }
                true
            }
            None => false,
        }
    }

    fn record_unresolved(&mut self, module: &str, bound: &str) {
        let names = self.unresolved.entry(module.to_string()).or_default();
        if !names.iter().any(|name| name == bound) {
            names.push(bound.to_string());
        }
    }
}

fn field_nodes<'a>(node: Node<'a>, field: &str) -> Vec<Node<'a>> {
    let mut out = Vec::new();
    let mut cursor = node.walk();
    if cursor.goto_first_child() {
        loop {
            if cursor.field_name() == Some(field) {
                out.push(cursor.node());
            }
            if !cursor.goto_next_sibling() {
                break;
            }
        }
    }
    out
}

/// `aliased_import` mirrors `ast.alias`: dotted name plus optional `asname`.
fn split_aliased(node: Node, source: &str) -> (String, Option<String>) {
    if node.kind() == "aliased_import" {
        let name = node
            .child_by_field_name("name")
            .map(|inner| text(inner, source).to_string())
            .unwrap_or_default();
        let alias = node
            .child_by_field_name("alias")
            .map(|inner| text(inner, source).to_string());
        return (name, alias);
    }
    (text(node, source).to_string(), None)
}

/// `ImportFrom.module` / `.level`: the dot run becomes the level, the dotted
/// remainder the module (empty for `from . import x`).
fn relative_module(node: Node, source: &str) -> (String, usize) {
    if node.kind() != "relative_import" {
        return (text(node, source).to_string(), 0);
    }
    let mut level = 0;
    let mut module = String::new();
    for child in named_children(node) {
        match child.kind() {
            "import_prefix" => level = text(child, source).chars().filter(|c| *c == '.').count(),
            "dotted_name" => module = text(child, source).to_string(),
            _ => {}
        }
    }
    (module, level)
}

// ---------------------------------------------------------------------------
// Call graph
// ---------------------------------------------------------------------------

/// `PythonParser.extract_call_graph`: every call inside a function body,
/// with the enclosing function's qualified name. The class prefix follows the
/// oracle's propagation rule — it changes only when the walk enters a class,
/// so a function nested inside a method still carries the class name.
fn collect_edges(root: Node, source: &str) -> Vec<EdgeInfo> {
    let mut edges = Vec::new();
    let mut stack: Vec<String> = Vec::new();
    walk_calls(root, source, &mut stack, None, &mut edges);
    edges
}

fn walk_calls(
    node: Node,
    source: &str,
    stack: &mut Vec<String>,
    parent_class: Option<&str>,
    edges: &mut Vec<EdgeInfo>,
) {
    let mut pushed = false;
    // The AST keeps decorators inside `FunctionDef.decorator_list`, so calls
    // in a decorator belong to the decorated function. tree-sitter puts them
    // in the enclosing `decorated_definition`, hence the early push here.
    let named_function = match node.kind() {
        "function_definition" => node.child_by_field_name("name"),
        "decorated_definition" => node
            .child_by_field_name("definition")
            .filter(|definition| definition.kind() == "function_definition")
            .and_then(|definition| definition.child_by_field_name("name")),
        _ => None,
    };
    if let Some(name_node) = named_function {
        let name = text(name_node, source);
        let qualified = match parent_class {
            Some(parent) => format!("{parent}.{name}"),
            None => name.to_string(),
        };
        stack.push(qualified);
        pushed = true;
    }

    if node.kind() == "call" && !stack.is_empty() {
        if let Some((callee, call_form)) = callee_of(node, source) {
            edges.push(EdgeInfo {
                caller: stack.last().cloned().unwrap_or_default(),
                callee,
                line: node.start_position().row as i64 + 1,
                call_form,
            });
        }
    }

    let class_name = if node.kind() == "class_definition" {
        node.child_by_field_name("name")
            .map(|name| text(name, source).to_string())
    } else {
        None
    };
    let child_parent = match &class_name {
        Some(name) => Some(name.as_str()),
        None => parent_class,
    };
    for child in named_children(node) {
        walk_calls(child, source, stack, child_parent, edges);
    }

    if pushed {
        stack.pop();
    }
}

/// `ast.Call.func`: a bare `Name` is a `name` call, an `Attribute` records the
/// attribute tail as an `attribute` call, everything else is not an edge.
fn callee_of(node: Node, source: &str) -> Option<(String, &'static str)> {
    let function = node.child_by_field_name("function")?;
    match function.kind() {
        "identifier" => Some((text(function, source).to_string(), "name")),
        "attribute" => function
            .child_by_field_name("attribute")
            .map(|attr| (text(attr, source).to_string(), "attribute")),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Event patterns
// ---------------------------------------------------------------------------

fn pattern_regex(cell: &'static OnceLock<Regex>, source: &'static str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(source).unwrap())
}

/// `PythonParser.extract_patterns`. The regexes run on the raw source even
/// when parsing failed, so a syntactically broken file still reports its
/// patterns with a `NULL` handler.
fn extract_patterns(source: &str, symbols: &[SymbolInfo]) -> Vec<PatternFact> {
    // Python `re` \s also matches U+001C..U+001F, which Rust's \s omits.
    static DJANGO_CONNECT: OnceLock<Regex> = OnceLock::new();
    static DJANGO_SEND: OnceLock<Regex> = OnceLock::new();
    static PYQT_CONNECT: OnceLock<Regex> = OnceLock::new();
    static PYQT_EMIT: OnceLock<Regex> = OnceLock::new();
    static OBSERVER_APPEND: OnceLock<Regex> = OnceLock::new();
    static OBSERVER_ITER: OnceLock<Regex> = OnceLock::new();
    static OBSERVER_INVOKE: OnceLock<Regex> = OnceLock::new();

    let django_connect = pattern_regex(&DJANGO_CONNECT, r"(\w+)\.connect\([\s\x{1c}-\x{1f}]*(\w+)");
    let django_send = pattern_regex(&DJANGO_SEND, r"(\w+)\.send\(");
    let pyqt_connect = pattern_regex(
        &PYQT_CONNECT,
        r"(\w+)\.connect\([\s\x{1c}-\x{1f}]*(?:self\.)?(\w+)",
    );
    let pyqt_emit = pattern_regex(&PYQT_EMIT, r"(\w+)\.emit\(");
    let observer_append = pattern_regex(&OBSERVER_APPEND, r"self\.(\w+)\.(?:append|add|insert)\(");
    let observer_iter = pattern_regex(
        &OBSERVER_ITER,
        r"for[\s\x{1c}-\x{1f}]+(\w+)[\s\x{1c}-\x{1f}]+in[\s\x{1c}-\x{1f}]+self\.(\w+)[\s\x{1c}-\x{1f}]*:",
    );
    let observer_invoke = pattern_regex(&OBSERVER_INVOKE, r"^\b(\w+)[\s\x{1c}-\x{1f}]*\(");

    let has_django = source.contains(".connect(") || source.contains(".send(");
    let has_pyqt = (source.contains("from PyQt") || source.contains("from PySide"))
        && (source.contains(".connect(") || source.contains(".emit("));

    let mut results = Vec::new();
    if has_django {
        for capture in django_send.captures_iter(source) {
            let line = line_at(source, capture.get(0).unwrap().start());
            results.push(pattern(
                "django_signal_send",
                Some(capture[1].to_string()),
                enclosing_function(symbols, line),
                line,
            ));
        }
        for capture in django_connect.captures_iter(source) {
            let line = line_at(source, capture.get(0).unwrap().start());
            results.push(pattern(
                "django_signal_connect",
                Some(capture[1].to_string()),
                Some(capture[2].to_string()),
                line,
            ));
        }
    }

    if has_pyqt {
        for capture in pyqt_emit.captures_iter(source) {
            let line = line_at(source, capture.get(0).unwrap().start());
            results.push(pattern(
                "pyqt_signal_emit",
                Some(capture[1].to_string()),
                enclosing_function(symbols, line),
                line,
            ));
        }
        for capture in pyqt_connect.captures_iter(source) {
            let line = line_at(source, capture.get(0).unwrap().start());
            results.push(pattern(
                "pyqt_signal_connect",
                Some(capture[1].to_string()),
                Some(capture[2].to_string()),
                line,
            ));
        }
    }

    for capture in observer_iter.captures_iter(source) {
        let loop_var = &capture[1];
        let field_name = capture[2].to_string();
        let whole = capture.get(0).unwrap();
        let after = &source[whole.end()..];
        let trimmed = after.trim_start();
        if let Some(invoke) = observer_invoke.captures(trimmed) {
            if &invoke[1] == loop_var {
                let line = line_at(source, whole.start());
                results.push(pattern(
                    "observer_emit",
                    Some(field_name),
                    enclosing_function(symbols, line),
                    line,
                ));
            }
        }
    }

    for capture in observer_append.captures_iter(source) {
        let whole = capture.get(0).unwrap();
        let line = line_at(source, whole.start());
        results.push(pattern(
            "observer_register",
            Some(capture[1].to_string()),
            enclosing_function(symbols, line),
            line,
        ));
    }

    results
}

fn pattern(
    pattern_type: &str,
    signal_name: Option<String>,
    handler: Option<String>,
    line: i64,
) -> PatternFact {
    PatternFact {
        pattern_type: pattern_type.to_string(),
        signal_name,
        handler,
        line: Some(line),
        metadata_json: None,
    }
}

fn line_at(source: &str, byte_offset: usize) -> i64 {
    source[..byte_offset].matches('\n').count() as i64 + 1
}

/// `_enclosing_func`: the innermost function symbol spanning `line`, ties
/// broken by the latest start line.
fn enclosing_function(symbols: &[SymbolInfo], line: i64) -> Option<String> {
    let mut best: Option<&SymbolInfo> = None;
    for symbol in symbols {
        if symbol.sym_type != "function" {
            continue;
        }
        let end = symbol.end_lineno.unwrap_or(symbol.lineno);
        if symbol.lineno <= line && line <= end {
            match best {
                Some(current) if symbol.lineno < current.lineno => {}
                _ => best = Some(symbol),
            }
        }
    }
    best.map(|symbol| symbol.name.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn facts(source: &str) -> PythonFacts {
        parse_file(source, Path::new("/proj/mod.py"), Path::new("/proj"))
    }

    #[test]
    fn symbols_cover_module_functions_and_one_class_level() {
        let source = "\
class A:
    class B:
        def inner(self): pass
    def m(self): pass
def top(): pass
if True:
    def cond(): pass
";
        let symbols = facts(source).symbols;
        let names: Vec<(&str, &str)> = symbols
            .iter()
            .map(|s| (s.name.as_str(), s.sym_type.as_str()))
            .collect();
        assert_eq!(
            names,
            vec![("A", "class"), ("A.m", "function"), ("top", "function")]
        );
    }

    #[test]
    fn decorated_definitions_start_at_the_def_line() {
        let source = "class A:\n    @property\n    def m(self):\n        return 1\n";
        let symbols = facts(source).symbols;
        let method = symbols.iter().find(|s| s.name == "A.m").unwrap();
        assert_eq!(method.lineno, 3);
        assert_eq!(method.end_lineno, Some(4));
        assert_eq!(method.source_segment, "def m(self):\n        return 1");
        let class = symbols.iter().find(|s| s.name == "A").unwrap();
        assert!(class.source_segment.starts_with("class A:"));
        assert!(class.source_segment.contains("@property"));
    }

    #[test]
    fn trailing_comments_are_outside_the_source_extent() {
        let source = "def f():\n    return 1\n    # trailing inside?\n\ndef g():\n    return 2\n";
        let symbols = facts(source).symbols;
        assert_eq!(symbols[0].end_lineno, Some(2));
        assert_eq!(symbols[0].source_segment, "def f():\n    return 1");
        assert_eq!(symbols[1].lineno, 5);
        assert_eq!(symbols[1].end_lineno, Some(6));
    }

    #[test]
    fn async_functions_start_at_the_async_keyword() {
        let source = "@deco\nasync def g(a):\n    return a\n";
        let symbols = facts(source).symbols;
        assert_eq!(symbols[0].lineno, 2);
        assert_eq!(symbols[0].source_segment, "async def g(a):\n    return a");
    }

    #[test]
    fn bases_drop_non_name_entries_but_keep_the_empty_list() {
        let source = "\
class A(Base1, ns.Base2, Generic[T], metaclass=M): pass
class B(Generic[T]): pass
class C: pass
";
        let symbols = facts(source).symbols;
        assert_eq!(
            symbols[0].bases,
            Some(vec!["Base1".to_string(), "Base2".to_string()])
        );
        assert_eq!(symbols[1].bases, Some(Vec::new()));
        assert_eq!(symbols[2].bases, None);
    }

    #[test]
    fn docstring_literal_is_spliced_out_of_the_hash_segment() {
        let source = "def f(a):\n    \"\"\"Doc with # inside.\"\"\"\n    return a\n";
        let symbols = facts(source).symbols;
        let f = &symbols[0];
        assert!(f.hash_source_segment.is_some());
        assert!(!f.hash_segment().contains("Doc with"));
        assert!(f.hash_segment().contains("return a"));
        assert!(f.source_segment.contains("Doc with"));

        let edited = "def f(a):\n    \"\"\"Entirely different words.\"\"\"\n    return a\n";
        let edited_symbols = facts(edited).symbols;
        assert_eq!(
            symbol_hash_input(f.hash_segment()),
            symbol_hash_input(edited_symbols[0].hash_segment()),
        );
    }

    #[test]
    fn concatenated_plain_docstring_is_removed_like_cpython_constant_folding() {
        let source = "def g(a):\n    \"part one \" \"part two\"\n    return a\n";
        let symbols = facts(source).symbols;
        let seg = symbols[0].hash_segment();
        assert!(!seg.contains("part one"));
        assert!(seg.contains("return a"));
    }

    #[test]
    fn non_docstring_first_statements_leave_the_hash_segment_unset() {
        for source in [
            "def f(a):\n    f\"\"\"formatted {a}\"\"\"\n    return a\n",
            "def f(a):\n    b\"\"\"bytes literal\"\"\"\n    return a\n",
            "def f(a):\n    s = \"\"\"assigned\"\"\"\n    return s\n",
            "def f(a):\n    return a\n",
        ] {
            let symbols = facts(source).symbols;
            assert!(symbols[0].hash_source_segment.is_none(), "{source}");
        }
    }

    #[test]
    fn class_and_method_docstrings_are_removed_per_symbol() {
        let source = "class C:\n    \"\"\"Class doc.\"\"\"\n\n    def m(self):\n        \"\"\"Method doc.\"\"\"\n        return 1\n";
        let symbols = facts(source).symbols;
        let class_sym = symbols.iter().find(|s| s.name == "C").unwrap();
        let method_sym = symbols.iter().find(|s| s.name == "C.m").unwrap();
        assert!(!class_sym.hash_segment().contains("Class doc"));
        assert!(!method_sym.hash_segment().contains("Method doc"));
        assert!(method_sym.hash_segment().contains("return 1"));
    }

    #[test]
    fn docstrings_are_cleandoc_normalized() {
        let source =
            "def f(a):\n    \"\"\"  Line one.\n        Indented two.\n\n    Tail.   \"\"\"\n    return a\n";
        let symbols = facts(source).symbols;
        assert_eq!(
            symbols[0].docstring.as_deref(),
            Some("Line one.\n    Indented two.\n\nTail.   ")
        );

        let tabbed = "def f():\n\t'''\tTabbed doc.\n\tSecond.'''\n\treturn 1\n";
        let symbols = facts(tabbed).symbols;
        assert_eq!(
            symbols[0].docstring.as_deref(),
            Some("Tabbed doc.\nSecond.")
        );
    }

    #[test]
    fn hash_input_strips_from_a_hash_inside_a_string() {
        let segment = "def f():\n    s = '# not a comment'\n    return s  # real";
        assert_eq!(
            symbol_hash_input(segment),
            "def f():\n    s = '\n    return s  "
        );
    }

    #[test]
    fn call_edges_carry_class_prefix_and_call_form() {
        let source = "\
class A:
    def m(self):
        helper()
        obj.attr_call(x)
        def inner():
            deep()
        inner()
def helper():
    pass
if True:
    def cond():
        c1()
";
        let edges = facts(source).edges;
        let rendered: Vec<(&str, &str, &str)> = edges
            .iter()
            .map(|e| (e.caller.as_str(), e.callee.as_str(), e.call_form))
            .collect();
        assert!(rendered.contains(&("A.m", "helper", "name")));
        assert!(rendered.contains(&("A.m", "attr_call", "attribute")));
        assert!(rendered.contains(&("A.inner", "deep", "name")));
        assert!(rendered.contains(&("A.m", "inner", "name")));
        assert!(rendered.contains(&("cond", "c1", "name")));
    }

    #[test]
    fn decorator_calls_belong_to_the_decorated_function() {
        let source = "\
import functools

@functools.lru_cache(maxsize=None)
def wrapper(a):
    return a

class A:
    @register(name=compute())
    def m(self):
        body()
";
        let edges = facts(source).edges;
        let rendered: Vec<(&str, &str, &str)> = edges
            .iter()
            .map(|e| (e.caller.as_str(), e.callee.as_str(), e.call_form))
            .collect();
        assert!(rendered.contains(&("wrapper", "lru_cache", "attribute")));
        assert!(rendered.contains(&("A.m", "register", "name")));
        assert!(rendered.contains(&("A.m", "compute", "name")));
        assert!(rendered.contains(&("A.m", "body", "name")));
    }

    #[test]
    fn default_value_and_return_annotation_calls_are_edges() {
        let source = "def f(x=compute()) -> annotate():\n    return x\n";
        let edges = facts(source).edges;
        let rendered: Vec<(&str, &str)> = edges
            .iter()
            .map(|e| (e.caller.as_str(), e.callee.as_str()))
            .collect();
        assert!(rendered.contains(&("f", "compute")));
        assert!(rendered.contains(&("f", "annotate")));
    }

    #[test]
    fn unparsable_source_keeps_patterns_but_drops_parsed_facts() {
        let source = "class X:\n    def m(self):\n        self.obs.append(h)\ndef f(:\n    pass\n";
        let parsed = facts(source);
        assert!(parsed.symbols.is_empty());
        assert!(parsed.edges.is_empty());
        assert!(parsed.imports.is_empty());
        assert_eq!(parsed.import_bindings_json, "[]");
        assert_eq!(parsed.patterns.len(), 1);
        assert_eq!(parsed.patterns[0].pattern_type, "observer_register");
        assert_eq!(parsed.patterns[0].signal_name.as_deref(), Some("obs"));
        assert_eq!(parsed.patterns[0].handler, None);
    }

    #[test]
    fn leading_bom_is_a_syntax_error_for_the_oracle() {
        let parsed = facts("\u{feff}def f(): pass\n");
        assert!(parsed.symbols.is_empty());
    }

    #[test]
    fn null_bytes_are_a_syntax_error_for_the_oracle() {
        let parsed = facts("x = 1\0\ndef f(): pass\n");
        assert!(parsed.symbols.is_empty());
        assert!(parsed.edges.is_empty());
        assert!(parsed.imports.is_empty());
        assert_eq!(parsed.import_bindings_json, "[]");
    }

    #[test]
    fn unresolved_bindings_are_sorted_by_module() {
        let source = "from __future__ import annotations\nimport os\nimport pkg.mod as pm\nfrom . import sibling\nfrom ..up import thing as t\nfrom json import loads\n";
        let parsed = parse_file(source, Path::new("/proj/sub/x.py"), Path::new("/proj"));
        assert_eq!(
            parsed.import_bindings_json,
            "[{\"module\": \"__future__\", \"names\": [\"annotations\"]}, \
             {\"module\": \"json\", \"names\": [\"loads\"]}, \
             {\"module\": \"os\", \"names\": [\"os\"]}, \
             {\"module\": \"pkg.mod\", \"names\": [\"pm\"]}]"
        );
    }

    #[test]
    fn wildcard_import_binds_the_star_name() {
        let parsed = parse_file(
            "from mod import *\n",
            Path::new("/proj/x.py"),
            Path::new("/proj"),
        );
        assert_eq!(
            parsed.import_bindings_json,
            "[{\"module\": \"mod\", \"names\": [\"*\"]}]"
        );
    }

    #[test]
    fn observer_emit_requires_the_loop_variable_to_be_called() {
        let matching =
            "class X:\n    def fire(self):\n        for cb in self.subs:\n            cb()\n";
        let parsed = facts(matching);
        assert_eq!(parsed.patterns.len(), 1);
        assert_eq!(parsed.patterns[0].pattern_type, "observer_emit");
        assert_eq!(parsed.patterns[0].signal_name.as_deref(), Some("subs"));
        assert_eq!(parsed.patterns[0].handler.as_deref(), Some("X.fire"));

        let other =
            "class X:\n    def fire(self):\n        for cb in self.subs:\n            other()\n";
        assert!(facts(other).patterns.is_empty());
    }
}
