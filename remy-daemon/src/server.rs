//! IPC server: loopback TCP listener, session token, accept loop handling
//! hello/ping/shutdown over JSON Lines (R1.2).
//!
//! Token check uses plain equality: the token guards against accidental
//! cross-user connections on shared machines, not against attackers
//! (plan §4.2 R1.2: "非安全边界").

use std::fs::{self};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::time::Duration;

use crate::logging::JsonLogger;
use crate::protocol::{Request, Response, MAX_LINE_BYTES, PROTOCOL_VERSION};

pub const PORT_FILE: &str = "daemon.port";
pub const TOKEN_FILE: &str = "daemon.token";
const IO_TIMEOUT: Duration = Duration::from_secs(2);
const TOKEN_CHARS: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const TOKEN_LENGTH: usize = 43;

/// Bind the loopback listener, publish token then port (token first, so a
/// client that sees the port file is guaranteed to find the token), and run
/// the accept loop until a shutdown command arrives.
pub fn serve(run_dir: &Path, daemon_version: &str, logger: &JsonLogger) -> io::Result<()> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();

    let token = generate_token();
    write_token(run_dir, &token)?;
    write_port(run_dir, port)?;

    logger.log(
        "info",
        "ipc_listening",
        serde_json::json!({"port": port, "protocol_version": PROTOCOL_VERSION}),
    )?;

    loop {
        let (stream, _) = listener.accept()?;
        match handle_connection(&stream, &token, daemon_version) {
            ConnectionOutcome::Continue => {}
            ConnectionOutcome::Shutdown => {
                logger.log("info", "ipc_shutdown_requested", serde_json::Value::Null)?;
                return Ok(());
            }
        }
    }
}

enum ConnectionOutcome {
    Continue,
    Shutdown,
}

/// Serve one connection. Total bytes read per connection are capped at
/// MAX_LINE_BYTES via `Read::take`, which closes the unbounded `read_line`
/// buffering surface; the per-line length can never exceed the remaining cap.
fn handle_connection(stream: &TcpStream, token: &str, daemon_version: &str) -> ConnectionOutcome {
    let _ = stream.set_read_timeout(Some(IO_TIMEOUT));
    let _ = stream.set_write_timeout(Some(IO_TIMEOUT));

    let reader = BufReader::new(stream.take(MAX_LINE_BYTES));
    let mut writer = BufWriter::new(stream);

    for line in reader.lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => return ConnectionOutcome::Continue,
        };

        let request: Request = match serde_json::from_str(&line) {
            Ok(request) => request,
            Err(_) => {
                write_response(&mut writer, &Response::error("invalid_json"));
                continue;
            }
        };

        let request_token = match &request {
            Request::Hello { token, .. }
            | Request::Ping { token }
            | Request::Shutdown { token } => token,
        };
        if request_token != token {
            write_response(&mut writer, &Response::error("bad_token"));
            return ConnectionOutcome::Continue;
        }

        match request {
            Request::Hello { .. } => {
                write_response(&mut writer, &Response::hello(daemon_version.to_string()));
            }
            Request::Ping { .. } => {
                write_response(&mut writer, &Response::ok());
            }
            Request::Shutdown { .. } => {
                write_response(&mut writer, &Response::ok());
                return ConnectionOutcome::Shutdown;
            }
        }
    }
    ConnectionOutcome::Continue
}

fn write_response(writer: &mut impl Write, response: &Response) {
    if let Ok(json) = serde_json::to_string(response) {
        let _ = writeln!(writer, "{json}");
        let _ = writer.flush();
    }
}

/// Random token from `RandomState`, whose per-instance keys come from OS
/// entropy. Each 64-bit SipHash output yields ten 6-bit characters, so five
/// rounds cover TOKEN_LENGTH with independent bits per character.
fn generate_token() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};

    let state = RandomState::new();
    let mut token = String::with_capacity(TOKEN_LENGTH);
    let mut round: u64 = 0;
    while token.len() < TOKEN_LENGTH {
        let mut hasher = state.build_hasher();
        hasher.write_u64(round);
        let mut bits = hasher.finish();
        for _ in 0..10 {
            if token.len() == TOKEN_LENGTH {
                break;
            }
            token.push(TOKEN_CHARS[(bits & 63) as usize] as char);
            bits >>= 6;
        }
        round += 1;
    }
    token
}

