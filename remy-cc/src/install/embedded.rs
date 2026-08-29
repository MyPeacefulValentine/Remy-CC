//! Build-time embedded install payload.
//!
//! `build.rs` packs the deployable Claude Code text artifacts into a gzip
//! tar archive (deterministic order and headers) and copies the two install
//! templates next to it in `OUT_DIR`; everything is pulled in here by
//! absolute `OUT_DIR` paths (R4.0 E.4: no source-relative includes).

use std::io::{self, Read};

use flate2::read::GzDecoder;

/// Gzip tar archive of every deployable file, path-relative to `~/.claude/`.
pub(crate) const ARCHIVE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/cc_artifacts.tar.gz"));

/// Newline-separated archive entry names, in archive order.
pub(crate) const ENTRY_LIST: &str = include_str!(concat!(env!("OUT_DIR"), "/cc_artifacts.list"));

/// Install input: settings.json merge template (repo `settings.example.json`).
pub(crate) const SETTINGS_TEMPLATE: &str =
    include_str!(concat!(env!("OUT_DIR"), "/settings.example.json"));

/// Install input: MCP registration template (repo `remy_mcp.json`).
pub(crate) const MCP_TEMPLATE: &str = include_str!(concat!(env!("OUT_DIR"), "/remy_mcp.json"));

/// Path of the per-skill description translations inside the archive.
pub(crate) const SKILL_DESCRIPTIONS_ENTRY: &str = "skills/skill_descriptions.json";

pub(crate) fn entry_names() -> Vec<&'static str> {
    ENTRY_LIST.lines().filter(|line| !line.is_empty()).collect()
}

/// Streams every archive entry as `(relative_path, bytes)`.
pub(crate) fn for_each_entry(
    mut callback: impl FnMut(&str, &[u8]) -> io::Result<()>,
) -> io::Result<()> {
    let mut archive = tar::Archive::new(GzDecoder::new(ARCHIVE));
    for entry in archive.entries()? {
        let mut entry = entry?;
        let path = entry.path()?.to_string_lossy().replace('\\', "/");
        let mut data = Vec::with_capacity(entry.size() as usize);
        entry.read_to_end(&mut data)?;
        callback(&path, &data)?;
    }
    Ok(())
}

