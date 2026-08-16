//! `RustParser` replication against the same grammar version
//! (tree-sitter-rust =0.24.2). The oracle has no regex fallback for Rust
//! and the grammar is compiled into this binary, so the
//! `rust-unavailable` rejection path stays Python-side only.
//!
//! Contracts replicated here:
//!
//! - **Symbol scope**: the R3.0b support matrix — free functions, impl
//!   methods (type-qualified), struct/enum/trait/type alias,
//!   `macro_rules!`, modules (recursive), trait method signatures.
//! - **Source extent**: contiguous immediately-preceding `attribute_item`
//!   siblings prepend to the segment so cfg-gated same-name duplicates
//!   hash differently.
//! - **Hash input**: parse the segment and drop every
//!   `line_comment`/`block_comment` token's byte range (nested block
//!   comments are single tokens; regex cannot express that).
//! - **Trait bases**: same-file `impl Trait for Type` merges the trait's
//!   short name into the type's bases (exact full-name match first, then a
//!   unique short-name fallback). Cross-file impls stay a known gap until
//!   R3.4.
//! - **Imports**: `mod x;` file-existence mapping plus deterministic `use`
//!   resolution — `crate::` anchors at the nearest lib.rs/main.rs ancestor,
//!   `self`/`super` walk module directories, and bare heads are external
//!   crates unless the file declares that module itself.

use crate::facts::{CacheIdentity, EdgeInfo, SymbolInfo};
use crate::parse_c_cpp::{normpath, relpath_slash};
use crate::pyjson;
use serde_json::json;
use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};
use tree_sitter::{Node, Parser};

pub const LANGUAGE_ID: &str = "RustParser";
pub const CACHE_CONTRACT_VERSION: &str = "2";
pub const EXTENSIONS: &[&str] = &[".rs"];

/// Crate versions pinned in Cargo.toml, recorded in `parser_environment`
/// (an ALLOWED_DIFF column under classification v2).
pub const TREE_SITTER_CRATE_VERSION: &str = "0.25";
pub const GRAMMAR_RUST_CRATE_VERSION: &str = "0.24.2";

const CRATE_ROOT_FILES: &[&str] = &["lib.rs", "main.rs"];
const MODULE_FILE_BASENAMES: &[&str] = &["mod.rs", "lib.rs", "main.rs"];
const COMMENT_NODE_TYPES: &[&str] = &["line_comment", "block_comment"];

pub fn cache_identity() -> CacheIdentity {
    let environment = pyjson::dumps_identity(&json!({
        "tree-sitter": TREE_SITTER_CRATE_VERSION,
        "tree-sitter-rust": GRAMMAR_RUST_CRATE_VERSION,
    }));
    CacheIdentity {
        contract_version: CACHE_CONTRACT_VERSION.to_string(),
        backend: "rust-tree-sitter".to_string(),
        environment,
    }
}

fn make_parser() -> Parser {
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_rust::LANGUAGE.into())
        .expect("bundled grammar must be ABI-compatible with the linked tree-sitter core");
    parser
}

fn text<'a>(node: Node, source: &'a str) -> &'a str {
    source.get(node.byte_range()).unwrap_or("")
}

fn field_text(node: Node, field: &str, source: &str) -> Option<String> {
    node.child_by_field_name(field)
        .map(|n| text(n, source).to_string())
}

fn qualified(prefix: Option<&str>, name: &str) -> String {
    match prefix {
        Some(parent) => format!("{parent}.{name}"),
        None => name.to_string(),
    }
}

/// `rust_parser._type_name`: descend an impl/trait type reference to its
/// trailing type identifier (plain, scoped, or generic).
fn type_name(node: Option<Node>, source: &str) -> Option<String> {
    let node = node?;
    match node.kind() {
        "type_identifier" | "identifier" => Some(text(node, source).to_string()),
        "scoped_type_identifier" => type_name(node.child_by_field_name("name"), source),
        "generic_type" => type_name(node.child_by_field_name("type"), source),
        _ => {
            let mut cursor = node.walk();
            let children: Vec<Node> = node.named_children(&mut cursor).collect();
            for child in children.into_iter().rev() {
                if let Some(name) = type_name(Some(child), source) {
                    return Some(name);
                }
            }
            None
        }
    }
}

