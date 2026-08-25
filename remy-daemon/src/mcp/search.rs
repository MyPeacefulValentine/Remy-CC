//! Text retrieval: exact, prefix, BM25, fuzzy.
//! Oracle: remy-src/index_mcp_search.py. The word tokenizer replicates
//! symbol_names.tokenize_symbol (ASCII-class regexes) plus the Unicode
//! letter/number word extraction; casefold maps to lowercase — equivalence is
//! declared for ASCII identifiers only (H.4 exclusion). The fuzzy ratio is a
//! faithful Ratcliff-Obershelp (difflib.SequenceMatcher.ratio without junk;
//! autojunk only engages at len >= 200, above any symbol name).

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

use rusqlite::functions::FunctionFlags;
use rusqlite::Connection;

use super::common::{get_layer, open_db, truthy_line, DB_NOT_FOUND};
use super::config::McpConfig;

pub const FUZZY_CUTOFF: f64 = 0.6;
const MATCH_MODES: &[&str] = &["all", "any", "phrase"];
const SYMBOL_TYPES: &[&str] = &[
    "function",
    "class",
    "struct",
    "enum",
    "typedef",
    "macro",
    "namespace",
    "interface",
    "type_alias",
];
const SORTED_SYMBOL_TYPES: &str =
    "class, enum, function, interface, macro, namespace, struct, type_alias, typedef";

pub fn language_values(language: &str) -> &'static [&'static str] {
    match language {
        "python" => &["pythonparser", "python"],
        "c_cpp" => &["ccppparser", "c_cpp", "c", "cpp"],
        "typescript" => &["tsparser", "typescript", "ts", "tsx"],
        "rust" => &["rustparser", "rust", "rs"],
        _ => &[],
    }
}

/// symbol_names.tokenize_symbol: snake_case / camelCase / namespace splits.
fn tokenize_symbol(name: &str) -> String {
    let spaced: String = name.replace('_', " ").replace("::", " ");
    let chars: Vec<char> = spaced.chars().collect();
    let mut out = String::with_capacity(spaced.len() + 8);
    for (i, &c) in chars.iter().enumerate() {
        if i > 0 {
            let prev = chars[i - 1];
            let lower_upper = prev.is_ascii_lowercase() && c.is_ascii_uppercase();
            let acronym_end = prev.is_ascii_uppercase()
                && c.is_ascii_uppercase()
                && chars.get(i + 1).is_some_and(|n| n.is_ascii_lowercase());
            if lower_upper || acronym_end {
                out.push(' ');
            }
        }
        out.push(c);
    }
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// index_mcp_search._extract_search_words: Unicode letter/number word runs,
/// casefolded (lowercase here; ASCII-equivalent, declared H.4 boundary).
pub fn extract_search_words(text: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut current = String::new();
    for c in tokenize_symbol(text).chars() {
        if c.is_alphanumeric() {
            current.push(c);
        } else if !current.is_empty() {
            words.push(current.to_lowercase());
            current = String::new();
        }
    }
    if !current.is_empty() {
        words.push(current.to_lowercase());
    }
    words
}

fn normalize_path(value: &str) -> String {
    value.trim().replace('\\', "/").to_lowercase()
}

fn word_prefix_count(value: &str, terms: &[String]) -> i64 {
    let words = extract_search_words(value);
    terms
        .iter()
        .filter(|term| words.iter().any(|word| word.starts_with(term.as_str())))
        .count() as i64
}

fn contains_phrase(value: &str, phrase: &str) -> i64 {
    let words = extract_search_words(value);
    let terms: Vec<&str> = phrase.split_whitespace().collect();
    if terms.is_empty() {
        return 0;
    }
    let width = terms.len();
    if words.len() < width {
        return 0;
    }
    for window in words.windows(width) {
        if window.iter().zip(&terms).all(|(w, t)| w == t) {
            return 1;
        }
    }
    0
}

pub fn register_search_functions(db: &Connection) -> rusqlite::Result<()> {
    let flags = FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC;
    db.create_scalar_function("remy_norm_path", 1, flags, |ctx| {
        let value: Option<String> = ctx.get(0)?;
        Ok(normalize_path(value.as_deref().unwrap_or("")))
    })?;
    db.create_scalar_function("remy_word_prefix_count", -1, flags, |ctx| {
        let value: Option<String> = ctx.get(0)?;
        let mut terms = Vec::with_capacity(ctx.len().saturating_sub(1));
        for i in 1..ctx.len() {
            let term: Option<String> = ctx.get(i)?;
            terms.push(term.unwrap_or_default());
        }
        Ok(word_prefix_count(value.as_deref().unwrap_or(""), &terms))
    })?;
    db.create_scalar_function("remy_contains_phrase", 2, flags, |ctx| {
        let value: Option<String> = ctx.get(0)?;
        let phrase: Option<String> = ctx.get(1)?;
        Ok(contains_phrase(
            value.as_deref().unwrap_or(""),
            phrase.as_deref().unwrap_or(""),
        ))
    })?;
    db.create_scalar_function("remy_casefold", 1, flags, |ctx| {
        let value: Option<String> = ctx.get(0)?;
        Ok(value.unwrap_or_default().to_lowercase())
    })?;
    Ok(())
}

#[derive(Debug, Clone)]
pub struct SearchQuery {
    pub text: String,
    pub words: Vec<String>,
    pub match_mode: String,
    pub limit: i64,
    pub path_hint: String,
    pub language_values: &'static [&'static str],
    pub symbol_type: String,
}

