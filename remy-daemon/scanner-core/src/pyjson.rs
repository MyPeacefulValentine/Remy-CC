//! Python `json.dumps` replication for fact-table JSON columns.
//!
//! Two formats coexist in the schema (both asserted by probe vectors in
//! the contract tests):
//! - `dumps_default`: `json.dumps(value)` — separators `", "` / `": "`,
//!   `ensure_ascii=True`, insertion order preserved.
//! - `dumps_identity`: `json.dumps(value, ensure_ascii=False,
//!   sort_keys=True, separators=(",", ":"))` — parser cache identity
//!   environment encoding (parsers/base.py ParserCacheIdentity.create).

use serde_json::Value;

pub fn dumps_default(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, ", ", ": ", true, false);
    out
}

pub fn dumps_identity(value: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, value, ",", ":", false, true);
    out
}

fn write_value(
    out: &mut String,
    value: &Value,
    item_sep: &str,
    key_sep: &str,
    ensure_ascii: bool,
    sort_keys: bool,
) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => write_string(out, s, ensure_ascii),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                write_value(out, item, item_sep, key_sep, ensure_ascii, sort_keys);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            let mut keys: Vec<&String> = map.keys().collect();
            if sort_keys {
                keys.sort();
            }
            for (i, key) in keys.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                write_string(out, key, ensure_ascii);
                out.push_str(key_sep);
                write_value(out, &map[*key], item_sep, key_sep, ensure_ascii, sort_keys);
            }
            out.push('}');
        }
    }
}

fn write_string(out: &mut String, s: &str, ensure_ascii: bool) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c if ensure_ascii && (c as u32) > 0x7f => {
                let code = c as u32;
                if code > 0xffff {
                    // Python json emits UTF-16 surrogate pairs for astral chars.
                    let v = code - 0x10000;
                    let high = 0xd800 + (v >> 10);
                    let low = 0xdc00 + (v & 0x3ff);
                    out.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
                } else {
                    out.push_str(&format!("\\u{code:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn default_format_matches_python_probe_vectors() {
        // Probe J1 (2026-08-16, Python 3.12.9).
        assert_eq!(
            dumps_default(&json!(["a/b.h", "c.h"])),
            r#"["a/b.h", "c.h"]"#
        );
        // Probe J2.
        assert_eq!(
            dumps_default(&json!([{"module": "m", "names": ["x", "y"]}])),
            r#"[{"module": "m", "names": ["x", "y"]}]"#
        );
        assert_eq!(dumps_default(&json!([])), "[]");
    }

    #[test]
    fn identity_format_matches_python_probe_vector() {
        // Probe J3: sort_keys + compact separators.
        let value = json!({"tree-sitter-c": "0.24.2", "tree-sitter": "0.25.2"});
        assert_eq!(
            dumps_identity(&value),
            r#"{"tree-sitter":"0.25.2","tree-sitter-c":"0.24.2"}"#
        );
        assert_eq!(dumps_identity(&json!({})), "{}");
    }

    #[test]
    fn ensure_ascii_escapes_non_ascii_in_default_format() {
        assert_eq!(dumps_default(&json!(["héllo"])), "[\"h\\u00e9llo\"]");
        assert_eq!(dumps_default(&json!(["😀"])), "[\"\\ud83d\\ude00\"]");
        assert_eq!(dumps_identity(&json!({"k": "é"})), "{\"k\":\"é\"}");
    }
}