fn skip_attributes_backward(node: Node) -> Option<Node> {
    let mut cursor = node.prev_named_sibling();
    while let Some(current) = cursor {
        if current.kind() != "attribute_item" {
            return Some(current);
        }
        cursor = current.prev_named_sibling();
    }
    None
}

/// `_extract_rust_doc`: `///` runs or a `/** */` block immediately before
/// the item, skipping contiguous attribute_item siblings in between.
fn extract_doc(node: Node, source: &str) -> Option<String> {
    let prev = skip_attributes_backward(node)?;
    if !COMMENT_NODE_TYPES.contains(&prev.kind()) {
        return None;
    }
    let comment = text(prev, source);
    if prev.kind() == "block_comment" && comment.starts_with("/**") {
        let mut raw = &comment[3..];
        if let Some(stripped) = raw.strip_suffix("*/") {
            raw = stripped;
        }
        let lines: Vec<String> = raw
            .lines()
            .map(|l| l.trim().trim_start_matches(['*', ' ']).trim().to_string())
            .filter(|l| !l.is_empty())
            .collect();
        if lines.is_empty() {
            return None;
        }
        return Some(lines[..lines.len().min(3)].join(" "));
    }
    if prev.kind() == "line_comment" && comment.starts_with("///") {
        let mut doc_lines = vec![comment[3..].trim().to_string()];
        let mut cursor = prev.prev_named_sibling();
        while let Some(prior) = cursor {
            if prior.kind() != "line_comment" {
                break;
            }
            let Some(stripped) = text(prior, source).strip_prefix("///") else {
                break;
            };
            doc_lines.insert(0, stripped.trim().to_string());
            cursor = prior.prev_named_sibling();
        }
        return Some(doc_lines[..doc_lines.len().min(3)].join(" "));
    }
    None
}

/// `_segment_with_attributes`: extend the item's byte range over the
/// contiguous immediately-preceding attribute_item siblings.
fn segment_with_attributes(node: Node, source: &str) -> String {
    let mut start = node.start_byte();
    let mut cursor = node.prev_named_sibling();
    while let Some(current) = cursor {
        if current.kind() != "attribute_item" {
            break;
        }
        start = current.start_byte();
        cursor = current.prev_named_sibling();
    }
    source.get(start..node.end_byte()).unwrap_or("").to_string()
}

/// `RustParser.symbol_hash_input`: reparse the segment and drop every
/// comment token's byte range.
pub fn symbol_hash_input(source_segment: &str) -> String {
    let mut parser = make_parser();
    let Some(tree) = parser.parse(source_segment.as_bytes(), None) else {
        return source_segment.to_string();
    };
    let mut ranges: Vec<(usize, usize)> = Vec::new();
    collect_comment_ranges(tree.root_node(), &mut ranges);
    if ranges.is_empty() {
        return source_segment.to_string();
    }
    ranges.sort_unstable();
    let bytes = source_segment.as_bytes();
    let mut kept: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut pos = 0;
    for (start, end) in ranges {
        kept.extend_from_slice(&bytes[pos..start]);
        pos = end;
    }
    kept.extend_from_slice(&bytes[pos..]);
    String::from_utf8_lossy(&kept).into_owned()
}

fn collect_comment_ranges(node: Node, ranges: &mut Vec<(usize, usize)>) {
    if COMMENT_NODE_TYPES.contains(&node.kind()) {
        ranges.push((node.start_byte(), node.end_byte()));
        return;
    }
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        collect_comment_ranges(child, ranges);
    }
}

/// `RustParser.parse_symbols`.
pub fn parse_symbols(source: &str) -> Vec<SymbolInfo> {
    let mut parser = make_parser();
    let Some(tree) = parser.parse(source.as_bytes(), None) else {
        return Vec::new();
    };
    let mut symbols = Vec::new();
    let mut trait_impls: BTreeMap<String, Vec<String>> = BTreeMap::new();
    walk_items(
        tree.root_node(),
        source,
        &mut symbols,
        None,
        &mut trait_impls,
    );
    merge_trait_bases(&mut symbols, &trait_impls);
    symbols.sort_by_key(|s| s.lineno);
    symbols
}

