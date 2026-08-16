//! `TSParser` replication (tree-sitter path only — the frozen oracle runs
//! with tree-sitter installed, so the regex fallback never contributes to
//! the baseline; the Rust grammars are compiled into the binary).
//!
//! Contracts replicated here:
//!
//! - **Symbol scope**: top-level declarations (with `export` unwrapping),
//!   class methods / abstract method signatures, interface method
//!   signatures, namespace members (recursive), and arrow functions bound
//!   by `const`/`let`/`var` declarators.
//! - **JSDoc**: `/** */` blocks drop `@tag` lines; `///` runs collect
//!   contiguous comment siblings. An arrow function's doc comment is looked
//!   up on the *declaration statement*, not the declarator — oracle quirk.
//! - **Hash input**: strip `//` line comments first, then `/* */` block
//!   comments (frozen order, string literals included).
//! - **Imports**: only `./`/`../` specifiers, candidate order
//!   `.ts` / `.tsx` / `index.ts` / `index.tsx`, deduplicated in source
//!   match order (import-from matches, then require matches — TS contract
//!   version 2 determinism fix).

use crate::facts::{CacheIdentity, EdgeInfo, SymbolInfo};
use crate::parse_c_cpp::{normpath, relpath_slash};
use crate::pyjson;
use regex::Regex;
use serde_json::json;
use std::path::Path;
use std::sync::OnceLock;
use tree_sitter::{Language as TsLanguage, Node, Parser};

pub const LANGUAGE_ID: &str = "TSParser";
pub const CACHE_CONTRACT_VERSION: &str = "2";
pub const EXTENSIONS: &[&str] = &[".ts", ".tsx"];

/// Crate versions pinned in Cargo.toml, recorded in `parser_environment`
/// (an ALLOWED_DIFF column under classification v2).
pub const TREE_SITTER_CRATE_VERSION: &str = "0.25";
pub const GRAMMAR_TS_CRATE_VERSION: &str = "0.23.2";

const DECLARATION_TYPES: &[&str] = &[
    "function_declaration",
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
    "namespace_declaration",
    "internal_module",
    "module",
    "lexical_declaration",
    "variable_declaration",
];

fn is_tsx(file_path: &str) -> bool {
    file_path.ends_with(".tsx")
}

pub fn cache_identity(file_path: &str) -> CacheIdentity {
    let environment = pyjson::dumps_identity(&json!({
        "tree-sitter": TREE_SITTER_CRATE_VERSION,
        "tree-sitter-typescript": GRAMMAR_TS_CRATE_VERSION,
    }));
    CacheIdentity {
        contract_version: CACHE_CONTRACT_VERSION.to_string(),
        backend: if is_tsx(file_path) {
            "tsx-tree-sitter".to_string()
        } else {
            "ts-tree-sitter".to_string()
        },
        environment,
    }
}

/// `TSParser.symbol_hash_input`: strip `//` line comments, then `/* */`
/// block comments (frozen order, string literals included).
pub fn symbol_hash_input(source_segment: &str) -> String {
    static LINE_COMMENT: OnceLock<Regex> = OnceLock::new();
    static BLOCK_COMMENT: OnceLock<Regex> = OnceLock::new();
    let line = LINE_COMMENT.get_or_init(|| Regex::new(r"//[^\n]*").unwrap());
    let block = BLOCK_COMMENT.get_or_init(|| Regex::new(r"/\*[\s\S]*?\*/").unwrap());
    let result = line.replace_all(source_segment, "");
    block.replace_all(&result, "").into_owned()
}

