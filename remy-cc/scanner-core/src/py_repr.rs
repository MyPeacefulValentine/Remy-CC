//! Python `repr()` and literal-decoding contracts needed to replicate
//! `ast.unparse` output byte for byte.
//!
//! `ast.unparse` writes constants through `repr()`, so a Rust replica must
//! reproduce CPython's quote selection, escape rules, and float formatting
//! rather than echoing the source text. Anything this module cannot decode
//! exactly returns `None`, letting the caller fall back to the verbatim
//! source slice instead of emitting a wrong value.

/// A decoded string/bytes literal value (the `Constant.value` an
/// `ast.parse` would produce).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LiteralValue {
    Str(String),
    Bytes(Vec<u8>),
}

/// Prefix flags of a Python string literal (`rb'...'`, `f"..."`, ...).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct LiteralPrefix {
    pub raw: bool,
    pub bytes: bool,
    pub format: bool,
    pub unicode: bool,
}

/// Split `text` (a complete string literal) into its prefix flags, quote
/// delimiter, and body. Returns `None` when the text is not a well-formed
/// literal.
pub fn split_literal(text: &str) -> Option<(LiteralPrefix, &'static str, &str)> {
    let mut prefix = LiteralPrefix::default();
    let mut rest = text;
    loop {
        let first = rest.chars().next()?;
        match first {
            'r' | 'R' => prefix.raw = true,
            'b' | 'B' => prefix.bytes = true,
            'f' | 'F' => prefix.format = true,
            'u' | 'U' => prefix.unicode = true,
            '\'' | '"' => break,
            _ => return None,
        }
        rest = &rest[first.len_utf8()..];
    }
    for quote in ["\"\"\"", "'''", "\"", "'"] {
        if let Some(body) = rest.strip_prefix(quote) {
            let body = body.strip_suffix(quote)?;
            return Some((prefix, quote, body));
        }
    }
    None
}

/// Decode a non-f-string literal body into its runtime value. Returns
/// `None` for `\N{NAME}` escapes (which need the Unicode name database)
/// and for malformed escapes.
pub fn decode_literal_body(prefix: LiteralPrefix, body: &str) -> Option<LiteralValue> {
    if prefix.raw {
        return Some(if prefix.bytes {
            LiteralValue::Bytes(body.as_bytes().to_vec())
        } else {
            LiteralValue::Str(body.to_string())
        });
    }

    let mut text = String::new();
    let mut bytes: Vec<u8> = Vec::new();
    let mut chars = body.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '\\' {
            if prefix.bytes {
                let mut buf = [0u8; 4];
                bytes.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            } else {
                text.push(c);
            }
            continue;
        }
        let escape = chars.next()?;
        let decoded: Option<char> = match escape {
            '\n' => continue,
            '\\' => Some('\\'),
            '\'' => Some('\''),
            '"' => Some('"'),
            'a' => Some('\u{7}'),
            'b' => Some('\u{8}'),
            'f' => Some('\u{c}'),
            'n' => Some('\n'),
            'r' => Some('\r'),
            't' => Some('\t'),
            'v' => Some('\u{b}'),
            '0'..='7' => {
                let mut value = escape as u32 - '0' as u32;
                for _ in 0..2 {
                    match chars.peek() {
                        Some(d @ '0'..='7') => {
                            value = value * 8 + (*d as u32 - '0' as u32);
                            chars.next();
                        }
                        _ => break,
                    }
                }
                if prefix.bytes {
                    if value > 0xff {
                        return None;
                    }
                    bytes.push(value as u8);
                    continue;
                }
                char::from_u32(value)
            }
            'x' => {
                let value = take_hex(&mut chars, 2)?;
                if prefix.bytes {
                    bytes.push(value as u8);
                    continue;
                }
                char::from_u32(value)
            }
            'u' if !prefix.bytes => char::from_u32(take_hex(&mut chars, 4)?),
            'U' if !prefix.bytes => char::from_u32(take_hex(&mut chars, 8)?),
            'N' if !prefix.bytes => return None,
            other => {
                // Unknown escapes keep the backslash (Python semantics).
                if prefix.bytes {
                    let mut buf = [0u8; 4];
                    bytes.push(b'\\');
                    bytes.extend_from_slice(other.encode_utf8(&mut buf).as_bytes());
                } else {
                    text.push('\\');
                    text.push(other);
                }
                continue;
            }
        };
        let decoded = decoded?;
        if prefix.bytes {
            let mut buf = [0u8; 4];
            bytes.extend_from_slice(decoded.encode_utf8(&mut buf).as_bytes());
        } else {
            text.push(decoded);
        }
    }

    Some(if prefix.bytes {
        LiteralValue::Bytes(bytes)
    } else {
        LiteralValue::Str(text)
    })
}

