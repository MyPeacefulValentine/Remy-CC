//! CCppParser replication (tree-sitter path only — the frozen oracle runs
//! with tree-sitter installed, so the regex fallback is out of scope).

use crate::facts::{CacheIdentity, EdgeInfo, SymbolInfo};
use crate::pyjson;
use regex::Regex;
use serde_json::json;
use std::path::Path;
use std::sync::OnceLock;
use tree_sitter::{Language, Node, Parser};

pub const LANGUAGE_ID: &str = "CCppParser";
pub const CACHE_CONTRACT_VERSION: &str = "1";

pub const EXTENSIONS: &[&str] = &[".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh", ".hxx"];

const CPP_EXTENSIONS: &[&str] = &[".cpp", ".hpp", ".cc", ".cxx", ".hh", ".hxx"];
const CPP_HEADER_INDICATORS: &[&str] = &[
    "class ",
    "namespace ",
    "template<",
    "template <",
    "public:",
    "private:",
    "protected:",
    "::",
];

pub fn handles(filename: &str) -> bool {
    EXTENSIONS.iter().any(|ext| filename.ends_with(ext))
}

/// CCppParser._uses_cpp_grammar: extension first, then the 8-substring
/// heuristic for `.h` headers.
pub fn uses_cpp_grammar(source: &str, file_path: &str) -> bool {
    if CPP_EXTENSIONS.iter().any(|ext| file_path.ends_with(ext)) {
        return true;
    }
    file_path.ends_with(".h")
        && CPP_HEADER_INDICATORS
            .iter()
            .any(|indicator| source.contains(indicator))
}

/// Crate versions pinned in Cargo.toml; recorded in parser_environment
/// (ALLOWED_DIFF) and in the producer manifest. Kept as constants because
/// dependency versions are not observable at compile time without a build
/// script.
pub const TREE_SITTER_CRATE_VERSION: &str = "0.25";
pub const GRAMMAR_C_CRATE_VERSION: &str = "0.24.2";
pub const GRAMMAR_CPP_CRATE_VERSION: &str = "0.23.4";

/// files.parser_* identity. Backend names replicate the Python oracle;
/// environment records this producer's own backend versions (the column is
/// ALLOWED_DIFF under classification v2).
pub fn cache_identity(source: &str, file_path: &str) -> CacheIdentity {
    let use_cpp = uses_cpp_grammar(source, file_path);
    let (backend, grammar_name, grammar_version) = if use_cpp {
        (
            "cpp-tree-sitter",
            "tree-sitter-cpp",
            GRAMMAR_CPP_CRATE_VERSION,
        )
    } else {
        ("c-tree-sitter", "tree-sitter-c", GRAMMAR_C_CRATE_VERSION)
    };
    let environment = pyjson::dumps_identity(&json!({
        "tree-sitter": TREE_SITTER_CRATE_VERSION,
        grammar_name: grammar_version,
    }));
    CacheIdentity {
        contract_version: CACHE_CONTRACT_VERSION.to_string(),
        backend: backend.to_string(),
        environment,
    }
}

/// CCppParser.symbol_hash_input: strip `//` line comments, then `/* */`
/// block comments (frozen order, string literals included — oracle quirk).
pub fn symbol_hash_input(source_segment: &str) -> String {
    static LINE_COMMENT: OnceLock<Regex> = OnceLock::new();
    static BLOCK_COMMENT: OnceLock<Regex> = OnceLock::new();
    let line = LINE_COMMENT.get_or_init(|| Regex::new(r"//[^\n]*").unwrap());
    let block = BLOCK_COMMENT.get_or_init(|| Regex::new(r"/\*[\s\S]*?\*/").unwrap());
    let result = line.replace_all(source_segment, "");
    block.replace_all(&result, "").into_owned()
}