fn make_parser(tsx: bool) -> Parser {
    let language: TsLanguage = if tsx {
        tree_sitter_typescript::LANGUAGE_TSX.into()
    } else {
        tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into()
    };
    let mut parser = Parser::new();
    parser
        .set_language(&language)
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

/// `TSParser._ts_params_str`.
fn params_str(node: Node, source: &str) -> String {
    if let Some(params) = field_text(node, "parameters", source) {
        return params;
    }
    if let Some(param) = field_text(node, "parameter", source) {
        return format!("({param})");
    }
    "()".to_string()
}

/// `_ts_extract_jsdoc`: `/** */` block (dropping `@tag` lines) or a run of
/// `///` line comments on the immediately preceding comment sibling.
fn extract_jsdoc(node: Node, source: &str) -> Option<String> {
    let prev = node.prev_named_sibling()?;
    if prev.kind() != "comment" {
        return None;
    }
    let comment = text(prev, source);
    if let Some(stripped) = comment.strip_prefix("/**") {
        let raw = stripped.trim_end_matches(['*', '/']).trim();
        let lines: Vec<String> = raw
            .lines()
            .filter_map(|l| {
                let unstarred = l.trim().trim_start_matches(['*', ' ']);
                if unstarred.trim().is_empty() || unstarred.starts_with('@') {
                    None
                } else {
                    Some(unstarred.trim().to_string())
                }
            })
            .collect();
        if !lines.is_empty() {
            return Some(lines[..lines.len().min(3)].join(" "));
        }
    } else if let Some(stripped) = comment.strip_prefix("///") {
        let mut doc_lines = vec![stripped.trim().to_string()];
        let mut cursor = prev.prev_named_sibling();
        while let Some(prior) = cursor {
            if prior.kind() != "comment" {
                break;
            }
            let Some(prior_stripped) = text(prior, source).strip_prefix("///") else {
                break;
            };
            doc_lines.insert(0, prior_stripped.trim().to_string());
            cursor = prior.prev_named_sibling();
        }
        return Some(doc_lines[..doc_lines.len().min(3)].join(" "));
    }
    None
}

/// `TSParser.parse_symbols` (tree-sitter path).
pub fn parse_symbols(source: &str, file_path: &str) -> Vec<SymbolInfo> {
    let mut parser = make_parser(is_tsx(file_path));
    let Some(tree) = parser.parse(source.as_bytes(), None) else {
        return Vec::new();
    };
    let mut symbols = Vec::new();
    walk_declarations(tree.root_node(), source, &mut symbols, None);
    symbols.sort_by_key(|s| s.lineno);
    symbols
}

fn walk_declarations(
    node: Node,
    source: &str,
    symbols: &mut Vec<SymbolInfo>,
    prefix: Option<&str>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "export_statement" {
            let mut export_cursor = child.walk();
            for sub in child.named_children(&mut export_cursor) {
                if DECLARATION_TYPES.contains(&sub.kind()) {
                    dispatch(sub, source, symbols, prefix);
                }
            }
        } else if DECLARATION_TYPES.contains(&child.kind()) {
            dispatch(child, source, symbols, prefix);
        }
    }
}

fn dispatch(node: Node, source: &str, symbols: &mut Vec<SymbolInfo>, prefix: Option<&str>) {
    match node.kind() {
        "function_declaration" => extract_function(node, source, symbols, prefix),
        "class_declaration" | "abstract_class_declaration" => {
            extract_class(node, source, symbols, prefix);
        }
        "interface_declaration" => extract_interface(node, source, symbols, prefix),
        "type_alias_declaration" => extract_named(node, source, symbols, prefix, "type_alias"),
        "enum_declaration" => extract_named(node, source, symbols, prefix, "enum"),
        "namespace_declaration" | "internal_module" | "module" => {
            extract_namespace(node, source, symbols, prefix);
        }
        "lexical_declaration" | "variable_declaration" => {
            extract_arrow_functions(node, source, symbols, prefix);
        }
        _ => {}
    }
}

/// Symbol skeleton for a declaration node: position/segment/JSDoc from the
/// node itself, empty args and no bases. Callers override the fields that
/// differ (function params, class bases, the arrow-function doc quirk).
fn base_symbol(node: Node, source: &str, name: String, sym_type: &str) -> SymbolInfo {
    SymbolInfo {
        name,
        args: String::new(),
        sym_type: sym_type.to_string(),
        lineno: node.start_position().row as i64 + 1,
        source_segment: text(node, source).to_string(),
        end_lineno: Some(node.end_position().row as i64 + 1),
        docstring: extract_jsdoc(node, source),
        bases: None,
    }
}

fn extract_function(node: Node, source: &str, symbols: &mut Vec<SymbolInfo>, prefix: Option<&str>) {
    let name = field_text(node, "name", source).unwrap_or_else(|| "<default>".to_string());
    let mut symbol = base_symbol(node, source, qualified(prefix, &name), "function");
    symbol.args = params_str(node, source);
    symbols.push(symbol);
}

