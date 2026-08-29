//! Intent navigation: bounded candidates, judge_cache, LLM ranking.
//! Oracle: remy-src/index_mcp_navigate.py. The cache key must be byte-equal
//! to Python's json.dumps(payload, sort_keys=True, separators=(",", ":"),
//! ensure_ascii=False) fed through SHA-256 — keys are therefore inserted in
//! sorted order explicitly. The LLM miss path is excluded from the H.4
//! baseline; it replicates llm_client.py behaviorally (retry, circuit codes,
//! fence stripping, truncation detection), with fatal HTTP codes surfacing as
//! an "Error:" string instead of a raised exception.

use std::cmp::Ordering;
use std::collections::HashMap;

use rusqlite::Connection;
use sha2::{Digest, Sha256};

use super::common::{get_latest_summary, open_db, DB_NOT_FOUND};
use super::config::McpConfig;
use super::search::{
    fts_expression, make_search_query, merge_candidates, register_search_functions, search_exact,
    search_fts, search_fuzzy, search_like, Hit, SearchQuery,
};

const NAVIGATE_PROMPT_VERSION: &str = "p1_4.1";
const NAVIGATE_DOC_COLUMNS: &str =
    "{name name_tokens signature file_path summary_short summary_full}";
const NAVIGATE_DOC_WEIGHTS: &str = "bm25(retrieval_fts, 1.0, 1.0, 0.0, 0.5, 5.0, 1.0)";

fn kind_order(kind: &str) -> i64 {
    match kind {
        "symbol" => 0,
        "file" => 1,
        _ => 2,
    }
}

