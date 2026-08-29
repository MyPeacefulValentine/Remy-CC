//! `remy-cc update`: binary self-update over the GitHub Releases API —
//! the sole update channel (the git-clone side channel is folded into
//! this path). Verification chain: sha256 is mandatory and hard-rejects,
//! attestation verification is opportunistic via the gh CLI.
//!
//! Flow: probe the latest release; compare versions (one shared sequence
//! from v2.0.0); download `remy-cc-{tag}-{target}` plus its `.sha256`;
//! verify; extract; sanity-run the new binary; swap it into
//! `~/.remy-cc/bin` (rename-and-replace, the displaced image goes to the
//! pending-deletes register); run the new binary's own
//! `install --non-interactive`; restart the daemon when one was running.
//! Any failure before the swap leaves the local installation unchanged.

use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::time::Duration;

use serde_json::Value;

use super::lock;
use super::pending::{self, PendingDeletes};
use super::settings;
use super::storage;
use super::{resolve_roots, InstallError};

const REPO: &str = "MyPeacefulValentine/Remy-CC";
const DEFAULT_TIMEOUT_SECONDS: u64 = 30;

/// Unique staging directory under the system temp dir; removed on drop
/// (best-effort — the OS temp cleaner is the backstop).
struct StagingDir {
    path: PathBuf,
}

impl StagingDir {
    fn create() -> std::io::Result<Self> {
        let path = std::env::temp_dir().join(format!(
            "remy-cc-update-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|duration| duration.as_millis())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&path)?;
        Ok(Self { path })
    }

    fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for StagingDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.path);
    }
}

