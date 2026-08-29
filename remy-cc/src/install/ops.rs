//! `remy-cc install` orchestration: the idempotent-rerun self-install
//! (R4.4 audit disposition (b)1, REQ-1..7).
//!
//! Model: no journal replay — recovery direction is forward. Every content
//! write is a staged same-directory file plus an atomic rename; the v4
//! manifest write is the commit point; obsolete deletions run before it so
//! an interrupted run converges on rerun; a locked target is renamed aside
//! and registered for deferred deletion (REQ-5). Ownership guards keep the
//! v3 texts, with two idempotency deviations from `facade.py::
//! _build_install_changes`: a planned target already holding the incoming
//! content is accepted (interrupted-run convergence), and an obsolete
//! record whose file is already gone is skipped instead of erroring.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

use super::embedded;
use super::lock;
use super::manifest::{self, FileRecord, LoadedManifest, Manifest};
use super::pending::PendingDeletes;
use super::pyprobe::{self, RuntimeDescriptor};
use super::settings;
use super::storage;
use super::util;
use super::InstallError;

pub(crate) const LEGACY_MANIFEST_NAME: &str = ".installer_manifest.json";

pub(crate) struct InstallParams {
    pub(crate) claude_root: PathBuf,
    pub(crate) remy_root: PathBuf,
    pub(crate) language: String,
    pub(crate) suite_version: String,
    pub(crate) source_binary: PathBuf,
    pub(crate) python: Option<RuntimeDescriptor>,
    pub(crate) artifact_sha256: Option<String>,
}

#[derive(Debug, Default)]
pub(crate) struct InstallReport {
    pub(crate) changed: Vec<String>,
    pub(crate) warnings: Vec<String>,
    pub(crate) post_commit_failed: bool,
}

enum Payload {
    Bytes(Vec<u8>),
    File(PathBuf),
}

struct PlannedFile {
    root: &'static str,
    path: String,
    payload: Payload,
    digest: String,
    role: &'static str,
    executable: bool,
    write: bool,
}

pub(crate) fn install(params: &InstallParams) -> Result<InstallReport, InstallError> {
    let _lock = lock::acquire(&params.remy_root)?;
    let pending = PendingDeletes::new(&params.claude_root, &params.remy_root);
    pending.sweep();
    let mut report = InstallReport::default();

    let loaded = manifest::load(&params.remy_root)?;
    let old_manifest: Option<&Manifest> = match &loaded {
        Some(LoadedManifest::Current(manifest)) | Some(LoadedManifest::V3Migration(manifest)) => {
            Some(manifest)
        }
        None => None,
    };

    let mut planned = plan_claude_contents(&params.language)?;
    let exe_name = settings::managed_exe_name(&params.remy_root);
    planned.push(plan_binary(params, exe_name)?);
    if let Some(descriptor) = &params.python {
        let target = params.remy_root.join("runtime").join("python.json");
        let bytes = pyprobe::descriptor_bytes(descriptor, &target);
        planned.push(PlannedFile {
            root: manifest::ROOT_REMY,
            path: "runtime/python.json".to_string(),
            digest: storage::sha256_hex(&bytes),
            payload: Payload::Bytes(bytes),
            role: "runtime_descriptor",
            executable: false,
            write: true,
        });
    } else {
        report
            .warnings
            .push("python probe unavailable; runtime descriptor not refreshed (Python 3.10+ is a runtime prerequisite)".to_string());
    }

    let mut old_records: std::collections::BTreeMap<(String, String), FileRecord> = old_manifest
        .map(|manifest| {
            manifest
                .files
                .iter()
                .map(|record| ((record.root.clone(), record.path.clone()), record.clone()))
                .collect()
        })
        .unwrap_or_default();

    for file in &mut planned {
        let target = root_dir(params, file.root).join(&file.path);
        let disk = if target.is_file() {
            Some(storage::sha256_file(&target).map_err(io_error)?)
        } else {
            None
        };
        let old = old_records.remove(&(file.root.to_string(), file.path.clone()));
        file.write = match (&disk, &old) {
            (None, _) => true,
            (Some(disk), _) if *disk == file.digest => false,
            (Some(disk), Some(old)) if *disk == old.sha256 => true,
            (Some(_), Some(_)) => {
                return Err(InstallError::runtime(
                    "a managed target changed after preflight",
                ))
            }
            (Some(_), None) => {
                return Err(InstallError::runtime(
                    "an unmanaged target has different content",
                ))
            }
        };
    }

    let mut obsolete: Vec<(PathBuf, FileRecord)> = Vec::new();
    for ((root, path), record) in old_records {
        let target = root_dir(params, &root).join(&path);
        let disk = if target.is_file() {
            Some(storage::sha256_file(&target).map_err(io_error)?)
        } else {
            None
        };
        match disk {
            None => {}
            Some(disk) if disk == record.sha256 => obsolete.push((target, record)),
            Some(_) => {
                return Err(InstallError::runtime(
                    "an obsolete managed target was modified",
                ))
            }
        }
    }

    let settings_path = params.claude_root.join("settings.json");
    let existing_settings = load_settings(&settings_path)?;
    let template: Value = serde_json::from_str(embedded::SETTINGS_TEMPLATE)
        .map_err(|_| InstallError::metadata("settings template is invalid"))?;
    let commands = settings::hook_commands(&params.remy_root)?;
    let prior_claim = old_manifest.map(|manifest| &manifest.settings_claim);
    let (merged_settings, settings_claim) = settings::merge_settings_document(
        &existing_settings,
        &template,
        &params.claude_root,
        &params.remy_root,
        &commands,
        prior_claim,
    )?;
    let settings_bytes = storage::canonical_json_bytes(&merged_settings);
    let settings_write = !settings_path.is_file()
        || storage::sha256_file(&settings_path).map_err(io_error)?
            != storage::sha256_hex(&settings_bytes);

    for file in &planned {
        if !file.write {
            continue;
        }
        let target = root_dir(params, file.root).join(&file.path);
        match &file.payload {
            Payload::Bytes(bytes) => {
                staged_write(&target, bytes, file.executable, false, &pending).map_err(io_error)?
            }
            Payload::File(source) => {
                deploy_binary(source, &target, &pending)?;
            }
        }
        report.changed.push(format!("{}/{}", file.root, file.path));
    }
    if settings_write {
        staged_write(&settings_path, &settings_bytes, false, true, &pending).map_err(io_error)?;
        report.changed.push("claude/settings.json".to_string());
    }

    for (target, _record) in &obsolete {
        remove_or_defer(target, &pending, &mut report.warnings);
    }

    let files: Vec<FileRecord> = {
        let mut records: Vec<FileRecord> = planned
            .iter()
            .map(|file| FileRecord {
                root: file.root.to_string(),
                path: file.path.clone(),
                sha256: file.digest.clone(),
                role: file.role.to_string(),
            })
            .collect();
        records.sort_by(|a, b| (&a.root, &a.path).cmp(&(&b.root, &b.path)));
        records
    };
    let mut new_manifest = Manifest {
        suite_version: params.suite_version.clone(),
        installed_at: util::iso8601_utc_now(),
        artifact_sha256: params.artifact_sha256.clone(),
        files,
        settings_claim,
    };
    if let Some(old) = old_manifest {
        if manifest_payload_equal(old, &new_manifest) {
            new_manifest.installed_at = old.installed_at.clone();
        }
    }
    new_manifest.write(&params.remy_root)?;

    post_commit(params, &pending, &mut report);
    Ok(report)
}