fn write_token(run_dir: &Path, token: &str) -> io::Result<()> {
    fs::create_dir_all(run_dir)?;
    let path = run_dir.join(TOKEN_FILE);
    fs::write(&path, token)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn write_port(run_dir: &Path, port: u16) -> io::Result<()> {
    fs::write(run_dir.join(PORT_FILE), port.to_string())
}

/// Remove endpoint files left behind by a previous daemon process (crash-only
/// residue, INV-R3). Must only be called while holding the single-instance
/// lock: the lock guarantees the previous owner is gone, so the files describe
/// no live endpoint. Port is removed before token so no reader can observe a
/// port file without a readable token. Returns the names actually removed.
pub fn clean_stale_endpoints(run_dir: &Path) -> io::Result<Vec<&'static str>> {
    let mut removed = Vec::new();
    for name in [PORT_FILE, TOKEN_FILE] {
        match fs::remove_file(run_dir.join(name)) {
            Ok(()) => removed.push(name),
            Err(err) if err.kind() == io::ErrorKind::NotFound => {}
            Err(err) => return Err(err),
        }
    }
    Ok(removed)
}

pub fn read_port(run_dir: &Path) -> Option<u16> {
    fs::read_to_string(run_dir.join(PORT_FILE))
        .ok()?
        .trim()
        .parse()
        .ok()
}

