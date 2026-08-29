//! Shared home and scanner-binary discovery.

use std::env;
use std::io;
use std::path::PathBuf;

pub fn rust_scanner_binary() -> io::Result<PathBuf> {
    env::current_exe()
}

pub fn user_home() -> io::Result<PathBuf> {
    scanner_core::rconfig::user_home()
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "cannot locate user home"))
}
