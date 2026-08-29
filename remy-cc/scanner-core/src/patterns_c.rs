//! CCppParser.extract_patterns replication: the four function-pointer
//! dispatch fact families feeding the c_fnptr_dispatch synthesizer
//! (c_fnptr_typedef / c_struct_layout / c_fnptr_register /
//! c_fnptr_dispatch). Offsets and line numbers are computed on a
//! comment-blanked copy that stays byte-for-byte aligned with the source.

use crate::facts::PatternFact;
use crate::pyjson;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::sync::OnceLock;

const C_TYPE_KEYWORDS: &[&str] = &[
    "void", "int", "char", "short", "long", "unsigned", "signed", "float", "double", "const",
    "struct", "union", "enum", "static", "volatile", "register", "inline", "return", "if", "while",
    "for", "switch", "sizeof", "case", "do", "else", "typedef",
];

fn re_fnptr_typedef() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"\btypedef\b[^;{}]*?\(\s*(?:\w+\s+)*\*\s*(?:(?:const|volatile|restrict|__restrict|__restrict__)\s+)*(\w+)\s*\)\s*\(",
        )
        .unwrap()
    })
}

fn re_fntype_typedef() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\btypedef\b([^;{}]*);").unwrap())
}

fn re_struct_def() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\bstruct\s+(\w+)\s*\{").unwrap())
}

fn re_table_init() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?m)(?:^|[;{}])\s*(?:(?:static|const|extern|register|volatile)\s+)*(?:struct\s+)?(\w+)\s+(\w+)\s*(\[[^\]]*\])?\s*=\s*\{",
        )
        .unwrap()
    })
}

fn re_dispatch() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"((?:\w+(?:\s*\[[^\]\[]*\])?\s*(?:->|\.)\s*)+)(\w+)\s*\)?\s*\(").unwrap()
    })
}

fn re_fnptr_field() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\(\s*(?:\w+\s+)*\*\s*(\w+)\s*\)\s*\(").unwrap())
}

fn re_designated() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    // Python _DESIGNATED_RE minus its `(?=\s*(?:,|$))` lookahead, which the
    // regex crate does not support; designated_tail_ok replicates it.
    RE.get_or_init(|| Regex::new(r"\.\s*([^\W\d]\w*)\s*=\s*&?\s*([^\W\d]\w*)").unwrap())
}

fn re_ident_only() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^&?\s*([^\W\d]\w*)\s*$").unwrap())
}

fn re_func() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(
            r"(?m)^[ \t]*(?:(?:static|inline|extern|const|volatile|unsigned|signed|long|short|register|__attribute__\s*\([^)]*\))\s+)*(?:(?:struct|enum|union)\s+)?([\w][\w\s\*&:<>]*?)\s+(\*?\s*\w[\w:]*)\s*\(([^)]*)\)\s*(?:const\s*)?(?:override\s*)?(?:noexcept(?:\s*\([^)]*\))?\s*)?\{",
        )
        .unwrap()
    })
}

fn re_ws_line() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?m)^[ \t]*#[^\n]*").unwrap())
}

fn re_block_comment() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"(?s)/\*.*?\*/").unwrap())
}

fn re_line_comment() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"//[^\n]*").unwrap())
}

/// _blank_comments: blank comments while preserving byte offsets and
/// newlines.
fn blank_comments(source: &str) -> String {
    let blanked = re_block_comment().replace_all(source, |caps: &regex::Captures| {
        caps[0]
            .chars()
            .map(|c| if c == '\n' { '\n' } else { ' ' })
            .collect::<String>()
    });
    re_line_comment()
        .replace_all(&blanked, |caps: &regex::Captures| {
            " ".repeat(caps[0].chars().count())
        })
        .into_owned()
}

/// _strip_preproc_lines.
fn strip_preproc_lines(body: &str) -> String {
    re_ws_line()
        .replace_all(body, |caps: &regex::Captures| {
            " ".repeat(caps[0].chars().count())
        })
        .into_owned()
}