fn extract_class(node: Node, source: &str, symbols: &mut Vec<SymbolInfo>, prefix: Option<&str>) {
    let name = field_text(node, "name", source).unwrap_or_else(|| "<default>".to_string());
    let full_name = qualified(prefix, &name);

    let mut bases_list = Vec::new();
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if matches!(child.kind(), "extends_clause" | "implements_clause") {
            let mut clause_cursor = child.walk();
            for type_node in child.named_children(&mut clause_cursor) {
                bases_list.push(text(type_node, source).to_string());
            }
        }
    }

    let mut symbol = base_symbol(node, source, full_name.clone(), "class");
    symbol.bases = if bases_list.is_empty() {
        None
    } else {
        Some(bases_list)
    };
    symbols.push(symbol);

    if let Some(body) = node.child_by_field_name("body") {
        let mut body_cursor = body.walk();
        for member in body.children(&mut body_cursor) {
            if matches!(
                member.kind(),
                "method_definition" | "abstract_method_signature"
            ) {
                if let Some(method_name) = field_text(member, "name", source) {
                    let mut method = base_symbol(
                        member,
                        source,
                        format!("{full_name}.{method_name}"),
                        "function",
                    );
                    method.args = params_str(member, source);
                    symbols.push(method);
                }
            }
        }
    }
}

fn extract_interface(
    node: Node,
    source: &str,
    symbols: &mut Vec<SymbolInfo>,
    prefix: Option<&str>,
) {
    let Some(name) = field_text(node, "name", source) else {
        return;
    };
    let full_name = qualified(prefix, &name);
    symbols.push(base_symbol(node, source, full_name.clone(), "interface"));
    if let Some(body) = node.child_by_field_name("body") {
        let mut body_cursor = body.walk();
        for member in body.children(&mut body_cursor) {
            if member.kind() == "method_signature" {
                if let Some(method_name) = field_text(member, "name", source) {
                    let mut method = base_symbol(
                        member,
                        source,
                        format!("{full_name}.{method_name}"),
                        "function",
                    );
                    method.args = params_str(member, source);
                    symbols.push(method);
                }
            }
        }
    }
}

fn extract_named(
    node: Node,
    source: &str,
    symbols: &mut Vec<SymbolInfo>,
    prefix: Option<&str>,
    sym_type: &str,
) {
    let Some(name) = field_text(node, "name", source) else {
        return;
    };
    symbols.push(base_symbol(
        node,
        source,
        qualified(prefix, &name),
        sym_type,
    ));
}

fn extract_namespace(
    node: Node,
    source: &str,
    symbols: &mut Vec<SymbolInfo>,
    prefix: Option<&str>,
) {
    let Some(name) = field_text(node, "name", source) else {
        return;
    };
    let full_name = qualified(prefix, &name);
    symbols.push(base_symbol(node, source, full_name.clone(), "namespace"));
    if let Some(body) = node.child_by_field_name("body") {
        walk_declarations(body, source, symbols, Some(&full_name));
    }
}

fn extract_arrow_functions(
    node: Node,
    source: &str,
    symbols: &mut Vec<SymbolInfo>,
    prefix: Option<&str>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() != "variable_declarator" {
            continue;
        }
        let Some(value) = child.child_by_field_name("value") else {
            continue;
        };
        if value.kind() != "arrow_function" {
            continue;
        }
        let Some(name) = field_text(child, "name", source) else {
            continue;
        };
        let mut symbol = base_symbol(child, source, qualified(prefix, &name), "function");
        symbol.args = params_str(value, source);
        // Oracle quirk: the JSDoc lookup runs on the declaration statement
        // (`node`), not the declarator.
        symbol.docstring = extract_jsdoc(node, source);
        symbols.push(symbol);
    }
}