fn emit(
    symbols: &mut Vec<SymbolInfo>,
    node: Node,
    source: &str,
    name: String,
    sym_type: &str,
) -> usize {
    symbols.push(SymbolInfo {
        name,
        args: String::new(),
        sym_type: sym_type.to_string(),
        lineno: node.start_position().row as i64 + 1,
        source_segment: segment_with_attributes(node, source),
        end_lineno: Some(node.end_position().row as i64 + 1),
        docstring: extract_doc(node, source),
        bases: None,
    });
    symbols.len() - 1
}

fn emit_function(symbols: &mut Vec<SymbolInfo>, node: Node, source: &str, prefix: Option<&str>) {
    let Some(name) = field_text(node, "name", source) else {
        return;
    };
    let full_name = qualified(prefix, &name);
    let params = field_text(node, "parameters", source).unwrap_or_else(|| "()".to_string());
    let index = emit(symbols, node, source, full_name, "function");
    symbols[index].args = params;
}

fn walk_items(
    node: Node,
    source: &str,
    symbols: &mut Vec<SymbolInfo>,
    prefix: Option<&str>,
    trait_impls: &mut BTreeMap<String, Vec<String>>,
) {
    let mut cursor = node.walk();
    let children: Vec<Node> = node.children(&mut cursor).collect();
    for child in children {
        match child.kind() {
            "function_item" => emit_function(symbols, child, source, prefix),
            "struct_item" | "enum_item" | "trait_item" | "type_item" => {
                let Some(name) = field_text(child, "name", source) else {
                    continue;
                };
                let full_name = qualified(prefix, &name);
                let sym_type = match child.kind() {
                    "struct_item" => "struct",
                    "enum_item" => "enum",
                    "trait_item" => "interface",
                    _ => "type_alias",
                };
                emit(symbols, child, source, full_name.clone(), sym_type);
                if child.kind() == "trait_item" {
                    if let Some(body) = child.child_by_field_name("body") {
                        let mut body_cursor = body.walk();
                        for member in body.children(&mut body_cursor) {
                            if matches!(member.kind(), "function_item" | "function_signature_item")
                            {
                                emit_function(symbols, member, source, Some(&full_name));
                            }
                        }
                    }
                }
            }
            "macro_definition" => {
                if let Some(name) = field_text(child, "name", source) {
                    emit(symbols, child, source, qualified(prefix, &name), "macro");
                }
            }
            "mod_item" => {
                let Some(name) = field_text(child, "name", source) else {
                    continue;
                };
                let Some(body) = child.child_by_field_name("body") else {
                    continue;
                };
                let full_mod = qualified(prefix, &name);
                emit(symbols, child, source, full_mod.clone(), "namespace");
                walk_items(body, source, symbols, Some(&full_mod), trait_impls);
            }
            "impl_item" => {
                let Some(impl_type) = type_name(child.child_by_field_name("type"), source) else {
                    continue;
                };
                let full_type = qualified(prefix, &impl_type);
                if let Some(trait_name) = type_name(child.child_by_field_name("trait"), source) {
                    trait_impls
                        .entry(full_type.clone())
                        .or_default()
                        .push(trait_name);
                }
                if let Some(body) = child.child_by_field_name("body") {
                    let mut body_cursor = body.walk();
                    for member in body.children(&mut body_cursor) {
                        if member.kind() == "function_item" {
                            emit_function(symbols, member, source, Some(&full_type));
                        }
                    }
                }
            }
            _ => {}
        }
    }
}

/// `RustParser._merge_trait_bases`: exact full-name match first, unique
/// short-name fallback among this file's struct/enum symbols.
fn merge_trait_bases(symbols: &mut [SymbolInfo], trait_impls: &BTreeMap<String, Vec<String>>) {
    if trait_impls.is_empty() {
        return;
    }
    let mut by_name: BTreeMap<String, usize> = BTreeMap::new();
    let mut by_short: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (index, symbol) in symbols.iter().enumerate() {
        if symbol.sym_type != "struct" && symbol.sym_type != "enum" {
            continue;
        }
        by_name.insert(symbol.name.clone(), index);
        let short = symbol.name.rsplit('.').next().unwrap_or(&symbol.name);
        by_short.entry(short.to_string()).or_default().push(index);
    }

    for (full_type, traits) in trait_impls {
        let target = match by_name.get(full_type) {
            Some(&index) => index,
            None => {
                let short = full_type.rsplit('.').next().unwrap_or(full_type);
                match by_short.get(short) {
                    Some(candidates) if candidates.len() == 1 => candidates[0],
                    _ => continue,
                }
            }
        };
        let mut merged = symbols[target].bases.clone().unwrap_or_default();
        for trait_name in traits {
            if !merged.contains(trait_name) {
                merged.push(trait_name.clone());
            }
        }
        symbols[target].bases = if merged.is_empty() {
            None
        } else {
            Some(merged)
        };
    }
}

