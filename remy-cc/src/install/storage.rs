//! Canonical JSON serialization and atomic file writes for managed metadata.
//!
//! Byte-compatible with the retired v3 storage layer: `json.dumps(document,
//! ensure_ascii=False, indent=2, sort_keys=True) + "\n"` — serde_json's
//! default map is sorted and its pretty printer uses two-space indentation,
//! so the shapes coincide; writes go through a same-directory temp file,
//! fsync, and rename.

use std::fs;
use std::io::{self, Write};
use std::path::Path;

use serde_json::Value;

use super::InstallError;

pub(crate) fn canonical_json_bytes(document: &Value) -> Vec<u8> {
    let mut bytes =
        serde_json::to_vec_pretty(&sorted_keys(document)).expect("serializable JSON value");
    bytes.push(b'\n');
    bytes
}

/// Recursively rebuilds objects in key order. The workspace compiles
/// serde_json with `preserve_order` (pulled in by rmcp), so maps keep
/// insertion order and `sort_keys=True` parity needs an explicit pass.
fn sorted_keys(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let mut pairs: Vec<(&String, &Value)> = object.iter().collect();
            pairs.sort_by(|a, b| a.0.cmp(b.0));
            Value::Object(
                pairs
                    .into_iter()
                    .map(|(key, child)| (key.clone(), sorted_keys(child)))
                    .collect(),
            )
        }
        Value::Array(items) => Value::Array(items.iter().map(sorted_keys).collect()),
        other => other.clone(),
    }
}

pub(crate) fn atomic_write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let file_name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let temp_path = parent.join(format!(".{}.{}.tmp", file_name, std::process::id()));
    let result = (|| -> io::Result<()> {
        let mut file = fs::File::create(&temp_path)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temp_path, path)?;
        #[cfg(unix)]
        {
            if let Ok(directory) = fs::File::open(parent) {
                let _ = directory.sync_all();
            }
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temp_path);
    }
    result
}

pub(crate) fn atomic_write_json(path: &Path, document: &Value) -> io::Result<()> {
    atomic_write(path, &canonical_json_bytes(document))
}

pub(crate) fn load_json(path: &Path) -> Result<Value, InstallError> {
    let text = fs::read_to_string(path).map_err(|_| {
        InstallError::metadata(format!(
            "invalid managed metadata: {}",
            path.file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default()
        ))
    })?;
    let value: Value = serde_json::from_str(&text).map_err(|_| {
        InstallError::metadata(format!(
            "invalid managed metadata: {}",
            path.file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default()
        ))
    })?;
    if !value.is_object() {
        return Err(InstallError::metadata(
            "managed metadata must be a JSON object",
        ));
    }
    Ok(value)
}

pub(crate) fn sha256_hex(data: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(data);
    format!("{:x}", hasher.finalize())
}

pub(crate) fn sha256_file(path: &Path) -> io::Result<String> {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    let mut file = fs::File::open(path)?;
    io::copy(&mut file, &mut hasher)?;
    Ok(format!("{:x}", hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn canonical_bytes_match_python_dumps_shape() {
        let document = json!({"b": 1, "a": {"c": [1, 2]}, "text": "中文"});
        let rendered = String::from_utf8(canonical_json_bytes(&document)).expect("utf8");
        let expected = "{\n  \"a\": {\n    \"c\": [\n      1,\n      2\n    ]\n  },\n  \"b\": 1,\n  \"text\": \"中文\"\n}\n";
        assert_eq!(rendered, expected);
    }

    #[test]
    fn atomic_write_round_trips_and_leaves_no_temp_files() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("nested").join("doc.json");
        atomic_write_json(&path, &json!({"k": "v"})).expect("write");
        let loaded = load_json(&path).expect("load");
        assert_eq!(loaded, json!({"k": "v"}));
        let residues: Vec<_> = std::fs::read_dir(path.parent().unwrap())
            .expect("read_dir")
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(residues.is_empty());
    }

    #[test]
    fn load_json_rejects_corrupt_and_non_object_documents() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("doc.json");
        std::fs::write(&path, "{broken").expect("write");
        let error = load_json(&path).expect_err("corrupt");
        assert!(error.message.contains("invalid managed metadata"));
        std::fs::write(&path, "[1, 2]").expect("write");
        let error = load_json(&path).expect_err("array");
        assert_eq!(error.message, "managed metadata must be a JSON object");
    }

    #[test]
    fn sha256_matches_known_vector() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
