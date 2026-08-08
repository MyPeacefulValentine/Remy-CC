//! Single-line JSON logging with size-threshold rotation (design guideline
//! §5.4). Thresholds are hardcoded constants (no env), injected through the
//! constructor so tests can exercise rotation with small values.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::UNIX_EPOCH;

use serde_json::{json, Value};

use crate::clock::Clock;

pub const DEFAULT_MAX_LOG_BYTES: u64 = 5 * 1024 * 1024;
pub const LOG_FILE: &str = "daemon.log";
pub const ROTATED_LOG_FILE: &str = "daemon.log.1";

pub struct JsonLogger {
    path: PathBuf,
    rotated_path: PathBuf,
    max_bytes: u64,
    clock: Arc<dyn Clock>,
}

impl JsonLogger {
    pub fn new(log_dir: &Path, max_bytes: u64, clock: Arc<dyn Clock>) -> io::Result<Self> {
        fs::create_dir_all(log_dir)?;
        Ok(Self {
            path: log_dir.join(LOG_FILE),
            rotated_path: log_dir.join(ROTATED_LOG_FILE),
            max_bytes,
            clock,
        })
    }

    /// Append one JSON record as a single line. `extra` must be a JSON object
    /// (or `Value::Null` for no extra fields); its entries are merged into the
    /// record. serde_json escapes embedded newlines, so the one-line invariant
    /// holds for arbitrary field content.
    pub fn log(&self, level: &str, event: &str, extra: Value) -> io::Result<()> {
        let mut record = json!({
            "ts": self.epoch_millis(),
            "level": level,
            "event": event,
            "pid": std::process::id(),
        });
        if let (Some(fields), Value::Object(extra_fields)) = (record.as_object_mut(), extra) {
            for (key, value) in extra_fields {
                fields.insert(key, value);
            }
        }
        let mut line = record.to_string();
        line.push('\n');

        self.rotate_if_needed(line.len() as u64)?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        file.write_all(line.as_bytes())
    }

    fn epoch_millis(&self) -> u64 {
        self.clock
            .now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }

