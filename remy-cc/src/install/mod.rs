//! Self-install subsystem: embedded Claude Code artifacts plus the
//! install/update/verify/uninstall machinery built on top of them.

use std::fmt;
use std::path::PathBuf;
use std::process::ExitCode;

pub(crate) mod delegate;
pub(crate) mod embedded;
pub(crate) mod interact;
pub(crate) mod lock;
pub(crate) mod manifest;
pub(crate) mod ops;
pub(crate) mod pending;
pub(crate) mod pyprobe;
pub(crate) mod settings;
pub(crate) mod storage;
pub(crate) mod update;
pub(crate) mod util;

/// Directory segment holding the deployed binary under the remy root.
pub(crate) const BIN_DIR: &str = "bin";

/// Directory segment holding installer state (manifest, lock, pending
/// deletes) under the remy root.
pub(crate) const INSTALL_STATE_DIR: &str = "install";

/// Template placeholder prefixes expanded against the resolved roots.
pub(crate) const CLAUDE_HOME_PLACEHOLDER: &str = "~/.claude/";
pub(crate) const REMY_HOME_PLACEHOLDER: &str = "~/.remy-cc/";

/// Managed roots: `CLAUDE_CONFIG_DIR` / `REMY_CC_HOME` override the
/// defaults under the user home (`HOME`, then `USERPROFILE`).
#[derive(Debug, Clone)]
pub(crate) struct Roots {
    pub(crate) claude_root: PathBuf,
    pub(crate) remy_root: PathBuf,
}

pub(crate) fn resolve_roots() -> Result<Roots, String> {
    let remy_root = crate::remy_home()?;
    let claude_root = match std::env::var_os("CLAUDE_CONFIG_DIR") {
        Some(value) if !value.is_empty() => PathBuf::from(value),
        _ => user_home()?.join(".claude"),
    };
    Ok(Roots {
        claude_root,
        remy_root,
    })
}

/// `remy-cc verify` entry.
pub(crate) fn run_verify() -> ExitCode {
    let roots = match resolve_roots() {
        Ok(roots) => roots,
        Err(message) => {
            eprintln!("remy-cc verify: {message}");
            return ExitCode::from(2);
        }
    };
    let warnings = ops::verify(&roots.claude_root, &roots.remy_root);
    if warnings.is_empty() {
        println!("remy-cc verify: passed");
        ExitCode::SUCCESS
    } else {
        for warning in &warnings {
            println!("  [!] {warning}");
        }
        eprintln!("remy-cc verify: {} issue(s) found", warnings.len());
        ExitCode::from(1)
    }
}

/// `remy-cc uninstall` entry.
pub(crate) fn run_uninstall(purge_state: bool, yes: bool) -> ExitCode {
    use std::io::IsTerminal;
    let roots = match resolve_roots() {
        Ok(roots) => roots,
        Err(message) => {
            eprintln!("remy-cc uninstall: {message}");
            return ExitCode::from(2);
        }
    };
    if !yes {
        if !std::io::stdin().is_terminal() {
            eprintln!("remy-cc uninstall: confirmation required; pass --yes");
            return ExitCode::from(1);
        }
        print!("This will remove all Remy-CC files and settings. Continue? [y/N] ");
        let _ = std::io::Write::flush(&mut std::io::stdout());
        let confirmed = interact::read_stdin_line()
            .is_some_and(|line| line.eq_ignore_ascii_case("y"));
        if !confirmed {
            println!("Uninstall cancelled.");
            return ExitCode::SUCCESS;
        }
    }
    match ops::uninstall(&roots.claude_root, &roots.remy_root, purge_state) {
        Ok(report) => {
            println!(
                "remy-cc uninstall: completed ({} file(s) removed)",
                report.changed.len()
            );
            for warning in &report.warnings {
                println!("  [!] {warning}");
            }
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("remy-cc uninstall: {error}");
            ExitCode::from(1)
        }
    }
}

fn user_home() -> Result<PathBuf, String> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or_else(|| "cannot determine home directory; set HOME or USERPROFILE".to_string())
}

/// `remy-cc install` entry: interactive resolution, the core operation, and
/// the post-install PATH/config surfaces.
pub(crate) fn run_install(language: Option<String>, non_interactive: bool) -> ExitCode {
    let roots = match resolve_roots() {
        Ok(roots) => roots,
        Err(message) => {
            eprintln!("remy-cc install: {message}");
            return ExitCode::from(2);
        }
    };
    let language =
        interact::resolve_language(language.as_deref(), non_interactive, &roots.claude_root);
    let source_binary = match std::env::current_exe() {
        Ok(path) => path,
        Err(error) => {
            eprintln!("remy-cc install: cannot resolve the current executable: {error}");
            return ExitCode::from(2);
        }
    };
    let params = ops::InstallParams {
        claude_root: roots.claude_root.clone(),
        remy_root: roots.remy_root.clone(),
        language: language.clone(),
        suite_version: env!("CARGO_PKG_VERSION").to_string(),
        source_binary,
        python: pyprobe::probe(),
        artifact_sha256: None,
    };
    match ops::install(&params) {
        Ok(report) => {
            println!(
                "remy-cc install: committed ({} file(s) written)",
                report.changed.len()
            );
            for warning in &report.warnings {
                println!("  [!] {warning}");
            }
            interact::register_path(&roots.remy_root.join(BIN_DIR), non_interactive);
            interact::print_config_guidance(&language);
            if report.post_commit_failed {
                eprintln!("remy-cc install: committed but post-install configuration failed");
                ExitCode::from(1)
            } else {
                ExitCode::SUCCESS
            }
        }
        Err(error) => {
            eprintln!("remy-cc install: {error}");
            ExitCode::from(1)
        }
    }
}

/// Error taxonomy inherited from the retired v3 installer: `metadata` means
/// managed metadata lacks required structure, `runtime` means a precondition
/// or ownership check failed. The constructors keep that classification in
/// the source; the error itself carries only the message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct InstallError {
    pub(crate) message: String,
}

impl InstallError {
    pub(crate) fn metadata(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }

    pub(crate) fn runtime(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for InstallError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for InstallError {}
