//! Platform divergence for process control, kept zero-`unsafe`:
//! - detach (Unix): `process_group(0)`; non-std fds are CLOEXEC, so the daemon
//!   inherits nothing beyond its nulled stdio;
//! - detach (Windows): via `powershell Start-Process` (ShellExecuteEx). Direct
//!   `CreateProcess` would run with `bInheritHandles=TRUE` (required by std for
//!   stdio), copying every inheritable handle of this process — including pipe
//!   write-ends owned by a caller capturing our output — into the long-lived
//!   daemon, so that caller would never observe EOF. ShellExecuteEx does not
//!   inherit arbitrary handles and breaks that chain;
//! - terminate: platform utility (`taskkill` / `kill`) as the R1.1 seam,
//!   replaced by the IPC shutdown command in R1.2.

use std::io;
use std::path::Path;
use std::process::{Command, Stdio};

/// Spawn `exe args` detached from the current console/terminal so the child
/// survives the parent CLI process and holds no handle of its pipes.
#[cfg(windows)]
pub fn spawn_detached(exe: &Path, args: &[&str]) -> io::Result<()> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    fn ps_quote(value: &str) -> String {
        format!("'{}'", value.replace('\'', "''"))
    }

    let arg_list = args
        .iter()
        .map(|arg| ps_quote(arg))
        .collect::<Vec<_>>()
        .join(",");
    let script = format!(
        "Start-Process -FilePath {} -ArgumentList {} -WindowStyle Hidden",
        ps_quote(&exe.to_string_lossy()),
        arg_list
    );

    let status = Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-Command", &script])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .status()?;
    if !status.success() {
        return Err(io::Error::other(format!(
            "Start-Process launcher exited with {status}"
        )));
    }
    Ok(())
}

/// Spawn `exe args` in a new process group; the daemon is adopted by init when
/// this CLI process exits and is not part of the terminal's foreground group.
#[cfg(unix)]
pub fn spawn_detached(exe: &Path, args: &[&str]) -> io::Result<()> {
    use std::os::unix::process::CommandExt;

    let mut cmd = Command::new(exe);
    cmd.args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0);

    // The daemon intentionally outlives this process; init reaps it.
    #[allow(clippy::zombie_processes)]
    cmd.spawn()?;
    Ok(())
}

/// Request termination of `pid`. Returns whether the utility reported success.
pub fn terminate(pid: u32) -> io::Result<bool> {
    #[cfg(windows)]
    let mut cmd = {
        let mut c = Command::new("taskkill");
        c.args(["/PID", &pid.to_string(), "/F"]);
        c
    };
    #[cfg(unix)]
    let mut cmd = {
        let mut c = Command::new("kill");
        c.args(["-TERM", &pid.to_string()]);
        c
    };

    let status = cmd.stdout(Stdio::null()).stderr(Stdio::null()).status()?;
    Ok(status.success())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminate_nonexistent_pid_returns_ok_false() {
        assert!(!terminate(4_000_000_000).unwrap());
    }

    #[cfg(windows)]
    fn cmd_exe() -> std::path::PathBuf {
        std::path::PathBuf::from(std::env::var_os("ComSpec").expect("ComSpec is always set"))
    }

    #[cfg(windows)]
    #[test]
    fn spawn_detached_arg_with_single_quote_is_escaped() {
        spawn_detached(&cmd_exe(), &["/C", "echo it's fine"]).unwrap();
    }

    #[cfg(windows)]
    #[test]
    fn spawn_detached_exe_path_with_spaces_and_quote_launches() {
        let dir = tempfile::tempdir().unwrap();
        let exe = dir.path().join("dir with space's").join("cmd copy.exe");
        std::fs::create_dir_all(exe.parent().unwrap()).unwrap();
        std::fs::copy(cmd_exe(), &exe).unwrap();
        spawn_detached(&exe, &["/C", "exit"]).unwrap();
    }
}
