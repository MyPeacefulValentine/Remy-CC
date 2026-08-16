//! symbol_selection.py replication: deterministic canonical selection for
//! same-name parsed symbols.

use crate::facts::SymbolInfo;
use std::cmp::Ordering;
use std::collections::BTreeMap;

pub const TYPE_VARIANT: &str = "type_variant";
pub const SIGNATURE_VARIANT: &str = "signature_variant";
pub const DUPLICATE_DEFINITION: &str = "duplicate_definition";

#[derive(Debug, Clone)]
pub struct SelectedOccurrence {
    pub symbol: SymbolInfo,
    pub occurrence_index: i64,
    pub is_canonical: bool,
    pub conflict_kind: String,
    pub selection_reason: String,
}

#[derive(Debug, Clone)]
pub struct Selection {
    pub canonical_symbols: Vec<SymbolInfo>,
    pub occurrences: Vec<SelectedOccurrence>,
}

pub fn normalize_signature(args: &str) -> String {
    args.split(crate::selection::is_python_re_space)
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// Python `re` \s in Unicode mode: everything str.isspace accepts.
pub(crate) fn is_python_re_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

fn stable_key_cmp(a: &SymbolInfo, b: &SymbolInfo) -> Ordering {
    let a_line = a.lineno;
    let b_line = b.lineno;
    a_line
        .cmp(&b_line)
        .then_with(|| {
            a.end_lineno
                .unwrap_or(a_line)
                .cmp(&b.end_lineno.unwrap_or(b_line))
        })
        .then_with(|| a.name.cmp(&b.name))
        .then_with(|| a.sym_type.cmp(&b.sym_type))
        .then_with(|| normalize_signature(&a.args).cmp(&normalize_signature(&b.args)))
        .then_with(|| a.source_segment.cmp(&b.source_segment))
        .then_with(|| {
            a.docstring
                .as_deref()
                .unwrap_or("")
                .cmp(b.docstring.as_deref().unwrap_or(""))
        })
        .then_with(|| {
            let empty: Vec<String> = Vec::new();
            a.bases
                .as_ref()
                .unwrap_or(&empty)
                .cmp(b.bases.as_ref().unwrap_or(&empty))
        })
}

fn source_extent_cmp(a: &SymbolInfo, b: &SymbolInfo) -> Ordering {
    let a_span = extent_span(a);
    let b_span = extent_span(b);
    let a_has = !a.source_segment.is_empty();
    let b_has = !b.source_segment.is_empty();
    // Python key: (-bool(segment), -span, -len(segment), stable_key).
    b_has
        .cmp(&a_has)
        .then_with(|| b_span.cmp(&a_span))
        .then_with(|| {
            b.source_segment
                .chars()
                .count()
                .cmp(&a.source_segment.chars().count())
        })
        .then_with(|| stable_key_cmp(a, b))
}

fn extent_span(symbol: &SymbolInfo) -> i64 {
    let lineno = symbol.lineno;
    let end = symbol.end_lineno.unwrap_or(lineno);
    (end - lineno).max(0)
}

fn classify(symbols: &[SymbolInfo]) -> &'static str {
    let mut types: Vec<&str> = symbols.iter().map(|s| s.sym_type.as_str()).collect();
    types.sort_unstable();
    types.dedup();
    if types.len() > 1 {
        return TYPE_VARIANT;
    }
    let mut signatures: Vec<String> = symbols
        .iter()
        .map(|s| normalize_signature(&s.args))
        .collect();
    signatures.sort_unstable();
    signatures.dedup();
    if signatures.len() > 1 {
        return SIGNATURE_VARIANT;
    }
    DUPLICATE_DEFINITION
}