impl SearchQuery {
    pub fn normalized_text(&self) -> String {
        self.words.join(" ")
    }
}

#[allow(clippy::too_many_arguments)]
pub fn make_search_query(
    cfg: &McpConfig,
    text: &str,
    limit: i64,
    file_hint: &str,
    match_mode: &str,
    language: &str,
    symbol_type: &str,
    path_hint: &str,
) -> Result<SearchQuery, String> {
    if text.trim().is_empty() {
        return Err("text must not be empty".to_string());
    }
    let words = extract_search_words(text);
    if words.is_empty() {
        return Err("text must contain at least one searchable word".to_string());
    }
    if limit < 1 || limit > cfg.result_limit {
        return Err(format!("limit must be between 1 and {}", cfg.result_limit));
    }
    let normalized_match = match_mode.trim().to_lowercase();
    if !MATCH_MODES.contains(&normalized_match.as_str()) {
        return Err("match must be one of: all, any, phrase".to_string());
    }

    for (label, value) in [("file_hint", file_hint), ("path_hint", path_hint)] {
        if value.contains('\0') {
            return Err(format!("{label} must not contain NUL"));
        }
    }
    let old_path = normalize_path(file_hint);
    let new_path = normalize_path(path_hint);
    if !old_path.is_empty() && !new_path.is_empty() && old_path != new_path {
        return Err("file_hint and path_hint must not conflict".to_string());
    }
    let normalized_path = if new_path.is_empty() {
        old_path
    } else {
        new_path
    };

    if !language.is_empty() && language.trim().is_empty() {
        return Err("language must not contain only whitespace".to_string());
    }
    let normalized_language = language.trim().to_lowercase();
    if !normalized_language.is_empty() && language_values(&normalized_language).is_empty() {
        return Err("language must be one of: python, c_cpp, typescript, rust".to_string());
    }

    if !symbol_type.is_empty() && symbol_type.trim().is_empty() {
        return Err("symbol_type must not contain only whitespace".to_string());
    }
    let normalized_type = symbol_type.trim().to_lowercase();
    if !normalized_type.is_empty() && !SYMBOL_TYPES.contains(&normalized_type.as_str()) {
        return Err(format!("symbol_type must be one of: {SORTED_SYMBOL_TYPES}"));
    }

    Ok(SearchQuery {
        text: text.trim().to_string(),
        words,
        match_mode: normalized_match,
        limit,
        path_hint: normalized_path,
        language_values: language_values(&normalized_language),
        symbol_type: normalized_type,
    })
}