/// `remy-cc verify`: v4 manifest hash reconciliation, settings claim check,
/// runtime descriptor probe, and a running-daemon version comparison.
/// Returns the warning list; empty means the installation verifies clean.
pub(crate) fn verify(claude_root: &Path, remy_root: &Path) -> Vec<String> {
    let mut warnings = Vec::new();
    let manifest = match manifest::load(remy_root) {
        Ok(Some(LoadedManifest::Current(manifest))) => Some(manifest),
        Ok(Some(LoadedManifest::V3Migration(_))) => {
            warnings
                .push("install manifest is schema v3; run remy-cc install to migrate".to_string());
            None
        }
        Ok(None) => {
            warnings.push("install manifest is missing".to_string());
            None
        }
        Err(error) => {
            warnings.push(error.message);
            None
        }
    };
    if let Some(manifest) = &manifest {
        for record in &manifest.files {
            let root = if record.root == manifest::ROOT_CLAUDE {
                claude_root
            } else {
                remy_root
            };
            let target = root.join(&record.path);
            match storage::sha256_file(&target) {
                Ok(digest) if digest == record.sha256 => {}
                Ok(_) => warnings.push(format!("an owned file was modified: {}", record.path)),
                Err(_) => warnings.push(format!("an owned file is missing: {}", record.path)),
            }
        }
        match load_settings(&claude_root.join("settings.json")) {
            Ok(settings_doc) => {
                if let Err(error) =
                    settings::verify_settings_claim(&settings_doc, &manifest.settings_claim)
                {
                    warnings.push(error.message);
                }
            }
            Err(error) => warnings.push(error.message),
        }
    }
    let descriptor_path = remy_root.join("runtime").join("python.json");
    match storage::load_json(&descriptor_path) {
        Ok(descriptor) => {
            let executable = descriptor.get("executable").and_then(Value::as_str);
            match executable {
                Some(executable) => {
                    if let Err(error) = pyprobe::probe_executable(executable) {
                        warnings.push(error.message);
                    }
                }
                None => warnings.push("runtime descriptor is invalid".to_string()),
            }
        }
        Err(_) => warnings.push(
            "runtime descriptor is missing (Python 3.10+ is a runtime prerequisite)".to_string(),
        ),
    }
    if let Some(running) = running_daemon_version(remy_root) {
        if running != env!("CARGO_PKG_VERSION") {
            warnings.push(format!(
                "daemon runs {running} but this binary is {}; run remy-cc daemon restart",
                env!("CARGO_PKG_VERSION")
            ));
        }
    }
    warnings
}