/// symbol_selection.select_symbols: one canonical symbol per name plus
/// auditable conflict occurrences.
pub fn select_symbols(symbols: Vec<SymbolInfo>) -> Selection {
    let mut groups: BTreeMap<String, Vec<SymbolInfo>> = BTreeMap::new();
    for symbol in symbols {
        groups.entry(symbol.name.clone()).or_default().push(symbol);
    }

    let mut canonical = Vec::new();
    let mut occurrences = Vec::new();
    for (_name, mut ordered) in groups {
        ordered.sort_by(stable_key_cmp);
        if ordered.len() == 1 {
            canonical.push(ordered.into_iter().next().unwrap());
            continue;
        }

        let conflict_kind = classify(&ordered);
        let (selected_index, selection_reason) = if conflict_kind == SIGNATURE_VARIANT {
            (0, "earliest_source_position")
        } else {
            let mut best = 0;
            for i in 1..ordered.len() {
                if source_extent_cmp(&ordered[i], &ordered[best]) == Ordering::Less {
                    best = i;
                }
            }
            (best, "max_source_extent_then_position")
        };

        canonical.push(ordered[selected_index].clone());
        for (index, symbol) in ordered.into_iter().enumerate() {
            occurrences.push(SelectedOccurrence {
                symbol,
                occurrence_index: index as i64,
                is_canonical: index == selected_index,
                conflict_kind: conflict_kind.to_string(),
                selection_reason: selection_reason.to_string(),
            });
        }
    }

    canonical.sort_by(stable_key_cmp);
    Selection {
        canonical_symbols: canonical,
        occurrences,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sym(name: &str, sym_type: &str, args: &str, lineno: i64, segment: &str) -> SymbolInfo {
        SymbolInfo {
            name: name.to_string(),
            args: args.to_string(),
            sym_type: sym_type.to_string(),
            lineno,
            source_segment: segment.to_string(),
            end_lineno: Some(lineno + segment.matches('\n').count() as i64),
            docstring: None,
            bases: None,
        }
    }

    #[test]
    fn single_symbol_has_no_occurrences() {
        let selection = select_symbols(vec![sym("f", "function", "()", 1, "int f() {}")]);
        assert_eq!(selection.canonical_symbols.len(), 1);
        assert!(selection.occurrences.is_empty());
    }

    #[test]
    fn signature_variant_selects_earliest() {
        let selection = select_symbols(vec![
            sym("f", "function", "(int a)", 10, "int f(int a) { return a; }"),
            sym("f", "function", "()", 2, "int f() {}"),
        ]);
        assert_eq!(selection.canonical_symbols.len(), 1);
        assert_eq!(selection.canonical_symbols[0].lineno, 2);
        let canonical: Vec<_> = selection
            .occurrences
            .iter()
            .filter(|o| o.is_canonical)
            .collect();
        assert_eq!(canonical.len(), 1);
        assert_eq!(canonical[0].selection_reason, "earliest_source_position");
        assert_eq!(canonical[0].conflict_kind, SIGNATURE_VARIANT);
    }

    #[test]
    fn duplicate_definition_selects_max_extent() {
        let selection = select_symbols(vec![
            sym("f", "function", "()", 2, "int f() {}"),
            sym(
                "f",
                "function",
                "()",
                10,
                "int f() {\n  work();\n  more();\n}",
            ),
        ]);
        assert_eq!(selection.canonical_symbols[0].lineno, 10);
        let canonical: Vec<_> = selection
            .occurrences
            .iter()
            .filter(|o| o.is_canonical)
            .collect();
        assert_eq!(
            canonical[0].selection_reason,
            "max_source_extent_then_position"
        );
        assert_eq!(canonical[0].conflict_kind, DUPLICATE_DEFINITION);
    }

    #[test]
    fn selection_is_input_order_independent() {
        let a = vec![
            sym("f", "function", "()", 2, "int f() {}"),
            sym("f", "function", "()", 10, "int f() {\n  work();\n}"),
            sym("g", "struct", "", 1, "struct g {};"),
        ];
        let mut b = a.clone();
        b.reverse();
        let left = select_symbols(a);
        let right = select_symbols(b);
        assert_eq!(left.canonical_symbols, right.canonical_symbols);
        assert_eq!(
            left.occurrences
                .iter()
                .map(|o| (&o.symbol.name, o.occurrence_index, o.is_canonical))
                .collect::<Vec<_>>(),
            right
                .occurrences
                .iter()
                .map(|o| (&o.symbol.name, o.occurrence_index, o.is_canonical))
                .collect::<Vec<_>>()
        );
    }
}