/// CCppParser.resolve_imports: local `#include "..."` resolution against
/// the including file's directory, then the project root. Insertion order
/// preserved, first hit wins per resolved path.
pub fn resolve_imports(source: &str, file_path: &Path, root_dir: &Path) -> Vec<String> {
    static INCLUDE_LOCAL: OnceLock<Regex> = OnceLock::new();
    let include_re =
        INCLUDE_LOCAL.get_or_init(|| Regex::new(r#"(?m)^\s*#\s*include\s+"([^"]+)""#).unwrap());

    let current_dir = file_path.parent().unwrap_or(Path::new(""));
    let mut seen = std::collections::HashSet::new();
    let mut imports = Vec::new();
    for capture in include_re.captures_iter(source) {
        let include_path = &capture[1];

        let candidate = normpath(&current_dir.join(include_path));
        if candidate.exists() {
            let rel = relpath_slash(&candidate, root_dir);
            if seen.insert(rel.clone()) {
                imports.push(rel);
            }
            continue;
        }

        let candidate = normpath(&root_dir.join(include_path));
        if candidate.exists() {
            let rel = relpath_slash(&candidate, root_dir);
            if seen.insert(rel.clone()) {
                imports.push(rel);
            }
        }
    }
    imports
}

/// os.path.normpath: purely lexical `.`/`..`/separator normalization.
pub fn normpath(path: &Path) -> std::path::PathBuf {
    use std::path::Component;
    let mut parts: Vec<std::ffi::OsString> = Vec::new();
    let mut prefix = std::path::PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(p) => prefix.push(p.as_os_str()),
            Component::RootDir => prefix.push(std::path::MAIN_SEPARATOR_STR),
            Component::CurDir => {}
            Component::ParentDir => {
                if parts.last().map(|p| p != "..").unwrap_or(false) {
                    parts.pop();
                } else if prefix.as_os_str().is_empty() {
                    parts.push("..".into());
                }
            }
            Component::Normal(p) => parts.push(p.to_os_string()),
        }
    }
    let mut out = prefix;
    for part in parts {
        out.push(part);
    }
    if out.as_os_str().is_empty() {
        out.push(".");
    }
    out
}

/// os.path.relpath(candidate, root).replace(os.sep, '/').
pub fn relpath_slash(path: &Path, base: &Path) -> String {
    let path_parts: Vec<_> = normpath(path)
        .components()
        .map(|c| c.as_os_str().to_os_string())
        .collect();
    let base_parts: Vec<_> = normpath(base)
        .components()
        .map(|c| c.as_os_str().to_os_string())
        .collect();
    let common = path_parts
        .iter()
        .zip(base_parts.iter())
        .take_while(|(a, b)| {
            if cfg!(windows) {
                a.to_string_lossy().to_lowercase() == b.to_string_lossy().to_lowercase()
            } else {
                a == b
            }
        })
        .count();
    let mut segments: Vec<String> = Vec::new();
    for _ in common..base_parts.len() {
        segments.push("..".to_string());
    }
    for part in &path_parts[common..] {
        segments.push(part.to_string_lossy().into_owned());
    }
    if segments.is_empty() {
        ".".to_string()
    } else {
        segments.join("/")
    }
}

fn node_text<'a>(source: &'a str, node: Node) -> &'a str {
    source.get(node.byte_range()).unwrap_or("")
}

fn make_parser(use_cpp: bool) -> Parser {
    let language: Language = if use_cpp {
        tree_sitter_cpp::LANGUAGE.into()
    } else {
        tree_sitter_c::LANGUAGE.into()
    };
    let mut parser = Parser::new();
    parser
        .set_language(&language)
        .expect("bundled grammar must be ABI-compatible with the linked tree-sitter core");
    parser
}

/// CCppParser.parse_symbols (tree-sitter path).
pub fn parse_symbols(source: &str, file_path: &str) -> Vec<SymbolInfo> {
    let use_cpp = uses_cpp_grammar(source, file_path);
    let mut parser = make_parser(use_cpp);
    let Some(tree) = parser.parse(source.as_bytes(), None) else {
        return Vec::new();
    };
    let mut symbols = Vec::new();
    walk_node(tree.root_node(), source, &mut symbols, None);
    symbols.sort_by_key(|s| s.lineno);
    symbols
}

