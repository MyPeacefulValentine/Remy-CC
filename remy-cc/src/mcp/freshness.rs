//! Startup index-staleness probe.
//! Oracle: index_mcp_server._resolve_git_head / _init_freshness. Runs once
//! before serving, like the Python server (which must probe before the event
//! loop for Windows asyncio reasons; the timing is kept for parity). Warning
//! strings are byte-identical. REMY_FRESHNESS_SAMPLE_SEED (H.4 test seam)
//! switches the hash-sampling fallback to a sorted, seed-rotated subset.

use std::path::Path;

use md5::{Digest, Md5};
use rusqlite::{Connection, OpenFlags};

use super::config::McpConfig;

fn git_output(args: &[&str], cwd: &Path) -> Option<String> {
    let output = std::process::Command::new("git")
        .args(args)
        .current_dir(cwd)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).to_string())
}

/// (head, cwd) of the first candidate directory where `git rev-parse` works.
fn resolve_git_head(root_dir: &Path, db: &Connection) -> Option<(String, std::path::PathBuf)> {
    let mut candidates = vec![root_dir.to_path_buf()];
    if let Ok(first_path) = db.query_row("SELECT path FROM files LIMIT 1", [], |row| {
        row.get::<_, String>(0)
    }) {
        let joined = root_dir.join(first_path);
        if let Some(parent) = joined.parent() {
            candidates.push(parent.to_path_buf());
        }
    }
    for candidate in candidates {
        if !candidate.is_dir() {
            continue;
        }
        if let Some(head) = git_output(&["rev-parse", "HEAD"], &candidate) {
            return Some((head.trim().to_string(), candidate));
        }
    }
    None
}

fn sample_files(all_files: &[(String, String)], sample_size: usize) -> Vec<(String, String)> {
    if let Ok(seed_raw) = std::env::var("REMY_FRESHNESS_SAMPLE_SEED") {
        let start: usize = seed_raw
            .parse::<i64>()
            .unwrap_or(0)
            .rem_euclid(all_files.len() as i64) as usize;
        let mut ordered: Vec<(String, String)> = all_files.to_vec();
        ordered.sort();
        return (0..sample_size)
            .map(|i| ordered[(start + i) % ordered.len()].clone())
            .collect();
    }
    // Sampling quality is irrelevant (staleness heuristic only): partial
    // Fisher-Yates over an LCG seeded from the clock.
    let mut state: u64 = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos() as u64 ^ d.as_secs())
        .unwrap_or(0x9e3779b9)
        | 1;
    let mut pool: Vec<(String, String)> = all_files.to_vec();
    let mut sample = Vec::with_capacity(sample_size);
    for i in 0..sample_size.min(pool.len()) {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let j = i + (state >> 33) as usize % (pool.len() - i);
        pool.swap(i, j);
        sample.push(pool[i].clone());
    }
    sample
}

pub fn init_freshness(cfg: &McpConfig) -> String {
    if !cfg.db_path.exists() {
        return String::new();
    }
    let Ok(db) = Connection::open_with_flags(
        &cfg.db_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) else {
        return String::new();
    };
    let _ = db.pragma_update(None, "busy_timeout", 5000);
    let _ = db.pragma_update(None, "journal_mode", "WAL");

    let stored: Option<String> = db
        .query_row(
            "SELECT value FROM meta WHERE key='source_commit'",
            [],
            |row| row.get(0),
        )
        .ok();
    let total: i64 = db
        .query_row("SELECT value FROM meta WHERE key='file_count'", [], |row| {
            row.get::<_, String>(0)
        })
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(1);

    let cwd = std::env::current_dir().unwrap_or_else(|_| ".".into());
    if let Some((head, git_cwd)) = resolve_git_head(&cwd, &db) {
        if let Some(stored) = &stored {
            if *stored == head {
                let Some(status) = git_output(&["status", "--porcelain"], &git_cwd) else {
                    return String::new();
                };
                let dirty = status
                    .lines()
                    .filter(|l| !l.trim().is_empty() && !l.starts_with("??"))
                    .count();
                if dirty == 0 {
                    return String::new();
                }
                let rate = dirty as f64 / total.max(1) as f64;
                if rate > 0.2 {
                    return format!(
                        "[Warning: index may be stale — {dirty} files modified since last scan. Consider running /remy-index.]"
                    );
                }
                return String::new();
            }
            return format!(
                "[Warning: index built at commit {}, current HEAD is {}. Run /remy-index to rebuild.]",
                &stored[..stored.len().min(8)],
                &head[..head.len().min(8)]
            );
        }
    }

    let mut stmt = match db.prepare("SELECT path, struct_hash FROM files") {
        Ok(stmt) => stmt,
        Err(_) => return String::new(),
    };
    let all_files: Vec<(String, String)> =
        match stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?))) {
            Ok(rows) => rows.filter_map(Result::ok).collect(),
            Err(_) => return String::new(),
        };
    if all_files.is_empty() {
        return String::new();
    }

    let sample_size = 10usize
        .min(((all_files.len() as f64) * 0.1).ceil() as usize)
        .max(1);
    let sample = sample_files(&all_files, sample_size);
    let mut mismatches = 0usize;
    for (path, stored_hash) in &sample {
        let file_path = Path::new(path);
        if !file_path.exists() {
            mismatches += 1;
            continue;
        }
        match std::fs::read(file_path)
            .ok()
            .and_then(|bytes| String::from_utf8(bytes).ok())
        {
            Some(content) => {
                let digest = Md5::digest(content.as_bytes());
                if format!("{digest:x}") != *stored_hash {
                    mismatches += 1;
                }
            }
            None => mismatches += 1,
        }
    }

    let rate = mismatches as f64 / sample_size as f64;
    if rate > 0.5 {
        format!(
            "[Warning: index may be stale — {mismatches}/{sample_size} sampled files differ. Run /remy-index to rebuild.]"
        )
    } else if rate > 0.2 {
        format!(
            "[Warning: index may be stale — {mismatches}/{sample_size} sampled files differ. Consider running /remy-index.]"
        )
    } else {
        String::new()
    }
}