/// Returns one entry's bytes, or `None` when the path is not in the archive.
pub(crate) fn entry_bytes(name: &str) -> io::Result<Option<Vec<u8>>> {
    let mut found = None;
    for_each_entry(|path, data| {
        if path == name {
            found = Some(data.to_vec());
        }
        Ok(())
    })?;
    Ok(found)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;
    use std::path::{Path, PathBuf};

    fn archive_names() -> BTreeSet<String> {
        let mut names = BTreeSet::new();
        for_each_entry(|path, _| {
            assert!(names.insert(path.to_string()), "duplicate entry: {path}");
            Ok(())
        })
        .expect("readable archive");
        names
    }

    #[test]
    fn entry_list_matches_archive_contents() {
        let listed: BTreeSet<String> = entry_names().iter().map(|s| s.to_string()).collect();
        assert_eq!(listed, archive_names());
        assert!(!listed.is_empty());
    }

    #[test]
    fn entry_names_are_safe_relative_paths() {
        for name in entry_names() {
            assert!(
                !name.starts_with('/') && !name.contains(':'),
                "absolute: {name}"
            );
            assert!(!name.contains('\\'), "backslash: {name}");
            assert!(
                Path::new(name)
                    .components()
                    .all(|c| matches!(c, std::path::Component::Normal(_))),
                "unsafe component: {name}"
            );
        }
    }

    #[test]
    fn expected_deploy_surface_is_present() {
        let names = archive_names();
        for marker in [
            "CLAUDE.md",
            "style.md",
            "tools_ref.md",
            "remy-src/cli.py",
            "remy-src/remy_config.py",
            "remy-src/install_runtime/models.py",
            SKILL_DESCRIPTIONS_ENTRY,
        ] {
            assert!(names.contains(marker), "missing: {marker}");
        }
        for prefix in [
            "hooks/",
            "skills/",
            "output-styles/",
            "remy-src/install_runtime/",
        ] {
            assert!(
                names.iter().any(|n| n.starts_with(prefix)),
                "empty prefix: {prefix}"
            );
        }
    }

    #[test]
    fn ignore_rules_hold_in_archive() {
        for name in archive_names() {
            assert!(!name.contains("__pycache__"), "cache leaked: {name}");
            assert!(
                !name.contains("/.claude/") && !name.starts_with(".claude/"),
                "{name}"
            );
            for suffix in [".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".lock"] {
                assert!(!name.ends_with(suffix), "ignored suffix leaked: {name}");
            }
            assert!(!name.contains(".bak"), "backup leaked: {name}");
        }
    }

    /// Differential reconciliation: an independent enumeration of the source
    /// tree with install.py's deploy rules must equal the archive entry set.
    #[test]
    fn archive_reconciles_with_source_tree() {
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("repo root")
            .to_path_buf();
        let mut expected = BTreeSet::new();
        for file in [
            "CLAUDE.md",
            "style.md",
            "tools_ref.md",
            "remy-src/cli.py",
            "remy-src/config_ui.py",
            "remy-src/config_ui.html",
            "remy-assets/logo.svg",
            "remy-src/patch_descriptions.py",
            "remy-src/remy_config.py",
        ] {
            assert!(
                repo_root.join(file).is_file(),
                "missing source file: {file}"
            );
            expected.insert(file.to_string());
        }
        for directory in [
            "hooks",
            "skills",
            "output-styles",
            "remy-src/install_runtime",
        ] {
            walk(&repo_root.join(directory), directory, &mut expected);
        }
        assert_eq!(expected, archive_names());
    }

    fn walk(directory: &Path, prefix: &str, expected: &mut BTreeSet<String>) {
        for item in std::fs::read_dir(directory).expect("read_dir") {
            let item = item.expect("entry");
            let name = item.file_name().to_string_lossy().into_owned();
            if name == "__pycache__"
                || name == ".claude"
                || [".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".lock"]
                    .iter()
                    .any(|s| name.ends_with(s))
                || name.contains(".bak")
            {
                continue;
            }
            let child = format!("{prefix}/{name}");
            if item.path().is_dir() {
                walk(&item.path(), &child, expected);
            } else {
                expected.insert(child);
            }
        }
    }

    #[test]
    fn templates_parse_and_carry_required_sections() {
        let settings: serde_json::Value =
            serde_json::from_str(SETTINGS_TEMPLATE).expect("settings template JSON");
        assert!(settings.get("hooks").is_some());
        assert!(settings.get("permissions").is_some());
        let mcp: serde_json::Value = serde_json::from_str(MCP_TEMPLATE).expect("mcp template JSON");
        assert!(mcp.as_object().is_some_and(|o| !o.is_empty()));
    }

    #[test]
    fn skill_descriptions_are_readable_json() {
        let bytes = entry_bytes(SKILL_DESCRIPTIONS_ENTRY)
            .expect("archive readable")
            .expect("descriptions present");
        let value: serde_json::Value = serde_json::from_slice(&bytes).expect("valid JSON");
        assert!(value.as_object().is_some_and(|o| !o.is_empty()));
    }

    /// Coarse payload gate; the release-binary <3% delta is measured and
    /// recorded at the packet's probe stage.
    #[test]
    fn archive_size_is_within_expected_band() {
        assert!(
            ARCHIVE.len() > 100 * 1024,
            "suspiciously small: {}",
            ARCHIVE.len()
        );
        assert!(
            ARCHIVE.len() < 2 * 1024 * 1024,
            "payload grew: {}",
            ARCHIVE.len()
        );
    }
}