const TS_PREPROC_CONTAINERS: &[&str] = &[
    "preproc_ifdef",
    "preproc_if",
    "preproc_else",
    "preproc_elif",
];

fn walk_node(node: Node, source: &str, symbols: &mut Vec<SymbolInfo>, parent_name: Option<&str>) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        match child.kind() {
            kind if TS_PREPROC_CONTAINERS.contains(&kind) => {
                walk_node(child, source, symbols, parent_name);
            }
            "function_definition" => extract_function(child, source, symbols, parent_name),
            "struct_specifier" | "class_specifier" => {
                extract_class_or_struct(child, source, symbols, parent_name);
            }
            "enum_specifier" => {
                if let Some(name_node) = child.child_by_field_name("name") {
                    let name = node_text(source, name_node);
                    let full_name = qualified(parent_name, name);
                    symbols.push(SymbolInfo {
                        name: full_name,
                        args: String::new(),
                        sym_type: "enum".to_string(),
                        lineno: child.start_position().row as i64 + 1,
                        source_segment: node_text(source, child).to_string(),
                        end_lineno: Some(child.end_position().row as i64 + 1),
                        docstring: extract_doxygen(source, child),
                        bases: None,
                    });
                }
            }
            "type_definition" => extract_typedef(child, source, symbols, parent_name),
            "namespace_definition" => {
                let ns_name = child
                    .child_by_field_name("name")
                    .map(|n| node_text(source, n).to_string());
                if let Some(ns_name) = ns_name.filter(|n| !n.is_empty()) {
                    let full_ns = qualified(parent_name, &ns_name);
                    symbols.push(SymbolInfo {
                        name: full_ns.clone(),
                        args: String::new(),
                        sym_type: "namespace".to_string(),
                        lineno: child.start_position().row as i64 + 1,
                        source_segment: node_text(source, child).to_string(),
                        end_lineno: Some(child.end_position().row as i64 + 1),
                        docstring: extract_doxygen(source, child),
                        bases: None,
                    });
                    if let Some(body) = child.child_by_field_name("body") {
                        walk_node(body, source, symbols, Some(&full_ns));
                    }
                }
            }
            "template_declaration" => {
                let mut template_cursor = child.walk();
                for tc in child.children(&mut template_cursor) {
                    match tc.kind() {
                        "class_specifier" | "struct_specifier" => {
                            extract_class_or_struct(tc, source, symbols, parent_name);
                        }
                        "function_definition" => {
                            extract_function(tc, source, symbols, parent_name);
                        }
                        _ => {}
                    }
                }
            }
            "preproc_function_def" => {
                if let Some(name_node) = child.child_by_field_name("name") {
                    let macro_name = node_text(source, name_node).to_string();
                    let params = child
                        .child_by_field_name("parameters")
                        .map(|p| node_text(source, p).to_string())
                        .unwrap_or_else(|| "()".to_string());
                    symbols.push(SymbolInfo {
                        name: macro_name,
                        args: params,
                        sym_type: "macro".to_string(),
                        lineno: child.start_position().row as i64 + 1,
                        source_segment: node_text(source, child).to_string(),
                        end_lineno: Some(child.end_position().row as i64 + 1),
                        docstring: None,
                        bases: None,
                    });
                }
            }
            _ => {}
        }
    }
}

fn qualified(parent_name: Option<&str>, name: &str) -> String {
    match parent_name {
        Some(parent) => format!("{parent}.{name}"),
        None => name.to_string(),
    }
}

fn extract_typedef(
    node: Node,
    source: &str,
    symbols: &mut Vec<SymbolInfo>,
    parent_name: Option<&str>,
) {
    let name = node
        .child_by_field_name("declarator")
        .and_then(|decl| declarator_name(source, decl))
        .filter(|name| !name.is_empty());
    let Some(name) = name else { return };
    symbols.push(SymbolInfo {
        name: qualified(parent_name, &name),
        args: String::new(),
        sym_type: "typedef".to_string(),
        lineno: node.start_position().row as i64 + 1,
        source_segment: node_text(source, node).to_string(),
        end_lineno: Some(node.end_position().row as i64 + 1),
        docstring: extract_doxygen(source, node),
        bases: None,
    });
}