pub(crate) fn run_update() -> ExitCode {
    let roots = match resolve_roots() {
        Ok(roots) => roots,
        Err(message) => {
            eprintln!("remy-cc update: {message}");
            return ExitCode::from(2);
        }
    };
    let timeout = update_timeout(&roots.claude_root);

    let release = match latest_release(timeout) {
        Ok(release) => release,
        Err(message) => {
            eprintln!("remy-cc update: {message}");
            return ExitCode::from(1);
        }
    };
    let tag_version = release.tag.trim_start_matches('v').to_string();
    if tag_version == env!("CARGO_PKG_VERSION") {
        println!("remy-cc update: already up to date ({})", release.tag);
        return ExitCode::SUCCESS;
    }
    if let (Some(remote), Some(local)) = (
        parse_version_triple(&tag_version),
        parse_version_triple(env!("CARGO_PKG_VERSION")),
    ) {
        if remote < local {
            eprintln!(
                "remy-cc update: the latest release {} is older than the installed {}; refusing to downgrade. To install an older version deliberately, use the bootstrap script with REMY_CC_TAG.",
                release.tag,
                env!("CARGO_PKG_VERSION")
            );
            return ExitCode::from(1);
        }
    }
    println!(
        "remy-cc update: {} -> {} available",
        env!("CARGO_PKG_VERSION"),
        release.tag
    );

    let asset_name = asset_name(&release.tag, env!("REMY_TARGET"));
    let Some(asset_url) = release.asset_url(&asset_name) else {
        eprintln!(
            "remy-cc update: release {} has no asset {asset_name}",
            release.tag
        );
        return ExitCode::from(1);
    };
    let Some(digest_url) = release.asset_url(&format!("{asset_name}.sha256")) else {
        eprintln!(
            "remy-cc update: release {} has no sha256 for {asset_name}",
            release.tag
        );
        return ExitCode::from(1);
    };

    println!("remy-cc update: downloading {asset_name}");
    let archive = match http_get(&asset_url, timeout) {
        Ok(bytes) => bytes,
        Err(message) => {
            eprintln!("remy-cc update: download failed: {message}");
            return ExitCode::from(1);
        }
    };
    let digest_text = match http_get(&digest_url, timeout) {
        Ok(bytes) => String::from_utf8_lossy(&bytes).into_owned(),
        Err(message) => {
            eprintln!("remy-cc update: sha256 download failed: {message}");
            return ExitCode::from(1);
        }
    };
    let expected = match parse_sha256_file(&digest_text) {
        Some(expected) => expected,
        None => {
            eprintln!("remy-cc update: the published sha256 file is invalid");
            return ExitCode::from(1);
        }
    };
    let actual = storage::sha256_hex(&archive);
    if actual != expected {
        eprintln!(
            "remy-cc update: sha256 verification failed for {asset_name} (expected {expected}, got {actual}); the local installation is unchanged"
        );
        return ExitCode::from(1);
    }
    println!("remy-cc update: sha256 verified");

    let staging = StagingDir::create();
    let staging = match &staging {
        Ok(staging) => staging,
        Err(error) => {
            eprintln!("remy-cc update: cannot create a staging directory: {error}");
            return ExitCode::from(1);
        }
    };
    let archive_path = staging.path().join(&asset_name);
    if let Err(error) = std::fs::write(&archive_path, &archive) {
        eprintln!("remy-cc update: cannot stage the download: {error}");
        return ExitCode::from(1);
    }
    verify_attestation(&archive_path);

    let exe_name = settings::managed_exe_name(&roots.remy_root);
    let new_binary = match extract_binary(&archive_path, exe_name, staging.path()) {
        Ok(path) => path,
        Err(message) => {
            eprintln!("remy-cc update: extraction failed: {message}");
            return ExitCode::from(1);
        }
    };
    match Command::new(&new_binary).arg("--version").output() {
        Ok(output) if output.status.success() => {
            let banner = String::from_utf8_lossy(&output.stdout).trim().to_string();
            if !banner_matches_version(&banner, &tag_version) {
                eprintln!(
                    "remy-cc update: downloaded binary reports \"{banner}\", expected version {tag_version}"
                );
                return ExitCode::from(1);
            }
        }
        _ => {
            eprintln!("remy-cc update: downloaded binary failed the --version sanity check");
            return ExitCode::from(1);
        }
    }

    let daemon_was_running = match crate::single_instance::is_held(&roots.remy_root.join("run")) {
        Ok(held) => held,
        Err(error) => {
            println!(
                "  [!] cannot probe the daemon lock: {error}; skipping the automatic daemon restart"
            );
            false
        }
    };
    let deployed = roots.remy_root.join(super::BIN_DIR).join(exe_name);
    {
        // Scope ends before the child install run, which takes the same lock.
        let _lock = match lock::acquire(&roots.remy_root) {
            Ok(lock) => lock,
            Err(error) => {
                eprintln!("remy-cc update: {error}");
                return ExitCode::from(1);
            }
        };
        let pending = PendingDeletes::new(&roots.claude_root, &roots.remy_root);
        if let Err(error) = swap_binary(&new_binary, &deployed, &pending) {
            eprintln!("remy-cc update: {error}");
            return ExitCode::from(1);
        }
    }
    println!("remy-cc update: binary replaced; running the new installer");

    let install_status = Command::new(&deployed)
        .args(["install", "--non-interactive"])
        .status();
    match install_status {
        Ok(status) if status.success() => {}
        Ok(status) => {
            eprintln!(
                "remy-cc update: the new installer exited with {}; rerun {} install",
                status.code().unwrap_or(1),
                deployed.display()
            );
            return ExitCode::from(1);
        }
        Err(error) => {
            eprintln!("remy-cc update: cannot run the new installer: {error}");
            return ExitCode::from(1);
        }
    }

    if daemon_was_running {
        match Command::new(&deployed).arg("restart").status() {
            Ok(status) if status.success() => println!("remy-cc update: daemon restarted"),
            _ => {
                eprintln!("remy-cc update: daemon restart failed; run: remy-cc daemon restart");
                return ExitCode::from(1);
            }
        }
    }
    println!("remy-cc update: completed ({})", release.tag);
    ExitCode::SUCCESS
}

