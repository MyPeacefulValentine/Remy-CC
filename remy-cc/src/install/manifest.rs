//! The v4 install manifest (Rust single owner) and the read-only v3
//! parser used as first-run migration input.
//!
//! v4 field set: `{schema_version: 4, suite_version, installed_at,
//! artifact_sha256, files, settings_claim}` — `hook_mode` is dropped (the
//! rust arm is the only world) and `artifact_sha256` records the sha256 of
//! the downloaded release asset (`null` for installs from a local build).
//! `files` and `settings_claim` keep the v3 record shapes; validation
//! messages keep the retired v3 validator's texts verbatim.

use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use super::settings::SettingsClaim;
use super::storage;
use super::InstallError;

pub(crate) const MANIFEST_SCHEMA_VERSION: u64 = 4;
pub(crate) const V3_SCHEMA_VERSION: u64 = 3;
pub(crate) const OWNER: &str = "remy-cc";
pub(crate) const ROOT_CLAUDE: &str = "claude";
pub(crate) const ROOT_REMY: &str = "remy";

/// `<remy root>/install/manifest.json` — shared with the v3 arm; the schema
/// version inside the document decides how it is read.
pub(crate) fn manifest_path(remy_root: &Path) -> PathBuf {
    remy_root.join("install").join("manifest.json")
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct FileRecord {
    pub(crate) root: String,
    pub(crate) path: String,
    pub(crate) sha256: String,
    pub(crate) role: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Manifest {
    pub(crate) suite_version: String,
    pub(crate) installed_at: String,
    pub(crate) artifact_sha256: Option<String>,
    pub(crate) files: Vec<FileRecord>,
    pub(crate) settings_claim: SettingsClaim,
}

/// A manifest document found on disk: the current contract, or a v3
/// document accepted purely as migration input.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum LoadedManifest {
    Current(Manifest),
    V3Migration(Manifest),
}

impl Manifest {
    pub(crate) fn to_value(&self) -> Value {
        let files = self
            .files
            .iter()
            .map(|record| {
                Value::Object(Map::from_iter([
                    ("root".to_string(), Value::String(record.root.clone())),
                    ("path".to_string(), Value::String(record.path.clone())),
                    ("sha256".to_string(), Value::String(record.sha256.clone())),
                    ("owner".to_string(), Value::String(OWNER.to_string())),
                    ("role".to_string(), Value::String(record.role.clone())),
                ]))
            })
            .collect();
        Value::Object(Map::from_iter([
            (
                "schema_version".to_string(),
                Value::from(MANIFEST_SCHEMA_VERSION),
            ),
            (
                "suite_version".to_string(),
                Value::String(self.suite_version.clone()),
            ),
            (
                "installed_at".to_string(),
                Value::String(self.installed_at.clone()),
            ),
            (
                "artifact_sha256".to_string(),
                self.artifact_sha256
                    .clone()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
            ),
            ("files".to_string(), Value::Array(files)),
            ("settings_claim".to_string(), self.settings_claim.to_value()),
        ]))
    }

    pub(crate) fn write(&self, remy_root: &Path) -> Result<(), InstallError> {
        validate_v4(&self.to_value())?;
        storage::atomic_write_json(&manifest_path(remy_root), &self.to_value())
            .map_err(|error| InstallError::runtime(format!("cannot write manifest: {error}")))
    }
}

/// Reads the manifest at the shared path, dispatching on `schema_version`.
/// `Ok(None)` when no manifest exists.
pub(crate) fn load(remy_root: &Path) -> Result<Option<LoadedManifest>, InstallError> {
    let path = manifest_path(remy_root);
    if !path.is_file() {
        return Ok(None);
    }
    let value = storage::load_json(&path)?;
    match value.get("schema_version").and_then(Value::as_u64) {
        Some(MANIFEST_SCHEMA_VERSION) => Ok(Some(LoadedManifest::Current(validate_v4(&value)?))),
        Some(V3_SCHEMA_VERSION) => Ok(Some(LoadedManifest::V3Migration(parse_v3(&value)?))),
        _ => Err(InstallError::metadata("unsupported manifest schema")),
    }
}

pub(crate) fn validate_v4(value: &Value) -> Result<Manifest, InstallError> {
    let object = expect_keys(
        value,
        &[
            "schema_version",
            "suite_version",
            "installed_at",
            "artifact_sha256",
            "files",
            "settings_claim",
        ],
    )?;
    if object.get("schema_version").and_then(Value::as_u64) != Some(MANIFEST_SCHEMA_VERSION) {
        return Err(InstallError::metadata("unsupported manifest schema"));
    }
    let artifact_sha256 = match object.get("artifact_sha256") {
        Some(Value::Null) => None,
        Some(Value::String(digest)) if is_sha256(digest) => Some(digest.clone()),
        _ => return Err(InstallError::metadata("invalid manifest artifact_sha256")),
    };
    let (suite_version, installed_at, files, settings_claim) = shared_fields(object)?;
    Ok(Manifest {
        suite_version,
        installed_at,
        artifact_sha256,
        files,
        settings_claim,
    })
}

/// Read-only v3 acceptance (migration input): the exact v3 field set is
/// required and `hook_mode` is validated then discarded.
pub(crate) fn parse_v3(value: &Value) -> Result<Manifest, InstallError> {
    let object = expect_keys(
        value,
        &[
            "schema_version",
            "suite_version",
            "hook_mode",
            "installed_at",
            "files",
            "settings_claim",
        ],
    )?;
    if object.get("schema_version").and_then(Value::as_u64) != Some(V3_SCHEMA_VERSION) {
        return Err(InstallError::metadata("unsupported manifest schema"));
    }
    match object.get("hook_mode").and_then(Value::as_str) {
        Some("python") | Some("rust") => {}
        _ => return Err(InstallError::metadata("invalid manifest hook_mode")),
    }
    let (suite_version, installed_at, files, settings_claim) = shared_fields(object)?;
    Ok(Manifest {
        suite_version,
        installed_at,
        artifact_sha256: None,
        files,
        settings_claim,
    })
}

fn expect_keys<'a>(
    value: &'a Value,
    expected: &[&str],
) -> Result<&'a Map<String, Value>, InstallError> {
    let object = value
        .as_object()
        .ok_or_else(|| InstallError::metadata("unsupported manifest schema"))?;
    if object.len() != expected.len() || !expected.iter().all(|key| object.contains_key(*key)) {
        return Err(InstallError::metadata("unsupported manifest schema"));
    }
    Ok(object)
}