fn extract_function(
    node: Node,
    source: &str,
    symbols: &mut Vec<SymbolInfo>,
    parent_name: Option<&str>,
) {
    let Some(func_name) = func_name(source, node).filter(|name| !name.is_empty()) else {
        return;
    };
    symbols.push(SymbolInfo {
        name: qualified(parent_name, &func_name),
        args: func_params(source, node),
        sym_type: "function".to_string(),
        lineno: node.start_position().row as i64 + 1,
        source_segment: node_text(source, node).to_string(),
        end_lineno: Some(node.end_position().row as i64 + 1),
        docstring: extract_doxygen(source, node),
        bases: None,
    });
}

fn extract_class_or_struct(
    node: Node,
    source: &str,
    symbols: &mut Vec<SymbolInfo>,
    parent_name: Option<&str>,
) {
    let Some(name_node) = node.child_by_field_name("name") else {
        return;
    };
    let name = node_text(source, name_node);
    let full_name = qualified(parent_name, name);
    let sym_type = if node.kind() == "class_specifier" {
        "class"
    } else {
        "struct"
    };

    let mut bases_list = Vec::new();
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "base_class_clause" {
            let mut base_cursor = child.walk();
            for sub in child.children(&mut base_cursor) {
                match sub.kind() {
                    "type_identifier" => bases_list.push(node_text(source, sub).to_string()),
                    "template_type" => {
                        if let Some(tn) = sub.child_by_field_name("name") {
                            bases_list.push(node_text(source, tn).to_string());
                        }
                    }
                    _ => {}
                }
            }
        }
    }

    symbols.push(SymbolInfo {
        name: full_name.clone(),
        args: String::new(),
        sym_type: sym_type.to_string(),
        lineno: node.start_position().row as i64 + 1,
        source_segment: node_text(source, node).to_string(),
        end_lineno: Some(node.end_position().row as i64 + 1),
        docstring: extract_doxygen(source, node),
        bases: if bases_list.is_empty() {
            None
        } else {
            Some(bases_list)
        },
    });

    if let Some(body) = node.child_by_field_name("body") {
        let mut body_cursor = body.walk();
        for member in body.children(&mut body_cursor) {
            if member.kind() == "function_definition" {
                extract_function(member, source, symbols, Some(&full_name));
            }
        }
    }
}

fn func_name(source: &str, node: Node) -> Option<String> {
    let decl = node.child_by_field_name("declarator")?;
    match decl.kind() {
        "function_declarator" => decl
            .child_by_field_name("declarator")
            .map(|n| node_text(source, n).to_string()),
        "pointer_declarator" => {
            let inner = decl.child_by_field_name("declarator")?;
            if inner.kind() == "function_declarator" {
                inner
                    .child_by_field_name("declarator")
                    .map(|n| node_text(source, n).to_string())
            } else {
                None
            }
        }
        _ => None,
    }
}

fn func_params(source: &str, node: Node) -> String {
    let Some(mut decl) = node.child_by_field_name("declarator") else {
        return "()".to_string();
    };
    if decl.kind() == "pointer_declarator" {
        match decl.child_by_field_name("declarator") {
            Some(inner) => decl = inner,
            None => return "()".to_string(),
        }
    }
    if decl.kind() == "function_declarator" {
        if let Some(params) = decl.child_by_field_name("parameters") {
            return node_text(source, params).to_string();
        }
    }
    "()".to_string()
}