/// `REMY_UPDATE_TIMEOUT` (seconds): environment first, then the deployed
/// remy-config.json, then the default. Applied separately to the release
/// probe and to each download.
pub(crate) fn update_timeout(claude_root: &Path) -> Duration {
    let from_env = std::env::var("REMY_UPDATE_TIMEOUT").ok();
    let from_config = storage::load_json(&claude_root.join("remy-config.json"))
        .ok()
        .and_then(|document| {
            document
                .get("values")
                .and_then(|values| values.get("REMY_UPDATE_TIMEOUT"))
                .and_then(Value::as_str)
                .map(str::to_string)
        });
    let seconds = from_env
        .or(from_config)
        .and_then(|text| text.trim().parse::<u64>().ok())
        .filter(|seconds| *seconds > 0)
        .unwrap_or(DEFAULT_TIMEOUT_SECONDS);
    Duration::from_secs(seconds)
}

pub(crate) struct Release {
    pub(crate) tag: String,
    pub(crate) assets: Vec<(String, String)>,
}

impl Release {
    pub(crate) fn asset_url(&self, name: &str) -> Option<String> {
        self.assets
            .iter()
            .find(|(asset, _)| asset == name)
            .map(|(_, url)| url.clone())
    }
}

fn latest_release(timeout: Duration) -> Result<Release, String> {
    let url = format!("https://api.github.com/repos/{REPO}/releases/latest");
    let body = http_get(&url, timeout).map_err(|message| {
        format!("release probe failed ({message}); the local installation is unchanged")
    })?;
    let payload: Value = serde_json::from_slice(&body)
        .map_err(|_| "release probe returned invalid JSON".to_string())?;
    parse_release(&payload)
        .ok_or_else(|| "release probe returned an unexpected document".to_string())
}

pub(crate) fn parse_release(payload: &Value) -> Option<Release> {
    let tag = payload.get("tag_name")?.as_str()?.to_string();
    let assets = payload
        .get("assets")?
        .as_array()?
        .iter()
        .filter_map(|asset| {
            Some((
                asset.get("name")?.as_str()?.to_string(),
                asset.get("browser_download_url")?.as_str()?.to_string(),
            ))
        })
        .collect();
    Some(Release { tag, assets })
}

/// `remy-cc-{tag}-{target}.tar.gz|zip` — the naming frozen in
/// release.yml (`tag` keeps its `v` prefix, matching `github.ref_name`).
pub(crate) fn asset_name(tag: &str, target: &str) -> String {
    let extension = if target.contains("windows") {
        "zip"
    } else {
        "tar.gz"
    };
    format!("remy-cc-{tag}-{target}.{extension}")
}

/// `x.y.z` as a comparable numeric triple. `None` for any other shape
/// (pre-release suffixes and the like): direction is then unknown and the
/// caller keeps the not-equal-means-update behavior.
pub(crate) fn parse_version_triple(text: &str) -> Option<(u64, u64, u64)> {
    let mut parts = text.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    let patch = parts.next()?.parse().ok()?;
    parts.next().is_none().then_some((major, minor, patch))
}

/// A `--version` banner passes when some whitespace token equals the tag
/// version exactly (a leading `v` on the token is tolerated). Substring
/// matching would accept collisions such as `12.0.01` for `2.0.0`.
pub(crate) fn banner_matches_version(banner: &str, tag_version: &str) -> bool {
    banner
        .split_whitespace()
        .any(|token| token.trim_start_matches('v') == tag_version)
}

/// First whitespace-delimited token must be a 64-digit lowercase hex digest
/// (`sha256sum` / release.yml output shape).
pub(crate) fn parse_sha256_file(text: &str) -> Option<String> {
    let token = text.split_whitespace().next()?;
    let token = token.to_lowercase();
    (token.len() == 64 && token.bytes().all(|b| b.is_ascii_hexdigit())).then_some(token)
}