fn shared_fields(
    object: &Map<String, Value>,
) -> Result<(String, String, Vec<FileRecord>, SettingsClaim), InstallError> {
    let suite_version = object.get("suite_version").and_then(Value::as_str);
    let installed_at = object.get("installed_at").and_then(Value::as_str);
    let (Some(suite_version), Some(installed_at)) = (suite_version, installed_at) else {
        return Err(InstallError::metadata("invalid manifest identity fields"));
    };
    let raw_files = object
        .get("files")
        .and_then(Value::as_array)
        .ok_or_else(|| InstallError::metadata("manifest files must be an array"))?;
    let mut files = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for raw in raw_files {
        let record = raw
            .as_object()
            .filter(|record| {
                record.len() == 5
                    && ["root", "path", "sha256", "owner", "role"]
                        .iter()
                        .all(|key| record.contains_key(*key))
            })
            .ok_or_else(|| InstallError::metadata("invalid manifest file record"))?;
        let field = |key: &str| record.get(key).and_then(Value::as_str);
        let (Some(root), Some(path), Some(digest), Some(owner), Some(role)) = (
            field("root"),
            field("path"),
            field("sha256"),
            field("owner"),
            field("role"),
        ) else {
            return Err(InstallError::metadata("invalid manifest file identity"));
        };
        if (root != ROOT_CLAUDE && root != ROOT_REMY)
            || path.is_empty()
            || digest.is_empty()
            || owner.is_empty()
            || role.is_empty()
        {
            return Err(InstallError::metadata("invalid manifest file identity"));
        }
        if owner != OWNER {
            return Err(InstallError::metadata("invalid manifest owner"));
        }
        let normalized = normalize_relative_path(path)?;
        if !is_sha256(digest) {
            return Err(InstallError::metadata("invalid manifest sha256"));
        }
        if !seen.insert((root.to_string(), normalized.clone())) {
            return Err(InstallError::metadata("duplicate manifest file record"));
        }
        files.push(FileRecord {
            root: root.to_string(),
            path: normalized,
            sha256: digest.to_string(),
            role: role.to_string(),
        });
    }
    let settings_claim =
        SettingsClaim::from_value(object.get("settings_claim").unwrap_or(&Value::Null))?;
    Ok((
        suite_version.to_string(),
        installed_at.to_string(),
        files,
        settings_claim,
    ))
}