/// _find_matching_brace on a comment-blanked buffer (string/char state kept
/// for parity with the Python helper).
fn find_matching_brace(source: &[u8], start_pos: usize) -> Option<usize> {
    let mut depth = 0i64;
    let mut in_string = false;
    let mut in_char = false;
    let mut in_line_comment = false;
    let mut in_block_comment = false;
    let mut escape_next = false;
    let mut i = start_pos;

    while i < source.len() {
        let ch = source[i];
        if escape_next {
            escape_next = false;
            i += 1;
            continue;
        }
        if in_line_comment {
            if ch == b'\n' {
                in_line_comment = false;
            }
            i += 1;
            continue;
        }
        if in_block_comment {
            if ch == b'*' && source.get(i + 1) == Some(&b'/') {
                in_block_comment = false;
                i += 2;
                continue;
            }
            i += 1;
            continue;
        }
        if ch == b'\\' && (in_string || in_char) {
            escape_next = true;
            i += 1;
            continue;
        }
        if ch == b'"' && !in_char {
            in_string = !in_string;
            i += 1;
            continue;
        }
        if ch == b'\'' && !in_string {
            in_char = !in_char;
            i += 1;
            continue;
        }
        if in_string || in_char {
            i += 1;
            continue;
        }
        if ch == b'/' {
            match source.get(i + 1) {
                Some(b'/') => {
                    in_line_comment = true;
                    i += 2;
                    continue;
                }
                Some(b'*') => {
                    in_block_comment = true;
                    i += 2;
                    continue;
                }
                _ => {}
            }
        }
        if ch == b'{' {
            depth += 1;
        } else if ch == b'}' {
            depth -= 1;
            if depth == 0 {
                return Some(i);
            }
        }
        i += 1;
    }
    None
}

fn line_number_at(source: &str, pos: usize) -> i64 {
    source.as_bytes()[..pos]
        .iter()
        .filter(|b| **b == b'\n')
        .count() as i64
        + 1
}

/// _split_top_level: split on `sep` at brace/paren/bracket depth 0.
fn split_top_level(body: &str, sep: char) -> Vec<&str> {
    let mut out = Vec::new();
    let mut depth = 0i64;
    let mut start = 0;
    for (i, c) in body.char_indices() {
        match c {
            '{' | '(' | '[' => depth += 1,
            '}' | ')' | ']' => depth -= 1,
            c if c == sep && depth == 0 => {
                out.push(&body[start..i]);
                start = i + c.len_utf8();
            }
            _ => {}
        }
    }
    out.push(&body[start..]);
    out
}

#[derive(Debug, Clone, PartialEq)]
struct StructField {
    name: String,
    index: i64,
    is_fnptr: bool,
    field_type: String,
}

/// _parse_struct_fields.
fn parse_struct_fields(inner: &str) -> Vec<StructField> {
    static FIRST_DECL: OnceLock<Regex> = OnceLock::new();
    static PLAIN_NAME: OnceLock<Regex> = OnceLock::new();
    let first_decl = FIRST_DECL.get_or_init(|| Regex::new(r"(\w+)\s+\**\s*(\w+)\s*$").unwrap());
    let plain_name = PLAIN_NAME.get_or_init(|| Regex::new(r"^\**\s*(\w+)").unwrap());

    let mut fields = Vec::new();
    let mut idx = 0i64;
    for raw in split_top_level(inner, ';') {
        let decl = raw.trim();
        if decl.is_empty() {
            continue;
        }
        let parts = split_top_level(decl, ',');
        let first = first_decl.captures(parts[0]);
        let shared_type = first.as_ref().map(|c| c[1].to_string()).unwrap_or_default();
        for (pi, part) in parts.iter().enumerate() {
            let p = part.trim();
            let mut name: Option<String> = None;
            let mut type_tok = String::new();
            let mut is_fnptr = false;
            if let Some(ptr) = re_fnptr_field().captures(p) {
                name = Some(ptr[1].to_string());
                is_fnptr = true;
            } else if pi == 0 {
                if let Some(first) = &first {
                    name = Some(first[2].to_string());
                    type_tok = shared_type.clone();
                }
            } else if let Some(dm) = plain_name.captures(p) {
                name = Some(dm[1].to_string());
                type_tok = shared_type.clone();
            }
            let has_name = name.is_some();
            fields.push(StructField {
                name: name.unwrap_or_default(),
                index: idx,
                is_fnptr: has_name && is_fnptr,
                field_type: type_tok,
            });
            idx += 1;
        }
    }
    fields
}