    fn rotate_if_needed(&self, incoming_bytes: u64) -> io::Result<()> {
        let current = match fs::metadata(&self.path) {
            Ok(meta) => meta.len(),
            Err(_) => return Ok(()),
        };
        if current == 0 || current + incoming_bytes <= self.max_bytes {
            return Ok(());
        }
        if self.rotated_path.exists() {
            fs::remove_file(&self.rotated_path)?;
        }
        fs::rename(&self.path, &self.rotated_path)
    }
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime};

    use super::*;
    use crate::clock::fake::FakeClock;

    const TEST_EPOCH_SECS: u64 = 1_754_000_000;

    fn logger_in(dir: &Path, max_bytes: u64) -> (JsonLogger, Arc<FakeClock>) {
        let start = SystemTime::UNIX_EPOCH + Duration::from_secs(TEST_EPOCH_SECS);
        let clock = Arc::new(FakeClock::new(start));
        let logger = JsonLogger::new(dir, max_bytes, clock.clone() as Arc<dyn Clock>).unwrap();
        (logger, clock)
    }

    fn read_lines(path: &Path) -> Vec<String> {
        fs::read_to_string(path)
            .unwrap()
            .lines()
            .map(str::to_owned)
            .collect()
    }

    #[test]
    fn every_line_is_parseable_json_with_required_fields() {
        let dir = tempfile::tempdir().unwrap();
        let (logger, _clock) = logger_in(dir.path(), DEFAULT_MAX_LOG_BYTES);

        logger
            .log("info", "daemon_started", json!({"version": "0.1.0"}))
            .unwrap();
        logger
            .log(
                "warn",
                "multi\nline \u{4e2d}\u{6587} \"quoted\"",
                Value::Null,
            )
            .unwrap();

        let lines = read_lines(&dir.path().join(LOG_FILE));
        assert_eq!(lines.len(), 2);
        for line in &lines {
            let value: Value = serde_json::from_str(line).unwrap();
            assert_eq!(value["ts"].as_u64(), Some(TEST_EPOCH_SECS * 1000));
            assert!(value["level"].is_string());
            assert!(value["event"].is_string());
            assert_eq!(value["pid"].as_u64(), Some(u64::from(std::process::id())));
        }
        let first: Value = serde_json::from_str(&lines[0]).unwrap();
        assert_eq!(first["version"], "0.1.0");
        let second: Value = serde_json::from_str(&lines[1]).unwrap();
        assert_eq!(second["event"], "multi\nline \u{4e2d}\u{6587} \"quoted\"");
    }

    #[test]
    fn timestamp_follows_injected_clock() {
        let dir = tempfile::tempdir().unwrap();
        let (logger, clock) = logger_in(dir.path(), DEFAULT_MAX_LOG_BYTES);

        logger.log("info", "first", Value::Null).unwrap();
        clock.sleep(Duration::from_millis(2500));
        logger.log("info", "second", Value::Null).unwrap();

        let lines = read_lines(&dir.path().join(LOG_FILE));
        let first: Value = serde_json::from_str(&lines[0]).unwrap();
        let second: Value = serde_json::from_str(&lines[1]).unwrap();
        assert_eq!(
            second["ts"].as_u64().unwrap() - first["ts"].as_u64().unwrap(),
            2500
        );
    }

    #[test]
    fn rotation_bounds_file_count_and_size() {
        let dir = tempfile::tempdir().unwrap();
        let (logger, _clock) = logger_in(dir.path(), 256);

        for i in 0..200 {
            logger.log("info", "event", json!({"i": i})).unwrap();
        }

        let active = dir.path().join(LOG_FILE);
        let rotated = dir.path().join(ROTATED_LOG_FILE);
        assert!(active.exists());
        assert!(rotated.exists());
        let log_files: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(log_files.len(), 2);
        assert!(fs::metadata(&rotated).unwrap().len() <= 256);

        for path in [&active, &rotated] {
            for line in read_lines(path) {
                serde_json::from_str::<Value>(&line).unwrap();
            }
        }
    }

    #[test]
    fn no_rotation_below_threshold() {
        let dir = tempfile::tempdir().unwrap();
        let (logger, _clock) = logger_in(dir.path(), DEFAULT_MAX_LOG_BYTES);

        for _ in 0..10 {
            logger.log("info", "event", Value::Null).unwrap();
        }
        assert!(!dir.path().join(ROTATED_LOG_FILE).exists());
        assert_eq!(read_lines(&dir.path().join(LOG_FILE)).len(), 10);
    }

    #[test]
    fn oversized_single_entry_still_written_after_rotation() {
        let dir = tempfile::tempdir().unwrap();
        let (logger, _clock) = logger_in(dir.path(), 64);

        logger.log("info", "small", Value::Null).unwrap();
        let big = "x".repeat(300);
        logger.log("info", &big, Value::Null).unwrap();

        let lines = read_lines(&dir.path().join(LOG_FILE));
        assert_eq!(lines.len(), 1);
        let value: Value = serde_json::from_str(&lines[0]).unwrap();
        assert_eq!(value["event"].as_str().unwrap().len(), 300);
    }

    #[test]
    fn rotation_threshold_boundary_is_inclusive() {
        let dir = tempfile::tempdir().unwrap();
        let (logger, _clock) = logger_in(dir.path(), 100);
        fs::write(dir.path().join(LOG_FILE), vec![b'x'; 60]).unwrap();

        logger.rotate_if_needed(40).unwrap();
        assert!(!dir.path().join(ROTATED_LOG_FILE).exists());

        logger.rotate_if_needed(41).unwrap();
        assert!(dir.path().join(ROTATED_LOG_FILE).exists());
    }

    #[test]
    fn rotate_if_needed_without_log_file_is_noop() {
        let dir = tempfile::tempdir().unwrap();
        let (logger, _clock) = logger_in(dir.path(), 64);

        logger.rotate_if_needed(1024).unwrap();

        assert!(!dir.path().join(LOG_FILE).exists());
        assert!(!dir.path().join(ROTATED_LOG_FILE).exists());
    }

    #[test]
    fn extra_field_with_newline_unicode_quotes_stays_single_line() {
        let dir = tempfile::tempdir().unwrap();
        let (logger, _clock) = logger_in(dir.path(), DEFAULT_MAX_LOG_BYTES);

        logger
            .log(
                "info",
                "boundary_evt",
                json!({"msg": "路径 \"C:\\tmp\"\nsecond-line", "emoji": "🦀"}),
            )
            .unwrap();

        let content = fs::read_to_string(dir.path().join(LOG_FILE)).unwrap();
        let lines: Vec<&str> = content.lines().collect();
        assert_eq!(lines.len(), 1);
        let value: Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(value["emoji"], "🦀");
        assert!(value["msg"].as_str().unwrap().contains('\n'));
    }
}