fn running_daemon_version(remy_root: &Path) -> Option<String> {
    let run_dir = remy_root.join("run");
    if !crate::single_instance::is_held(&run_dir).unwrap_or(false) {
        return None;
    }
    let hello = crate::protocol::Request::Hello {
        protocol_version: crate::protocol::PROTOCOL_VERSION,
        token: crate::server::read_token(&run_dir).unwrap_or_default(),
    };
    match crate::ipc_roundtrip(&run_dir, &hello) {
        Some(crate::protocol::Response::Hello { daemon_version, .. }) => Some(daemon_version),
        _ => None,
    }
}

/// `remy-cc uninstall`: precise removal by the manifest (v4, or v3 as
/// migration input), claim-lenient settings cleanup, and optional engine
/// state purge. Files already gone are skipped (forward-recovery rerun);
/// locked files leave through rename-aside plus the pending register.
pub(crate) fn uninstall(
    claude_root: &Path,
    remy_root: &Path,
    purge_state: bool,
) -> Result<InstallReport, InstallError> {
    let mut report = InstallReport::default();
    {
        let _lock = lock::acquire(remy_root)?;
        let pending = PendingDeletes::new(claude_root, remy_root);
        let manifest = match manifest::load(remy_root)? {
            Some(LoadedManifest::Current(manifest))
            | Some(LoadedManifest::V3Migration(manifest)) => manifest,
            None => return Err(InstallError::runtime("install manifest is missing")),
        };
        let run_dir = remy_root.join("run");
        if crate::single_instance::is_held(&run_dir).unwrap_or(false) {
            return Err(InstallError::runtime(
                "daemon must be stopped before uninstall; run: remy-cc daemon stop",
            ));
        }

        let mut parents: std::collections::BTreeSet<PathBuf> = std::collections::BTreeSet::new();
        for record in &manifest.files {
            let root = if record.root == manifest::ROOT_CLAUDE {
                claude_root
            } else {
                remy_root
            };
            let target = root.join(&record.path);
            if !target.is_file() {
                continue;
            }
            remove_or_defer(&target, &pending, &mut report.warnings);
            report
                .changed
                .push(format!("{}/{}", record.root, record.path));
            let mut parent = target.parent();
            while let Some(directory) = parent {
                if directory == root {
                    break;
                }
                parents.insert(directory.to_path_buf());
                parent = directory.parent();
            }
        }
        for directory in parents.iter().rev() {
            let _ = fs::remove_dir(directory);
        }

        let settings_path = claude_root.join("settings.json");
        if settings_path.is_file() {
            match load_settings(&settings_path) {
                Ok(settings_doc) => {
                    let cleaned = settings::remove_settings_claim_lenient(
                        &settings_doc,
                        &manifest.settings_claim,
                    );
                    let bytes = storage::canonical_json_bytes(&cleaned);
                    let unchanged = storage::sha256_file(&settings_path)
                        .map(|digest| digest == storage::sha256_hex(&bytes))
                        .unwrap_or(false);
                    if !unchanged {
                        staged_write(&settings_path, &bytes, false, true, &pending)
                            .map_err(io_error)?;
                        report.changed.push("claude/settings.json".to_string());
                    }
                }
                Err(error) => report
                    .warnings
                    .push(format!("settings cleanup skipped: {}", error.message)),
            }
        }

        let manifest_file = manifest::manifest_path(remy_root);
        remove_or_defer(&manifest_file, &pending, &mut report.warnings);
    }
    if purge_state {
        match fs::remove_dir_all(remy_root) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => {
                return Err(InstallError::runtime(
                    "uninstall committed but engine-state cleanup is incomplete",
                ))
            }
        }
    }
    Ok(report)
}

fn plan_claude_contents(language: &str) -> Result<Vec<PlannedFile>, InstallError> {
    let mut contents: Vec<(String, Vec<u8>)> = Vec::new();
    embedded::for_each_entry(|path, data| {
        contents.push((path.to_string(), data.to_vec()));
        Ok(())
    })
    .map_err(|error| InstallError::runtime(format!("embedded payload unreadable: {error}")))?;
    patch_descriptions(&mut contents, language)?;
    let directive = if language == "zh-CN" {
        "Always respond in Chinese-simplified\n"
    } else {
        "Always respond in English\n"
    };
    contents.push(("language.md".to_string(), directive.as_bytes().to_vec()));
    Ok(contents
        .into_iter()
        .map(|(path, bytes)| PlannedFile {
            root: manifest::ROOT_CLAUDE,
            role: role_for(&path),
            digest: storage::sha256_hex(&bytes),
            payload: Payload::Bytes(bytes),
            path,
            executable: false,
            write: true,
        })
        .collect())
}

