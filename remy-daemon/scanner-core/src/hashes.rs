//! Hash and name-token contracts replicated from the Python oracle.

use md5::{Digest, Md5};
use regex::Regex;
use std::sync::OnceLock;

/// `StructScanner._calculate_symbol_hash`: MD5 over the segment with all
/// Python `str.split()` whitespace removed.
pub fn symbol_hash(hash_input: &str) -> String {
    let normalized: String = hash_input
        .chars()
        .filter(|c| !is_python_whitespace(*c))
        .collect();
    md5_hex(normalized.as_bytes())
}

/// `StructScanner._compute_struct_hash`: MD5 over the full decoded source.
pub fn struct_hash(source: &str) -> String {
    md5_hex(source.as_bytes())
}

fn md5_hex(data: &[u8]) -> String {
    let mut hasher = Md5::new();
    hasher.update(data);
    let digest = hasher.finalize();
    let mut out = String::with_capacity(32);
    for byte in digest {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// Python `str.split()` whitespace: Unicode White_Space plus the four
/// information-separator control characters U+001C..U+001F, which Python's
/// `str.isspace` accepts but Rust's `char::is_whitespace` does not.
fn is_python_whitespace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python text-mode file decoding: strict UTF-8 plus universal-newline
/// translation. The BOM, if present, stays in the string (the oracle opens
/// files with encoding='utf-8', not 'utf-8-sig').
pub fn decode_source(bytes: &[u8]) -> Result<String, std::str::Utf8Error> {
    let text = std::str::from_utf8(bytes)?;
    Ok(translate_newlines(text))
}

fn translate_newlines(text: &str) -> String {
    if !text.contains('\r') {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\r' {
            if chars.peek() == Some(&'\n') {
                chars.next();
            }
            out.push('\n');
        } else {
            out.push(c);
        }
    }
    out
}

/// `symbol_names.tokenize_symbol`: split snake_case, camelCase, and
/// namespace separators into space-separated tokens.
pub fn tokenize_symbol(name: &str) -> String {
    static CAMEL_LOWER_UPPER: OnceLock<Regex> = OnceLock::new();
    static CAMEL_ACRONYM: OnceLock<Regex> = OnceLock::new();
    static WHITESPACE_RUN: OnceLock<Regex> = OnceLock::new();
    let lower_upper = CAMEL_LOWER_UPPER.get_or_init(|| Regex::new(r"([a-z])([A-Z])").unwrap());
    let acronym = CAMEL_ACRONYM.get_or_init(|| Regex::new(r"([A-Z]+)([A-Z][a-z])").unwrap());
    let ws = WHITESPACE_RUN.get_or_init(|| Regex::new(r"\s+").unwrap());

    let value = name.replace('_', " ").replace("::", " ");
    let value = lower_upper.replace_all(&value, "$1 $2");
    let value = acronym.replace_all(&value, "$1 $2");
    ws.replace_all(&value, " ").trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn symbol_hash_matches_python_probe_vector() {
        // Probe H1 (2026-08-16, Python 3.12.9): comment stripping happens
        // upstream in symbol_hash_input; this vector feeds the stripped
        // segment through whitespace removal + MD5.
        let segment = "int f(int a) { /* c */ return a; } // t";
        let stripped = crate::parse_c_cpp::symbol_hash_input(segment);
        assert_eq!(symbol_hash(&stripped), "ef7f1a56a2b93178b8e5b8db47d6a78e");
    }

    #[test]
    fn struct_hash_matches_python_probe_vector() {
        // Probe H2.
        assert_eq!(struct_hash("int x;\n"), "06c25fe0c80b8959051a62f8f034710a");
    }

    #[test]
    fn python_whitespace_covers_information_separators() {
        assert_eq!(symbol_hash("a\u{1c}b"), symbol_hash("ab"));
        assert_eq!(symbol_hash("a\u{85}b"), symbol_hash("ab"));
    }

    #[test]
    fn comment_only_and_whitespace_only_edits_keep_symbol_hash() {
        let base = "int f(int a) {\n    return a;\n}";
        let with_comments = "int f(int a) { // doc\n    /* body */ return a;\n}";
        let reindented = "int  f(int a)\n{\n\treturn a;\n}";
        let hash = |segment: &str| symbol_hash(&crate::parse_c_cpp::symbol_hash_input(segment));
        assert_eq!(hash(base), hash(with_comments));
        assert_eq!(hash(base), hash(reindented));
        let behavior_change = "int f(int a) {\n    return a + 1;\n}";
        assert_ne!(hash(base), hash(behavior_change));
    }

    #[test]
    fn newline_translation_is_universal() {
        assert_eq!(translate_newlines("a\r\nb\rc\n"), "a\nb\nc\n");
    }

    #[test]
    fn tokenize_matches_python_semantics() {
        assert_eq!(tokenize_symbol("snake_case_name"), "snake case name");
        assert_eq!(tokenize_symbol("Ns::Klass"), "Ns Klass");
        assert_eq!(tokenize_symbol("camelCaseName"), "camel Case Name");
        assert_eq!(tokenize_symbol("HTTPServer"), "HTTP Server");
        assert_eq!(tokenize_symbol("Outer.innerFunc"), "Outer.inner Func");
    }
}