fn append_search_filters(
    sql: &mut String,
    params: &mut Vec<String>,
    query: &SearchQuery,
    projection_alias: Option<&str>,
    symbol_alias: &str,
    file_alias: &str,
) {
    if !query.language_values.is_empty() {
        let alias = projection_alias.unwrap_or(file_alias);
        let placeholders = vec!["?"; query.language_values.len()].join(",");
        sql.push_str(&format!("AND lower({alias}.language) IN ({placeholders}) "));
        params.extend(query.language_values.iter().map(|v| v.to_string()));
    }
    if !query.symbol_type.is_empty() {
        let column = match projection_alias {
            Some(alias) => format!("{alias}.symbol_type"),
            None => format!("{symbol_alias}.type"),
        };
        sql.push_str(&format!("AND lower({column}) = ? "));
        params.push(query.symbol_type.clone());
    }
    if !query.path_hint.is_empty() {
        let alias = projection_alias.unwrap_or(symbol_alias);
        sql.push_str(&format!(
            "AND instr(remy_norm_path({alias}.file_path), ?) > 0 "
        ));
        params.push(query.path_hint.clone());
    }
}

pub fn fts_expression(query: &SearchQuery, terms: Option<&[String]>) -> String {
    let selected = terms.unwrap_or(&query.words);
    if query.match_mode == "phrase" {
        return format!("\"{}\"", selected.join(" ").replace('"', "\"\""));
    }
    let separator = if query.match_mode == "any" {
        " OR "
    } else {
        " "
    };
    selected
        .iter()
        .map(|term| format!("\"{}\"*", term.replace('"', "\"\"")))
        .collect::<Vec<_>>()
        .join(separator)
}

/// (name, file_path, lineno, symbol_type, score) — the Python row tuple.
pub type Hit = (String, String, Option<i64>, String, f64);

fn py_str_cmp(a: &str, b: &str) -> Ordering {
    a.cmp(b)
}

fn cmp_hit_tail(a: &Hit, b: &Hit) -> Ordering {
    py_str_cmp(&a.0.to_lowercase(), &b.0.to_lowercase())
        .then_with(|| py_str_cmp(&a.0, &b.0))
        .then_with(|| py_str_cmp(&a.1, &b.1))
        .then_with(|| a.2.unwrap_or(0).cmp(&b.2.unwrap_or(0)))
}

struct FtsRow {
    name: String,
    file_path: String,
    lineno: Option<i64>,
    symbol_type: String,
    rank: f64,
}

fn fts_rows(
    db: &Connection,
    query: &SearchQuery,
    expression: &str,
    row_limit: i64,
) -> rusqlite::Result<Vec<FtsRow>> {
    let mut sql = String::from(
        "SELECT d.name, d.file_path, s.lineno, d.symbol_type, s.short_name, \
         bm25(retrieval_fts, 0.0, 0.0, 0.0, 0.0, 5.0, 1.0) AS rank \
         FROM retrieval_fts \
         JOIN retrieval_documents d ON d.doc_id = retrieval_fts.rowid \
         JOIN symbols s ON s.file_path = d.file_path AND s.name = d.name \
         WHERE retrieval_fts MATCH ? AND d.node_kind = 'symbol' ",
    );
    let mut params: Vec<String> = vec![format!("{{summary_short summary_full}} : ({expression})")];
    append_search_filters(&mut sql, &mut params, query, Some("d"), "s", "f");
    sql.push_str(
        "ORDER BY rank, lower(d.name), d.name, d.file_path, COALESCE(s.lineno, 0) LIMIT ?",
    );
    params.push(row_limit.to_string());
    let mut stmt = db.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(params.iter()), |row| {
        Ok(FtsRow {
            name: row.get(0)?,
            file_path: row.get(1)?,
            lineno: row.get(2)?,
            symbol_type: row.get(3)?,
            rank: row.get(5)?,
        })
    })?;
    rows.collect()
}