fn http_get(url: &str, timeout: Duration) -> Result<Vec<u8>, String> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|error| error.to_string())?;
    runtime.block_on(async {
        let client = reqwest::Client::builder()
            .user_agent("remy-cc")
            .timeout(timeout)
            .build()
            .map_err(|error| error.to_string())?;
        let response = client
            .get(url)
            .header("Accept", "application/vnd.github+json")
            .send()
            .await
            .map_err(|error| error.to_string())?;
        if !response.status().is_success() {
            return Err(format!("HTTP {} from {url}", response.status().as_u16()));
        }
        response
            .bytes()
            .await
            .map(|bytes| bytes.to_vec())
            .map_err(|error| error.to_string())
    })
}

/// Opportunistic provenance verification (T1): executed when the gh CLI is
/// available; absence or failure warns without blocking.
fn verify_attestation(archive_path: &Path) {
    let available = Command::new("gh")
        .arg("--version")
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false);
    if !available {
        println!(
            "  [!] provenance attestation not verified (gh CLI unavailable); sha256 verification already passed"
        );
        return;
    }
    let verified = Command::new("gh")
        .args(["attestation", "verify"])
        .arg(archive_path)
        .args(["--repo", REPO])
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false);
    if verified {
        println!("  [+] provenance attestation verified");
    } else {
        println!(
            "  [!] provenance attestation could not be verified; sha256 verification already passed"
        );
    }
}

/// Extracts the release binary from the archive into `staging`, returning
/// its path. tar.gz unpacks in-process; zip goes through the system `tar`
/// (libarchive, Windows 10+) with a PowerShell Expand-Archive fallback —
/// no zip crate dependency.
pub(crate) fn extract_binary(
    archive_path: &Path,
    exe_name: &str,
    staging: &Path,
) -> Result<PathBuf, String> {
    let extracted = staging.join("extracted");
    std::fs::create_dir_all(&extracted).map_err(|error| error.to_string())?;
    let name = archive_path.to_string_lossy();
    if name.ends_with(".tar.gz") {
        let file = std::fs::File::open(archive_path).map_err(|error| error.to_string())?;
        let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(file));
        archive
            .unpack(&extracted)
            .map_err(|error| format!("tar.gz unpack failed: {error}"))?;
    } else {
        let tar_status = Command::new("tar")
            .arg("-xf")
            .arg(archive_path)
            .arg("-C")
            .arg(&extracted)
            .output();
        let tar_ok = tar_status
            .map(|output| output.status.success())
            .unwrap_or(false);
        if !tar_ok {
            let expand = Command::new("powershell")
                .args(["-NoProfile", "-Command"])
                .arg(format!(
                    "Expand-Archive -Path \"{}\" -DestinationPath \"{}\" -Force",
                    archive_path.display(),
                    extracted.display()
                ))
                .output()
                .map(|output| output.status.success())
                .unwrap_or(false);
            if !expand {
                return Err("neither tar nor Expand-Archive could unpack the archive".to_string());
            }
        }
    }
    find_file(&extracted, exe_name)
        .ok_or_else(|| format!("{exe_name} not found inside the archive"))
}

fn find_file(root: &Path, name: &str) -> Option<PathBuf> {
    let mut stack = vec![root.to_path_buf()];
    while let Some(directory) = stack.pop() {
        for entry in std::fs::read_dir(&directory).ok()? {
            let path = entry.ok()?.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.file_name().is_some_and(|file| file == name) {
                return Some(path);
            }
        }
    }
    None
}