pub(crate) fn normalize_relative_path(value: &str) -> Result<String, InstallError> {
    if value.is_empty() || value.contains('\\') {
        return Err(InstallError::metadata(
            "managed path must be a non-empty POSIX relative path",
        ));
    }
    let path = Path::new(value);
    if path.is_absolute()
        || value.starts_with('/')
        || value.as_bytes().get(1) == Some(&b':')
        || !path
            .components()
            .all(|c| matches!(c, std::path::Component::Normal(_)))
    {
        return Err(InstallError::metadata(
            "managed path must be a non-empty POSIX relative path",
        ));
    }
    Ok(value.to_string())
}

fn is_sha256(digest: &str) -> bool {
    digest.len() == 64
        && digest
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::install::settings::ClaimedHook;
    use serde_json::json;

    fn sample_claim() -> SettingsClaim {
        SettingsClaim {
            hooks: vec![ClaimedHook {
                event: "PreToolUse".to_string(),
                matcher: "Read|Glob|Grep".to_string(),
                command: "\"/x/bin/remy-cc\" hook enrich".to_string(),
            }],
            permissions: vec!["Skill(remy-index)".to_string()],
        }
    }

    fn sample_manifest() -> Manifest {
        Manifest {
            suite_version: "2.0.0".to_string(),
            installed_at: "2026-08-29T00:00:00+00:00".to_string(),
            artifact_sha256: None,
            files: vec![FileRecord {
                root: ROOT_CLAUDE.to_string(),
                path: "hooks/pre_tool_guard.py".to_string(),
                sha256: "a".repeat(64),
                role: "python_hook".to_string(),
            }],
            settings_claim: sample_claim(),
        }
    }

    #[test]
    fn v4_round_trips_through_disk() {
        let dir = tempfile::tempdir().expect("tempdir");
        let manifest = sample_manifest();
        manifest.write(dir.path()).expect("write");
        let loaded = load(dir.path()).expect("load").expect("present");
        assert_eq!(loaded, LoadedManifest::Current(manifest));
    }

    #[test]
    fn missing_manifest_loads_as_none() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert_eq!(load(dir.path()).expect("load"), None);
    }

    #[test]
    fn v4_accepts_artifact_sha256_null_and_hex() {
        let mut manifest = sample_manifest();
        assert!(validate_v4(&manifest.to_value()).is_ok());
        manifest.artifact_sha256 = Some("b".repeat(64));
        assert!(validate_v4(&manifest.to_value()).is_ok());
        let mut value = manifest.to_value();
        value["artifact_sha256"] = json!("not-hex");
        let error = validate_v4(&value).expect_err("invalid digest");
        assert_eq!(error.message, "invalid manifest artifact_sha256");
    }

    #[test]
    fn v4_rejects_hook_mode_and_field_drift() {
        let mut with_hook_mode = sample_manifest().to_value();
        with_hook_mode["hook_mode"] = json!("rust");
        let error = validate_v4(&with_hook_mode).expect_err("extra key");
        assert_eq!(error.message, "unsupported manifest schema");
        let mut missing = sample_manifest().to_value();
        missing.as_object_mut().unwrap().remove("installed_at");
        assert!(validate_v4(&missing).is_err());
    }

    #[test]
    fn v4_rejects_invalid_records() {
        let base = sample_manifest();
        let mut wrong_owner = base.to_value();
        wrong_owner["files"][0]["owner"] = json!("someone-else");
        assert_eq!(
            validate_v4(&wrong_owner).expect_err("owner").message,
            "invalid manifest owner"
        );
        let mut bad_digest = base.to_value();
        bad_digest["files"][0]["sha256"] = json!("ABC");
        assert_eq!(
            validate_v4(&bad_digest).expect_err("sha").message,
            "invalid manifest sha256"
        );
        let mut traversal = base.to_value();
        traversal["files"][0]["path"] = json!("../escape");
        assert!(validate_v4(&traversal).is_err());
        let mut backslash = base.to_value();
        backslash["files"][0]["path"] = json!("hooks\\x.py");
        assert!(validate_v4(&backslash).is_err());
        let mut duplicated = base.to_value();
        let record = duplicated["files"][0].clone();
        duplicated["files"].as_array_mut().unwrap().push(record);
        assert_eq!(
            validate_v4(&duplicated).expect_err("dup").message,
            "duplicate manifest file record"
        );
    }

    #[test]
    fn v3_parses_as_migration_input_and_discards_hook_mode() {
        let value = json!({
            "schema_version": 3,
            "suite_version": "1.7.3",
            "hook_mode": "rust",
            "installed_at": "2026-08-01T00:00:00+00:00",
            "files": [{
                "root": "claude",
                "path": "bin/remy-cc",
                "sha256": "c".repeat(64),
                "owner": "remy-cc",
                "role": "cli_shim",
            }],
            "settings_claim": {"hooks": [], "permissions": []},
        });
        let manifest = parse_v3(&value).expect("v3");
        assert_eq!(manifest.suite_version, "1.7.3");
        assert_eq!(manifest.artifact_sha256, None);
        assert_eq!(manifest.files.len(), 1);
        let mut bad_mode = value.clone();
        bad_mode["hook_mode"] = json!("cobol");
        assert_eq!(
            parse_v3(&bad_mode).expect_err("mode").message,
            "invalid manifest hook_mode"
        );
    }

    #[test]
    fn load_dispatches_on_schema_version() {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = manifest_path(dir.path());
        let v3 = json!({
            "schema_version": 3,
            "suite_version": "1.7.3",
            "hook_mode": "rust",
            "installed_at": "2026-08-01T00:00:00+00:00",
            "files": [],
            "settings_claim": {"hooks": [], "permissions": []},
        });
        crate::install::storage::atomic_write_json(&path, &v3).expect("write");
        assert!(matches!(
            load(dir.path()).expect("load"),
            Some(LoadedManifest::V3Migration(_))
        ));
        crate::install::storage::atomic_write_json(&path, &json!({"schema_version": 2}))
            .expect("write");
        let error = load(dir.path()).expect_err("v2");
        assert_eq!(error.message, "unsupported manifest schema");
    }

    #[test]
    fn normalize_rejects_traversal_and_absolutes() {
        assert!(normalize_relative_path("hooks/x.py").is_ok());
        // "a/./b" is accepted: PurePosixPath.parts collapses "." the same way
        // Path::components does, so the Python validator admits it too.
        assert!(normalize_relative_path("a/./b").is_ok());
        for bad in ["", "/abs", "a\\b", "../up", "C:/x"] {
            assert!(normalize_relative_path(bad).is_err(), "accepted: {bad}");
        }
    }
}
