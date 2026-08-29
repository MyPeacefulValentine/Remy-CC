//! Single-instance guard for install-family operations (REQ-6): one
//! install/update/uninstall at a time per `~/.remy-cc`, via the same OS
//! advisory file-lock mechanism as the daemon lock (released by the OS on
//! process death — no stale-lock recovery path is needed, INV-R3).

use std::fs::{self, File, TryLockError};
use std::path::{Path, PathBuf};

use super::InstallError;

pub(crate) const LOCK_FILE: &str = "install.lock";

pub(crate) fn install_lock_path(remy_root: &Path) -> PathBuf {
    remy_root.join("install").join(LOCK_FILE)
}

/// Held for the duration of an install-family operation; dropping releases.
#[derive(Debug)]
pub(crate) struct InstallLock {
    _file: File,
}

pub(crate) fn acquire(remy_root: &Path) -> Result<InstallLock, InstallError> {
    let path = install_lock_path(remy_root);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            InstallError::runtime(format!("cannot prepare install lock: {error}"))
        })?;
    }
    let file = File::options()
        .create(true)
        .write(true)
        .truncate(false)
        .open(&path)
        .map_err(|error| InstallError::runtime(format!("cannot open install lock: {error}")))?;
    match file.try_lock() {
        Ok(()) => Ok(InstallLock { _file: file }),
        Err(TryLockError::WouldBlock) => Err(InstallError::runtime(
            "another remy-cc install operation is already running",
        )),
        Err(TryLockError::Error(error)) => Err(InstallError::runtime(format!(
            "cannot acquire install lock: {error}"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn second_acquire_is_rejected_while_held() {
        let dir = tempfile::tempdir().expect("tempdir");
        let guard = acquire(dir.path()).expect("first acquire");
        let error = acquire(dir.path()).expect_err("second acquire");
        assert_eq!(
            error.message,
            "another remy-cc install operation is already running"
        );
        drop(guard);
        assert!(acquire(dir.path()).is_ok());
    }
}