/// `RustParser.extract_call_graph`.
pub fn extract_call_graph(source: &str) -> Vec<EdgeInfo> {
    let mut parser = make_parser();
    let Some(tree) = parser.parse(source.as_bytes(), None) else {
        return Vec::new();
    };
    let mut edges = Vec::new();
    walk_calls(tree.root_node(), source, None, None, &mut edges);
    edges
}

fn callee_name(func_node: Option<Node>, source: &str) -> Option<String> {
    let node = func_node?;
    match node.kind() {
        "identifier" => Some(text(node, source).to_string()),
        "field_expression" => node
            .child_by_field_name("field")
            .map(|f| text(f, source).to_string()),
        "scoped_identifier" => node
            .child_by_field_name("name")
            .map(|n| text(n, source).to_string()),
        "generic_function" => callee_name(node.child_by_field_name("function"), source),
        _ => None,
    }
}

fn walk_calls(
    node: Node,
    source: &str,
    prefix: Option<&str>,
    current_fn: Option<&str>,
    edges: &mut Vec<EdgeInfo>,
) {
    match node.kind() {
        "function_item" => {
            if let Some(name) = field_text(node, "name", source) {
                let qualified_name = qualified(prefix, &name);
                let mut cursor = node.walk();
                let children: Vec<Node> = node.children(&mut cursor).collect();
                for child in children {
                    walk_calls(child, source, prefix, Some(&qualified_name), edges);
                }
                return;
            }
        }
        "impl_item" => {
            if let Some(impl_type) = type_name(node.child_by_field_name("type"), source) {
                let new_prefix = qualified(prefix, &impl_type);
                let mut cursor = node.walk();
                let children: Vec<Node> = node.children(&mut cursor).collect();
                for child in children {
                    walk_calls(child, source, Some(&new_prefix), current_fn, edges);
                }
                return;
            }
        }
        "mod_item" | "trait_item" => {
            if let Some(name) = field_text(node, "name", source) {
                let new_prefix = qualified(prefix, &name);
                let mut cursor = node.walk();
                let children: Vec<Node> = node.children(&mut cursor).collect();
                for child in children {
                    walk_calls(child, source, Some(&new_prefix), current_fn, edges);
                }
                return;
            }
        }
        "call_expression" => {
            if let Some(caller) = current_fn {
                let func_node = node.child_by_field_name("function");
                if let Some(callee) = callee_name(func_node, source) {
                    edges.push(EdgeInfo {
                        caller: caller.to_string(),
                        callee,
                        line: node.start_position().row as i64 + 1,
                        call_form: if func_node.map(|n| n.kind()) == Some("field_expression") {
                            "attribute"
                        } else {
                            "name"
                        },
                    });
                }
            }
        }
        _ => {}
    }

    let mut cursor = node.walk();
    let children: Vec<Node> = node.children(&mut cursor).collect();
    for child in children {
        walk_calls(child, source, prefix, current_fn, edges);
    }
}

/// `os.path.dirname` on an absolute path: parent directory, or the path
/// itself at a filesystem root (dirname("C:\\") == "C:\\").
fn dirname(path: &Path) -> PathBuf {
    match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.to_path_buf(),
        Some(_) => PathBuf::new(),
        None => path.to_path_buf(),
    }
}

fn normcase_eq(left: &Path, right: &Path) -> bool {
    if cfg!(windows) {
        left.to_string_lossy().to_lowercase().replace('/', "\\")
            == right.to_string_lossy().to_lowercase().replace('/', "\\")
    } else {
        left == right
    }
}