/// Port of `patch_descriptions.py::patch`: replace the `description:` line
/// within the first eight lines of each skill's SKILL.md with the selected
/// language's text (falling back to English).
fn patch_descriptions(
    contents: &mut [(String, Vec<u8>)],
    language: &str,
) -> Result<(), InstallError> {
    let descriptions = contents
        .iter()
        .find(|(path, _)| path == embedded::SKILL_DESCRIPTIONS_ENTRY)
        .map(|(_, bytes)| bytes.clone());
    let Some(descriptions) = descriptions else {
        return Ok(());
    };
    let descriptions: Value = serde_json::from_slice(&descriptions)
        .map_err(|_| InstallError::metadata("skill_descriptions.json is invalid"))?;
    let Some(descriptions) = descriptions.as_object() else {
        return Err(InstallError::metadata("skill_descriptions.json is invalid"));
    };
    for (skill, lang_map) in descriptions {
        let description = lang_map
            .get(language)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .or_else(|| {
                lang_map
                    .get("en")
                    .and_then(Value::as_str)
                    .filter(|t| !t.is_empty())
            });
        let Some(description) = description else {
            continue;
        };
        let entry = format!("skills/{skill}/SKILL.md");
        let Some((_, bytes)) = contents.iter_mut().find(|(path, _)| *path == entry) else {
            continue;
        };
        let Ok(text) = std::str::from_utf8(bytes) else {
            continue;
        };
        let mut lines: Vec<&str> = text.split_inclusive('\n').collect();
        let mut replaced: Option<(usize, String)> = None;
        for (index, line) in lines.iter().take(8).enumerate() {
            if let Some(rest) = line.strip_prefix("description:") {
                if rest.starts_with(' ')
                    || rest.trim_end_matches(['\r', '\n']).is_empty()
                    || rest.starts_with('\t')
                {
                    replaced = Some((index, format!("description: {description}\n")));
                }
                break;
            }
        }
        if let Some((index, line)) = replaced {
            let new_line = line;
            let mut rebuilt = String::new();
            for (current, original) in lines.iter_mut().enumerate() {
                if current == index {
                    rebuilt.push_str(&new_line);
                } else {
                    rebuilt.push_str(original);
                }
            }
            *bytes = rebuilt.into_bytes();
        }
    }
    Ok(())
}

fn plan_binary(params: &InstallParams, exe_name: &str) -> Result<PlannedFile, InstallError> {
    if !params.source_binary.is_file() {
        return Err(InstallError::runtime("no usable remy-cc binary was found"));
    }
    let digest = storage::sha256_file(&params.source_binary).map_err(io_error)?;
    Ok(PlannedFile {
        root: manifest::ROOT_REMY,
        path: format!("bin/{exe_name}"),
        digest,
        payload: Payload::File(params.source_binary.clone()),
        role: "daemon_binary",
        executable: true,
        write: true,
    })
}

fn role_for(path: &str) -> &'static str {
    if path.starts_with("hooks/") {
        "python_hook"
    } else if path.starts_with("skills/") {
        "claude_skill"
    } else if path.starts_with("output-styles/") {
        "output_style"
    } else if path.starts_with("remy-src/") {
        "cli_runtime"
    } else {
        "claude_protocol"
    }
}

fn root_dir<'a>(params: &'a InstallParams, root: &str) -> &'a Path {
    if root == manifest::ROOT_CLAUDE {
        &params.claude_root
    } else {
        &params.remy_root
    }
}

fn load_settings(path: &Path) -> Result<Value, InstallError> {
    if !path.is_file() {
        return Ok(Value::Object(serde_json::Map::new()));
    }
    let text =
        fs::read_to_string(path).map_err(|_| InstallError::metadata("settings.json is invalid"))?;
    let value: Value = serde_json::from_str(&text)
        .map_err(|_| InstallError::metadata("settings.json is invalid"))?;
    if !value.is_object() {
        return Err(InstallError::metadata("settings.json must be an object"));
    }
    Ok(value)
}

/// Staged same-directory write plus atomic rename. POSIX modes follow the
/// v3 rule: an existing target keeps its mode; a new settings.json gets
/// 0600; a new executable gets 0755. A rename onto a locked target moves
/// the target aside and registers the residue for deferred deletion.
fn staged_write(
    target: &Path,
    bytes: &[u8],
    executable: bool,
    private: bool,
    pending: &PendingDeletes,
) -> std::io::Result<()> {
    let parent = target.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let name = target
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let stage = parent.join(format!(".{}.remy-stage-{}", name, std::process::id()));
    fs::write(&stage, bytes)?;
    apply_mode(&stage, target, executable, private);
    rename_over(&stage, target, pending)
}

#[cfg(unix)]
fn apply_mode(stage: &Path, target: &Path, executable: bool, private: bool) {
    use std::os::unix::fs::PermissionsExt;
    let mode = if let Ok(metadata) = target.metadata() {
        metadata.permissions().mode() & 0o777
    } else if private {
        0o600
    } else if executable {
        0o755
    } else {
        return;
    };
    let _ = fs::set_permissions(stage, fs::Permissions::from_mode(mode));
}

#[cfg(not(unix))]
fn apply_mode(_stage: &Path, _target: &Path, _executable: bool, _private: bool) {}

fn rename_over(stage: &Path, target: &Path, pending: &PendingDeletes) -> std::io::Result<()> {
    match fs::rename(stage, target) {
        Ok(()) => Ok(()),
        Err(_) => {
            let aside = target.with_extension(format!("old-{}", std::process::id()));
            match fs::rename(target, &aside) {
                Ok(()) => {
                    let _ = pending.register(std::slice::from_ref(&aside));
                    fs::rename(stage, target)
                }
                Err(error) => {
                    let _ = fs::remove_file(stage);
                    Err(error)
                }
            }
        }
    }
}

