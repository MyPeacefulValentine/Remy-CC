//! Built-in, explicit, immutable language dispatch — the Rust counterpart
//! of the Python-side ParserRegistry contract (language_id uniqueness,
//! extension-to-parser resolution via longest-suffix matching, per-language
//! symbol hash input). Adding a language means adding an enum variant; the
//! exhaustive matches below turn any missed dispatch point into a compile
//! error.

use crate::facts::{CacheIdentity, EdgeInfo, PatternFact, SymbolInfo};
use crate::{parse_c_cpp, parse_python, parse_rust, parse_ts};
use std::path::Path;

/// Everything a language module extracts from one decoded source file
/// (parser.resolve_imports / collect_import_bindings / parse_symbols /
/// extract_call_graph / extract_patterns plus the cache identity).
#[derive(Debug, Clone)]
pub struct ParsedFile {
    pub identity: CacheIdentity,
    /// files.imports keys, insertion order preserved.
    pub imports: Vec<String>,
    /// files.import_bindings column, already json.dumps-default encoded.
    pub import_bindings_json: String,
    pub symbols: Vec<SymbolInfo>,
    pub edges: Vec<EdgeInfo>,
    pub patterns: Vec<PatternFact>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Language {
    CCpp,
    Python,
    Ts,
    Rust,
}

/// Extension table shared by resolve(); kept sorted by suffix length at
/// lookup time so a longer registered suffix always wins (ParserRegistry's
/// longest-suffix rule).
const EXTENSIONS: &[(&str, Language)] = &[
    (".c", Language::CCpp),
    (".h", Language::CCpp),
    (".cpp", Language::CCpp),
    (".hpp", Language::CCpp),
    (".cc", Language::CCpp),
    (".cxx", Language::CCpp),
    (".hh", Language::CCpp),
    (".hxx", Language::CCpp),
    (".py", Language::Python),
    (".ts", Language::Ts),
    (".tsx", Language::Ts),
    (".rs", Language::Rust),
];

impl Language {
    pub fn resolve(filename: &str) -> Option<Language> {
        let mut best: Option<(&str, Language)> = None;
        for (ext, language) in EXTENSIONS {
            if filename.ends_with(ext) {
                match best {
                    Some((prev, _)) if prev.len() >= ext.len() => {}
                    _ => best = Some((ext, *language)),
                }
            }
        }
        best.map(|(_, language)| language)
    }

    pub fn from_language_id(language_id: &str) -> Option<Language> {
        match language_id {
            parse_c_cpp::LANGUAGE_ID => Some(Language::CCpp),
            parse_python::LANGUAGE_ID => Some(Language::Python),
            parse_ts::LANGUAGE_ID => Some(Language::Ts),
            parse_rust::LANGUAGE_ID => Some(Language::Rust),
            _ => None,
        }
    }

    pub fn language_id(self) -> &'static str {
        match self {
            Language::CCpp => parse_c_cpp::LANGUAGE_ID,
            Language::Python => parse_python::LANGUAGE_ID,
            Language::Ts => parse_ts::LANGUAGE_ID,
            Language::Rust => parse_rust::LANGUAGE_ID,
        }
    }

    /// Per-language comment stripping ahead of the shared whitespace-free
    /// MD5 (LanguageParser.symbol_hash_input).
    pub fn symbol_hash_input(self, source_segment: &str) -> String {
        match self {
            Language::CCpp => parse_c_cpp::symbol_hash_input(source_segment),
            Language::Python => parse_python::symbol_hash_input(source_segment),
            Language::Ts => parse_ts::symbol_hash_input(source_segment),
            Language::Rust => parse_rust::symbol_hash_input(source_segment),
        }
    }

    /// LanguageParser.cache_identity_candidates: every identity this
    /// producer could stamp for the path without reading source content;
    /// scan_files re-parses stored rows outside this set (identity-invalid).
    pub fn identity_candidates(self, filename: &str) -> Vec<CacheIdentity> {
        match self {
            Language::CCpp => parse_c_cpp::cache_identity_candidates(filename),
            Language::Python => vec![parse_python::cache_identity()],
            Language::Ts => vec![parse_ts::cache_identity(filename)],
            Language::Rust => vec![parse_rust::cache_identity()],
        }
    }

    /// Full per-file fact extraction for one decoded source. Infallible:
    /// every language module maps oracle-side parse failures to empty facts
    /// (grammars are compiled into the binary and tree-sitter always yields
    /// a tree); unreadable/undecodable files fail upstream in parse_one.
    pub fn parse_file(
        self,
        source: &str,
        full_path: &Path,
        file_path_str: &str,
        root_dir: &Path,
    ) -> ParsedFile {
        match self {
            Language::CCpp => ParsedFile {
                identity: parse_c_cpp::cache_identity(source, file_path_str),
                imports: parse_c_cpp::resolve_imports(source, full_path, root_dir),
                import_bindings_json: "[]".to_string(),
                symbols: parse_c_cpp::parse_symbols(source, file_path_str),
                edges: parse_c_cpp::extract_call_graph(source, file_path_str),
                patterns: crate::patterns_c::extract_patterns(source),
            },
            Language::Python => {
                let facts = parse_python::parse_file(source, full_path, root_dir);
                ParsedFile {
                    identity: parse_python::cache_identity(),
                    imports: facts.imports,
                    import_bindings_json: facts.import_bindings_json,
                    symbols: facts.symbols,
                    edges: facts.edges,
                    patterns: facts.patterns,
                }
            }
            Language::Ts => ParsedFile {
                identity: parse_ts::cache_identity(file_path_str),
                imports: parse_ts::resolve_imports(source, full_path, root_dir),
                import_bindings_json: "[]".to_string(),
                symbols: parse_ts::parse_symbols(source, file_path_str),
                edges: parse_ts::extract_call_graph(source, file_path_str),
                patterns: Vec::new(),
            },
            Language::Rust => ParsedFile {
                identity: parse_rust::cache_identity(),
                imports: parse_rust::resolve_imports(source, full_path, root_dir),
                import_bindings_json: "[]".to_string(),
                symbols: parse_rust::parse_symbols(source),
                edges: parse_rust::extract_call_graph(source),
                patterns: parse_rust::extract_patterns(source),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_registered_extensions() {
        for name in [
            "a.c", "a.h", "a.cpp", "a.hpp", "a.cc", "a.cxx", "a.hh", "a.hxx",
        ] {
            assert_eq!(Language::resolve(name), Some(Language::CCpp), "{name}");
        }
        assert_eq!(Language::resolve("a.py"), Some(Language::Python));
        assert_eq!(Language::resolve("a.ts"), Some(Language::Ts));
        assert_eq!(Language::resolve("a.tsx"), Some(Language::Ts));
        assert_eq!(Language::resolve("a.rs"), Some(Language::Rust));
        assert_eq!(Language::resolve("a.txt"), None);
        assert_eq!(Language::resolve("noext"), None);
    }

    #[test]
    fn language_id_round_trips() {
        for language in [
            Language::CCpp,
            Language::Python,
            Language::Ts,
            Language::Rust,
        ] {
            assert_eq!(
                Language::from_language_id(language.language_id()),
                Some(language)
            );
        }
        assert_eq!(Language::from_language_id("UnknownParser"), None);
    }

    #[test]
    fn extension_table_has_no_duplicates() {
        let mut seen = std::collections::HashSet::new();
        for (extension, _) in EXTENSIONS {
            assert!(extension.starts_with('.'), "{extension}");
            assert!(seen.insert(*extension), "duplicate extension {extension}");
        }
    }
}