/// _function_ranges: function name -> (start, end) byte offsets.
fn function_ranges(scan: &str) -> Vec<(String, (usize, usize))> {
    let mut ranges: Vec<(String, (usize, usize))> = Vec::new();
    let bytes = scan.as_bytes();
    for m in re_func().captures_iter(scan) {
        let whole = m.get(0).unwrap();
        let Some(brace) = scan[whole.start()..].find('{').map(|p| p + whole.start()) else {
            continue;
        };
        let Some(end) = find_matching_brace(bytes, brace) else {
            continue;
        };
        let name = m[2].trim().trim_start_matches('*').trim().to_string();
        if !name.is_empty() && !ranges.iter().any(|(n, _)| *n == name) {
            ranges.push((name, (whole.start(), end)));
        }
    }
    ranges
}

/// _enclosing_function: innermost function whose range contains pos.
fn enclosing_function(func_ranges: &[(String, (usize, usize))], pos: usize) -> Option<&str> {
    let mut best: Option<&str> = None;
    let mut best_span = usize::MAX;
    for (name, (s, e)) in func_ranges {
        if *s <= pos && pos <= *e {
            let span = e - s;
            if best.is_none() || span < best_span {
                best = Some(name);
                best_span = span;
            }
        }
    }
    best
}

/// _local_var_type: declared struct type of a local/param inside a body.
fn local_var_type(body: &str, var: &str) -> Option<String> {
    let pattern = format!(
        r"(?:struct\s+)?(\w+)\s*\*?\s*\b{}\b\s*(?:[,)=;]|\[)",
        regex::escape(var)
    );
    let re = Regex::new(&pattern).ok()?;
    let captures = re.captures(body)?;
    let name = &captures[1];
    if C_TYPE_KEYWORDS.contains(&name) {
        None
    } else {
        Some(name.to_string())
    }
}

/// _DESIGNATED_RE's `(?=\s*(?:,|$))` lookahead.
fn designated_tail_ok(rest: &str) -> bool {
    let trimmed = rest.trim_start_matches(crate::selection::is_python_re_space);
    trimmed.is_empty() || trimmed.starts_with(',')
}

fn ident_only(text: &str) -> Option<String> {
    re_ident_only().captures(text).map(|c| c[1].to_string())
}

fn fields_metadata(fields: &[StructField]) -> Value {
    Value::Array(
        fields
            .iter()
            .map(|f| {
                let mut map = Map::new();
                map.insert("name".to_string(), json!(f.name));
                map.insert("index".to_string(), json!(f.index));
                map.insert("is_fnptr".to_string(), json!(f.is_fnptr));
                map.insert("type".to_string(), json!(f.field_type));
                Value::Object(map)
            })
            .collect(),
    )
}

fn pattern(
    pattern_type: &str,
    signal_name: &str,
    handler: Option<&str>,
    line: i64,
    metadata: Value,
) -> PatternFact {
    PatternFact {
        pattern_type: pattern_type.to_string(),
        signal_name: Some(signal_name.to_string()),
        handler: handler.map(|h| h.to_string()),
        line: Some(line),
        metadata_json: Some(pyjson::dumps_default(&metadata)),
    }
}

