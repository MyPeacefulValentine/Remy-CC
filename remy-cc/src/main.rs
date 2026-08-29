//! remy-cc — Remy-CC resident daemon.
//!
//! Scope (R1 + R2.1): single-instance lifecycle, JSON line logging, loopback
//! IPC version handshake, and persistent job registration/query/cancellation.
//! Scanner workers and hook clients remain outside this binary at R2.1.
//!
//! Exit codes: 0 = success; 1 = already running (`start`) / not running
//! (`status`); 2 = unexpected error or timeout.

mod clock;
mod hook_client;
mod install;
mod logging;
mod mcp;
mod process;
mod protocol;
mod provider;
mod runtime;
mod scheduler;
mod server;
mod single_instance;
mod state;
mod worker;

use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};
use serde_json::json;

use clock::{Clock, SystemClock};
use logging::JsonLogger;
use single_instance::AcquireOutcome;

/// Overrides the state root (default `~/.remy-cc`); primarily a test seam.
const HOME_ENV: &str = "REMY_CC_HOME";
const START_WAIT: Duration = Duration::from_secs(10);
const START_POLL_INTERVAL: Duration = Duration::from_millis(50);
const STOP_WAIT: Duration = Duration::from_secs(5);
const STOP_POLL_INTERVAL: Duration = Duration::from_millis(100);
const IPC_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Parser)]
#[command(
    name = "remy-cc",
    version,
    about = "Remy-CC resident daemon (R1.1 skeleton)"
)]
struct Cli {
    #[command(subcommand)]
    command: DaemonCommand,
}