fn take_hex(chars: &mut std::iter::Peekable<std::str::Chars>, count: usize) -> Option<u32> {
    let mut value: u32 = 0;
    for _ in 0..count {
        let digit = chars.next()?.to_digit(16)?;
        value = value * 16 + digit;
    }
    Some(value)
}

/// `repr(str)`: prefer single quotes, switch to double quotes only when the
/// value contains a single quote and no double quote, escape backslash, the
/// active quote, `\t\n\r`, and every non-printable code point.
pub fn repr_str(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(value.len() + 2);
    out.push(quote);
    for c in value.chars() {
        push_escaped_char(&mut out, c, quote);
    }
    out.push(quote);
    out
}

fn push_escaped_char(out: &mut String, c: char, quote: char) {
    match c {
        '\\' => out.push_str("\\\\"),
        '\t' => out.push_str("\\t"),
        '\n' => out.push_str("\\n"),
        '\r' => out.push_str("\\r"),
        _ if c == quote => {
            out.push('\\');
            out.push(c);
        }
        _ if is_printable(c) => out.push(c),
        _ => {
            let code = c as u32;
            if code < 0x100 {
                out.push_str(&format!("\\x{code:02x}"));
            } else if code < 0x10000 {
                out.push_str(&format!("\\u{code:04x}"));
            } else {
                out.push_str(&format!("\\U{code:08x}"));
            }
        }
    }
}