/// CCppParser.extract_patterns.
pub fn extract_patterns(source: &str) -> Vec<PatternFact> {
    let mut patterns = Vec::new();
    let scan = blank_comments(source);
    let scan_bytes = scan.as_bytes();

    for m in re_fnptr_typedef().captures_iter(&scan) {
        patterns.push(pattern(
            "c_fnptr_typedef",
            &m[1],
            None,
            line_number_at(&scan, m.get(0).unwrap().start()),
            json!({"kind": "fnptr"}),
        ));
    }
    static FNTYPE_NAME: OnceLock<Regex> = OnceLock::new();
    let fntype_name = FNTYPE_NAME.get_or_init(|| Regex::new(r"\b(\w+)\s*\(").unwrap());
    for m in re_fntype_typedef().captures_iter(&scan) {
        let guts = &m[1];
        if guts.contains("(*") || guts.contains("( *") {
            continue;
        }
        if let Some(fm) = fntype_name.captures(guts) {
            if !C_TYPE_KEYWORDS.contains(&&fm[1]) {
                patterns.push(pattern(
                    "c_fnptr_typedef",
                    &fm[1],
                    None,
                    line_number_at(&scan, m.get(0).unwrap().start()),
                    json!({"kind": "fntype"}),
                ));
            }
        }
    }

    for m in re_struct_def().captures_iter(&scan) {
        let start = m.get(0).unwrap().start();
        let Some(brace) = scan[start..].find('{').map(|p| p + start) else {
            continue;
        };
        let Some(end) = find_matching_brace(scan_bytes, brace) else {
            continue;
        };
        let fields = parse_struct_fields(&scan[brace + 1..end]);
        if !fields.is_empty() {
            patterns.push(pattern(
                "c_struct_layout",
                &m[1],
                None,
                line_number_at(&scan, start),
                json!({"fields": fields_metadata(&fields)}),
            ));
        }
    }

    let mut var_type: HashMap<String, String> = HashMap::new();
    for m in re_table_init().captures_iter(&scan) {
        let struct_name = &m[1];
        let var_name = &m[2];
        let is_array = m.get(3).is_some();
        let brace = m.get(0).unwrap().end() - 1;
        let Some(end) = find_matching_brace(scan_bytes, brace) else {
            continue;
        };
        var_type.insert(var_name.to_string(), struct_name.to_string());
        let line = line_number_at(&scan, m.get(0).unwrap().start());
        let body = strip_preproc_lines(&scan[brace + 1..end]);
        let elements: Vec<String> = if is_array {
            split_top_level(&body, ',')
                .into_iter()
                .map(|s| s.to_string())
                .collect()
        } else {
            vec![body]
        };
        for el in elements {
            let el = el.trim();
            if el.is_empty() || (is_array && !el.starts_with('{')) {
                continue;
            }
            let inner: &str = if let Some(rest) = el.strip_prefix('{') {
                let Some(e) = find_matching_brace(el.as_bytes(), 0) else {
                    continue;
                };
                if !el[e + 1..].trim().is_empty() {
                    continue;
                }
                &rest[..e - 1]
            } else {
                el
            };

            let designated: Vec<regex::Captures> = re_designated()
                .captures_iter(inner)
                .filter(|dm| designated_tail_ok(&inner[dm.get(0).unwrap().end()..]))
                .collect();
            if !designated.is_empty() {
                for dm in designated {
                    patterns.push(pattern(
                        "c_fnptr_register",
                        struct_name,
                        Some(&dm[2]),
                        line,
                        json!({"field": &dm[1], "table_var": var_name}),
                    ));
                }
                continue;
            }
            for (slot, sv) in split_top_level(inner, ',').iter().enumerate() {
                if let Some(handler) = ident_only(sv.trim()) {
                    patterns.push(pattern(
                        "c_fnptr_register",
                        struct_name,
                        Some(&handler),
                        line,
                        json!({"slot": slot, "table_var": var_name}),
                    ));
                }
            }
        }
    }

    static DISPATCH_TAIL: OnceLock<Regex> = OnceLock::new();
    static SUBSCRIPT: OnceLock<Regex> = OnceLock::new();
    let dispatch_tail = DISPATCH_TAIL.get_or_init(|| Regex::new(r"\s*(?:->|\.)\s*$").unwrap());
    let subscript = SUBSCRIPT.get_or_init(|| Regex::new(r"\s*\[[^\]]*\]").unwrap());
    let func_ranges = function_ranges(&scan);
    for m in re_dispatch().captures_iter(&scan) {
        let base_chain = dispatch_tail.replace(&m[1], "").trim().to_string();
        let field = &m[2];
        let pos = m.get(0).unwrap().start();
        let Some(enclosing) = enclosing_function(&func_ranges, pos) else {
            continue;
        };
        let last_seg = subscript
            .replace_all(&base_chain, "")
            .replace("->", ".")
            .rsplit('.')
            .next()
            .unwrap_or("")
            .trim()
            .to_string();
        let struct_hint = var_type.get(&last_seg).cloned().or_else(|| {
            let (s, e) = func_ranges
                .iter()
                .find(|(n, _)| n == enclosing)
                .map(|(_, r)| *r)
                .unwrap();
            local_var_type(&scan[s..e], &last_seg)
        });
        patterns.push(pattern(
            "c_fnptr_dispatch",
            field,
            Some(enclosing),
            line_number_at(&scan, pos),
            json!({"receiver": last_seg, "struct_hint": struct_hint}),
        ));
    }
    patterns
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
typedef int (*handler_fn)(int);
typedef void reply_fn(char code);

struct ops {
    int id;
    int (*fire)(int);
    handler_fn on_event;
};

static struct ops table = {
    .fire = do_fire,
    .on_event = on_evt,
};

int do_fire(int x) { return x; }
int on_evt(int x) { return x; }

void run(struct ops *o) {
    o->fire(1);
}
"#;

    #[test]
    fn emits_four_fact_families() {
        let patterns = extract_patterns(SAMPLE);
        let types: Vec<&str> = patterns.iter().map(|p| p.pattern_type.as_str()).collect();
        assert!(types.contains(&"c_fnptr_typedef"));
        assert!(types.contains(&"c_struct_layout"));
        assert!(types.contains(&"c_fnptr_register"));
        assert!(types.contains(&"c_fnptr_dispatch"));
    }

    #[test]
    fn typedef_kinds_are_distinguished() {
        let patterns = extract_patterns(SAMPLE);
        let typedefs: Vec<(&str, &str)> = patterns
            .iter()
            .filter(|p| p.pattern_type == "c_fnptr_typedef")
            .map(|p| {
                (
                    p.signal_name.as_deref().unwrap(),
                    p.metadata_json.as_deref().unwrap(),
                )
            })
            .collect();
        assert!(typedefs.contains(&("handler_fn", r#"{"kind": "fnptr"}"#)));
        assert!(typedefs.contains(&("reply_fn", r#"{"kind": "fntype"}"#)));
    }

    #[test]
    fn struct_layout_fields_replicate_python_shape() {
        let patterns = extract_patterns(SAMPLE);
        let layout = patterns
            .iter()
            .find(|p| p.pattern_type == "c_struct_layout")
            .unwrap();
        assert_eq!(layout.signal_name.as_deref(), Some("ops"));
        assert_eq!(
            layout.metadata_json.as_deref().unwrap(),
            r#"{"fields": [{"name": "id", "index": 0, "is_fnptr": false, "type": "int"}, {"name": "fire", "index": 1, "is_fnptr": true, "type": ""}, {"name": "on_event", "index": 2, "is_fnptr": false, "type": "handler_fn"}]}"#
        );
    }

    #[test]
    fn designated_registrations_capture_field_and_handler() {
        let patterns = extract_patterns(SAMPLE);
        let registers: Vec<(&str, &str)> = patterns
            .iter()
            .filter(|p| p.pattern_type == "c_fnptr_register")
            .map(|p| {
                (
                    p.handler.as_deref().unwrap(),
                    p.metadata_json.as_deref().unwrap(),
                )
            })
            .collect();
        assert!(registers.contains(&("do_fire", r#"{"field": "fire", "table_var": "table"}"#)));
        assert!(registers.contains(&("on_evt", r#"{"field": "on_event", "table_var": "table"}"#)));
    }

    #[test]
    fn dispatch_records_receiver_and_struct_hint() {
        let patterns = extract_patterns(SAMPLE);
        let dispatch = patterns
            .iter()
            .find(|p| p.pattern_type == "c_fnptr_dispatch")
            .unwrap();
        assert_eq!(dispatch.signal_name.as_deref(), Some("fire"));
        assert_eq!(dispatch.handler.as_deref(), Some("run"));
        assert_eq!(
            dispatch.metadata_json.as_deref().unwrap(),
            r#"{"receiver": "o", "struct_hint": "ops"}"#
        );
    }

    #[test]
    fn slot_registrations_for_positional_arrays() {
        let source = "\
struct ops { int (*a)(void); int (*b)(void); };
static struct ops table[] = {
    { fn_a, fn_b },
};
";
        let patterns = extract_patterns(source);
        let slots: Vec<(&str, &str)> = patterns
            .iter()
            .filter(|p| p.pattern_type == "c_fnptr_register")
            .map(|p| {
                (
                    p.handler.as_deref().unwrap(),
                    p.metadata_json.as_deref().unwrap(),
                )
            })
            .collect();
        assert_eq!(
            slots,
            vec![
                ("fn_a", r#"{"slot": 0, "table_var": "table"}"#),
                ("fn_b", r#"{"slot": 1, "table_var": "table"}"#),
            ]
        );
    }

    #[test]
    fn comments_are_blanked_with_offsets_preserved() {
        let source = "/* typedef int (*fake)(void); */\ntypedef int (*real_fn)(int);\n";
        let patterns = extract_patterns(source);
        assert_eq!(patterns.len(), 1);
        assert_eq!(patterns[0].signal_name.as_deref(), Some("real_fn"));
        assert_eq!(patterns[0].line, Some(2));
    }
}