fn declarator_name(source: &str, node: Node) -> Option<String> {
    match node.kind() {
        "identifier" | "type_identifier" | "field_identifier" => {
            Some(node_text(source, node).to_string())
        }
        _ => {
            // _ts_declarator_name: a declarator field is followed
            // unconditionally (even when the chain yields nothing); the
            // named-children fallback only runs without one.
            if let Some(declarator) = node.child_by_field_name("declarator") {
                return declarator_name(source, declarator);
            }
            let mut cursor = node.walk();
            for child in node.named_children(&mut cursor) {
                if let Some(name) = declarator_name(source, child) {
                    if !name.is_empty() {
                        return Some(name);
                    }
                }
            }
            None
        }
    }
}

/// _ts_extract_doxygen: `/** */` block or a run of `///` line comments
/// immediately preceding the node.
fn extract_doxygen(source: &str, node: Node) -> Option<String> {
    let prev = node.prev_named_sibling()?;
    if prev.kind() != "comment" {
        return None;
    }
    let text = node_text(source, prev);
    if let Some(stripped) = text.strip_prefix("/**") {
        let raw = stripped.trim_end_matches(['*', '/']).trim();
        let lines: Vec<String> = raw
            .lines()
            .map(|l| l.trim().trim_start_matches(['*', ' ']).trim().to_string())
            .filter(|l| !l.is_empty())
            .collect();
        if !lines.is_empty() {
            return Some(lines[..lines.len().min(3)].join(" "));
        }
    } else if let Some(stripped) = text.strip_prefix("///") {
        let mut doc_lines = vec![stripped.trim().to_string()];
        let mut cursor = prev.prev_named_sibling();
        while let Some(prior) = cursor {
            if prior.kind() != "comment" {
                break;
            }
            let prior_text = node_text(source, prior);
            let Some(prior_stripped) = prior_text.strip_prefix("///") else {
                break;
            };
            doc_lines.insert(0, prior_stripped.trim().to_string());
            cursor = prior.prev_named_sibling();
        }
        return Some(doc_lines[..doc_lines.len().min(3)].join(" "));
    }
    None
}

/// CCppParser.extract_call_graph: caller/callee pairs from call
/// expressions inside function definitions, callee reduced to its last
/// `.`/`::` segment.
pub fn extract_call_graph(source: &str, file_path: &str) -> Vec<EdgeInfo> {
    let use_cpp = uses_cpp_grammar(source, file_path);
    let mut parser = make_parser(use_cpp);
    let Some(tree) = parser.parse(source.as_bytes(), None) else {
        return Vec::new();
    };
    let mut edges = Vec::new();
    let mut function_stack: Vec<String> = Vec::new();
    walk_calls(tree.root_node(), source, &mut edges, &mut function_stack);
    edges
}