fn deploy_binary(
    source: &Path,
    target: &Path,
    pending: &PendingDeletes,
) -> Result<(), InstallError> {
    let same_file = match (source.canonicalize(), target.canonicalize()) {
        (Ok(left), Ok(right)) => left == right,
        _ => false,
    };
    if same_file {
        return Ok(());
    }
    let bytes = fs::read(source).map_err(io_error)?;
    staged_write(target, &bytes, true, false, pending).map_err(io_error)
}

fn remove_or_defer(target: &Path, pending: &PendingDeletes, warnings: &mut Vec<String>) {
    match fs::remove_file(target) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => {
            let aside = target.with_extension(format!("old-{}", std::process::id()));
            let residue = if fs::rename(target, &aside).is_ok() {
                aside
            } else {
                target.to_path_buf()
            };
            let _ = pending.register(std::slice::from_ref(&residue));
            warnings.push(format!(
                "cleanup deferred for a locked file: {}",
                residue.display()
            ));
        }
    }
}

fn manifest_payload_equal(old: &Manifest, new: &Manifest) -> bool {
    old.suite_version == new.suite_version
        && old.artifact_sha256 == new.artifact_sha256
        && old.files == new.files
        && old.settings_claim == new.settings_claim
}

fn post_commit(params: &InstallParams, pending: &PendingDeletes, report: &mut InstallReport) {
    let legacy = params.claude_root.join(LEGACY_MANIFEST_NAME);
    if legacy.is_file() {
        remove_or_defer(&legacy, pending, &mut report.warnings);
    }
    let v3_transaction = params.remy_root.join("install").join("transaction.json");
    if v3_transaction.is_file() {
        remove_or_defer(&v3_transaction, pending, &mut report.warnings);
    }
    if let Err(error) = register_mcp(&params.claude_root, &params.remy_root) {
        report
            .warnings
            .push(format!("MCP registration failed: {error}"));
        report.post_commit_failed = true;
    }
    if let Err(error) = super::interact::save_language(&params.claude_root, &params.language) {
        report.warnings.push(format!(
            "could not persist REMY_LANG into remy-config.json: {error}"
        ));
        report.post_commit_failed = true;
    }
}

/// Renders the embedded MCP template against the resolved roots and merges
/// it into the user-level `.claude.json` next to the Claude home.
pub(crate) fn register_mcp(claude_root: &Path, remy_root: &Path) -> Result<(), InstallError> {
    let mut template: Value = serde_json::from_str(embedded::MCP_TEMPLATE)
        .map_err(|_| InstallError::metadata("MCP template is invalid"))?;
    let claude_prefix = claude_root.to_string_lossy().replace('\\', "/");
    let remy_prefix = remy_root.to_string_lossy().replace('\\', "/");
    let daemon_command = format!(
        "{remy_prefix}/bin/{}",
        settings::managed_exe_name(remy_root)
    );
    let expand = |value: &str| -> String {
        if value == "~/.remy-cc/bin/remy-cc" {
            daemon_command.clone()
        } else if value.contains("~/.claude/") {
            value.replace("~/.claude/", &format!("{claude_prefix}/"))
        } else if value.contains("~/.remy-cc/") {
            value.replace("~/.remy-cc/", &format!("{remy_prefix}/"))
        } else {
            value.to_string()
        }
    };
    let servers = template
        .as_object_mut()
        .ok_or_else(|| InstallError::metadata("MCP template is invalid"))?;
    for config in servers.values_mut() {
        let Some(config) = config.as_object_mut() else {
            continue;
        };
        if let Some(command) = config.get("command").and_then(Value::as_str) {
            let expanded = expand(command);
            config.insert("command".to_string(), Value::String(expanded));
        }
        if let Some(args) = config.get_mut("args").and_then(Value::as_array_mut) {
            for argument in args {
                if let Some(text) = argument.as_str() {
                    *argument = Value::String(expand(text));
                }
            }
        }
    }

    let claude_json_path = claude_root
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join(".claude.json");
    let mut existing = if claude_json_path.exists() {
        let text = fs::read_to_string(&claude_json_path)
            .map_err(|_| InstallError::metadata(".claude.json is invalid"))?;
        let value: Value = serde_json::from_str(&text)
            .map_err(|_| InstallError::metadata(".claude.json is invalid"))?;
        if !value.is_object() {
            return Err(InstallError::metadata(".claude.json must be an object"));
        }
        value
    } else {
        Value::Object(serde_json::Map::new())
    };
    let object = existing.as_object_mut().expect("checked above");
    let servers_section = object
        .entry("mcpServers")
        .or_insert_with(|| Value::Object(serde_json::Map::new()));
    let servers_section = servers_section
        .as_object_mut()
        .ok_or_else(|| InstallError::metadata(".claude.json mcpServers must be an object"))?;
    for (name, config) in template.as_object().expect("object checked") {
        servers_section.insert(name.clone(), config.clone());
    }
    storage::atomic_write_json(&claude_json_path, &existing)
        .map_err(|error| InstallError::runtime(format!("cannot write .claude.json: {error}")))
}