/// `repr(bytes)`: same quote rule as `str`, every byte outside printable
/// ASCII becomes `\xNN`.
pub fn repr_bytes(value: &[u8]) -> String {
    let quote = if value.contains(&b'\'') && !value.contains(&b'"') {
        b'"'
    } else {
        b'\''
    };
    let mut out = String::with_capacity(value.len() + 3);
    out.push('b');
    out.push(quote as char);
    for &byte in value {
        match byte {
            b'\\' => out.push_str("\\\\"),
            b'\t' => out.push_str("\\t"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            _ if byte == quote => {
                out.push('\\');
                out.push(byte as char);
            }
            0x20..=0x7e => out.push(byte as char),
            _ => out.push_str(&format!("\\x{byte:02x}")),
        }
    }
    out.push(quote as char);
    out
}

/// `Py_UNICODE_ISPRINTABLE`: everything except the Cc/Cf/Cs/Co/Cn/Zl/Zp/Zs
/// categories, with U+0020 explicitly printable.
///
/// ASCII is exact. Beyond ASCII this covers Cc, Zs, Zl, Zp, Cs, Co, and the
/// Cf ranges that occur in practice; unassigned code points (Cn) are treated
/// as printable because deciding them needs the full Unicode database, which
/// the scanner deliberately does not depend on. A mismatch can therefore only
/// appear inside a string constant holding an unassigned code point.
fn is_printable(c: char) -> bool {
    let code = c as u32;
    if code < 0x80 {
        return (0x20..0x7f).contains(&code);
    }
    !matches!(code,
        0x80..=0x9f            // Cc
        | 0xa0 | 0x1680        // Zs
        | 0x2000..=0x200a
        | 0x202f | 0x205f | 0x3000
        | 0x2028               // Zl
        | 0x2029               // Zp
        | 0xad                 // Cf
        | 0x600..=0x605 | 0x61c | 0x6dd | 0x70f | 0x890 | 0x891 | 0x8e2
        | 0x180e
        | 0x200b..=0x200f
        | 0x202a..=0x202e
        | 0x2060..=0x2064
        | 0x2066..=0x206f
        | 0xfeff
        | 0xfff9..=0xfffb
        | 0x110bd | 0x110cd
        | 0x13430..=0x1343f
        | 0x1bca0..=0x1bca3
        | 0x1d173..=0x1d17a
        | 0xe0001 | 0xe0020..=0xe007f
        | 0xd800..=0xdfff      // Cs
        | 0xe000..=0xf8ff      // Co
        | 0xf0000..=0xffffd
        | 0x100000..=0x10fffd
    )
}

/// `repr(int)` for an integer literal's source text. Decimal literals are
/// normalized textually so arbitrary precision keeps working; radix literals
/// go through `i128` and return `None` on overflow.
pub fn repr_int_literal(text: &str) -> Option<String> {
    let cleaned: String = text.chars().filter(|c| *c != '_').collect();
    let lower = cleaned.to_ascii_lowercase();
    let (radix, digits) = if let Some(rest) = lower.strip_prefix("0x") {
        (16, rest)
    } else if let Some(rest) = lower.strip_prefix("0o") {
        (8, rest)
    } else if let Some(rest) = lower.strip_prefix("0b") {
        (2, rest)
    } else {
        let trimmed = cleaned.trim_start_matches('0');
        let normalized = if trimmed.is_empty() { "0" } else { trimmed };
        return normalized
            .chars()
            .all(|c| c.is_ascii_digit())
            .then(|| normalized.to_string());
    };
    i128::from_str_radix(digits, radix)
        .ok()
        .map(|value| value.to_string())
}

/// `repr(float)`: shortest round-trip digits, fixed notation while the
/// decimal point sits in `(-4, 16]`, exponent notation otherwise, and a
/// trailing `.0` for integral values.
pub fn repr_float(value: f64) -> String {
    format_double(value, true)
}

/// `repr(complex)` for a pure imaginary literal — the same digits as
/// `repr_float` but without the `.0` suffix (CPython omits
/// `Py_DTSF_ADD_DOT_0` for complex parts).
pub fn repr_imaginary(value: f64) -> String {
    format!("{}j", format_double(value, false))
}

fn format_double(value: f64, add_dot_zero: bool) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    let sign = if value.is_sign_negative() { "-" } else { "" };
    if value.is_infinite() {
        return format!("{sign}inf");
    }
    let magnitude = value.abs();
    if magnitude == 0.0 {
        return format!("{sign}0{}", if add_dot_zero { ".0" } else { "" });
    }

    let (digits, decpt) = shortest_digits(magnitude);
    let body = if decpt <= -4 || decpt > 16 {
        let mantissa = if digits.len() == 1 {
            digits.clone()
        } else {
            format!("{}.{}", &digits[..1], &digits[1..])
        };
        let exponent = decpt - 1;
        let exp_sign = if exponent < 0 { '-' } else { '+' };
        format!("{mantissa}e{exp_sign}{:02}", exponent.abs())
    } else if decpt <= 0 {
        format!("0.{}{}", "0".repeat((-decpt) as usize), digits)
    } else if (decpt as usize) >= digits.len() {
        let padded = format!("{}{}", digits, "0".repeat(decpt as usize - digits.len()));
        if add_dot_zero {
            format!("{padded}.0")
        } else {
            padded
        }
    } else {
        format!(
            "{}.{}",
            &digits[..decpt as usize],
            &digits[decpt as usize..]
        )
    };
    format!("{sign}{body}")
}

