//! Packs the Claude Code text artifacts into a gzip tar archive at build
//! time so `remy-cc install` can deploy them without a source checkout.
//!
//! Paths are anchored on `CARGO_MANIFEST_DIR` (the R4.0 E.4 lesson: relative
//! includes silently break under directory moves); the archive and the two
//! standalone install templates land in `OUT_DIR` and are pulled in with
//! `include_bytes!`/`include_str!` by `src/install/embedded.rs`.
//!
//! The entry set mirrors `install.py::_build_install_candidates` minus the
//! install-time products (generated `language.md`, merged `settings.json`,
//! the retired shim): `DEPLOY_DIRS` + `DEPLOY_FILES_MAP`, with the same
//! ignore rules as its `shutil.ignore_patterns` call.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

/// Directories deployed verbatim under `~/.claude/` (install.py DEPLOY_DIRS).
const DEPLOY_DIRS: &[&str] = &[
    "hooks",
    "skills",
    "output-styles",
    "remy-src/install_runtime",
];

/// Single files deployed under `~/.claude/` (install.py DEPLOY_FILES_MAP;
/// source path equals destination path for every current entry).
const DEPLOY_FILES: &[&str] = &[
    "CLAUDE.md",
    "style.md",
    "tools_ref.md",
    "remy-src/cli.py",
    "remy-src/config_ui.py",
    "remy-src/config_ui.html",
    "remy-assets/logo.svg",
    "remy-src/patch_descriptions.py",
    "remy-src/remy_config.py",
];

/// Install inputs embedded standalone (not deployed as files).
const TEMPLATE_FILES: &[&str] = &["settings.example.json", "remy_mcp.json"];

const ARCHIVE_NAME: &str = "cc_artifacts.tar.gz";

fn main() {
    println!(
        "cargo:rustc-env=REMY_TARGET={}",
        std::env::var("TARGET").expect("TARGET")
    );
    let manifest_dir =
        PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir
        .parent()
        .expect("crate sits inside the repo root")
        .to_path_buf();
    let out_dir = PathBuf::from(std::env::var("OUT_DIR").expect("OUT_DIR"));

    let mut entries: Vec<(String, PathBuf)> = Vec::new();
    for relative in DEPLOY_FILES {
        let source = repo_root.join(relative);
        assert!(
            source.is_file(),
            "deploy file missing: {}",
            source.display()
        );
        entries.push(((*relative).to_string(), source));
    }
    for directory in DEPLOY_DIRS {
        let source = repo_root.join(directory);
        assert!(
            source.is_dir(),
            "deploy directory missing: {}",
            source.display()
        );
        collect_dir(&source, directory, &mut entries);
    }
    entries.sort_by(|a, b| a.0.cmp(&b.0));

    let archive_path = out_dir.join(ARCHIVE_NAME);
    let file = fs::File::create(&archive_path).expect("create archive");
    let encoder = flate2::write::GzEncoder::new(file, flate2::Compression::best());
    let mut builder = tar::Builder::new(encoder);
    for (name, source) in &entries {
        let data = fs::read(source).unwrap_or_else(|e| panic!("read {}: {e}", source.display()));
        let mut header = tar::Header::new_gnu();
        header.set_size(data.len() as u64);
        header.set_mode(0o644);
        header.set_mtime(0);
        header.set_uid(0);
        header.set_gid(0);
        header.set_cksum();
        builder
            .append_data(&mut header, name, data.as_slice())
            .expect("append entry");
        println!("cargo:rerun-if-changed={}", source.display());
    }
    builder
        .into_inner()
        .expect("finish tar")
        .finish()
        .expect("finish gzip");

    for relative in TEMPLATE_FILES {
        let source = repo_root.join(relative);
        assert!(source.is_file(), "template missing: {}", source.display());
        fs::copy(
            &source,
            out_dir.join(Path::new(relative).file_name().expect("file name")),
        )
        .expect("copy template");
        println!("cargo:rerun-if-changed={}", source.display());
    }
    for directory in DEPLOY_DIRS {
        println!(
            "cargo:rerun-if-changed={}",
            repo_root.join(directory).display()
        );
    }

    let mut manifest = fs::File::create(out_dir.join("cc_artifacts.list")).expect("create list");
    for (name, _) in &entries {
        writeln!(manifest, "{name}").expect("write list");
    }
}

/// Recursive enumeration with install.py's ignore rules
/// (`__pycache__`, `*.pyc`, `*.pyo`, `.claude`, `*.db*`, `*.lock`, `*.bak*`).
fn collect_dir(directory: &Path, prefix: &str, entries: &mut Vec<(String, PathBuf)>) {
    for item in
        fs::read_dir(directory).unwrap_or_else(|e| panic!("read_dir {}: {e}", directory.display()))
    {
        let item = item.expect("dir entry");
        let name = item.file_name().to_string_lossy().into_owned();
        let path = item.path();
        if ignored(&name) {
            continue;
        }
        let child = format!("{prefix}/{name}");
        if path.is_dir() {
            collect_dir(&path, &child, entries);
        } else {
            entries.push((child, path));
        }
    }
}

fn ignored(name: &str) -> bool {
    name == "__pycache__"
        || name == ".claude"
        || name.ends_with(".pyc")
        || name.ends_with(".pyo")
        || name.ends_with(".db")
        || name.ends_with(".db-wal")
        || name.ends_with(".db-shm")
        || name.ends_with(".lock")
        || name.contains(".bak")
}