fn io_error(error: std::io::Error) -> InstallError {
    InstallError::runtime(format!("filesystem operation failed: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    struct Env {
        _dir: tempfile::TempDir,
        params: InstallParams,
    }

    fn setup(language: &str) -> Env {
        let dir = tempfile::tempdir().expect("tempdir");
        let claude = dir.path().join("claude");
        let remy = dir.path().join("remy");
        let source = dir.path().join("incoming-remy-cc.bin");
        fs::write(&source, b"binary-payload-v1").expect("binary");
        Env {
            params: InstallParams {
                claude_root: claude,
                remy_root: remy,
                language: language.to_string(),
                suite_version: "0.8.0".to_string(),
                source_binary: source,
                python: None,
                artifact_sha256: None,
            },
            _dir: dir,
        }
    }

    fn tree_snapshot(root: &Path) -> std::collections::BTreeMap<String, Vec<u8>> {
        let mut snapshot = std::collections::BTreeMap::new();
        if !root.exists() {
            return snapshot;
        }
        let mut stack = vec![root.to_path_buf()];
        while let Some(directory) = stack.pop() {
            for entry in fs::read_dir(&directory).expect("read_dir") {
                let path = entry.expect("entry").path();
                if path.is_dir() {
                    stack.push(path);
                } else {
                    let key = path
                        .strip_prefix(root)
                        .expect("prefix")
                        .to_string_lossy()
                        .replace('\\', "/");
                    snapshot.insert(key, fs::read(&path).expect("read"));
                }
            }
        }
        snapshot
    }

    #[test]
    fn fresh_install_deploys_the_full_surface() {
        let env = setup("zh-CN");
        let report = install(&env.params).expect("install");
        assert!(
            !report.post_commit_failed,
            "warnings: {:?}",
            report.warnings
        );

        assert!(env.params.claude_root.join("CLAUDE.md").is_file());
        assert!(env
            .params
            .claude_root
            .join("hooks")
            .join("pre_tool_guard.py")
            .is_file());
        assert_eq!(
            fs::read_to_string(env.params.claude_root.join("language.md")).expect("language"),
            "Always respond in Chinese-simplified\n"
        );
        let exe = settings::managed_exe_name(&env.params.remy_root);
        assert!(env.params.remy_root.join("bin").join(exe).is_file());

        let loaded = manifest::load(&env.params.remy_root)
            .expect("load")
            .expect("present");
        let LoadedManifest::Current(manifest) = loaded else {
            panic!("expected a v4 manifest");
        };
        assert_eq!(manifest.suite_version, "0.8.0");
        assert!(manifest
            .files
            .iter()
            .any(|f| f.path == format!("bin/{exe}")));
        assert!(!manifest.files.iter().any(|f| f.path == "settings.json"));

        let settings_doc =
            storage::load_json(&env.params.claude_root.join("settings.json")).expect("settings");
        let text = serde_json::to_string(&settings_doc).expect("serialize");
        assert!(text.contains("hook enrich") && text.contains("hook dirty"));

        let claude_json = storage::load_json(
            &env.params
                .claude_root
                .parent()
                .unwrap()
                .join(".claude.json"),
        )
        .expect("claude.json");
        let command = claude_json["mcpServers"]["remy-index"]["command"]
            .as_str()
            .expect("command");
        assert!(command.ends_with(&format!("bin/{exe}")));

        let config =
            storage::load_json(&env.params.claude_root.join("remy-config.json")).expect("config");
        assert_eq!(config["values"]["REMY_LANG"], "zh-CN");
    }

    #[test]
    fn skill_descriptions_are_patched_for_the_selected_language() {
        let env = setup("zh-CN");
        install(&env.params).expect("install");
        let descriptions: Value = serde_json::from_slice(
            &embedded::entry_bytes(embedded::SKILL_DESCRIPTIONS_ENTRY)
                .expect("archive")
                .expect("descriptions"),
        )
        .expect("json");
        let (skill, lang_map) = descriptions
            .as_object()
            .expect("object")
            .iter()
            .find(|(name, langs)| {
                langs.get("zh-CN").and_then(Value::as_str).is_some()
                    && env
                        .params
                        .claude_root
                        .join("skills")
                        .join(name.as_str())
                        .join("SKILL.md")
                        .is_file()
            })
            .expect("a patchable skill");
        let expected = lang_map["zh-CN"].as_str().expect("zh text");
        let skill_md = fs::read_to_string(
            env.params
                .claude_root
                .join("skills")
                .join(skill)
                .join("SKILL.md"),
        )
        .expect("skill md");
        let description_line = skill_md
            .lines()
            .take(8)
            .find(|line| line.starts_with("description:"))
            .expect("description line");
        assert_eq!(description_line, format!("description: {expected}"));
    }

    #[test]
    fn rerun_is_idempotent_including_the_manifest() {
        let env = setup("en");
        install(&env.params).expect("first");
        let claude_before = tree_snapshot(&env.params.claude_root);
        let remy_before = tree_snapshot(&env.params.remy_root);
        let report = install(&env.params).expect("second");
        assert!(report.changed.is_empty(), "changed: {:?}", report.changed);
        assert_eq!(claude_before, tree_snapshot(&env.params.claude_root));
        assert_eq!(remy_before, tree_snapshot(&env.params.remy_root));
    }

    #[test]
    fn unmanaged_target_with_different_content_rejects() {
        let env = setup("en");
        let target = env.params.claude_root.join("CLAUDE.md");
        fs::create_dir_all(target.parent().unwrap()).expect("dirs");
        fs::write(&target, b"user data").expect("write");
        let error = install(&env.params).expect_err("unmanaged");
        assert_eq!(error.message, "an unmanaged target has different content");
        assert_eq!(fs::read(&target).expect("read"), b"user data");
        assert!(manifest::load(&env.params.remy_root)
            .expect("load")
            .is_none());
    }

    #[test]
    fn modified_owned_target_rejects_on_reinstall() {
        let env = setup("en");
        install(&env.params).expect("first");
        let target = env.params.claude_root.join("CLAUDE.md");
        fs::write(&target, b"user edited").expect("modify");
        let error = install(&env.params).expect_err("modified");
        assert_eq!(error.message, "a managed target changed after preflight");
    }

    #[test]
    fn interrupted_run_converges_when_disk_already_holds_incoming_content() {
        let env = setup("en");
        let expected = embedded::entry_bytes("CLAUDE.md")
            .expect("archive")
            .expect("entry");
        let target = env.params.claude_root.join("CLAUDE.md");
        fs::create_dir_all(target.parent().unwrap()).expect("dirs");
        fs::write(&target, &expected).expect("pre-write");
        install(&env.params).expect("converges");
    }

    #[test]
    fn v3_migration_removes_the_shim_and_old_binary_and_writes_v4() {
        let env = setup("en");
        let shim = env.params.claude_root.join("bin").join("remy-cc");
        fs::create_dir_all(shim.parent().unwrap()).expect("dirs");
        fs::write(&shim, b"#!/bin/sh\nexec python cli.py\n").expect("shim");
        let old_exe = if settings::managed_exe_name(&env.params.remy_root) == "remy-cc.exe" {
            "remy-daemon.exe"
        } else {
            "remy-daemon"
        };
        let old_binary = env.params.remy_root.join("bin").join(old_exe);
        fs::create_dir_all(old_binary.parent().unwrap()).expect("dirs");
        fs::write(&old_binary, b"old-binary").expect("old binary");
        let v3 = json!({
            "schema_version": 3,
            "suite_version": "1.7.3",
            "hook_mode": "rust",
            "installed_at": "2026-08-01T00:00:00+00:00",
            "files": [
                {
                    "root": "claude",
                    "path": "bin/remy-cc",
                    "sha256": storage::sha256_file(&shim).expect("hash"),
                    "owner": "remy-cc",
                    "role": "cli_shim",
                },
                {
                    "root": "remy",
                    "path": format!("bin/{old_exe}"),
                    "sha256": storage::sha256_file(&old_binary).expect("hash"),
                    "owner": "remy-cc",
                    "role": "daemon_binary",
                },
            ],
            "settings_claim": {"hooks": [], "permissions": []},
        });
        storage::atomic_write_json(&manifest::manifest_path(&env.params.remy_root), &v3)
            .expect("v3 manifest");

        install(&env.params).expect("migration install");

        assert!(!shim.exists(), "shim must be removed by the manifest diff");
        assert!(!old_binary.exists(), "old binary must be removed");
        assert!(matches!(
            manifest::load(&env.params.remy_root).expect("load"),
            Some(LoadedManifest::Current(_))
        ));
    }

    #[test]
    fn legacy_v2_manifest_is_deleted_post_commit() {
        let env = setup("en");
        let legacy = env.params.claude_root.join(LEGACY_MANIFEST_NAME);
        fs::create_dir_all(legacy.parent().unwrap()).expect("dirs");
        fs::write(&legacy, b"{}").expect("legacy");
        install(&env.params).expect("install");
        assert!(!legacy.exists());
    }

    #[test]
    fn old_hook_registrations_are_cleared_via_claim_and_legacy_rules() {
        let env = setup("en");
        let old_exe = if settings::managed_exe_name(&env.params.remy_root) == "remy-cc.exe" {
            "remy-daemon.exe"
        } else {
            "remy-daemon"
        };
        let old_command = format!(
            "\"{}\" hook enrich",
            env.params
                .remy_root
                .join("bin")
                .join(old_exe)
                .to_string_lossy()
        );
        fs::create_dir_all(&env.params.claude_root).expect("dirs");
        fs::write(
            env.params.claude_root.join("settings.json"),
            serde_json::to_string(&json!({
                "hooks": {"PreToolUse": [{
                    "matcher": "Read|Glob|Grep",
                    "hooks": [{"type": "command", "command": old_command}],
                }]},
            }))
            .expect("serialize"),
        )
        .expect("settings");

        install(&env.params).expect("install");

        let settings_doc =
            storage::load_json(&env.params.claude_root.join("settings.json")).expect("settings");
        let text = serde_json::to_string(&settings_doc).expect("serialize");
        assert!(
            !text.contains("remy-daemon"),
            "old registration must be gone"
        );
        assert!(text.contains("hook enrich"));
    }

    #[test]
    fn verify_is_clean_after_install_and_flags_every_drift_class() {
        let env = setup("en");
        install(&env.params).expect("install");
        let warnings = verify(&env.params.claude_root, &env.params.remy_root);
        assert_eq!(
            warnings,
            vec![
                "runtime descriptor is missing (Python 3.10+ is a runtime prerequisite)"
                    .to_string()
            ],
            "python was not probed in this environment"
        );

        fs::write(env.params.claude_root.join("CLAUDE.md"), b"drift").expect("modify");
        fs::remove_file(env.params.claude_root.join("style.md")).expect("delete");
        let warnings = verify(&env.params.claude_root, &env.params.remy_root);
        assert!(warnings
            .iter()
            .any(|w| w == "an owned file was modified: CLAUDE.md"));
        assert!(warnings
            .iter()
            .any(|w| w == "an owned file is missing: style.md"));

        let settings_path = env.params.claude_root.join("settings.json");
        let mut settings_doc = storage::load_json(&settings_path).expect("settings");
        settings_doc["hooks"]["PreToolUse"][0]["hooks"][0]["command"] =
            Value::String("tampered".to_string());
        fs::write(
            &settings_path,
            serde_json::to_string(&settings_doc).expect("serialize"),
        )
        .expect("write");
        let warnings = verify(&env.params.claude_root, &env.params.remy_root);
        assert!(warnings
            .iter()
            .any(|w| w == "a managed settings Hook was modified"));
    }

    #[test]
    fn verify_reports_a_missing_manifest() {
        let env = setup("en");
        let warnings = verify(&env.params.claude_root, &env.params.remy_root);
        assert!(warnings.iter().any(|w| w == "install manifest is missing"));
    }

    #[test]
    fn uninstall_removes_managed_state_and_preserves_user_data() {
        let env = setup("en");
        install(&env.params).expect("install");
        let user_file = env.params.claude_root.join("projects").join("user.txt");
        fs::create_dir_all(user_file.parent().unwrap()).expect("dirs");
        fs::write(&user_file, b"user").expect("user file");
        let state = env.params.remy_root.join("state.db");
        fs::write(&state, b"state").expect("state");

        let report =
            uninstall(&env.params.claude_root, &env.params.remy_root, false).expect("uninstall");
        assert!(!report.changed.is_empty());

        assert!(!env.params.claude_root.join("CLAUDE.md").exists());
        assert!(
            !env.params.claude_root.join("hooks").exists(),
            "emptied dirs are removed"
        );
        assert!(user_file.exists());
        assert!(state.exists(), "engine state survives a default uninstall");
        assert!(env.params.claude_root.join("remy-config.json").exists());
        assert!(manifest::load(&env.params.remy_root)
            .expect("load")
            .is_none());
        let settings_doc =
            storage::load_json(&env.params.claude_root.join("settings.json")).expect("settings");
        let text = serde_json::to_string(&settings_doc).expect("serialize");
        assert!(!text.contains("hook enrich"), "managed hooks removed");
    }

    #[test]
    fn uninstall_purge_removes_the_remy_root_only() {
        let env = setup("en");
        install(&env.params).expect("install");
        fs::write(env.params.remy_root.join("state.db"), b"state").expect("state");
        uninstall(&env.params.claude_root, &env.params.remy_root, true).expect("uninstall");
        assert!(!env.params.remy_root.exists());
        assert!(env.params.claude_root.exists());
    }

    #[test]
    fn uninstall_skips_files_already_gone() {
        let env = setup("en");
        install(&env.params).expect("install");
        fs::remove_file(env.params.claude_root.join("CLAUDE.md")).expect("pre-delete");
        uninstall(&env.params.claude_root, &env.params.remy_root, false)
            .expect("uninstall tolerates missing targets");
    }

    #[cfg(unix)]
    #[test]
    fn posix_modes_follow_the_v3_rule() {
        use std::os::unix::fs::PermissionsExt;
        let env = setup("en");
        install(&env.params).expect("install");
        let settings_mode = std::fs::metadata(env.params.claude_root.join("settings.json"))
            .expect("settings")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(settings_mode, 0o600, "fresh settings.json must be private");
        let exe = settings::managed_exe_name(&env.params.remy_root);
        let binary_mode = std::fs::metadata(env.params.remy_root.join("bin").join(exe))
            .expect("binary")
            .permissions()
            .mode()
            & 0o777;
        assert_eq!(binary_mode, 0o755, "deployed binary must be executable");
    }

    #[test]
    fn uninstall_without_a_manifest_rejects() {
        let env = setup("en");
        let error = uninstall(&env.params.claude_root, &env.params.remy_root, false)
            .expect_err("no manifest");
        assert_eq!(error.message, "install manifest is missing");
    }
}