pub fn search_fts(db: &Connection, query: &SearchQuery) -> rusqlite::Result<Vec<Hit>> {
    register_search_functions(db)?;
    let cap = query.limit * 5;

    if query.match_mode == "any" && query.words.len() > 1 {
        struct Agg {
            name: String,
            file_path: String,
            lineno: Option<i64>,
            symbol_type: String,
            coverage: i64,
            rank: f64,
        }
        let mut aggregated: HashMap<(String, String), Agg> = HashMap::new();
        let mut order: Vec<(String, String)> = Vec::new();
        for term in &query.words {
            let expression = fts_expression(query, Some(std::slice::from_ref(term)));
            let rows = fts_rows(db, query, &expression, cap + 1)?;
            for row in rows.into_iter().take(cap.max(0) as usize) {
                let key = (row.file_path.clone(), row.name.clone());
                let entry = aggregated.entry(key.clone()).or_insert_with(|| {
                    order.push(key);
                    Agg {
                        name: row.name.clone(),
                        file_path: row.file_path.clone(),
                        lineno: row.lineno,
                        symbol_type: row.symbol_type.clone(),
                        coverage: 0,
                        rank: 0.0,
                    }
                });
                entry.coverage += 1;
                entry.rank += row.rank;
            }
        }
        let mut items: Vec<&Agg> = order.iter().map(|key| &aggregated[key]).collect();
        items.sort_by(|a, b| {
            b.coverage
                .cmp(&a.coverage)
                .then_with(|| a.rank.partial_cmp(&b.rank).unwrap_or(Ordering::Equal))
                .then_with(|| py_str_cmp(&a.name.to_lowercase(), &b.name.to_lowercase()))
                .then_with(|| py_str_cmp(&a.name, &b.name))
                .then_with(|| py_str_cmp(&a.file_path, &b.file_path))
                .then_with(|| a.lineno.unwrap_or(0).cmp(&b.lineno.unwrap_or(0)))
        });
        return Ok(items
            .into_iter()
            .take(query.limit.max(0) as usize)
            .map(|item| {
                (
                    item.name.clone(),
                    item.file_path.clone(),
                    item.lineno,
                    item.symbol_type.clone(),
                    item.rank,
                )
            })
            .collect());
    }

    let rows = fts_rows(db, query, &fts_expression(query, None), cap + 1)?;
    let mut results: Vec<Hit> = Vec::new();
    let mut seen: HashSet<(String, String)> = HashSet::new();
    for row in rows.into_iter().take(cap.max(0) as usize) {
        let key = (row.file_path.clone(), row.name.clone());
        if !seen.insert(key) {
            continue;
        }
        results.push((
            row.name,
            row.file_path,
            row.lineno,
            row.symbol_type,
            row.rank,
        ));
    }
    results.sort_by(|a, b| {
        a.4.partial_cmp(&b.4)
            .unwrap_or(Ordering::Equal)
            .then_with(|| cmp_hit_tail(a, b))
    });
    results.truncate(query.limit.max(0) as usize);
    Ok(results)
}

type PlainRow = (String, String, Option<i64>, String);

fn plain_rows(db: &Connection, sql: &str, params: &[String]) -> rusqlite::Result<Vec<PlainRow>> {
    let mut stmt = db.prepare(sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(params.iter()), |row| {
        Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
    })?;
    rows.collect()
}

pub fn search_exact(db: &Connection, query: &SearchQuery) -> rusqlite::Result<Vec<Hit>> {
    register_search_functions(db)?;
    let folded = query.text.to_lowercase();
    let mut sql = String::from(
        "SELECT s.name, s.file_path, s.lineno, s.type FROM symbols s \
         JOIN files f ON f.path = s.file_path \
         WHERE (remy_casefold(s.name) = ? OR remy_casefold(s.short_name) = ?) ",
    );
    let mut params = vec![folded.clone(), folded];
    append_search_filters(&mut sql, &mut params, query, None, "s", "f");
    let rows = plain_rows(db, &sql, &params)?;
    let mut results: Vec<Hit> = rows
        .into_iter()
        .map(|(name, fpath, lineno, stype)| (name, fpath, lineno, stype, 0.0))
        .collect();
    results.sort_by(cmp_hit_tail);
    results.truncate(query.limit.max(0) as usize);
    Ok(results)
}

