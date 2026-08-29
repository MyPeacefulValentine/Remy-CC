//! Self-install subsystem (R4.4 I3): embedded Claude Code artifacts plus the
//! install/update/verify/uninstall machinery built on top of them.
#![allow(dead_code)]

use std::fmt;

pub(crate) mod embedded;
pub(crate) mod lock;
pub(crate) mod manifest;
pub(crate) mod pending;
pub(crate) mod settings;
pub(crate) mod storage;

/// Error taxonomy mirroring `install_runtime.models`: `Metadata` corresponds
/// to `MetadataError` (managed metadata lacks required structure), `Runtime`
/// to plain `InstallRuntimeError` (a precondition or ownership check failed).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ErrorKind {
    Metadata,
    Runtime,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct InstallError {
    pub(crate) kind: ErrorKind,
    pub(crate) message: String,
}

impl InstallError {
    pub(crate) fn metadata(message: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::Metadata,
            message: message.into(),
        }
    }

    pub(crate) fn runtime(message: impl Into<String>) -> Self {
        Self {
            kind: ErrorKind::Runtime,
            message: message.into(),
        }
    }
}

impl fmt::Display for InstallError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for InstallError {}
