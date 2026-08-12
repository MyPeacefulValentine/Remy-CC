//! Shared Python runtime and deployed-script discovery.

use std::env;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub fn scanner_runtime() -> io::Result<(PathBuf, PathBuf)> {
    Ok((
        locate_python()?,
        locate_script(&["skills", "remy-index", "struct_scan.py"])?,
    ))
}

pub fn hook_runtime(script_name: &str) -> io::Result<(PathBuf, PathBuf)> {
    Ok((locate_python()?, locate_script(&["hooks", script_name])?))
}

pub fn user_home() -> io::Result<PathBuf> {
    env::var_os("HOME")
        .or_else(|| env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "cannot locate user home"))
}

pub fn claude_home() -> io::Result<PathBuf> {
    env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .or_else(|| user_home().ok().map(|home| home.join(".claude")))
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "cannot locate Claude home"))
}

fn locate_script(components: &[&str]) -> io::Result<PathBuf> {
    let configured = components
        .iter()
        .fold(claude_home()?, |path, component| path.join(component));
    let source_tree = components.iter().fold(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("remy-daemon has a repository parent")
            .to_path_buf(),
        |path, component| path.join(component),
    );
    if cfg!(debug_assertions) && source_tree.is_file() {
        return Ok(source_tree);
    }
    if configured.is_file() {
        return Ok(configured);
    }
    if source_tree.is_file() {
        return Ok(source_tree);
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        format!("runtime script not found at {}", configured.display()),
    ))
}

fn locate_python() -> io::Result<PathBuf> {
    if let Some(value) = env::var_os("REMY_PYTHON") {
        return Ok(PathBuf::from(value));
    }
    let candidates: &[&str] = if cfg!(windows) {
        &["python.exe", "python"]
    } else {
        &["python3", "python"]
    };
    for candidate in candidates {
        if Command::new(candidate)
            .args([
                "-c",
                "import sys; raise SystemExit(sys.version_info < (3, 10))",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
        {
            return Ok(PathBuf::from(candidate));
        }
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        "no compatible Python 3.10+ interpreter found",
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_tree_scripts_are_discoverable_in_debug_builds() {
        assert!(scanner_runtime().unwrap().1.ends_with("struct_scan.py"));
        assert!(hook_runtime("logic_dirty_tracker.py")
            .unwrap()
            .1
            .ends_with("logic_dirty_tracker.py"));
    }
}