#[derive(Subcommand)]
enum DaemonCommand {
    /// Start the daemon (detached by default)
    Start {
        /// Run in the foreground instead of detaching
        #[arg(long)]
        foreground: bool,
    },
    /// Stop a running daemon
    Stop,
    /// Restart the daemon (equivalent to start when it is not running)
    Restart,
    /// Print the daemon log (~/.remy-cc/log/daemon.log)
    Logs {
        /// Only print the last N lines
        #[arg(long)]
        tail: Option<usize>,
        /// Keep the log open and print appended lines
        #[arg(long)]
        follow: bool,
    },
    /// Report whether a daemon is running (exit 0 = running, 1 = not)
    Status {
        /// Emit a stable JSON diagnostic object
        #[arg(long)]
        json: bool,
    },
    /// Execute a Claude Code hook client
    Hook {
        #[command(subcommand)]
        command: HookCommand,
    },
    /// Serve the remy-index MCP read path over stdio (per-session, R4.1)
    Mcp,
    /// Install the suite: deploy the embedded Claude Code artifacts, this
    /// binary, and the managed settings entries (idempotent rerun, R4.4)
    Install {
        /// Interface language for the deployed artifacts
        #[arg(long, value_parser = ["en", "zh-CN"])]
        lang: Option<String>,
        /// Skip prompts; fall back to the deployed configuration
        #[arg(long)]
        non_interactive: bool,
    },
    /// Verify the installation: manifest hash reconciliation, settings
    /// claim, runtime descriptor, and daemon version comparison
    Verify,
    /// Self-update from the latest GitHub release (sha256 verified;
    /// provenance attestation checked when the gh CLI is available)
    Update,
    /// Remove the managed installation (project data and user settings
    /// entries are preserved)
    Uninstall {
        /// Also delete the engine state root (~/.remy-cc)
        #[arg(long)]
        purge_state: bool,
        /// Skip the confirmation prompt
        #[arg(long)]
        yes: bool,
    },
    /// Open the configuration UI (delegated to the deployed Python CLI)
    Config {
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    /// Recompute hierarchical summaries (delegated to the deployed Python CLI)
    SummaryRebuild {
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    /// Show a node's summary version history (delegated to the deployed Python CLI)
    SummaryAudit {
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    /// Prune old judge-cache entries (delegated to the deployed Python CLI)
    SummaryVacuum {
        #[arg(trailing_var_arg = true, allow_hyphen_values = true)]
        args: Vec<String>,
    },
    /// Scan a source tree into a logic index database (R3.4+: four-language
    /// per-file facts plus the single-transaction global postprocess;
    /// R3.5a: project scan lock, incremental exclusion/identity semantics)
    Scan {
        /// Project root to scan
        #[arg(long)]
        root: PathBuf,
        /// Target SQLite database path
        #[arg(long)]
        db: PathBuf,
        /// Incremental mode: only scan these files (relative to root)
        #[arg(long, num_args = 1..)]
        files: Vec<String>,
        /// Parse worker count (default: logical CPU count; 1 = serial baseline)
        #[arg(long)]
        jobs: Option<usize>,
        /// Emit the worker JSON Lines scan_result contract
        #[arg(long)]
        result_json: bool,
        /// Emit PROGRESS lines to stderr every 250 files (full scans only)
        #[arg(long)]
        progress: bool,
        /// Emit JSON Lines progress events (type=progress) to stdout
        #[arg(long)]
        progress_json: bool,
        /// Seconds to wait for the project scan lock
        /// (default: REMY_INDEX_SCAN_LOCK_TIMEOUT, 30)
        #[arg(long)]
        lock_timeout: Option<f64>,
    },
}

#[derive(Subcommand)]
enum HookCommand {
    /// Record a modified source file
    Dirty,
    /// Emit read-time logic context
    Enrich,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let command = match cli.command {
        DaemonCommand::Mcp => {
            return mcp::run();
        }
        DaemonCommand::Install {
            lang,
            non_interactive,
        } => {
            return install::run_install(lang, non_interactive);
        }
        DaemonCommand::Verify => {
            return install::run_verify();
        }
        DaemonCommand::Update => {
            return install::update::run_update();
        }
        DaemonCommand::Uninstall { purge_state, yes } => {
            return install::run_uninstall(purge_state, yes);
        }
        DaemonCommand::Config { args } => {
            return install::delegate::run_delegated("config", &args);
        }
        DaemonCommand::SummaryRebuild { args } => {
            return install::delegate::run_delegated("summary-rebuild", &args);
        }
        DaemonCommand::SummaryAudit { args } => {
            return install::delegate::run_delegated("summary-audit", &args);
        }
        DaemonCommand::SummaryVacuum { args } => {
            return install::delegate::run_delegated("summary-vacuum", &args);
        }
        DaemonCommand::Hook { command } => {
            return hook_client::run(match command {
                HookCommand::Dirty => hook_client::HookKind::Dirty,
                HookCommand::Enrich => hook_client::HookKind::Enrich,
            });
        }
        DaemonCommand::Scan {
            root,
            db,
            files,
            jobs,
            result_json,
            progress,
            progress_json,
            lock_timeout,
        } => {
            return ExitCode::from(scanner_core::scan::run_scan(
                &scanner_core::scan::ScanArgs {
                    root,
                    db,
                    files,
                    jobs,
                    result_json,
                    progress,
                    progress_json,
                    lock_timeout,
                },
            ));
        }
        command => command,
    };
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let home = match remy_home() {
        Ok(home) => home,
        Err(message) => {
            eprintln!("remy-cc: {message}");
            return ExitCode::from(2);
        }
    };

    let result = match command {
        DaemonCommand::Start { foreground: true } => run_foreground(&home, &clock),
        DaemonCommand::Start { foreground: false } => start_detached(&home, &clock),
        DaemonCommand::Stop => stop(&home, &clock),
        DaemonCommand::Restart => restart(&home, &clock),
        DaemonCommand::Logs { tail, follow } => logs(&home, tail, follow),
        DaemonCommand::Status { json } => status(&home, json),
        DaemonCommand::Hook { .. }
        | DaemonCommand::Scan { .. }
        | DaemonCommand::Mcp
        | DaemonCommand::Install { .. }
        | DaemonCommand::Verify
        | DaemonCommand::Update
        | DaemonCommand::Uninstall { .. }
        | DaemonCommand::Config { .. }
        | DaemonCommand::SummaryRebuild { .. }
        | DaemonCommand::SummaryAudit { .. }
        | DaemonCommand::SummaryVacuum { .. } => {
            unreachable!("dispatched before daemon home resolution")
        }
    };

    match result {
        Ok(code) => code,
        Err(err) => {
            eprintln!("remy-cc: {err}");
            ExitCode::from(2)
        }
    }
}

pub(crate) fn remy_home() -> Result<PathBuf, String> {
    if let Some(overridden) = std::env::var_os(HOME_ENV) {
        return Ok(PathBuf::from(overridden));
    }
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(|base| PathBuf::from(base).join(".remy-cc"))
        .ok_or_else(|| {
            format!("cannot determine home directory; set HOME, USERPROFILE or {HOME_ENV}")
        })
}

fn run_dir(home: &Path) -> PathBuf {
    home.join("run")
}

fn run_foreground(home: &Path, clock: &Arc<dyn Clock>) -> io::Result<ExitCode> {
    let run_dir = run_dir(home);
    match single_instance::acquire(&run_dir)? {
        AcquireOutcome::Held => {
            eprintln!("remy-cc: already running");
            Ok(ExitCode::from(1))
        }
        AcquireOutcome::Acquired(_guard) => {
            let residue = server::clean_stale_endpoints(&run_dir)?;
            let logger = JsonLogger::new(
                &home.join("log"),
                logging::DEFAULT_MAX_LOG_BYTES,
                Arc::clone(clock),
            )?;
            let (mut state, recovery) =
                state::StateStore::open(home, Arc::clone(clock)).map_err(io::Error::other)?;
            single_instance::write_pid(&run_dir, std::process::id())?;
            logger.log(
                "info",
                "daemon_started",
                json!({"version": env!("CARGO_PKG_VERSION")}),
            )?;
            if !residue.is_empty() {
                logger.log("info", "residue_cleaned", json!({"files": residue}))?;
            }
            if recovery != state::RecoveryReport::default() {
                logger.log(
                    "info",
                    "jobs_recovered",
                    json!({
                        "requeued": recovery.requeued,
                        "cancelled": recovery.cancelled,
                        "superseded": recovery.superseded,
                    }),
                )?;
            }
            let sync = provider::sync(&mut state, env!("CARGO_PKG_VERSION"), &logger)?;
            let full_scan_projects = sync.full_scan_project_ids.clone();
            let scanner_status = sync.status.clone();
            let (scheduler, _scheduler_thread) = scheduler::start(state, Arc::clone(clock))?;
            for project_id in full_scan_projects {
                if let Err(error) = scheduler.submit_full_scan(project_id) {
                    logger.log(
                        "warning",
                        "full_scan_submit_failed",
                        json!({"project_id": project_id, "error": error.to_string()}),
                    )?;
                }
            }
            server::serve(
                &run_dir,
                env!("CARGO_PKG_VERSION"),
                &logger,
                &scheduler,
                scanner_status,
            )?;
            scheduler.shutdown();
            Ok(ExitCode::SUCCESS)
        }
    }
}

fn start_detached(home: &Path, clock: &Arc<dyn Clock>) -> io::Result<ExitCode> {
    let run_dir = run_dir(home);
    if single_instance::is_held(&run_dir)? {
        eprintln!("remy-cc: already running{}", pid_suffix(&run_dir));
        return Ok(ExitCode::from(1));
    }

    let exe = std::env::current_exe()?;
    process::spawn_detached(&exe, &["start", "--foreground"])?;

    let started_at = clock.now();
    loop {
        if single_instance::is_held(&run_dir)?
            && server::read_port(&run_dir).is_some()
            && server::read_token(&run_dir).is_some()
        {
            println!("remy-cc: started{}", pid_suffix(&run_dir));
            return Ok(ExitCode::SUCCESS);
        }
        let elapsed = clock.now().duration_since(started_at).unwrap_or_default();
        if elapsed >= START_WAIT {
            eprintln!(
                "remy-cc: start timed out after {}s; check {}",
                START_WAIT.as_secs(),
                home.join("log").join(logging::LOG_FILE).display()
            );
            return Ok(ExitCode::from(2));
        }
        clock.sleep(START_POLL_INTERVAL);
    }
}

fn status(home: &Path, json_output: bool) -> io::Result<ExitCode> {
    let run_dir = run_dir(home);
    if !single_instance::is_held(&run_dir)? {
        if json_output {
            println!(
                "{}",
                status_json(
                    false,
                    false,
                    None,
                    Vec::new(),
                    Vec::new(),
                    None,
                    Some(("not_running", "daemon is not running"))
                )
            );
        } else {
            println!("remy-cc: not running");
        }
        return Ok(ExitCode::from(1));
    }
    let hello = protocol::Request::Hello {
        protocol_version: protocol::PROTOCOL_VERSION,
        token: server::read_token(&run_dir).unwrap_or_default(),
    };
    match ipc_roundtrip(&run_dir, &hello) {
        Some(protocol::Response::Hello { daemon_version, .. }) => {
            if json_output {
                let request = protocol::Request::StatusSnapshot {
                    protocol_version: protocol::PROTOCOL_VERSION,
                    state_schema_version: state::STATE_SCHEMA_VERSION,
                    token: server::read_token(&run_dir).unwrap_or_default(),
                };
                match ipc_roundtrip(&run_dir, &request) {
                    Some(protocol::Response::Status {
                        active_jobs,
                        recent_errors,
                        scanner,
                    }) => println!(
                        "{}",
                        status_json(
                            true,
                            true,
                            Some(daemon_version),
                            active_jobs,
                            recent_errors,
                            Some(scanner),
                            None
                        )
                    ),
                    _ => println!(
                        "{}",
                        status_json(
                            true,
                            false,
                            Some(daemon_version),
                            Vec::new(),
                            Vec::new(),
                            None,
                            Some(("invalid_response", "status response is invalid"))
                        )
                    ),
                }
            } else {
                println!(
                    "remy-cc: running{} (version {daemon_version})",
                    pid_suffix(&run_dir)
                );
            }
        }
        _ => {
            if json_output {
                println!(
                    "{}",
                    status_json(
                        true,
                        false,
                        None,
                        Vec::new(),
                        Vec::new(),
                        None,
                        Some(("ipc_unresponsive", "daemon IPC is unresponsive"))
                    )
                );
            } else {
                println!(
                    "remy-cc: running{} (ipc-unresponsive)",
                    pid_suffix(&run_dir)
                );
            }
        }
    }
    Ok(ExitCode::SUCCESS)
}

fn status_json(
    running: bool,
    ipc_responsive: bool,
    daemon_version: Option<String>,
    active_jobs: Vec<state::Job>,
    recent_errors: Vec<state::Job>,
    scanner: Option<protocol::ScannerStatus>,
    diagnostic: Option<(&str, &str)>,
) -> serde_json::Value {
    json!({
        "running": running,
        "ipc_responsive": ipc_responsive,
        "daemon_version": daemon_version,
        "protocol_version": protocol::PROTOCOL_VERSION,
        "state_schema_version": if running && ipc_responsive { Some(state::STATE_SCHEMA_VERSION) } else { None },
        "active_jobs": active_jobs,
        "recent_errors": recent_errors,
        "scanner": scanner,
        "diagnostic_error": diagnostic.map(|(kind, message)| json!({"kind": kind, "message": message})),
    })
}

/// systemctl semantics: a running daemon is stopped first; a stopped one
/// simply starts.
fn restart(home: &Path, clock: &Arc<dyn Clock>) -> io::Result<ExitCode> {
    if single_instance::is_held(&run_dir(home))? {
        let _ = stop(home, clock)?;
        if single_instance::is_held(&run_dir(home))? {
            eprintln!("remy-cc: daemon did not stop; restart aborted");
            return Ok(ExitCode::from(2));
        }
    }
    start_detached(home, clock)
}

fn logs(home: &Path, tail: Option<usize>, follow: bool) -> io::Result<ExitCode> {
    use std::io::{Read, Seek, SeekFrom};
    let path = home.join("log").join(logging::LOG_FILE);
    if !path.is_file() {
        eprintln!("remy-cc: no log file at {}", path.display());
        return Ok(ExitCode::from(1));
    }
    let content = std::fs::read_to_string(&path)?;
    let mut stdout = io::stdout().lock();
    match tail {
        Some(count) => {
            let lines: Vec<&str> = content.lines().collect();
            let start = lines.len().saturating_sub(count);
            for line in &lines[start..] {
                writeln!(stdout, "{line}")?;
            }
        }
        None => stdout.write_all(content.as_bytes())?,
    }
    stdout.flush()?;
    if !follow {
        return Ok(ExitCode::SUCCESS);
    }
    let mut offset = content.len() as u64;
    loop {
        std::thread::sleep(Duration::from_millis(500));
        let Ok(mut file) = std::fs::File::open(&path) else {
            continue;
        };
        let length = file.metadata()?.len();
        if length < offset {
            offset = 0;
        }
        if length > offset {
            file.seek(SeekFrom::Start(offset))?;
            let mut appended = String::new();
            file.read_to_string(&mut appended)?;
            offset = length;
            let mut stdout = io::stdout().lock();
            stdout.write_all(appended.as_bytes())?;
            stdout.flush()?;
        }
    }
}

fn stop(home: &Path, clock: &Arc<dyn Clock>) -> io::Result<ExitCode> {
    let run_dir = run_dir(home);
    if !single_instance::is_held(&run_dir)? {
        println!("remy-cc: not running");
        return Ok(ExitCode::SUCCESS);
    }

    let shutdown = protocol::Request::Shutdown {
        token: server::read_token(&run_dir).unwrap_or_default(),
    };
    let acknowledged = matches!(
        ipc_roundtrip(&run_dir, &shutdown),
        Some(protocol::Response::Ack)
    );
    if !acknowledged {
        let Some(pid) = single_instance::read_pid(&run_dir) else {
            eprintln!(
                "remy-cc: running but pid file is unreadable; terminate the process manually"
            );
            return Ok(ExitCode::from(2));
        };
        if !process::terminate(pid)? {
            eprintln!("remy-cc: failed to terminate pid {pid}");
            return Ok(ExitCode::from(2));
        }
    }

    let requested_at = clock.now();
    loop {
        if !single_instance::is_held(&run_dir)? {
            println!("remy-cc: stopped");
            return Ok(ExitCode::SUCCESS);
        }
        let elapsed = clock.now().duration_since(requested_at).unwrap_or_default();
        if elapsed >= STOP_WAIT {
            eprintln!(
                "remy-cc: daemon still holds the lock after {}s",
                STOP_WAIT.as_secs()
            );
            return Ok(ExitCode::from(2));
        }
        clock.sleep(STOP_POLL_INTERVAL);
    }
}

/// One-shot IPC exchange: connect to the port published in `run_dir`, send a
/// single request line, read a single response line. `None` on any transport
/// or parse failure — callers fall back to the R1.1 lock/pid paths (INV-R1).
pub(crate) fn ipc_roundtrip(
    run_dir: &Path,
    request: &protocol::Request,
) -> Option<protocol::Response> {
    let port = server::read_port(run_dir)?;
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let stream = TcpStream::connect_timeout(&addr, IPC_TIMEOUT).ok()?;
    stream.set_read_timeout(Some(IPC_TIMEOUT)).ok()?;
    stream.set_write_timeout(Some(IPC_TIMEOUT)).ok()?;

    let json = serde_json::to_string(request).ok()?;
    let mut writer = BufWriter::new(&stream);
    writeln!(writer, "{json}").ok()?;
    writer.flush().ok()?;

    let mut line = String::new();
    BufReader::new(&stream).read_line(&mut line).ok()?;
    serde_json::from_str(&line).ok()
}

fn pid_suffix(run_dir: &Path) -> String {
    single_instance::read_pid(run_dir)
        .map(|pid| format!(" (pid {pid})"))
        .unwrap_or_default()
}
