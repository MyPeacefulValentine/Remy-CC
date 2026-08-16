//! File discovery: os.walk replication with directory pruning
//! (StructScanner.scan_all's discovery loop).

use crate::config::ScanConfig;
use crate::parse_c_cpp;
use std::path::{Path, PathBuf};

/// One discovered source file: absolute path plus the forward-slash
/// relative path used as the files.path key.
#[derive(Debug, Clone)]
pub struct DiscoveredFile {
    pub full_path: PathBuf,
    pub rel_path: String,
}

pub fn rel_path_slash(full_path: &Path, root_dir: &Path) -> String {
    parse_c_cpp::relpath_slash(full_path, root_dir)
}

/// Walk `root_dir`, pruning excluded directories, and collect files the
/// C/C++ parser handles. Order is not significant: rows are keyed by path
/// and the comparison projections sort deterministically.
pub fn discover(root_dir: &Path, config: &ScanConfig) -> std::io::Result<Vec<DiscoveredFile>> {
    let mut discovered = Vec::new();
    let mut stack = vec![root_dir.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = std::fs::read_dir(&dir)?;
        for entry in entries {
            let entry = entry?;
            let path = entry.path();
            let file_type = entry.file_type()?;
            let rel = rel_path_slash(&path, root_dir);
            if file_type.is_dir() {
                if !config.is_excluded(&rel, true) {
                    stack.push(path);
                }
                continue;
            }
            if config.is_excluded(&rel, false) {
                continue;
            }
            let file_name = entry.file_name();
            let file_name = file_name.to_string_lossy();
            if !parse_c_cpp::handles(&file_name) {
                continue;
            }
            discovered.push(DiscoveredFile {
                full_path: path,
                rel_path: rel,
            });
        }
    }
    Ok(discovered)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn discovers_c_files_and_prunes_excluded_dirs() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        std::fs::create_dir_all(root.join("src")).unwrap();
        std::fs::create_dir_all(root.join("build")).unwrap();
        std::fs::write(root.join("src/a.c"), "int a;\n").unwrap();
        std::fs::write(root.join("src/b.h"), "int b;\n").unwrap();
        std::fs::write(root.join("build/gen.c"), "int g;\n").unwrap();
        std::fs::write(root.join("notes.txt"), "x\n").unwrap();

        let config = ScanConfig::parse("!build/\n");
        let mut found: Vec<String> = discover(root, &config)
            .unwrap()
            .into_iter()
            .map(|f| f.rel_path)
            .collect();
        found.sort();
        assert_eq!(found, vec!["src/a.c", "src/b.h"]);
    }
}
