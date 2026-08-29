//! Single-instance mutual exclusion via an OS advisory file lock
//! (`LockFileEx` on Windows, `flock` on Unix — std `File::try_lock`).
//! The lock is released by the OS when the holding process dies, so a
//! crashed daemon never leaves a stale lock (INV-R3).

use std::fs::{self, File, TryLockError};
use std::io;
use std::path::Path;

pub const LOCK_FILE: &str = "daemon.lock";
pub const PID_FILE: &str = "daemon.pid";

/// Holds the exclusive lock; the OS releases it when this is dropped
/// (or when the process terminates for any reason).
pub struct LockGuard {
    _file: File,
}

pub enum AcquireOutcome {
    Acquired(LockGuard),
    Held,
}

pub fn acquire(run_dir: &Path) -> io::Result<AcquireOutcome> {
    fs::create_dir_all(run_dir)?;
    let file = File::options()
        .create(true)
        .write(true)
        .truncate(false)
        .open(run_dir.join(LOCK_FILE))?;
    match file.try_lock() {
        Ok(()) => Ok(AcquireOutcome::Acquired(LockGuard { _file: file })),
        Err(TryLockError::WouldBlock) => Ok(AcquireOutcome::Held),
        Err(TryLockError::Error(err)) => Err(err),
    }
}

/// Probe whether some process currently holds the lock.
///
/// The probe briefly acquires and releases the lock, so a concurrent `start`
/// inside that window can be rejected as already-running. This race is benign
/// for a diagnostic command and does not exist on the IPC ping path.
pub fn is_held(run_dir: &Path) -> io::Result<bool> {
    match acquire(run_dir)? {
        AcquireOutcome::Acquired(guard) => {
            drop(guard);
            Ok(false)
        }
        AcquireOutcome::Held => Ok(true),
    }
}

pub fn write_pid(run_dir: &Path, pid: u32) -> io::Result<()> {
    fs::write(run_dir.join(PID_FILE), pid.to_string())
}

pub fn read_pid(run_dir: &Path) -> Option<u32> {
    fs::read_to_string(run_dir.join(PID_FILE))
        .ok()?
        .trim()
        .parse()
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_run_dir() -> tempfile::TempDir {
        tempfile::tempdir().expect("create temp dir")
    }

    #[test]
    fn second_acquire_reports_held() {
        let dir = temp_run_dir();
        let first = acquire(dir.path()).unwrap();
        assert!(matches!(first, AcquireOutcome::Acquired(_)));

        let second = acquire(dir.path()).unwrap();
        assert!(matches!(second, AcquireOutcome::Held));
    }

    #[test]
    fn lock_is_reacquirable_after_drop() {
        let dir = temp_run_dir();
        let first = acquire(dir.path()).unwrap();
        drop(first);

        let second = acquire(dir.path()).unwrap();
        assert!(matches!(second, AcquireOutcome::Acquired(_)));
    }

    #[test]
    fn is_held_probe_is_non_destructive() {
        let dir = temp_run_dir();
        assert!(!is_held(dir.path()).unwrap());

        let guard = acquire(dir.path()).unwrap();
        assert!(is_held(dir.path()).unwrap());
        drop(guard);

        assert!(!is_held(dir.path()).unwrap());
        assert!(matches!(
            acquire(dir.path()).unwrap(),
            AcquireOutcome::Acquired(_)
        ));
    }

    #[test]
    fn acquire_creates_missing_run_dir() {
        let dir = temp_run_dir();
        let nested = dir.path().join("run");
        assert!(!nested.exists());
        let outcome = acquire(&nested).unwrap();
        assert!(matches!(outcome, AcquireOutcome::Acquired(_)));
        assert!(nested.join(LOCK_FILE).exists());
    }

    #[test]
    fn pid_roundtrip_and_invalid_content() {
        let dir = temp_run_dir();
        assert_eq!(read_pid(dir.path()), None);

        write_pid(dir.path(), 4242).unwrap();
        assert_eq!(read_pid(dir.path()), Some(4242));

        fs::write(dir.path().join(PID_FILE), "not-a-pid").unwrap();
        assert_eq!(read_pid(dir.path()), None);
    }
}