fn normalize_intent(intent: &str) -> String {
    intent
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

#[derive(Debug, Clone)]
struct Candidate {
    kind: String,
    node_ref: String,
    cluster: String,
    file: Option<String>,
    symbol: Option<String>,
    short: String,
    content_hash: String,
    sources: Vec<(String, i64)>,
}

fn file_cluster_map(db: &Connection) -> HashMap<String, String> {
    let Ok(mut stmt) = db.prepare(
        "SELECT cm.file_path, c.name FROM cluster_members cm \
         JOIN clusters c ON c.id = cm.cluster_id",
    ) else {
        return HashMap::new();
    };
    stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .map(|rows| rows.filter_map(Result::ok).collect())
        .unwrap_or_default()
}

fn navigate_doc_rows(
    db: &Connection,
    query: &SearchQuery,
    kind: &str,
    row_limit: i64,
) -> Vec<(String, Option<String>, Option<String>)> {
    let sql = format!(
        "SELECT d.node_ref, d.summary_short, d.content_hash, {NAVIGATE_DOC_WEIGHTS} AS rank \
         FROM retrieval_fts \
         JOIN retrieval_documents d ON d.doc_id = retrieval_fts.rowid \
         WHERE retrieval_fts MATCH ? AND d.node_kind = ? \
         ORDER BY rank, d.node_ref LIMIT ?"
    );
    let expression = fts_expression(query, None);
    let match_param = format!("{NAVIGATE_DOC_COLUMNS} : ({expression})");
    let Ok(mut stmt) = db.prepare(&sql) else {
        return Vec::new();
    };
    stmt.query_map(rusqlite::params![match_param, kind, row_limit], |row| {
        Ok((row.get(0)?, row.get(1)?, row.get(2)?))
    })
    .map(|rows| rows.filter_map(Result::ok).collect())
    .unwrap_or_default()
}

fn navigate_symbol_rows(db: &Connection, query: &SearchQuery) -> Vec<super::search::Merged> {
    let mut channels: Vec<(&str, Vec<Hit>)> = Vec::new();
    for (channel, search) in [
        (
            "exact",
            search_exact as fn(&Connection, &SearchQuery) -> rusqlite::Result<Vec<Hit>>,
        ),
        ("prefix", search_like),
        ("bm25", search_fts),
    ] {
        channels.push((channel, search(db, query).unwrap_or_default()));
    }
    let merged = merge_candidates(&channels, query.limit);
    if merged.is_empty() && query.words.len() == 1 {
        let fuzzy = search_fuzzy(db, query).unwrap_or_default();
        return merge_candidates(&[("fuzzy", fuzzy)], query.limit);
    }
    merged
}

fn navigate_symbol_docs(
    db: &Connection,
    refs: &[String],
) -> HashMap<String, (String, Option<String>)> {
    let mut docs = HashMap::new();
    for node_ref in refs {
        let row: Option<(Option<String>, Option<String>)> = db
            .query_row(
                "SELECT content_hash, summary_short FROM retrieval_documents \
                 WHERE node_kind = 'symbol' AND node_ref = ?1",
                [node_ref],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .ok();
        let value = match row {
            Some((chash, short)) => (chash.unwrap_or_default(), short),
            None => (String::new(), None),
        };
        docs.insert(node_ref.clone(), value);
    }
    docs
}

fn navigate_candidates(db: &Connection, cfg: &McpConfig, intent: &str) -> Vec<Candidate> {
    let symbol_limit = cfg.nav_symbols.min(cfg.result_limit);
    let Ok(symbol_query) = make_search_query(cfg, intent, symbol_limit, "", "any", "", "", "")
    else {
        return Vec::new();
    };
    if register_search_functions(db).is_err() {
        return Vec::new();
    }
    let file_to_cluster = file_cluster_map(db);
    let mut candidates = Vec::new();

    for (kind, quota) in [("cluster", cfg.nav_clusters), ("file", cfg.nav_files)] {
        let rows = navigate_doc_rows(db, &symbol_query, kind, quota);
        for (position, (node_ref, short, chash)) in rows.into_iter().enumerate() {
            let cluster = if kind == "cluster" {
                node_ref.clone()
            } else {
                file_to_cluster
                    .get(&node_ref)
                    .cloned()
                    .unwrap_or_else(|| "(unclustered)".to_string())
            };
            candidates.push(Candidate {
                kind: kind.to_string(),
                node_ref: node_ref.clone(),
                cluster,
                file: if kind == "cluster" {
                    None
                } else {
                    Some(node_ref)
                },
                symbol: None,
                short: short.unwrap_or_default(),
                content_hash: chash.unwrap_or_default(),
                sources: vec![("bm25".to_string(), position as i64 + 1)],
            });
        }
    }

    let merged = navigate_symbol_rows(db, &symbol_query);
    let merged = &merged[..merged.len().min(cfg.nav_symbols.max(0) as usize)];
    let refs: Vec<String> = merged
        .iter()
        .map(|item| format!("{}::{}", item.file_path, item.name))
        .collect();
    let docs = navigate_symbol_docs(db, &refs);
    for (item, node_ref) in merged.iter().zip(&refs) {
        let (chash, short) = docs.get(node_ref).cloned().unwrap_or_default();
        candidates.push(Candidate {
            kind: "symbol".to_string(),
            node_ref: node_ref.clone(),
            cluster: file_to_cluster
                .get(&item.file_path)
                .cloned()
                .unwrap_or_else(|| "(unclustered)".to_string()),
            file: Some(item.file_path.clone()),
            symbol: Some(item.name.clone()),
            short: short.unwrap_or_default(),
            content_hash: chash,
            sources: item.sources.clone(),
        });
    }
    candidates
}

struct ClusterCorpusEntry {
    name: String,
    label: Option<String>,
    short: Option<String>,
}

fn collect_cluster_corpus(db: &Connection) -> Vec<ClusterCorpusEntry> {
    let Ok(mut stmt) = db.prepare("SELECT name, label FROM clusters ORDER BY file_count DESC")
    else {
        return Vec::new();
    };
    let rows: Vec<(String, Option<String>)> = stmt
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .map(|rows| rows.filter_map(Result::ok).collect())
        .unwrap_or_default();
    rows.into_iter()
        .map(|(name, label)| {
            let short = get_latest_summary(db, "cluster", &name).and_then(|s| s.short);
            ClusterCorpusEntry { name, label, short }
        })
        .collect()
}

fn cluster_fallback_candidates(db: &Connection, clusters: &[ClusterCorpusEntry]) -> Vec<Candidate> {
    clusters
        .iter()
        .map(|cluster| {
            let chash: Option<String> = db
                .query_row(
                    "SELECT content_hash FROM retrieval_documents \
                     WHERE node_kind = 'cluster' AND node_ref = ?1",
                    [&cluster.name],
                    |row| row.get(0),
                )
                .ok();
            let short = cluster
                .short
                .clone()
                .filter(|s| !s.is_empty())
                .or_else(|| cluster.label.clone().filter(|l| !l.is_empty()))
                .unwrap_or_default();
            Candidate {
                kind: "cluster".to_string(),
                node_ref: cluster.name.clone(),
                cluster: cluster.name.clone(),
                file: None,
                symbol: None,
                short,
                content_hash: chash.unwrap_or_default(),
                sources: Vec::new(),
            }
        })
        .collect()
}

/// json.dumps(payload, sort_keys=True, separators=(",", ":"),
/// ensure_ascii=False) — keys inserted in sorted order, compact output.
fn navigate_cache_key(intent: &str, top_k: i64, candidates: &[Candidate]) -> String {
    let candidate_rows: Vec<serde_json::Value> = candidates
        .iter()
        .map(|entry| serde_json::json!([entry.kind, entry.node_ref, entry.content_hash]))
        .collect();
    let mut payload = serde_json::Map::new();
    payload.insert(
        "candidates".to_string(),
        serde_json::Value::Array(candidate_rows),
    );
    payload.insert(
        "intent".to_string(),
        serde_json::Value::String(normalize_intent(intent)),
    );
    payload.insert(
        "template".to_string(),
        serde_json::Value::String(NAVIGATE_PROMPT_VERSION.to_string()),
    );
    payload.insert("top_k".to_string(), serde_json::Value::Number(top_k.into()));
    let encoded = serde_json::Value::Object(payload).to_string();
    let mut hasher = Sha256::new();
    hasher.update(encoded.as_bytes());
    format!("navigate:{:x}", hasher.finalize())
}

fn build_navigate_prompt(intent: &str, candidates: &[Candidate], top_k: i64) -> String {
    let candidate_rows: Vec<serde_json::Value> = candidates
        .iter()
        .map(|entry| {
            let mut row = serde_json::Map::new();
            row.insert("kind".to_string(), entry.kind.clone().into());
            row.insert("cluster".to_string(), entry.cluster.clone().into());
            row.insert(
                "file".to_string(),
                entry
                    .file
                    .clone()
                    .map(Into::into)
                    .unwrap_or(serde_json::Value::Null),
            );
            row.insert(
                "symbol".to_string(),
                entry
                    .symbol
                    .clone()
                    .map(Into::into)
                    .unwrap_or(serde_json::Value::Null),
            );
            row.insert("short".to_string(), entry.short.clone().into());
            serde_json::Value::Object(row)
        })
        .collect();
    let mut payload = serde_json::Map::new();
    payload.insert("intent".to_string(), intent.into());
    payload.insert("top_k".to_string(), top_k.into());
    payload.insert(
        "candidates".to_string(),
        serde_json::Value::Array(candidate_rows),
    );
    let body =
        serde_json::to_string_pretty(&serde_json::Value::Object(payload)).unwrap_or_default();
    format!(
        "Task: Rank the candidate code locations by relevance to the given intent. \
         Choose only from the provided candidates. \
         Return a JSON array of <= top_k entries, each \
         {{\"cluster\": str, \"file\": str|null, \"symbol\": str|null, \
         \"relevance_score\": float in [0,1], \"rationale\": str}}.\n\
         Higher scores indicate stronger match. Output JSON only, no prose.\n\n{body}"
    )
}

#[derive(Debug, Clone)]
struct Ranked {
    cluster: String,
    file: Option<String>,
    symbol: Option<String>,
    relevance_score: f64,
    rationale: String,
}

fn ranked_from_value(value: &serde_json::Value) -> Option<Ranked> {
    let entry = value.as_object()?;
    let cluster = entry.get("cluster")?.as_str()?.to_string();
    let score = match entry.get("relevance_score") {
        Some(serde_json::Value::Number(number)) => number.as_f64().unwrap_or(0.0),
        Some(serde_json::Value::String(text)) => text.parse().unwrap_or(0.0),
        _ => 0.0,
    };
    Some(Ranked {
        cluster,
        file: entry
            .get("file")
            .and_then(|v| v.as_str())
            .map(str::to_string),
        symbol: entry
            .get("symbol")
            .and_then(|v| v.as_str())
            .map(str::to_string),
        relevance_score: score.clamp(0.0, 1.0),
        rationale: entry
            .get("rationale")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
    })
}

fn parse_navigate_response(raw: &str, top_k: i64) -> Vec<Ranked> {
    if raw.starts_with("Error:") {
        return Vec::new();
    }
    let Ok(data) = serde_json::from_str::<serde_json::Value>(raw) else {
        return Vec::new();
    };
    let Some(entries) = data.as_array() else {
        return Vec::new();
    };
    let mut cleaned: Vec<Ranked> = entries
        .iter()
        .take(top_k.max(0) as usize)
        .filter_map(ranked_from_value)
        .collect();
    cleaned.sort_by(|a, b| {
        b.relevance_score
            .partial_cmp(&a.relevance_score)
            .unwrap_or(Ordering::Equal)
    });
    cleaned
}

fn ranked_to_json(ranked: &[Ranked]) -> String {
    let rows: Vec<serde_json::Value> = ranked
        .iter()
        .map(|entry| {
            let mut row = serde_json::Map::new();
            row.insert("cluster".to_string(), entry.cluster.clone().into());
            row.insert(
                "file".to_string(),
                entry
                    .file
                    .clone()
                    .map(Into::into)
                    .unwrap_or(serde_json::Value::Null),
            );
            row.insert(
                "symbol".to_string(),
                entry
                    .symbol
                    .clone()
                    .map(Into::into)
                    .unwrap_or(serde_json::Value::Null),
            );
            row.insert(
                "relevance_score".to_string(),
                serde_json::Number::from_f64(entry.relevance_score)
                    .map(serde_json::Value::Number)
                    .unwrap_or(serde_json::Value::Null),
            );
            row.insert("rationale".to_string(), entry.rationale.clone().into());
            serde_json::Value::Object(row)
        })
        .collect();
    serde_json::Value::Array(rows).to_string()
}

fn ranked_from_cache(raw: &str) -> Option<Vec<Ranked>> {
    let data = serde_json::from_str::<serde_json::Value>(raw).ok()?;
    let entries = data.as_array()?;
    Some(
        entries
            .iter()
            .filter_map(|value| {
                let entry = value.as_object()?;
                Some(Ranked {
                    cluster: entry
                        .get("cluster")
                        .and_then(|v| v.as_str())
                        .unwrap_or("?")
                        .to_string(),
                    file: entry
                        .get("file")
                        .and_then(|v| v.as_str())
                        .map(str::to_string),
                    symbol: entry
                        .get("symbol")
                        .and_then(|v| v.as_str())
                        .map(str::to_string),
                    relevance_score: entry
                        .get("relevance_score")
                        .and_then(|v| v.as_f64())
                        .unwrap_or(0.0),
                    rationale: entry
                        .get("rationale")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                })
            })
            .collect(),
    )
}

fn heuristic_navigate(candidates: &[Candidate], top_k: i64) -> Vec<Ranked> {
    let mut ordered: Vec<usize> = (0..candidates.len()).collect();
    ordered.sort_by_key(|&index| (kind_order(&candidates[index].kind), index));
    ordered
        .into_iter()
        .take(top_k.max(0) as usize)
        .map(|index| {
            let entry = &candidates[index];
            let sources = if entry.sources.is_empty() {
                "cluster-fallback".to_string()
            } else {
                entry
                    .sources
                    .iter()
                    .map(|(channel, rank)| format!("{channel}#{rank}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            };
            Ranked {
                cluster: entry.cluster.clone(),
                file: entry.file.clone(),
                symbol: entry.symbol.clone(),
                relevance_score: 0.0,
                rationale: format!("sources: {sources}"),
            }
        })
        .collect()
}

fn format_navigate(ranked: &[Ranked], intent: &str, source: &str) -> String {
    if ranked.is_empty() {
        return format!("No matches for intent '{intent}' (source={source}).");
    }
    let mut lines = vec![format!(
        "## Navigate results for '{intent}' (top {}, source={source})\n",
        ranked.len()
    )];
    for (i, entry) in ranked.iter().enumerate() {
        let mut path = entry.cluster.clone();
        if let Some(file) = entry.file.as_deref().filter(|f| !f.is_empty()) {
            path.push_str(&format!(" / {file}"));
        }
        if let Some(symbol) = entry.symbol.as_deref().filter(|s| !s.is_empty()) {
            path.push_str(&format!(" :: {symbol}"));
        }
        lines.push(format!("{}. [{:.2}] {path}", i + 1, entry.relevance_score));
        if !entry.rationale.is_empty() {
            lines.push(format!("   - {}", entry.rationale));
        }
    }
    lines.join("\n")
}

fn local_timestamp_seconds() -> String {
    // datetime.now().isoformat(timespec="seconds") equivalent; delegated to
    // SQLite to avoid a chrono dependency (created_at is audit-only).
    let conn = Connection::open_in_memory();
    if let Ok(conn) = conn {
        if let Ok(value) = conn.query_row(
            "SELECT strftime('%Y-%m-%dT%H:%M:%S','now','localtime')",
            [],
            |row| row.get::<_, String>(0),
        ) {
            return value;
        }
    }
    String::new()
}

pub async fn query_navigate_impl(cfg: &McpConfig, intent: &str, top_k: i64) -> String {
    let Some(db) = open_db(&cfg.db_path) else {
        return DB_NOT_FOUND.to_string();
    };
    if intent.trim().is_empty() {
        return "Error: intent must not be empty.".to_string();
    }
    let top_k = top_k.clamp(1, 20);

    let mut candidates = navigate_candidates(&db, cfg, intent);
    let llm_enabled = !cfg.llm_api_key.is_empty();

    let mut source = "llm";
    if candidates.is_empty() {
        let clusters = collect_cluster_corpus(&db);
        let has_files: Option<i64> = db
            .query_row("SELECT 1 FROM files LIMIT 1", [], |row| row.get(0))
            .ok();
        if clusters.is_empty() && has_files.is_none() {
            return "No clusters or files indexed; run /remy-index first.".to_string();
        }
        if !llm_enabled || clusters.is_empty() {
            return format!("No matches for intent '{intent}' (source=heuristic).");
        }
        candidates = cluster_fallback_candidates(&db, &clusters);
        source = "llm-cluster-only";
    }

    let cache_key = navigate_cache_key(intent, top_k, &candidates);
    let cached: Option<String> = db
        .query_row(
            "SELECT result FROM judge_cache WHERE payload_hash = ?1",
            [&cache_key],
            |row| row.get(0),
        )
        .ok();
    if let Some(raw) = cached {
        if let Some(ranked) = ranked_from_cache(&raw) {
            return format_navigate(&ranked, intent, "cache");
        }
    }

    if !llm_enabled {
        let ranked = heuristic_navigate(&candidates, top_k);
        return format_navigate(&ranked, intent, "heuristic");
    }

    let prompt = build_navigate_prompt(intent, &candidates, top_k);
    let raw = llm_call(cfg, &prompt).await;
    let ranked = parse_navigate_response(&raw, top_k);
    if ranked.is_empty() {
        let ranked = heuristic_navigate(&candidates, top_k);
        return format_navigate(&ranked, intent, "heuristic-fallback");
    }

    let _ = db.execute(
        "INSERT OR REPLACE INTO judge_cache (payload_hash, result, created_at) VALUES (?1,?2,?3)",
        rusqlite::params![
            cache_key,
            ranked_to_json(&ranked),
            local_timestamp_seconds()
        ],
    );
    format_navigate(&ranked, intent, source)
}

const RETRY_BACKOFF_CAP_SECONDS: u64 = 60;
const LLM_USER_AGENT: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36";

fn strip_fences(content: &str) -> String {
    if let Some(rest) = content.split("```json").nth(1) {
        return rest.split("```").next().unwrap_or("").trim().to_string();
    }
    if content.contains("```") {
        return content.split("```").nth(1).unwrap_or("").trim().to_string();
    }
    content.to_string()
}

fn backoff_seconds(retries: u32) -> u64 {
    RETRY_BACKOFF_CAP_SECONDS.min(2u64.saturating_pow(retries))
}

/// llm_client.LlmClient.call replication (OpenAI-compatible single POST).
async fn llm_call(cfg: &McpConfig, prompt: &str) -> String {
    if cfg.llm_api_key.is_empty() {
        return "Error: REMY_LLM_API_KEY not set.".to_string();
    }
    let mut builder = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(cfg.llm_timeout.max(0) as u64));
    if cfg.llm_tls_insecure {
        builder = builder.danger_accept_invalid_certs(true);
    }
    let client = match builder.build() {
        Ok(client) => client,
        Err(error) => return format!("Error: {error}"),
    };

    let body = serde_json::json!({
        "model": cfg.llm_model,
        "messages": [
            {"role": "system", "content": "You are a code analysis assistant. Respond in English. Respond with valid JSON only."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2,
        "max_tokens": cfg.llm_max_tokens,
        "response_format": {"type": "json_object"}
    });

    let retry_limit = cfg.llm_retry_limit.max(0) as u32;
    let mut retries: u32 = 0;
    loop {
        let response = client
            .post(&cfg.llm_base_url)
            .header("Content-Type", "application/json")
            .header("Authorization", format!("Bearer {}", cfg.llm_api_key))
            .header("User-Agent", LLM_USER_AGENT)
            .json(&body)
            .send()
            .await;

        match response {
            Ok(response) => {
                let status = response.status().as_u16();
                if matches!(status, 401 | 403 | 429) {
                    return format!("Error: Fatal API Error {status}");
                }
                if matches!(status, 500 | 502 | 503 | 504) {
                    if retries < retry_limit {
                        retries += 1;
                        tokio::time::sleep(std::time::Duration::from_secs(backoff_seconds(
                            retries,
                        )))
                        .await;
                        continue;
                    }
                    return format!("Error: HTTP {status}");
                }
                let raw = match response.text().await {
                    Ok(raw) => raw,
                    Err(error) => return format!("Error: {error}"),
                };
                let Ok(result) = serde_json::from_str::<serde_json::Value>(&raw) else {
                    return "Error: Unexpected API response format.".to_string();
                };
                let Some(content) = result
                    .get("choices")
                    .and_then(|c| c.get(0))
                    .and_then(|c| c.get("message"))
                    .and_then(|m| m.get("content"))
                    .and_then(|c| c.as_str())
                else {
                    return "Error: Unexpected API response format.".to_string();
                };
                let text_content = strip_fences(content.trim());
                let trimmed = text_content.trim();
                if !trimmed.ends_with('}') && !trimmed.ends_with(']') {
                    if retries < retry_limit {
                        retries += 1;
                        continue;
                    }
                    return "Error: Response truncated (incomplete JSON)".to_string();
                }
                return text_content;
            }
            Err(error) => {
                let message = error.to_string();
                if message.contains("certificate") {
                    return format!(
                        "Error: TLS certificate verification failed ({message}); \
                         set REMY_LLM_TLS_INSECURE=true to bypass (insecure)"
                    );
                }
                if retries < retry_limit {
                    retries += 1;
                    tokio::time::sleep(std::time::Duration::from_secs(backoff_seconds(retries)))
                        .await;
                    continue;
                }
                return format!("Error: Network error ({message})");
            }
        }
    }
}
