//! Project scan lock (index_state.InterProcessFileLock counterpart): same
//! `.claude/logic_index_scan.lock` file, same advisory OS semantics —
//! Python locks byte 0 via msvcrt/flock, this side locks via std
//! `File::try_lock`; mutual exclusion holds in both directions.

use std::fs::{File, OpenOptions, TryLockError};
use std::io::{Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// index_state.SCAN_LOCK_FILE, relative to the project root.
pub const SCAN_LOCK_FILE: &str = ".claude/logic_index_scan.lock";

const POLL_INTERVAL: Duration = Duration::from_millis(50);

/// Held project scan lock; released on drop and by the OS on process exit
/// (what makes kill-based cancellation safe).
#[derive(Debug)]
pub struct ScanLock {
    file: File,
    pub path: PathBuf,
}

impl Drop for ScanLock {
    fn drop(&mut self) {
        let _ = self.file.unlock();
    }
}

/// Acquire the advisory scan lock under `<root>/.claude/`, polling until
/// `timeout_secs` elapses. A zero timeout means a single attempt.
pub fn acquire_project_scan_lock(root_dir: &Path, timeout_secs: f64) -> Result<ScanLock, String> {
    let path = root_dir.join(SCAN_LOCK_FILE);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("cannot create lock directory {}: {e}", parent.display()))?;
    }
    let mut file = OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(&path)
        .map_err(|e| format!("cannot open lock file {}: {e}", path.display()))?;
    // The Python locker locks byte 0 with length 1, so the file must hold
    // at least one byte.
    if file
        .seek(SeekFrom::End(0))
        .map_err(|e| format!("lock file seek failed: {e}"))?
        == 0
    {
        file.write_all(b"0")
            .map_err(|e| format!("lock file init failed: {e}"))?;
        file.flush()
            .map_err(|e| format!("lock file flush failed: {e}"))?;
    }
    let deadline = Instant::now() + Duration::from_secs_f64(timeout_secs.max(0.0));
    loop {
        match file.try_lock() {
            Ok(()) => return Ok(ScanLock { file, path }),
            Err(TryLockError::WouldBlock) => {}
            Err(TryLockError::Error(e)) => {
                return Err(format!("lock attempt on {} failed: {e}", path.display()))
            }
        }
        let now = Instant::now();
        if now >= deadline {
            return Err(format!(
                "Timed out acquiring project scan lock: {}",
                path.display()
            ));
        }
        std::thread::sleep(POLL_INTERVAL.min(deadline - now));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn acquire_seeds_lock_byte_and_releases_on_drop() {
        let dir = tempfile::tempdir().unwrap();
        {
            let lock = acquire_project_scan_lock(dir.path(), 0.0).unwrap();
            assert!(lock.path.ends_with("logic_index_scan.lock"));
            assert_eq!(std::fs::metadata(&lock.path).unwrap().len(), 1);
        }
        // A fresh handle can lock again once the guard is dropped.
        let relocked = acquire_project_scan_lock(dir.path(), 0.0).unwrap();
        drop(relocked);
    }

    #[test]
    fn second_handle_times_out_while_held() {
        let dir = tempfile::tempdir().unwrap();
        let _held = acquire_project_scan_lock(dir.path(), 0.0).unwrap();
        let started = Instant::now();
        let error = acquire_project_scan_lock(dir.path(), 0.2).unwrap_err();
        assert!(
            error.contains("Timed out acquiring project scan lock"),
            "{error}"
        );
        assert!(started.elapsed() >= Duration::from_millis(150));
    }

    #[test]
    fn zero_timeout_is_a_single_attempt() {
        let dir = tempfile::tempdir().unwrap();
        let _held = acquire_project_scan_lock(dir.path(), 0.0).unwrap();
        let started = Instant::now();
        assert!(acquire_project_scan_lock(dir.path(), 0.0).is_err());
        assert!(started.elapsed() < Duration::from_millis(500));
    }
}