fn walk_calls(
    node: Node,
    source: &str,
    edges: &mut Vec<EdgeInfo>,
    function_stack: &mut Vec<String>,
) {
    let mut pushed = false;
    if node.kind() == "function_definition" {
        if let Some(name) = func_name(source, node).filter(|name| !name.is_empty()) {
            function_stack.push(name);
            pushed = true;
        }
    }

    if node.kind() == "call_expression" && !function_stack.is_empty() {
        if let Some(func_node) = node.child_by_field_name("function") {
            let raw = node_text(source, func_node);
            let mut callee = raw.split('(').next().unwrap_or("").trim().to_string();
            if callee.contains('.') {
                callee = callee.rsplit('.').next().unwrap_or("").to_string();
            } else if callee.contains("::") {
                callee = callee.rsplit("::").next().unwrap_or("").to_string();
            }
            if !callee.is_empty() {
                edges.push(EdgeInfo {
                    caller: function_stack.last().unwrap().clone(),
                    callee,
                    line: node.start_position().row as i64 + 1,
                });
            }
        }
    }

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        walk_calls(child, source, edges, function_stack);
    }

    if pushed {
        function_stack.pop();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cpp_grammar_heuristic_matches_oracle() {
        assert!(uses_cpp_grammar("", "x.cpp"));
        assert!(uses_cpp_grammar("class Foo {};", "x.h"));
        assert!(uses_cpp_grammar("a::b", "x.h"));
        assert!(!uses_cpp_grammar("int f(void);", "x.h"));
        assert!(!uses_cpp_grammar("class Foo {};", "x.c"));
    }

    #[test]
    fn hash_input_strips_comments_in_frozen_order() {
        let segment = "int f(int a) { /* c */ return a; } // t";
        assert_eq!(symbol_hash_input(segment), "int f(int a) {  return a; } ");
    }

    #[test]
    fn parses_c_function_struct_enum_typedef_macro() {
        let source = "\
/** Adds two ints. */
int add(int a, int b) { return a + b; }

struct point { int x; int y; };

enum color { RED, GREEN };

typedef unsigned long ulong_t;

#define MAX(a, b) ((a) > (b) ? (a) : (b))
";
        let symbols = parse_symbols(source, "sample.c");
        let names: Vec<(&str, &str)> = symbols
            .iter()
            .map(|s| (s.name.as_str(), s.sym_type.as_str()))
            .collect();
        assert_eq!(
            names,
            vec![
                ("add", "function"),
                ("point", "struct"),
                ("color", "enum"),
                ("ulong_t", "typedef"),
                ("MAX", "macro"),
            ]
        );
        assert_eq!(symbols[0].args, "(int a, int b)");
        assert_eq!(symbols[0].docstring.as_deref(), Some("Adds two ints."));
        assert_eq!(symbols[4].args, "(a, b)");
    }

    #[test]
    fn parses_cpp_namespace_class_and_members() {
        let source = "\
namespace ns {
class Widget : public Base {
public:
    int area() { return w * h; }
private:
    int w, h;
};
}
";
        let symbols = parse_symbols(source, "widget.hpp");
        let names: Vec<(&str, &str)> = symbols
            .iter()
            .map(|s| (s.name.as_str(), s.sym_type.as_str()))
            .collect();
        assert_eq!(
            names,
            vec![
                ("ns", "namespace"),
                ("ns.Widget", "class"),
                ("ns.Widget.area", "function"),
            ]
        );
        assert_eq!(symbols[1].bases, Some(vec!["Base".to_string()]));
    }

    #[test]
    fn walks_preproc_containers() {
        let source = "\
#ifdef FEATURE
int guarded(void) { return 1; }
#else
int guarded(void) { return 0; }
#endif
";
        let symbols = parse_symbols(source, "guard.c");
        assert_eq!(symbols.len(), 2);
        assert!(symbols.iter().all(|s| s.name == "guarded"));
    }

    #[test]
    fn call_graph_reduces_callee_to_last_segment() {
        let source = "\
void helper(void) {}
void run(struct ops *o) {
    helper();
    o->table.fire();
}
";
        let edges = extract_call_graph(source, "calls.c");
        let pairs: Vec<(&str, &str, i64)> = edges
            .iter()
            .map(|e| (e.caller.as_str(), e.callee.as_str(), e.line))
            .collect();
        assert!(pairs.contains(&("run", "helper", 3)));
        assert!(pairs.contains(&("run", "fire", 4)));
    }

    #[test]
    fn macro_style_typedef_without_identifier_leaf_is_skipped() {
        // drivers/gpu regression: `typedef DECLARE_BITMAP(name, N);` must
        // not produce an empty-name symbol (Python's falsy-name guard).
        let source = "typedef DECLARE_BITMAP(mdp5_smp_state_t, MAX_SMP_BLOCKS);\n";
        let symbols = parse_symbols(source, "bitmap.h");
        assert!(symbols.iter().all(|s| !s.name.is_empty()));
    }

    #[test]
    fn triple_slash_doxygen_runs_are_collected() {
        let source = "\
/// First line.
/// Second line.
/// Third line.
/// Fourth line.
int f(void) { return 0; }
";
        let symbols = parse_symbols(source, "doc.c");
        assert_eq!(
            symbols[0].docstring.as_deref(),
            Some("First line. Second line. Third line.")
        );
    }
}