/// `RustParser._find_crate_root`.
fn find_crate_root(start_dir: &Path, root_dir: &Path) -> Option<PathBuf> {
    let root_abs = normpath(&std::path::absolute(root_dir).unwrap_or_else(|_| root_dir.into()));
    let mut cursor = start_dir.to_path_buf();
    loop {
        if CRATE_ROOT_FILES
            .iter()
            .any(|marker| cursor.join(marker).is_file())
        {
            return Some(cursor);
        }
        if normcase_eq(&cursor, &root_abs) {
            return None;
        }
        let parent = dirname(&cursor);
        if parent == cursor {
            return None;
        }
        cursor = parent;
    }
}

/// `RustParser.resolve_imports`: `mod x;` and `use` file-existence mapping.
pub fn resolve_imports(source: &str, file_path: &Path, root_dir: &Path) -> Vec<String> {
    let mut parser = make_parser();
    let Some(tree) = parser.parse(source.as_bytes(), None) else {
        return Vec::new();
    };
    let root = tree.root_node();

    let abs_file = normpath(&std::path::absolute(file_path).unwrap_or_else(|_| file_path.into()));
    let current_dir = dirname(&abs_file);
    let basename = abs_file
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let module_dir = if MODULE_FILE_BASENAMES.contains(&basename.as_str()) {
        current_dir.clone()
    } else {
        let stem = basename
            .rsplit_once('.')
            .map(|(stem, _)| stem.to_string())
            .unwrap_or_else(|| basename.clone());
        current_dir.join(stem)
    };
    let crate_root = find_crate_root(&current_dir, root_dir);

    let mut imports: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    let record = |candidate: &Path, imports: &mut Vec<String>, seen: &mut HashSet<String>| {
        if candidate.is_file() {
            let rel = relpath_slash(candidate, root_dir);
            if !rel.starts_with("..") {
                if seen.insert(rel.clone()) {
                    imports.push(rel);
                }
                return true;
            }
        }
        false
    };

    let try_module = |base_dir: Option<&Path>,
                      segments: &[String],
                      imports: &mut Vec<String>,
                      seen: &mut HashSet<String>| {
        let Some(base_dir) = base_dir else {
            return false;
        };
        if segments.is_empty() {
            return false;
        }
        for k in (1..=segments.len()).rev() {
            let mut stem = base_dir.to_path_buf();
            for segment in &segments[..k] {
                stem.push(segment);
            }
            let mut with_rs = stem.clone().into_os_string();
            with_rs.push(".rs");
            if record(Path::new(&with_rs), imports, seen)
                || record(&stem.join("mod.rs"), imports, seen)
            {
                return true;
            }
        }
        false
    };

    let mut declared_mods: HashSet<String> = HashSet::new();
    let mut root_cursor = root.walk();
    let top_children: Vec<Node> = root.children(&mut root_cursor).collect();
    for child in &top_children {
        if child.kind() == "mod_item" {
            let Some(name) = field_text(*child, "name", source) else {
                continue;
            };
            declared_mods.insert(name.clone());
            if child.child_by_field_name("body").is_none() {
                try_module(Some(&module_dir), &[name], &mut imports, &mut seen);
            }
        }
    }

    let mut use_stack = vec![root];
    while let Some(node) = use_stack.pop() {
        if node.kind() == "use_declaration" {
            let argument = node.child_by_field_name("argument");
            for segments in use_paths(argument, source) {
                if segments.is_empty() {
                    continue;
                }
                let head = segments[0].as_str();
                let rest = &segments[1..];
                match head {
                    "crate" => {
                        try_module(crate_root.as_deref(), rest, &mut imports, &mut seen);
                    }
                    "self" => {
                        try_module(Some(&module_dir), rest, &mut imports, &mut seen);
                    }
                    "super" => {
                        let mut base = dirname(&module_dir);
                        let mut remaining = rest;
                        while remaining.first().map(String::as_str) == Some("super") {
                            base = dirname(&base);
                            remaining = &remaining[1..];
                        }
                        try_module(Some(&base), remaining, &mut imports, &mut seen);
                    }
                    _ => {
                        if declared_mods.contains(head) {
                            try_module(Some(&module_dir), &segments, &mut imports, &mut seen);
                        }
                    }
                }
            }
            continue;
        }
        let mut cursor = node.walk();
        // Stack order does not matter for correctness: hits are keyed by
        // resolved path and the oracle records dict insertion order per
        // use_declaration, which the outer traversal preserves below.
        let mut children: Vec<Node> = node.children(&mut cursor).collect();
        children.reverse();
        use_stack.extend(children);
    }

    imports
}

