//! Shared Python runtime and deployed-script discovery.

use serde::Deserialize;
use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const RUNTIME_SCHEMA_VERSION: u32 = 1;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PythonRuntimeDescriptor {
    schema_version: u32,
    executable: PathBuf,
    version: Vec<u32>,
    implementation: String,
    platform: String,
    probed_at: String,
}

pub fn scanner_runtime() -> io::Result<(PathBuf, PathBuf)> {
    Ok((
        locate_python()?,
        locate_script(&["skills", "remy-index", "struct_scan.py"])?,
    ))
}

pub fn rust_scanner_binary() -> io::Result<PathBuf> {
    env::current_exe()
}

pub fn hook_runtime(script_name: &str) -> io::Result<(PathBuf, PathBuf)> {
    Ok((locate_python()?, locate_script(&["hooks", script_name])?))
}

pub fn user_home() -> io::Result<PathBuf> {
    scanner_core::rconfig::user_home()
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
    if let Ok(home) = crate::remy_home() {
        if let Some(executable) = load_managed_python(&home) {
            return Ok(executable);
        }
    }
    if let Some(value) = env::var_os("REMY_PYTHON") {
        let candidate = PathBuf::from(value);
        if probe_python(&candidate) {
            return Ok(candidate);
        }
    }
    let candidates: &[&str] = if cfg!(windows) {
        &["python.exe", "python"]
    } else {
        &["python3", "python"]
    };
    for candidate in candidates {
        let path = PathBuf::from(candidate);
        if probe_python(&path) {
            return Ok(path);
        }
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        "no compatible Python 3.10+ interpreter found",
    ))
}

fn load_managed_python(remy_home: &Path) -> Option<PathBuf> {
    let path = remy_home.join("runtime").join("python.json");
    let bytes = fs::read(path).ok()?;
    let descriptor: PythonRuntimeDescriptor = serde_json::from_slice(&bytes).ok()?;
    if descriptor.schema_version != RUNTIME_SCHEMA_VERSION
        || !descriptor.executable.is_absolute()
        || descriptor.version.len() != 3
        || descriptor.version[0] < 3
        || (descriptor.version[0] == 3 && descriptor.version[1] < 10)
        || descriptor.implementation.is_empty()
        || descriptor.platform.is_empty()
        || descriptor.probed_at.is_empty()
        || !probe_python(&descriptor.executable)
    {
        return None;
    }
    Some(descriptor.executable)
}

fn probe_python(executable: &Path) -> bool {
    Command::new(executable)
        .args([
            "-I",
            "-c",
            "import sys; raise SystemExit(sys.version_info < (3, 10))",
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn source_tree_scripts_are_discoverable_in_debug_builds() {
        assert!(scanner_runtime().unwrap().1.ends_with("struct_scan.py"));
        assert!(hook_runtime("logic_dirty_tracker.py")
            .unwrap()
            .1
            .ends_with("logic_dirty_tracker.py"));
    }

    #[test]
    fn invalid_runtime_descriptor_falls_back() {
        let home = tempfile::tempdir().unwrap();
        fs::create_dir_all(home.path().join("runtime")).unwrap();
        fs::write(home.path().join("runtime/python.json"), b"{not-json").unwrap();
        assert!(load_managed_python(home.path()).is_none());
    }

    #[test]
    fn descriptor_rejects_unknown_fields() {
        let home = tempfile::tempdir().unwrap();
        fs::create_dir_all(home.path().join("runtime")).unwrap();
        fs::write(
            home.path().join("runtime/python.json"),
            b"{\"schema_version\":1,\"executable\":\"C:/python.exe\",\"version\":[3,10,0],\"implementation\":\"CPython\",\"platform\":\"test\",\"probed_at\":\"now\",\"unknown\":true}",
        )
        .unwrap();
        assert!(load_managed_python(home.path()).is_none());
    }

    #[test]
    fn descriptor_requires_absolute_executable() {
        let home = tempfile::tempdir().unwrap();
        fs::create_dir_all(home.path().join("runtime")).unwrap();
        let mut file = fs::File::create(home.path().join("runtime/python.json")).unwrap();
        write!(
            file,
            "{{\"schema_version\":1,\"executable\":\"python\",\"version\":[3,10,0],\"implementation\":\"CPython\",\"platform\":\"test\",\"probed_at\":\"now\"}}"
        )
        .unwrap();
        assert!(load_managed_python(home.path()).is_none());
    }
}