fn like_sort_key(hit: &Hit, query: &SearchQuery) -> (i64, i64, String, String, String, i64) {
    let name = &hit.0;
    let name_folded = name.to_lowercase();
    let normalized_name = extract_search_words(name).join(" ");
    let normalized_query = query.normalized_text();
    let prefix_count = word_prefix_count(&normalized_name, &query.words);
    let text_folded = query.text.to_lowercase();
    let category = if name_folded == text_folded {
        0
    } else if normalized_name == normalized_query {
        1
    } else if name_folded.starts_with(&text_folded) {
        2
    } else if normalized_name.starts_with(&normalized_query) {
        3
    } else if prefix_count > 0 {
        4
    } else if name_folded.contains(&text_folded) {
        5
    } else if normalized_name.contains(&normalized_query) {
        6
    } else {
        7
    };
    (
        category,
        -prefix_count,
        name_folded,
        name.clone(),
        hit.1.clone(),
        hit.2.unwrap_or(0),
    )
}

pub fn search_like(db: &Connection, query: &SearchQuery) -> rusqlite::Result<Vec<Hit>> {
    register_search_functions(db)?;
    let mut sql = String::from(
        "SELECT s.name, s.file_path, s.lineno, s.type FROM symbols s \
         JOIN files f ON f.path = s.file_path WHERE ",
    );
    let mut params: Vec<String> = Vec::new();
    if query.match_mode == "phrase" {
        sql.push_str("remy_contains_phrase(s.name_tokens, ?) = 1 ");
        params.push(query.normalized_text());
    } else {
        let conditions = vec!["remy_word_prefix_count(s.name_tokens, ?) > 0"; query.words.len()];
        let joiner = if query.match_mode == "all" {
            " AND "
        } else {
            " OR "
        };
        sql.push_str(&format!("({}) ", conditions.join(joiner)));
        params.extend(query.words.iter().cloned());
    }
    append_search_filters(&mut sql, &mut params, query, None, "s", "f");
    let rows = plain_rows(db, &sql, &params)?;
    let mut results: Vec<Hit> = rows
        .into_iter()
        .map(|(name, fpath, lineno, stype)| (name, fpath, lineno, stype, 0.0))
        .collect();
    results.sort_by_key(|hit| like_sort_key(hit, query));
    results.truncate(query.limit.max(0) as usize);
    Ok(results)
}

/// difflib.SequenceMatcher(None, a, b).ratio() — Ratcliff-Obershelp over
/// chars, no junk (autojunk needs len(b) >= 200, above any symbol name).
pub fn sequence_ratio(a: &str, b: &str) -> f64 {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    let mut b2j: HashMap<char, Vec<usize>> = HashMap::new();
    for (j, &c) in b.iter().enumerate() {
        b2j.entry(c).or_default().push(j);
    }

    fn longest_match(
        a: &[char],
        b2j: &HashMap<char, Vec<usize>>,
        alo: usize,
        ahi: usize,
        blo: usize,
        bhi: usize,
    ) -> (usize, usize, usize) {
        let (mut besti, mut bestj, mut bestsize) = (alo, blo, 0usize);
        let mut j2len: HashMap<usize, usize> = HashMap::new();
        for (i, ac) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut newj2len: HashMap<usize, usize> = HashMap::new();
            if let Some(indices) = b2j.get(ac) {
                for &j in indices {
                    if j < blo {
                        continue;
                    }
                    if j >= bhi {
                        break;
                    }
                    let k = j2len.get(&j.wrapping_sub(1)).copied().unwrap_or(0) + 1;
                    newj2len.insert(j, k);
                    if k > bestsize {
                        besti = i + 1 - k;
                        bestj = j + 1 - k;
                        bestsize = k;
                    }
                }
            }
            j2len = newj2len;
        }
        (besti, bestj, bestsize)
    }

    let mut matches = 0usize;
    let mut queue = vec![(0usize, a.len(), 0usize, b.len())];
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (i, j, k) = longest_match(&a, &b2j, alo, ahi, blo, bhi);
        if k > 0 {
            matches += k;
            queue.push((alo, i, blo, j));
            queue.push((i + k, ahi, j + k, bhi));
        }
    }
    2.0 * matches as f64 / (a.len() + b.len()) as f64
}