/// Shortest round-trip decimal digits of a positive finite double, plus the
/// decimal exponent `decpt` defined by `value == 0.<digits> * 10^decpt`.
fn shortest_digits(magnitude: f64) -> (String, i32) {
    let formatted = format!("{magnitude:e}");
    let (mantissa, exponent) = formatted
        .split_once('e')
        .expect("Rust LowerExp always emits an exponent");
    let exponent: i32 = exponent.parse().expect("LowerExp exponent is an integer");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let digits = digits.trim_end_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    (digits.to_string(), exponent + 1)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_repr_matches_python_vectors() {
        // Probe vectors (Python 3.12.9 repr).
        for (value, expected) in [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (1000.0, "1000.0"),
            (0.001, "0.001"),
            (1e-4, "0.0001"),
            (1e-5, "1e-05"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1e17, "1e+17"),
            (1.5e300, "1.5e+300"),
            (5e-324, "5e-324"),
            (0.1, "0.1"),
            (2.0 / 3.0, "0.6666666666666666"),
            (-2.5, "-2.5"),
        ] {
            assert_eq!(repr_float(value), expected, "repr({value})");
        }
    }

    #[test]
    fn imaginary_repr_drops_the_dot_zero() {
        assert_eq!(repr_imaginary(3.0), "3j");
        assert_eq!(repr_imaginary(3.5), "3.5j");
        assert_eq!(repr_imaginary(0.0), "0j");
        assert_eq!(repr_imaginary(1e16), "1e+16j");
    }

    #[test]
    fn str_repr_matches_python_vectors() {
        for (value, expected) in [
            ("plain", "'plain'"),
            ("has'sq", "\"has'sq\""),
            ("has\"dq", "'has\"dq'"),
            ("both'and\"", "'both\\'and\"'"),
            ("nl\nhere", "'nl\\nhere'"),
            ("tab\there", "'tab\\there'"),
            ("cr\rhere", "'cr\\rhere'"),
            ("back\\slash", "'back\\\\slash'"),
            ("\u{0}null", "'\\x00null'"),
            ("\u{1b}esc", "'\\x1besc'"),
            ("\u{7f}del", "'\\x7fdel'"),
            ("unié", "'unié'"),
            ("日本", "'日本'"),
            ("\u{a0}nbsp", "'\\xa0nbsp'"),
            ("\u{2028}lsep", "'\\u2028lsep'"),
            ("emoji\u{1F600}", "'emoji\u{1F600}'"),
            ("\u{feff}bom", "'\\ufeffbom'"),
            ("mix'\"\n\\", "'mix\\'\"\\n\\\\'"),
        ] {
            assert_eq!(repr_str(value), expected, "repr({value:?})");
        }
    }

    #[test]
    fn bytes_repr_matches_python_vectors() {
        assert_eq!(repr_bytes(b"plain"), "b'plain'");
        assert_eq!(repr_bytes(b"has'sq"), "b\"has'sq\"");
        assert_eq!(repr_bytes(b"has\"dq"), "b'has\"dq'");
        assert_eq!(repr_bytes(&[0x00, 0xff]), "b'\\x00\\xff'");
        assert_eq!(repr_bytes(b"nl\n"), "b'nl\\n'");
        assert_eq!(repr_bytes(b"\\back"), "b'\\\\back'");
    }

    #[test]
    fn int_literal_repr_normalizes_radix_and_separators() {
        assert_eq!(repr_int_literal("0x10").as_deref(), Some("16"));
        assert_eq!(repr_int_literal("0o17").as_deref(), Some("15"));
        assert_eq!(repr_int_literal("0b101").as_deref(), Some("5"));
        assert_eq!(repr_int_literal("1_000_000").as_deref(), Some("1000000"));
        assert_eq!(repr_int_literal("0").as_deref(), Some("0"));
        assert_eq!(
            repr_int_literal("340282366920938463463374607431768211456").as_deref(),
            Some("340282366920938463463374607431768211456")
        );
        assert_eq!(
            repr_int_literal("0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"),
            None
        );
    }

    #[test]
    fn literal_decoding_handles_prefixes_and_escapes() {
        let (prefix, quote, body) = split_literal("rb'raw\\n'").unwrap();
        assert!(prefix.raw && prefix.bytes);
        assert_eq!(quote, "'");
        assert_eq!(
            decode_literal_body(prefix, body),
            Some(LiteralValue::Bytes(b"raw\\n".to_vec()))
        );

        let (prefix, _, body) = split_literal("'a\\nb\\x41\\101\\u00e9'").unwrap();
        assert_eq!(
            decode_literal_body(prefix, body),
            Some(LiteralValue::Str("a\nbAAé".to_string()))
        );

        let (prefix, quote, body) = split_literal("'''tri\nple'''").unwrap();
        assert_eq!(quote, "'''");
        assert_eq!(
            decode_literal_body(prefix, body),
            Some(LiteralValue::Str("tri\nple".to_string()))
        );

        let (prefix, _, body) = split_literal("'\\N{BULLET}'").unwrap();
        assert_eq!(decode_literal_body(prefix, body), None);

        let (prefix, _, body) = split_literal("'line\\\ncont'").unwrap();
        assert_eq!(
            decode_literal_body(prefix, body),
            Some(LiteralValue::Str("linecont".to_string()))
        );

        let (prefix, _, body) = split_literal("'keep\\q'").unwrap();
        assert_eq!(
            decode_literal_body(prefix, body),
            Some(LiteralValue::Str("keep\\q".to_string()))
        );
    }
}