/// `RustParser._use_paths`: flatten a use-tree into segment lists.
fn use_paths(node: Option<Node>, source: &str) -> Vec<Vec<String>> {
    let Some(node) = node else {
        return Vec::new();
    };
    match node.kind() {
        "identifier" | "metavariable" => vec![vec![text(node, source).to_string()]],
        "crate" | "super" | "self" => vec![vec![node.kind().to_string()]],
        "scoped_identifier" => {
            let prefixes = match node.child_by_field_name("path") {
                Some(path) => use_paths(Some(path), source),
                None => vec![Vec::new()],
            };
            let suffix: Vec<String> = node
                .child_by_field_name("name")
                .map(|n| vec![text(n, source).to_string()])
                .unwrap_or_default();
            prefixes
                .into_iter()
                .map(|mut p| {
                    p.extend(suffix.iter().cloned());
                    p
                })
                .collect()
        }
        "use_as_clause" => use_paths(node.child_by_field_name("path"), source),
        "scoped_use_list" => {
            let prefixes = match node.child_by_field_name("path") {
                Some(path) => use_paths(Some(path), source),
                None => vec![Vec::new()],
            };
            let mut results = Vec::new();
            if let Some(list) = node.child_by_field_name("list") {
                let mut cursor = list.walk();
                for child in list.named_children(&mut cursor) {
                    for tail in use_paths(Some(child), source) {
                        for prefix in &prefixes {
                            let mut combined = prefix.clone();
                            combined.extend(tail.iter().cloned());
                            results.push(combined);
                        }
                    }
                }
            }
            results
        }
        "use_list" => {
            let mut results = Vec::new();
            let mut cursor = node.walk();
            for child in node.named_children(&mut cursor) {
                results.extend(use_paths(Some(child), source));
            }
            results
        }
        "use_wildcard" => {
            let mut cursor = node.walk();
            if let Some(child) = node.named_children(&mut cursor).next() {
                return use_paths(Some(child), source);
            }
            Vec::new()
        }
        _ => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_support_matrix_symbols() {
        let source = "\
/// Adds one.
#[inline]
pub fn add_one(x: i64) -> i64 { x + 1 }

pub struct Point { x: i64 }

enum Color { Red }

trait Shape {
    fn area(&self) -> f64;
    fn zero() -> f64 { 0.0 }
}

type Alias = Point;

macro_rules! twice { ($x:expr) => { $x * 2 }; }

mod geometry {
    pub fn inner() -> i64 { 1 }
}

impl Shape for Point {
    fn area(&self) -> f64 { 1.0 }
}
";
        let symbols = parse_symbols(source);
        let names: Vec<(&str, &str)> = symbols
            .iter()
            .map(|s| (s.name.as_str(), s.sym_type.as_str()))
            .collect();
        assert_eq!(
            names,
            vec![
                ("add_one", "function"),
                ("Point", "struct"),
                ("Color", "enum"),
                ("Shape", "interface"),
                ("Shape.area", "function"),
                ("Shape.zero", "function"),
                ("Alias", "type_alias"),
                ("twice", "macro"),
                ("geometry", "namespace"),
                ("geometry.inner", "function"),
                ("Point.area", "function"),
            ]
        );
        assert_eq!(symbols[0].docstring.as_deref(), Some("Adds one."));
        assert!(symbols[0].source_segment.starts_with("#[inline]"));
        assert_eq!(symbols[0].args, "(x: i64)");
        let point = symbols.iter().find(|s| s.name == "Point").unwrap();
        assert_eq!(point.bases, Some(vec!["Shape".to_string()]));
    }

    #[test]
    fn cfg_gated_duplicates_get_distinct_segments() {
        let source = "\
#[cfg(unix)]
fn imp() -> i64 { 1 }
#[cfg(windows)]
fn imp() -> i64 { 2 }
";
        let symbols = parse_symbols(source);
        assert_eq!(symbols.len(), 2);
        assert!(symbols[0].source_segment.contains("cfg(unix)"));
        assert!(symbols[1].source_segment.contains("cfg(windows)"));
        assert_ne!(
            crate::hashes::symbol_hash(&symbol_hash_input(&symbols[0].source_segment)),
            crate::hashes::symbol_hash(&symbol_hash_input(&symbols[1].source_segment)),
        );
    }

    #[test]
    fn hash_input_drops_nested_block_and_doc_comments() {
        let with_comments =
            "/// doc line\nfn f() -> i64 { /* outer /* nested */ still */ 1 } // tail";
        // The line_comment token includes its trailing newline in grammar
        // 0.24.2, so the doc line strips together with it (oracle-verified).
        let clean = "fn f() -> i64 {  1 } ";
        assert_eq!(symbol_hash_input(with_comments), clean);
        let literal = "fn f() -> &'static str { \"// not a comment\" }";
        assert_eq!(symbol_hash_input(literal), literal);
    }

    #[test]
    fn unique_short_name_fallback_merges_mod_scoped_impl() {
        let source = "\
mod shapes {
    pub struct Circle;
}
impl Round for shapes::Circle {}
trait Round {}
";
        let symbols = parse_symbols(source);
        let circle = symbols.iter().find(|s| s.name == "shapes.Circle").unwrap();
        assert_eq!(circle.bases, Some(vec!["Round".to_string()]));
    }

    #[test]
    fn call_graph_prefixes_and_call_forms() {
        let source = "\
fn helper() {}
mod app {
    pub fn run(v: &Vec<i64>) {
        helper();
        v.len();
        std::mem::drop(v);
        core::convert::identity::<i64>(1);
    }
}
impl Widget {
    fn draw(&self) { self.render(); }
}
";
        let edges = extract_call_graph(source);
        let rows: Vec<(&str, &str, &str)> = edges
            .iter()
            .map(|e| (e.caller.as_str(), e.callee.as_str(), e.call_form))
            .collect();
        assert_eq!(
            rows,
            vec![
                ("app.run", "helper", "name"),
                ("app.run", "len", "attribute"),
                ("app.run", "drop", "name"),
                ("app.run", "identity", "name"),
                ("Widget.draw", "render", "attribute"),
            ]
        );
    }

    #[test]
    fn imports_map_mod_use_crate_self_super_and_bare_heads() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        std::fs::create_dir_all(root.join("src/util")).unwrap();
        std::fs::write(root.join("src/main.rs"), "").unwrap();
        std::fs::write(root.join("src/config.rs"), "").unwrap();
        std::fs::write(root.join("src/util/mod.rs"), "").unwrap();
        std::fs::write(root.join("src/util/paths.rs"), "").unwrap();
        std::fs::write(root.join("src/util/extra.rs"), "").unwrap();

        let source = "\
mod config;
mod util;
use crate::util::paths;
use self::util::extra;
use serde::Deserialize;
use util::paths::helper_fn;
";
        let imports = resolve_imports(source, &root.join("src/main.rs"), root);
        assert_eq!(
            imports,
            vec![
                "src/config.rs",
                "src/util/mod.rs",
                "src/util/paths.rs",
                "src/util/extra.rs",
            ]
        );

        // `super` walks up from the *file's own module directory*
        // (src/util/paths for paths.rs), so super::config probes
        // src/util/config.rs — absent — and only crate::util resolves
        // (oracle-verified).
        let sibling = "use super::config;\nuse crate::util;\n";
        let imports = resolve_imports(sibling, &root.join("src/util/paths.rs"), root);
        assert_eq!(imports, vec!["src/util/mod.rs"]);
    }

    #[test]
    fn grouped_use_lists_flatten() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        std::fs::create_dir_all(root.join("src")).unwrap();
        std::fs::write(root.join("src/lib.rs"), "").unwrap();
        std::fs::write(root.join("src/alpha.rs"), "").unwrap();
        std::fs::write(root.join("src/beta.rs"), "").unwrap();

        let source = "use crate::{alpha, beta::helper as h};\n";
        let imports = resolve_imports(source, &root.join("src/lib.rs"), root);
        assert_eq!(imports, vec!["src/alpha.rs", "src/beta.rs"]);
    }

    #[test]
    fn cache_identity_is_the_rust_producer() {
        let identity = cache_identity();
        assert_eq!(identity.contract_version, "2");
        assert_eq!(identity.backend, "rust-tree-sitter");
        assert!(identity.environment.contains("tree-sitter-rust"));
    }
}