pub fn search_fuzzy(db: &Connection, query: &SearchQuery) -> rusqlite::Result<Vec<Hit>> {
    if query.text.chars().any(char::is_whitespace) {
        return Ok(Vec::new());
    }
    register_search_functions(db)?;
    let mut sql = String::from(
        "SELECT s.name, s.file_path, s.lineno, s.type FROM symbols s \
         JOIN files f ON f.path = s.file_path WHERE 1=1 ",
    );
    let mut params: Vec<String> = Vec::new();
    append_search_filters(&mut sql, &mut params, query, None, "s", "f");
    let rows = plain_rows(db, &sql, &params)?;
    let query_folded = query.text.to_lowercase();
    let mut results: Vec<Hit> = Vec::new();
    let mut seen: HashSet<(String, String)> = HashSet::new();
    for (name, fpath, lineno, stype) in rows {
        let key = (fpath.clone(), name.clone());
        if !seen.insert(key) {
            continue;
        }
        let score = sequence_ratio(&query_folded, &name.to_lowercase());
        if score >= FUZZY_CUTOFF {
            results.push((name, fpath, lineno, stype, score));
        }
    }
    results.sort_by(|a, b| {
        b.4.partial_cmp(&a.4)
            .unwrap_or(Ordering::Equal)
            .then_with(|| cmp_hit_tail(a, b))
    });
    results.truncate(query.limit.max(0) as usize);
    Ok(results)
}

pub const CHANNEL_PRIORITY: &[(&str, i64)] =
    &[("exact", 0), ("prefix", 1), ("bm25", 2), ("fuzzy", 3)];

fn channel_priority(channel: &str) -> i64 {
    CHANNEL_PRIORITY
        .iter()
        .find(|(name, _)| *name == channel)
        .map(|(_, p)| *p)
        .unwrap_or(i64::MAX)
}

#[derive(Debug, Clone)]
pub struct Merged {
    pub name: String,
    pub file_path: String,
    pub lineno: Option<i64>,
    pub symbol_type: String,
    pub sources: Vec<(String, i64)>,
    pub priority: i64,
    pub best_rank: i64,
}

pub fn merge_candidates(channel_results: &[(&str, Vec<Hit>)], limit: i64) -> Vec<Merged> {
    let mut merged: HashMap<(String, String), usize> = HashMap::new();
    let mut items: Vec<Merged> = Vec::new();
    for (channel, rows) in channel_results {
        let priority = channel_priority(channel);
        for (rank, (name, fpath, lineno, stype, _score)) in rows.iter().enumerate() {
            let rank = rank as i64 + 1;
            let key = (fpath.clone(), name.clone());
            let index = *merged.entry(key).or_insert_with(|| {
                items.push(Merged {
                    name: name.clone(),
                    file_path: fpath.clone(),
                    lineno: *lineno,
                    symbol_type: stype.clone(),
                    sources: Vec::new(),
                    priority,
                    best_rank: rank,
                });
                items.len() - 1
            });
            items[index].sources.push((channel.to_string(), rank));
        }
    }
    items.sort_by(|a, b| {
        a.priority
            .cmp(&b.priority)
            .then_with(|| a.best_rank.cmp(&b.best_rank))
            .then_with(|| py_str_cmp(&a.name.to_lowercase(), &b.name.to_lowercase()))
            .then_with(|| py_str_cmp(&a.name, &b.name))
            .then_with(|| py_str_cmp(&a.file_path, &b.file_path))
            .then_with(|| a.lineno.unwrap_or(0).cmp(&b.lineno.unwrap_or(0)))
    });
    items.truncate(limit.max(0) as usize);
    items
}

