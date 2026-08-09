//! remy-daemon — Remy-CC resident daemon.
//!
//! Scope (R1.1 + R1.2): single-instance mutual exclusion, `start|stop|status`,
//! JSON line logging under `~/.remy-cc/log/`, loopback TCP IPC with version
//! handshake (hello/ping/shutdown). No index duties.
//!
//! Exit codes: 0 = success; 1 = already running (`start`) / not running
//! (`status`); 2 = unexpected error or timeout.

mod clock;
mod logging;
mod process;
mod protocol;
mod server;
mod single_instance;

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
    name = "remy-daemon",
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
    /// Report whether a daemon is running (exit 0 = running, 1 = not)
    Status,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let home = match remy_home() {
        Ok(home) => home,
        Err(message) => {
            eprintln!("remy-daemon: {message}");
            return ExitCode::from(2);
        }
    };

    let result = match cli.command {
        DaemonCommand::Start { foreground: true } => run_foreground(&home, &clock),
        DaemonCommand::Start { foreground: false } => start_detached(&home, &clock),
        DaemonCommand::Stop => stop(&home, &clock),
        DaemonCommand::Status => status(&home),
    };

    match result {
        Ok(code) => code,
        Err(err) => {
            eprintln!("remy-daemon: {err}");
            ExitCode::from(2)
        }
    }
}

fn remy_home() -> Result<PathBuf, String> {
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
            eprintln!("remy-daemon: already running");
            Ok(ExitCode::from(1))
        }
        AcquireOutcome::Acquired(_guard) => {
            single_instance::write_pid(&run_dir, std::process::id())?;
            let logger = JsonLogger::new(
                &home.join("log"),
                logging::DEFAULT_MAX_LOG_BYTES,
                Arc::clone(clock),
            )?;
            logger.log(
                "info",
                "daemon_started",
                json!({"version": env!("CARGO_PKG_VERSION")}),
            )?;
            server::serve(&run_dir, env!("CARGO_PKG_VERSION"), &logger)?;
            Ok(ExitCode::SUCCESS)
        }
    }
}

fn start_detached(home: &Path, clock: &Arc<dyn Clock>) -> io::Result<ExitCode> {
    let run_dir = run_dir(home);
    if single_instance::is_held(&run_dir)? {
        eprintln!("remy-daemon: already running{}", pid_suffix(&run_dir));
        return Ok(ExitCode::from(1));
    }

    let exe = std::env::current_exe()?;
    process::spawn_detached(&exe, &["start", "--foreground"])?;

    let started_at = clock.now();
    loop {
        if single_instance::is_held(&run_dir)? {
            println!("remy-daemon: started{}", pid_suffix(&run_dir));
            return Ok(ExitCode::SUCCESS);
        }
        let elapsed = clock.now().duration_since(started_at).unwrap_or_default();
        if elapsed >= START_WAIT {
            eprintln!(
                "remy-daemon: start timed out after {}s; check {}",
                START_WAIT.as_secs(),
                home.join("log").join(logging::LOG_FILE).display()
            );
            return Ok(ExitCode::from(2));
        }
        clock.sleep(START_POLL_INTERVAL);
    }
}

fn status(home: &Path) -> io::Result<ExitCode> {
    let run_dir = run_dir(home);
    if !single_instance::is_held(&run_dir)? {
        println!("remy-daemon: not running");
        return Ok(ExitCode::from(1));
    }
    let hello = protocol::Request::Hello {
        protocol_version: protocol::PROTOCOL_VERSION,
        token: server::read_token(&run_dir).unwrap_or_default(),
    };
    match ipc_roundtrip(&run_dir, &hello) {
        Some(protocol::Response::Ok(ok)) if ok.ok => {
            let version = ok.daemon_version.unwrap_or_else(|| "unknown".to_string());
            println!(
                "remy-daemon: running{} (version {version})",
                pid_suffix(&run_dir)
            );
        }
        _ => {
            println!(
                "remy-daemon: running{} (ipc-unresponsive)",
                pid_suffix(&run_dir)
            );
        }
    }
    Ok(ExitCode::SUCCESS)
}

fn stop(home: &Path, clock: &Arc<dyn Clock>) -> io::Result<ExitCode> {
    let run_dir = run_dir(home);
    if !single_instance::is_held(&run_dir)? {
        println!("remy-daemon: not running");
        return Ok(ExitCode::SUCCESS);
    }

    let shutdown = protocol::Request::Shutdown {
        token: server::read_token(&run_dir).unwrap_or_default(),
    };
    let acknowledged = matches!(
        ipc_roundtrip(&run_dir, &shutdown),
        Some(protocol::Response::Ok(ok)) if ok.ok
    );
    if !acknowledged {
        let Some(pid) = single_instance::read_pid(&run_dir) else {
            eprintln!(
                "remy-daemon: running but pid file is unreadable; terminate the process manually"
            );
            return Ok(ExitCode::from(2));
        };
        if !process::terminate(pid)? {
            eprintln!("remy-daemon: failed to terminate pid {pid}");
            return Ok(ExitCode::from(2));
        }
    }

    let requested_at = clock.now();
    loop {
        if !single_instance::is_held(&run_dir)? {
            println!("remy-daemon: stopped");
            return Ok(ExitCode::SUCCESS);
        }
        let elapsed = clock.now().duration_since(requested_at).unwrap_or_default();
        if elapsed >= STOP_WAIT {
            eprintln!(
                "remy-daemon: daemon still holds the lock after {}s",
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
fn ipc_roundtrip(run_dir: &Path, request: &protocol::Request) -> Option<protocol::Response> {
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