pub fn read_token(run_dir: &Path) -> Option<String> {
    let token = fs::read_to_string(run_dir.join(TOKEN_FILE)).ok()?;
    let token = token.trim();
    if token.is_empty() {
        None
    } else {
        Some(token.to_string())
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::{Duration, SystemTime};

    use super::*;
    use crate::clock::fake::FakeClock;
    use crate::clock::Clock;

    fn test_logger(dir: &Path) -> JsonLogger {
        let clock = Arc::new(FakeClock::new(
            SystemTime::UNIX_EPOCH + Duration::from_secs(1_754_000_000),
        ));
        JsonLogger::new(dir, 1024 * 1024, clock as Arc<dyn Clock>).unwrap()
    }

    fn request_line(value: serde_json::Value) -> String {
        let mut line = value.to_string();
        line.push('\n');
        line
    }

    fn roundtrip(stream: &TcpStream, line: &str) -> serde_json::Value {
        let mut writer = BufWriter::new(stream);
        writer.write_all(line.as_bytes()).unwrap();
        writer.flush().unwrap();
        let mut reader = BufReader::new(stream);
        let mut response = String::new();
        reader.read_line(&mut response).unwrap();
        serde_json::from_str(&response).unwrap()
    }

    #[test]
    fn generate_token_has_expected_length_and_charset() {
        let token = generate_token();
        assert_eq!(token.len(), TOKEN_LENGTH);
        assert!(token.bytes().all(|b| TOKEN_CHARS.contains(&b)));
    }

    #[test]
    fn generate_token_produces_distinct_values() {
        assert_ne!(generate_token(), generate_token());
    }

    #[test]
    fn generate_token_characters_are_not_constant() {
        let token = generate_token();
        let first = token.as_bytes()[0];
        assert!(token.bytes().any(|b| b != first));
    }

    #[test]
    fn token_and_port_files_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        write_token(dir.path(), "secret").unwrap();
        write_port(dir.path(), 45678).unwrap();
        assert_eq!(read_token(dir.path()).as_deref(), Some("secret"));
        assert_eq!(read_port(dir.path()), Some(45678));
    }

    #[test]
    fn clean_stale_endpoints_removes_port_and_token() {
        let dir = tempfile::tempdir().unwrap();
        write_token(dir.path(), "old-token").unwrap();
        write_port(dir.path(), 12345).unwrap();

        let removed = clean_stale_endpoints(dir.path()).unwrap();

        assert_eq!(removed, vec![PORT_FILE, TOKEN_FILE]);
        assert_eq!(read_port(dir.path()), None);
        assert_eq!(read_token(dir.path()), None);
    }

    #[test]
    fn clean_stale_endpoints_on_clean_dir_is_noop() {
        let dir = tempfile::tempdir().unwrap();
        assert!(clean_stale_endpoints(dir.path()).unwrap().is_empty());
    }

    /// Non-NotFound removal errors must propagate (caller run_foreground then
    /// fails fast with exit 2) instead of serving next to unremovable residue.
    #[cfg(windows)]
    #[test]
    fn clean_stale_endpoints_propagates_sharing_violation() {
        use std::os::windows::fs::OpenOptionsExt;
        // FILE_SHARE_READ | FILE_SHARE_WRITE, without FILE_SHARE_DELETE, so
        // remove_file hits a sharing violation while the handle is held.
        const SHARE_READ_WRITE: u32 = 0x1 | 0x2;

        let dir = tempfile::tempdir().unwrap();
        write_port(dir.path(), 12345).unwrap();
        let _hold = fs::OpenOptions::new()
            .read(true)
            .share_mode(SHARE_READ_WRITE)
            .open(dir.path().join(PORT_FILE))
            .unwrap();

        let err = clean_stale_endpoints(dir.path()).unwrap_err();
        assert_ne!(err.kind(), io::ErrorKind::NotFound);
    }

    /// Same contract on Unix via an unwritable parent directory (assumes the
    /// test does not run as root, which is true for CI runners and dev shells).
    #[cfg(unix)]
    #[test]
    fn clean_stale_endpoints_propagates_permission_errors() {
        use std::os::unix::fs::PermissionsExt;

        let dir = tempfile::tempdir().unwrap();
        write_port(dir.path(), 12345).unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o555)).unwrap();

        let result = clean_stale_endpoints(dir.path());

        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::PermissionDenied);
    }

    #[test]
    fn read_port_and_token_absent_return_none() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(read_port(dir.path()), None);
        assert_eq!(read_token(dir.path()), None);
    }

    #[test]
    fn serve_answers_hello_ping_and_exits_on_shutdown() {
        let dir = tempfile::tempdir().unwrap();
        let run_dir = dir.path().join("run");
        let log_dir = dir.path().join("log");
        let logger = test_logger(&log_dir);

        let run_dir_clone = run_dir.clone();
        let handle = std::thread::spawn(move || {
            serve(&run_dir_clone, "0.1.0-test", &logger).unwrap();
        });

        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let port = loop {
            if let Some(port) = read_port(&run_dir) {
                break port;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "port file not written"
            );
            std::thread::sleep(Duration::from_millis(10));
        };
        let token = read_token(&run_dir).expect("token file written before port file");

        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let hello = roundtrip(
            &stream,
            &request_line(serde_json::json!({
                "cmd": "hello", "protocol_version": PROTOCOL_VERSION, "token": token,
            })),
        );
        assert_eq!(hello["ok"], true);
        assert_eq!(hello["protocol_version"], PROTOCOL_VERSION);
        assert_eq!(hello["daemon_version"], "0.1.0-test");

        let ping = roundtrip(
            &stream,
            &request_line(serde_json::json!({"cmd": "ping", "token": token})),
        );
        assert_eq!(ping, serde_json::json!({"ok": true}));

        let shutdown = roundtrip(
            &stream,
            &request_line(serde_json::json!({"cmd": "shutdown", "token": token})),
        );
        assert_eq!(shutdown, serde_json::json!({"ok": true}));

        handle.join().unwrap();
    }

    #[test]
    fn bad_token_is_rejected_and_connection_closed() {
        let dir = tempfile::tempdir().unwrap();
        let run_dir = dir.path().join("run");
        let logger = test_logger(&dir.path().join("log"));

        let run_dir_clone = run_dir.clone();
        let handle = std::thread::spawn(move || {
            serve(&run_dir_clone, "0.1.0-test", &logger).unwrap();
        });

        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let port = loop {
            if let Some(port) = read_port(&run_dir) {
                break port;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "port file not written"
            );
            std::thread::sleep(Duration::from_millis(10));
        };
        let token = read_token(&run_dir).unwrap();

        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let rejected = roundtrip(
            &stream,
            &request_line(serde_json::json!({"cmd": "ping", "token": "wrong"})),
        );
        assert_eq!(
            rejected,
            serde_json::json!({"ok": false, "error": "bad_token"})
        );

        let mut reader = BufReader::new(&stream);
        let mut rest = String::new();
        reader.read_line(&mut rest).unwrap();
        assert!(rest.is_empty(), "server should close after bad token");

        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let shutdown = roundtrip(
            &stream,
            &request_line(serde_json::json!({"cmd": "shutdown", "token": token})),
        );
        assert_eq!(shutdown["ok"], true);
        handle.join().unwrap();
    }

    #[test]
    fn invalid_json_gets_error_and_connection_stays_usable() {
        let dir = tempfile::tempdir().unwrap();
        let run_dir = dir.path().join("run");
        let logger = test_logger(&dir.path().join("log"));

        let run_dir_clone = run_dir.clone();
        let handle = std::thread::spawn(move || {
            serve(&run_dir_clone, "0.1.0-test", &logger).unwrap();
        });

        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let port = loop {
            if let Some(port) = read_port(&run_dir) {
                break port;
            }
            assert!(
                std::time::Instant::now() < deadline,
                "port file not written"
            );
            std::thread::sleep(Duration::from_millis(10));
        };
        let token = read_token(&run_dir).unwrap();

        let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let error = roundtrip(&stream, "this is not json\n");
        assert_eq!(
            error,
            serde_json::json!({"ok": false, "error": "invalid_json"})
        );

        let shutdown = roundtrip(
            &stream,
            &request_line(serde_json::json!({"cmd": "shutdown", "token": token})),
        );
        assert_eq!(shutdown["ok"], true);
        handle.join().unwrap();
    }
}