/// `TSParser.resolve_imports`: relative specifiers only, candidate chain
/// `.ts` / `.tsx` / `index.ts` / `index.tsx`, source match order.
pub fn resolve_imports(source: &str, file_path: &Path, root_dir: &Path) -> Vec<String> {
    static IMPORT_FROM: OnceLock<Regex> = OnceLock::new();
    static REQUIRE: OnceLock<Regex> = OnceLock::new();
    let import_from = IMPORT_FROM.get_or_init(|| {
        Regex::new(
            r#"import\s+(?:type\s+)?(?:\*\s+as\s+\w+|\{[^}]*\}|\w+)(?:\s*,\s*(?:\{[^}]*\}|\w+))?\s+from\s+['"]([^'"]+)['"]"#,
        )
        .unwrap()
    });
    let require =
        REQUIRE.get_or_init(|| Regex::new(r#"require\s*\(\s*['"]([^'"]+)['"]\s*\)"#).unwrap());

    let current_dir = file_path.parent().unwrap_or(Path::new(""));
    let mut raw_paths: Vec<&str> = Vec::new();
    let mut seen_raw = std::collections::HashSet::new();
    for regex in [import_from, require] {
        for capture in regex.captures_iter(source) {
            let raw = capture.get(1).map(|m| m.as_str()).unwrap_or("");
            if seen_raw.insert(raw) {
                raw_paths.push(raw);
            }
        }
    }

    let mut imports = Vec::new();
    let mut seen_resolved = std::collections::HashSet::new();
    for raw in raw_paths {
        if !(raw.starts_with("./") || raw.starts_with("../")) {
            continue;
        }
        let base = normpath(&current_dir.join(raw));
        let candidates = [
            base.with_extension_appended(".ts"),
            base.with_extension_appended(".tsx"),
            base.join("index.ts"),
            base.join("index.tsx"),
        ];
        for candidate in candidates {
            if candidate.exists() {
                let rel = relpath_slash(&candidate, root_dir);
                if seen_resolved.insert(rel.clone()) {
                    imports.push(rel);
                }
                break;
            }
        }
    }
    imports
}

trait AppendExtension {
    fn with_extension_appended(&self, suffix: &str) -> std::path::PathBuf;
}

impl AppendExtension for std::path::PathBuf {
    /// Python builds candidates with plain string concatenation
    /// (`base + '.ts'`), which appends rather than replaces any existing
    /// dot-suffix in the path.
    fn with_extension_appended(&self, suffix: &str) -> std::path::PathBuf {
        let mut raw = self.as_os_str().to_os_string();
        raw.push(suffix);
        std::path::PathBuf::from(raw)
    }
}

/// `TSParser.extract_call_graph` (tree-sitter path). Every TS edge keeps
/// the default `call_form = "name"` — the oracle never sets `attribute`.
pub fn extract_call_graph(source: &str, file_path: &str) -> Vec<EdgeInfo> {
    let mut parser = make_parser(is_tsx(file_path));
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
    match node.kind() {
        "function_declaration" | "method_definition" => {
            if let Some(name) = field_text(node, "name", source) {
                function_stack.push(name);
                pushed = true;
            }
        }
        "arrow_function" => {
            if let Some(parent) = node.parent() {
                if parent.kind() == "variable_declarator" {
                    if let Some(name) = field_text(parent, "name", source) {
                        function_stack.push(name);
                        pushed = true;
                    }
                }
            }
        }
        "call_expression" if !function_stack.is_empty() => {
            if let Some(func_node) = node.child_by_field_name("function") {
                let raw = text(func_node, source);
                let callee = match raw.rfind('.') {
                    Some(index) => &raw[index + 1..],
                    None => raw,
                };
                if !callee.is_empty() {
                    edges.push(EdgeInfo {
                        caller: function_stack.last().unwrap().clone(),
                        callee: callee.to_string(),
                        line: node.start_position().row as i64 + 1,
                        call_form: "name",
                    });
                }
            }
        }
        _ => {}
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
    fn parses_ts_declaration_families() {
        let source = "\
/** Runs the job. */
export function run(a: number): void {}

/** Local doc. */
function local(): void {}

export abstract class Base<T> extends Root implements Kind<T> {
    /** Area doc. */
    area(w: number, h: number): number { return w * h; }
    abstract describe(): string;
}

interface Shape {
    perimeter(): number;
}

export type Alias<T> = T | null;

const enum Color { Red, Green }

namespace bare {
    export function hidden(): void {}
}

export namespace geo {
    export function inner(): void {}
}

export const arrow = (x: number) => x + 1;
";
        let symbols = parse_symbols(source, "sample.ts");
        let names: Vec<(&str, &str)> = symbols
            .iter()
            .map(|s| (s.name.as_str(), s.sym_type.as_str()))
            .collect();
        // A bare `namespace bare {}` statement sits inside an
        // expression_statement in grammar 0.23.2, so the oracle never
        // extracts it — only `module` and exported namespaces surface.
        assert_eq!(
            names,
            vec![
                ("run", "function"),
                ("local", "function"),
                ("Base", "class"),
                ("Base.area", "function"),
                ("Base.describe", "function"),
                ("Shape", "interface"),
                ("Shape.perimeter", "function"),
                ("Alias", "type_alias"),
                ("Color", "enum"),
                ("geo", "namespace"),
                ("geo.inner", "function"),
                ("arrow", "function"),
            ]
        );
        // The exported function's JSDoc sits before the export_statement,
        // not the inner declaration, so the oracle returns None for it.
        assert_eq!(symbols[0].docstring, None);
        assert_eq!(symbols[0].args, "(a: number)");
        assert_eq!(symbols[1].docstring.as_deref(), Some("Local doc."));
        // extends/implements sit under an intermediate class_heritage node
        // in grammar 0.23.2, so the oracle's direct-children clause lookup
        // never fires: TS class bases are always None in the baseline.
        assert_eq!(symbols[2].bases, None);
        assert_eq!(symbols[3].docstring.as_deref(), Some("Area doc."));
        assert_eq!(symbols[11].args, "(x: number)");
    }

    #[test]
    fn tsx_grammar_handles_jsx_elements() {
        let source = "export function App() { return <div a={1} />; }\n";
        let symbols = parse_symbols(source, "app.tsx");
        assert_eq!(symbols.len(), 1);
        assert_eq!(symbols[0].name, "App");
    }

    #[test]
    fn jsdoc_drops_tag_lines_and_caps_three() {
        let source = "\
/**
 * First.
 * @param x ignored
 * Second.
 * Third.
 * Fourth.
 */
function f(x: number) {}
";
        let symbols = parse_symbols(source, "doc.ts");
        assert_eq!(
            symbols[0].docstring.as_deref(),
            Some("First. Second. Third.")
        );
    }

    #[test]
    fn triple_slash_runs_are_collected() {
        let source = "/// One.\n/// Two.\nfunction f() {}\n";
        let symbols = parse_symbols(source, "doc.ts");
        assert_eq!(symbols[0].docstring.as_deref(), Some("One. Two."));
    }

    #[test]
    fn hash_input_strips_comments_in_frozen_order() {
        let segment = "function f() { /* c */ return 1; } // t";
        assert_eq!(symbol_hash_input(segment), "function f() {  return 1; } ");
    }

    #[test]
    fn cache_identity_switches_backend_by_extension() {
        assert_eq!(cache_identity("a.ts").backend, "ts-tree-sitter");
        assert_eq!(cache_identity("a.tsx").backend, "tsx-tree-sitter");
        assert_eq!(cache_identity("a.ts").contract_version, "2");
    }

    #[test]
    fn imports_resolve_in_source_order_with_candidate_chain() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        std::fs::create_dir_all(root.join("sub/pkg")).unwrap();
        std::fs::write(root.join("sub/delta.ts"), "export const d = 1;\n").unwrap();
        std::fs::write(root.join("sub/alpha.tsx"), "export const a = 1;\n").unwrap();
        std::fs::write(root.join("sub/pkg/index.ts"), "export const p = 1;\n").unwrap();
        let source = "\
import {d} from './delta';
import {a} from './alpha';
import type {P} from './pkg';
import ext from 'external';
const d2 = require('./delta');
";
        let imports = resolve_imports(source, &root.join("sub/main.ts"), root);
        assert_eq!(
            imports,
            vec!["sub/delta.ts", "sub/alpha.tsx", "sub/pkg/index.ts"]
        );
    }

    #[test]
    fn call_graph_uses_last_dot_segment_and_name_form() {
        let source = "\
function helper() {}
function run(obj: any) {
    helper();
    obj.deep.fire();
}
const arrow = () => helper();
";
        let edges = extract_call_graph(source, "calls.ts");
        let triples: Vec<(&str, &str, &str)> = edges
            .iter()
            .map(|e| (e.caller.as_str(), e.callee.as_str(), e.call_form))
            .collect();
        assert_eq!(
            triples,
            vec![
                ("run", "helper", "name"),
                ("run", "fire", "name"),
                ("arrow", "helper", "name"),
            ]
        );
    }
}