fn result_detail(db: &Connection, file_path: &str, name: &str) -> String {
    let row: Option<(Option<String>, Option<String>)> = db
        .query_row(
            "SELECT signature, summary_short FROM retrieval_documents \
             WHERE node_kind = 'symbol' AND node_ref = ?1",
            [format!("{file_path}::{name}")],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .ok();
    let Some((signature, summary)) = row else {
        return String::new();
    };
    let mut parts: Vec<String> = Vec::new();
    if let Some(signature) = signature.filter(|s| !s.is_empty()) {
        parts.push(format!("sig: ({signature})"));
    }
    if let Some(summary) = summary.filter(|s| !s.is_empty()) {
        parts.push(format!("summary: {summary}"));
    }
    parts.join(" | ")
}

fn fts_available(db: &Connection) -> rusqlite::Result<bool> {
    let row: Option<String> = db
        .query_row(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='retrieval_fts'",
            [],
            |row| row.get(0),
        )
        .ok();
    Ok(row.is_some())
}

fn channel_error(channel: &str) -> String {
    format!("Error: {channel} search failed (SqliteError).")
}

#[allow(clippy::too_many_arguments)]
pub fn query_search_impl(
    cfg: &McpConfig,
    text: &str,
    limit: i64,
    file_hint: &str,
    match_mode: &str,
    language: &str,
    symbol_type: &str,
    path_hint: &str,
) -> String {
    let query = match make_search_query(
        cfg,
        text,
        limit,
        file_hint,
        match_mode,
        language,
        symbol_type,
        path_hint,
    ) {
        Ok(query) => query,
        Err(error) => return format!("Error: {error}."),
    };

    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    match fts_available(&db) {
        Ok(true) => {}
        Ok(false) => {
            return "Error: FTS index not available. Run struct_scan to rebuild the index."
                .to_string()
        }
        Err(_) => return channel_error("FTS"),
    }

    let mut deterministic: Vec<(&str, Vec<Hit>)> = Vec::new();
    for (channel, label) in [("exact", "EXACT"), ("prefix", "LIKE"), ("bm25", "FTS")] {
        let rows = match channel {
            "exact" => search_exact(&db, &query),
            "prefix" => search_like(&db, &query),
            _ => search_fts(&db, &query),
        };
        match rows {
            Ok(rows) => deterministic.push((channel, rows)),
            Err(_) => return channel_error(label),
        }
    }
    let mut results = merge_candidates(&deterministic, query.limit);
    let mut search_level = "union";

    if results.is_empty() {
        let fuzzy_rows = match search_fuzzy(&db, &query) {
            Ok(rows) => rows,
            Err(_) => return channel_error("fuzzy"),
        };
        results = merge_candidates(&[("fuzzy", fuzzy_rows)], query.limit);
        search_level = "fuzzy";
    }

    if results.is_empty() {
        return format!("No symbols found matching '{}'", query.text);
    }

    let mut lines = vec![format!(
        "search results for '{}' ({} results, matched via {search_level})\n",
        query.text,
        results.len()
    )];
    for item in &results {
        let layer = get_layer(&db, &item.file_path);
        let loc = match truthy_line(item.lineno) {
            Some(lineno) => format!("L{lineno}"),
            None => String::new(),
        };
        lines.push(format!(
            "  [{}] {}::{}  {}:{} ({})",
            item.symbol_type, item.file_path, item.name, item.file_path, loc, layer
        ));
        let sources = item
            .sources
            .iter()
            .map(|(channel, rank)| format!("{channel}#{rank}"))
            .collect::<Vec<_>>()
            .join(", ");
        lines.push(format!(
            "        sources: {sources} | priority={}",
            item.priority
        ));
        let detail = result_detail(&db, &item.file_path, &item.name);
        if !detail.is_empty() {
            lines.push(format!("        {detail}"));
        }
    }
    lines.join("\n")
}