/// Rename-and-replace: the new binary lands next to the target,
/// the displaced image is renamed aside and registered for deferred
/// deletion, and the new file is renamed into place.
pub(crate) fn swap_binary(
    new_binary: &Path,
    deployed: &Path,
    pending: &PendingDeletes,
) -> Result<(), InstallError> {
    let parent = deployed
        .parent()
        .ok_or_else(|| InstallError::runtime("deployed binary has no parent directory"))?;
    std::fs::create_dir_all(parent).map_err(|error| {
        InstallError::runtime(format!("cannot prepare {}: {error}", parent.display()))
    })?;
    let incoming = deployed.with_extension(format!("new-{}", std::process::id()));
    std::fs::copy(new_binary, &incoming)
        .map_err(|error| InstallError::runtime(format!("cannot stage the new binary: {error}")))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&incoming, std::fs::Permissions::from_mode(0o755));
    }
    if deployed.exists() {
        let aside = pending::aside_path(deployed);
        std::fs::rename(deployed, &aside).map_err(|error| {
            let _ = std::fs::remove_file(&incoming);
            InstallError::runtime(format!("cannot move the running image aside: {error}"))
        })?;
        if let Some(warning) = pending.register_or_warn(&aside) {
            eprintln!("  [!] {warning}");
        }
    }
    std::fs::rename(&incoming, deployed)
        .map_err(|error| InstallError::runtime(format!("cannot activate the new binary: {error}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn sha256_file_parsing_accepts_sum_output_and_rejects_garbage() {
        let digest = "a".repeat(64);
        assert_eq!(
            parse_sha256_file(&format!("{digest}  remy-cc-v2.0.0-x.tar.gz\n")),
            Some(digest.clone())
        );
        assert_eq!(parse_sha256_file(&digest.to_uppercase()), Some(digest));
        assert_eq!(parse_sha256_file("<html>not found</html>"), None);
        assert_eq!(parse_sha256_file(""), None);
        assert_eq!(parse_sha256_file("abc123"), None);
    }

    #[test]
    fn banner_matching_requires_an_exact_version_token() {
        assert!(banner_matches_version("remy-cc 2.0.0", "2.0.0"));
        assert!(banner_matches_version("remy-cc v2.0.0", "2.0.0"));
        assert!(banner_matches_version("2.0.0", "2.0.0"));
        assert!(!banner_matches_version("remy-cc 12.0.01", "2.0.0"));
        assert!(!banner_matches_version("remy-cc 2.0.0-beta.1", "2.0.0"));
        assert!(!banner_matches_version("", "2.0.0"));
    }

    #[test]
    fn version_triples_compare_numerically_and_reject_other_shapes() {
        assert_eq!(parse_version_triple("2.0.0"), Some((2, 0, 0)));
        assert!(parse_version_triple("2.10.0") > parse_version_triple("2.9.9"));
        assert_eq!(parse_version_triple("2.0"), None);
        assert_eq!(parse_version_triple("2.0.0-beta.1"), None);
        assert_eq!(parse_version_triple("2.0.0.1"), None);
    }

    #[test]
    fn asset_names_follow_the_frozen_naming() {
        assert_eq!(
            asset_name("v2.0.0", "x86_64-pc-windows-msvc"),
            "remy-cc-v2.0.0-x86_64-pc-windows-msvc.zip"
        );
        assert_eq!(
            asset_name("v2.0.0", "x86_64-unknown-linux-musl"),
            "remy-cc-v2.0.0-x86_64-unknown-linux-musl.tar.gz"
        );
        assert_eq!(
            asset_name("v2.0.0", "aarch64-apple-darwin"),
            "remy-cc-v2.0.0-aarch64-apple-darwin.tar.gz"
        );
    }

    #[test]
    fn release_parsing_extracts_tag_and_assets() {
        let payload = json!({
            "tag_name": "v2.0.0",
            "assets": [
                {"name": "a.tar.gz", "browser_download_url": "https://example/a.tar.gz"},
                {"name": "a.tar.gz.sha256", "browser_download_url": "https://example/a.sha256"},
            ],
        });
        let release = parse_release(&payload).expect("release");
        assert_eq!(release.tag, "v2.0.0");
        assert_eq!(
            release.asset_url("a.tar.gz").as_deref(),
            Some("https://example/a.tar.gz")
        );
        assert_eq!(release.asset_url("missing"), None);
        assert!(parse_release(&json!({"message": "Not Found"})).is_none());
    }

    #[test]
    fn timeout_resolution_prefers_config_and_falls_back_to_default() {
        let dir = tempfile::tempdir().expect("tempdir");
        assert_eq!(update_timeout(dir.path()), Duration::from_secs(30));
        storage::atomic_write_json(
            &dir.path().join("remy-config.json"),
            &json!({"schema_version": "1.0.0", "values": {"REMY_UPDATE_TIMEOUT": "90"}}),
        )
        .expect("write");
        assert_eq!(update_timeout(dir.path()), Duration::from_secs(90));
        storage::atomic_write_json(
            &dir.path().join("remy-config.json"),
            &json!({"schema_version": "1.0.0", "values": {"REMY_UPDATE_TIMEOUT": "junk"}}),
        )
        .expect("write");
        assert_eq!(update_timeout(dir.path()), Duration::from_secs(30));
    }

    #[test]
    fn tar_gz_extraction_finds_the_binary() {
        let staging = tempfile::tempdir().expect("tempdir");
        let archive_path = staging.path().join("remy-cc-v9.9.9-test.tar.gz");
        let file = std::fs::File::create(&archive_path).expect("archive");
        let encoder = flate2::write::GzEncoder::new(file, flate2::Compression::fast());
        let mut builder = tar::Builder::new(encoder);
        let payload = b"fake-binary";
        let mut header = tar::Header::new_gnu();
        header.set_size(payload.len() as u64);
        header.set_mode(0o755);
        header.set_cksum();
        builder
            .append_data(&mut header, "remy-cc", payload.as_slice())
            .expect("append");
        builder.into_inner().expect("tar").finish().expect("gzip");
        let extracted = extract_binary(&archive_path, "remy-cc", staging.path()).expect("extract");
        assert_eq!(std::fs::read(extracted).expect("read"), payload);
    }

    #[cfg(windows)]
    #[test]
    fn zip_extraction_via_system_tar_finds_the_binary() {
        let staging = tempfile::tempdir().expect("tempdir");
        let source = staging.path().join("remy-cc.exe");
        std::fs::write(&source, b"fake-binary").expect("source");
        let archive_path = staging.path().join("remy-cc-v9.9.9-test.zip");
        // System tar (libarchive, Windows 10+) writes zip with -a; the same
        // binary is the production extraction arm.
        let compressed = Command::new("tar")
            .arg("-a")
            .arg("-cf")
            .arg(&archive_path)
            .arg("-C")
            .arg(staging.path())
            .arg("remy-cc.exe")
            .output()
            .expect("tar")
            .status
            .success();
        assert!(compressed, "system tar must create the zip");
        std::fs::remove_file(&source).expect("remove source");
        let extracted =
            extract_binary(&archive_path, "remy-cc.exe", staging.path()).expect("extract");
        assert_eq!(std::fs::read(extracted).expect("read"), b"fake-binary");
    }

    #[test]
    fn swap_binary_displaces_the_old_image_and_registers_it() {
        let dir = tempfile::tempdir().expect("tempdir");
        let claude = dir.path().join("claude");
        let remy = dir.path().join("remy");
        std::fs::create_dir_all(&claude).expect("claude");
        std::fs::create_dir_all(remy.join("bin")).expect("bin");
        let deployed = remy.join("bin").join("remy-cc.exe");
        std::fs::write(&deployed, b"old").expect("old");
        let new_binary = dir.path().join("incoming.exe");
        std::fs::write(&new_binary, b"new").expect("new");
        let pending = PendingDeletes::new(&claude, &remy);

        swap_binary(&new_binary, &deployed, &pending).expect("swap");

        assert_eq!(std::fs::read(&deployed).expect("read"), b"new");
        let register = storage::load_json(&remy.join("install").join("pending_deletes.json"))
            .expect("register");
        let entries = register["paths"].as_array().expect("paths");
        assert_eq!(entries.len(), 1);
        assert!(entries[0].as_str().expect("entry").contains("old-"));
    }
}
