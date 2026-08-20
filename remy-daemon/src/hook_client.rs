//! Claude Code hook clients with daemon IPC and Python fallback.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::env;
use std::fs;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, ExitCode, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::protocol::{Request, Response, MAX_LINE_BYTES, PROTOCOL_VERSION};
use crate::state::{Job, JobPriority, JobStatus, STATE_SCHEMA_VERSION};

// Windows scheduler-quantum floor (15.625 ms): connect needs >= 2 quanta.
// Read covers one round-trip plus the submit transaction's fsyncs
// (synchronous=FULL); 3 quanta proved insufficient under CI fsync spikes
// (M6 recurrence, run 87812349791), so read holds the pre-declared
// fallback budget of 6 quanta.
const CONNECT_TIMEOUT: Duration = Duration::from_millis(35);
const READ_TIMEOUT: Duration = Duration::from_millis(100);
const DIRTY_FALLBACK_TIMEOUT: Duration = Duration::from_secs(5);
const ENRICH_FALLBACK_TIMEOUT: Duration = Duration::from_secs(35);
const FALLBACK_STDOUT_LIMIT: usize = 1024 * 1024;
const FALLBACK_STDERR_LIMIT: usize = 64 * 1024;
const CHILD_POLL_INTERVAL: Duration = Duration::from_millis(10);
const CONFIG_SCHEMA_VERSION: &str = "1.0.0";
const SOURCE_EXTENSIONS: &[&str] = &[
    "py", "c", "h", "cpp", "hpp", "cc", "cxx", "hh", "hxx", "ts", "tsx", "rs",
];

#[derive(Debug, Clone, Copy)]
pub enum HookKind {
    Dirty,
    Enrich,
}

#[derive(Debug, Default, Deserialize, Serialize)]
struct ToolInput {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    file_path: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    path: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
struct HookInput {
    #[serde(default)]
    tool_name: String,
    #[serde(default)]
    tool_input: ToolInput,
    #[serde(default = "default_cwd")]
    cwd: String,
}

#[derive(Debug)]
struct HookConfig {
    db_path: PathBuf,
    tier_full_max: usize,
    tier_mid_max: usize,
    cap: usize,
    cap_large: usize,
    sig_max: usize,
}

#[derive(Debug)]
struct ClientError {
    kind: &'static str,
    detail: String,
}

impl ClientError {
    fn new(kind: &'static str, detail: impl Into<String>) -> Self {
        Self {
            kind,
            detail: detail.into(),
        }
    }
}

impl From<io::Error> for ClientError {
    fn from(error: io::Error) -> Self {
        Self::new("io", error.to_string())
    }
}

impl From<rusqlite::Error> for ClientError {
    fn from(error: rusqlite::Error) -> Self {
        Self::new("sqlite", error.to_string())
    }
}

struct IpcConnection {
    reader: BufReader<TcpStream>,
    writer: TcpStream,
    token: String,
}

#[derive(Debug)]
struct Captured {
    bytes: Vec<u8>,
    truncated: bool,
}

pub fn run(kind: HookKind) -> ExitCode {
    let input = match serde_json::from_reader::<_, HookInput>(io::stdin().lock()) {
        Ok(input) => input,
        Err(_) => return ExitCode::SUCCESS,
    };
    if !is_relevant(kind, &input) {
        return ExitCode::SUCCESS;
    }
    let output = match run_daemon_path(kind, &input) {
        Ok(output) => output,
        Err(_) => match run_python_fallback(kind, &input) {
            Ok(output) => output,
            Err(error) => {
                emit_diagnostic(&error);
                None
            }
        },
    };
    if let Some(output) = output {
        if let Ok(encoded) = serde_json::to_string(&output) {
            println!("{encoded}");
        }
    }
    ExitCode::SUCCESS
}

fn default_cwd() -> String {
    env::current_dir()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned()
}

fn is_relevant(kind: HookKind, input: &HookInput) -> bool {
    match kind {
        HookKind::Dirty => matches!(input.tool_name.as_str(), "Edit" | "Write"),
        HookKind::Enrich => matches!(input.tool_name.as_str(), "Read" | "Glob" | "Grep"),
    }
}

fn requested_path(kind: HookKind, input: &HookInput) -> Option<&str> {
    match kind {
        HookKind::Dirty => input.tool_input.file_path.as_deref(),
        HookKind::Enrich => input
            .tool_input
            .file_path
            .as_deref()
            .or(input.tool_input.path.as_deref()),
    }
}

fn run_daemon_path(kind: HookKind, input: &HookInput) -> Result<Option<Value>, ClientError> {
    let Some(requested) = requested_path(kind, input) else {
        return Ok(None);
    };
    let root = fs::canonicalize(&input.cwd)
        .map_err(|error| ClientError::new("project_path", error.to_string()))?;
    let Some(file_path) = normalize_source_path(&root, requested) else {
        return Ok(None);
    };
    let config = HookConfig::load(&root)?;
    match kind {
        HookKind::Dirty => {
            let mut ipc = IpcConnection::connect()?;
            ipc.submit(&root, &config.db_path, &file_path, JobPriority::Background)?;
            Ok(None)
        }
        HookKind::Enrich => {
            if dirty_queue_present(&root) {
                return Err(ClientError::new(
                    "dirty_queue",
                    "Python queue recovery required",
                ));
            }
            run_enrichment(&root, &file_path, &config)
        }
    }
}

impl HookConfig {
    fn load(root: &Path) -> Result<Self, ClientError> {
        let user_values = read_config_values(
            &crate::runtime::user_home()?
                .join(".claude")
                .join("remy-config.json"),
        );
        let project_values = read_config_values(&root.join(".claude").join("remy-config.json"));
        let db_raw = string_setting(
            "REMY_LOGIC_INDEX_DB_PATH",
            &project_values,
            &user_values,
            ".claude/logic_index.db",
        );
        let db_path = resolve_config_path(root, &db_raw);
        let mut config = Self {
            db_path,
            tier_full_max: int_setting(
                "REMY_ENRICHMENT_TIER_FULL_MAX",
                &project_values,
                &user_values,
                200,
                0,
                10_000,
            ),
            tier_mid_max: int_setting(
                "REMY_ENRICHMENT_TIER_MID_MAX",
                &project_values,
                &user_values,
                1_000,
                0,
                50_000,
            ),
            cap: int_setting(
                "REMY_ENRICHMENT_CAP",
                &project_values,
                &user_values,
                15,
                1,
                100,
            ),
            cap_large: int_setting(
                "REMY_ENRICHMENT_CAP_LARGE",
                &project_values,
                &user_values,
                10,
                1,
                100,
            ),
            sig_max: int_setting(
                "REMY_ENRICHMENT_SIG_MAX_CHARS",
                &project_values,
                &user_values,
                80,
                0,
                500,
            ),
        };
        if config.tier_full_max > config.tier_mid_max {
            config.tier_full_max = 200;
            config.tier_mid_max = 1_000;
        }
        if config.cap < config.cap_large {
            config.cap = 15;
            config.cap_large = 10;
        }
        Ok(config)
    }
}

fn read_config_values(path: &Path) -> HashMap<String, String> {
    let Ok(bytes) = fs::read(path) else {
        return HashMap::new();
    };
    let Ok(document) = serde_json::from_slice::<Value>(&bytes) else {
        return HashMap::new();
    };
    if document.get("schema_version").and_then(Value::as_str) != Some(CONFIG_SCHEMA_VERSION) {
        return HashMap::new();
    }
    document
        .get("values")
        .and_then(Value::as_object)
        .map(|values| {
            values
                .iter()
                .filter_map(|(key, value)| {
                    value.as_str().map(|value| (key.clone(), value.to_owned()))
                })
                .collect()
        })
        .unwrap_or_default()
}

fn setting_candidates<'a>(
    key: &str,
    project: &'a HashMap<String, String>,
    user: &'a HashMap<String, String>,
    default: &'a str,
) -> Vec<String> {
    let mut values = Vec::with_capacity(4);
    if let Ok(value) = env::var(key) {
        values.push(value);
    }
    if let Some(value) = project.get(key) {
        values.push(value.clone());
    }
    if let Some(value) = user.get(key) {
        values.push(value.clone());
    }
    values.push(default.to_owned());
    values
}

fn string_setting(
    key: &str,
    project: &HashMap<String, String>,
    user: &HashMap<String, String>,
    default: &str,
) -> String {
    setting_candidates(key, project, user, default)
        .into_iter()
        .find(|value| !value.is_empty())
        .unwrap_or_else(|| default.to_owned())
}

fn int_setting(
    key: &str,
    project: &HashMap<String, String>,
    user: &HashMap<String, String>,
    default: usize,
    minimum: usize,
    maximum: usize,
) -> usize {
    setting_candidates(key, project, user, &default.to_string())
        .into_iter()
        .filter_map(|value| value.trim().parse::<usize>().ok())
        .find(|value| *value >= minimum && *value <= maximum)
        .unwrap_or(default)
}

fn resolve_config_path(root: &Path, value: &str) -> PathBuf {
    let expanded = expand_tilde(value);
    let path = PathBuf::from(expanded);
    let candidate = if path.is_absolute() {
        path
    } else {
        root.join(path)
    };
    canonicalize_allow_missing(&candidate).unwrap_or(candidate)
}

fn expand_tilde(value: &str) -> String {
    if value == "~" {
        return crate::runtime::user_home()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
    }
    if let Some(rest) = value
        .strip_prefix("~/")
        .or_else(|| value.strip_prefix("~\\"))
    {
        if let Ok(home) = crate::runtime::user_home() {
            return home.join(rest).to_string_lossy().into_owned();
        }
    }
    value.to_owned()
}

fn normalize_source_path(root: &Path, file_path: &str) -> Option<String> {
    if file_path.is_empty() {
        return None;
    }
    let input = Path::new(file_path);
    let candidate = if input.is_absolute() {
        input.to_path_buf()
    } else {
        root.join(input)
    };
    let resolved = canonicalize_allow_missing(&candidate)?;
    let canonical_root = fs::canonicalize(root).ok()?;
    if resolved.is_dir() {
        return None;
    }
    let relative = resolved.strip_prefix(canonical_root).ok()?;
    let extension = relative.extension()?.to_string_lossy().to_ascii_lowercase();
    if !SOURCE_EXTENSIONS.contains(&extension.as_str()) {
        return None;
    }
    Some(relative.to_string_lossy().replace('\\', "/"))
}

fn canonicalize_allow_missing(path: &Path) -> Option<PathBuf> {
    if let Ok(resolved) = fs::canonicalize(path) {
        return Some(resolved);
    }
    let mut cursor = path;
    let mut missing = Vec::new();
    while !cursor.exists() {
        missing.push(cursor.file_name()?.to_os_string());
        cursor = cursor.parent()?;
    }
    let mut resolved = fs::canonicalize(cursor).ok()?;
    for component in missing.iter().rev() {
        resolved.push(component);
    }
    normalize_lexically(&resolved)
}

fn normalize_lexically(path: &Path) -> Option<PathBuf> {
    let mut result = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(_) | Component::RootDir | Component::Normal(_) => {
                result.push(component)
            }
            Component::CurDir => {}
            Component::ParentDir => {
                if !result.pop() {
                    return None;
                }
            }
        }
    }
    Some(result)
}

fn dirty_queue_present(root: &Path) -> bool {
    let claude = root.join(".claude");
    if claude.join("logic_index_dirty").exists()
        || claude.join("logic_index_dirty.processing").exists()
    {
        return true;
    }
    match fs::read_dir(claude) {
        Ok(entries) => entries.filter_map(Result::ok).any(|entry| {
            entry
                .file_name()
                .to_string_lossy()
                .starts_with("logic_index_dirty.pending.")
        }),
        Err(error) => error.kind() != io::ErrorKind::NotFound,
    }
}

impl IpcConnection {
    fn connect() -> Result<Self, ClientError> {
        let home = crate::remy_home().map_err(|message| ClientError::new("home", message))?;
        let run_dir = home.join("run");
        let port = crate::server::read_port(&run_dir)
            .ok_or_else(|| ClientError::new("endpoint", "daemon port is unavailable"))?;
        let token = crate::server::read_token(&run_dir)
            .ok_or_else(|| ClientError::new("endpoint", "daemon token is unavailable"))?;
        let address = SocketAddr::from(([127, 0, 0, 1], port));
        let stream = TcpStream::connect_timeout(&address, CONNECT_TIMEOUT)?;
        stream.set_read_timeout(Some(READ_TIMEOUT))?;
        stream.set_write_timeout(Some(READ_TIMEOUT))?;
        let _ = stream.set_nodelay(true);
        Ok(Self {
            reader: BufReader::new(stream.try_clone()?),
            writer: stream,
            token,
        })
    }

    fn request(&mut self, request: &Request) -> Result<Response, ClientError> {
        let encoded = serde_json::to_vec(request)
            .map_err(|error| ClientError::new("request_json", error.to_string()))?;
        self.writer.write_all(&encoded)?;
        self.writer.write_all(b"\n")?;
        self.writer.flush()?;
        let mut line = Vec::new();
        let read = (&mut self.reader)
            .take(MAX_LINE_BYTES + 1)
            .read_until(b'\n', &mut line)?;
        if read == 0 {
            return Err(ClientError::new("eof", "daemon closed without a response"));
        }
        if read as u64 > MAX_LINE_BYTES || !line.ends_with(b"\n") {
            return Err(ClientError::new(
                "response_size",
                "daemon response exceeds line limit",
            ));
        }
        serde_json::from_slice(&line)
            .map_err(|error| ClientError::new("response_json", error.to_string()))
    }

    fn submit(
        &mut self,
        project: &Path,
        db_path: &Path,
        file_path: &str,
        priority: JobPriority,
    ) -> Result<Job, ClientError> {
        let request = Request::SubmitJob {
            protocol_version: PROTOCOL_VERSION,
            state_schema_version: STATE_SCHEMA_VERSION,
            token: self.token.clone(),
            project_path: project.to_string_lossy().into_owned(),
            db_path: db_path.to_string_lossy().into_owned(),
            file_path: file_path.to_owned(),
            priority,
        };
        match self.request(&request)? {
            Response::Submitted { job, .. } => Ok(job),
            Response::Error { code, .. } => Err(ClientError::new("daemon_error", code)),
            _ => Err(ClientError::new(
                "response_type",
                "expected submitted response",
            )),
        }
    }

    fn latest_job(&mut self, project: &Path, file_path: &str) -> Result<Option<Job>, ClientError> {
        let request = Request::ListJobs {
            protocol_version: PROTOCOL_VERSION,
            state_schema_version: STATE_SCHEMA_VERSION,
            token: self.token.clone(),
            project_path: Some(project.to_string_lossy().into_owned()),
            file_path: Some(file_path.to_owned()),
            status: None,
            job_type: Some("incremental_scan".to_owned()),
            limit: Some(1),
        };
        match self.request(&request)? {
            Response::JobList { mut jobs, .. } => Ok(jobs.pop()),
            Response::Error { code, .. } => Err(ClientError::new("daemon_error", code)),
            _ => Err(ClientError::new(
                "response_type",
                "expected job_list response",
            )),
        }
    }

    fn promote(&mut self, job_id: i64) -> Result<Job, ClientError> {
        let request = Request::PromoteJob {
            protocol_version: PROTOCOL_VERSION,
            state_schema_version: STATE_SCHEMA_VERSION,
            token: self.token.clone(),
            job_id,
            priority: JobPriority::Interactive,
        };
        match self.request(&request)? {
            Response::Promoted { job, .. } => Ok(job),
            Response::Error { code, .. } => Err(ClientError::new("daemon_error", code)),
            _ => Err(ClientError::new(
                "response_type",
                "expected promoted response",
            )),
        }
    }
}

fn run_enrichment(
    root: &Path,
    target: &str,
    config: &HookConfig,
) -> Result<Option<Value>, ClientError> {
    let db = open_logic_db(&config.db_path)?;
    let mut targets = BTreeSet::from([target.to_owned()]);
    if let Some(connection) = db.as_ref() {
        for imported in load_imports(connection, target)? {
            if let Some(imported) = normalize_source_path(root, &imported) {
                targets.insert(imported);
            }
        }
    }

    let mut freshness: BTreeMap<&'static str, Vec<String>> = BTreeMap::new();
    let mut ipc = IpcConnection::connect()?;
    for file_path in &targets {
        let Some(job) = ipc.latest_job(root, file_path)? else {
            continue;
        };
        apply_job_state(&mut ipc, root, config, job, &mut freshness)?;
    }
    drop(ipc);

    let enrichment = db
        .as_ref()
        .map(|connection| build_enrichment(connection, target, config))
        .transpose()?
        .flatten();
    build_hook_output(enrichment, freshness)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum JobAction {
    None,
    Promote,
    Running,
    Retry,
}

fn job_action(status: JobStatus) -> JobAction {
    match status {
        JobStatus::Succeeded => JobAction::None,
        JobStatus::Pending => JobAction::Promote,
        JobStatus::Running => JobAction::Running,
        JobStatus::CancelRequested
        | JobStatus::Failed
        | JobStatus::Cancelled
        | JobStatus::Superseded => JobAction::Retry,
    }
}

fn apply_job_state(
    ipc: &mut IpcConnection,
    root: &Path,
    config: &HookConfig,
    job: Job,
    freshness: &mut BTreeMap<&'static str, Vec<String>>,
) -> Result<(), ClientError> {
    match job_action(job.status) {
        JobAction::None => {}
        JobAction::Promote => {
            let current = ipc.promote(job.id)?;
            match job_action(current.status) {
                JobAction::None => {}
                JobAction::Promote => push_freshness(freshness, "pending", &current.file_path),
                JobAction::Running => push_freshness(freshness, "running", &current.file_path),
                JobAction::Retry => {
                    ipc.submit(
                        root,
                        &config.db_path,
                        &current.file_path,
                        JobPriority::Interactive,
                    )?;
                    push_freshness(freshness, "retrying", &current.file_path);
                }
            }
        }
        JobAction::Running => push_freshness(freshness, "running", &job.file_path),
        JobAction::Retry => {
            ipc.submit(
                root,
                &config.db_path,
                &job.file_path,
                JobPriority::Interactive,
            )?;
            push_freshness(freshness, "retrying", &job.file_path);
        }
    }
    Ok(())
}

fn push_freshness(
    freshness: &mut BTreeMap<&'static str, Vec<String>>,
    status: &'static str,
    file_path: &str,
) {
    freshness
        .entry(status)
        .or_default()
        .push(file_path.to_owned());
}

fn open_logic_db(path: &Path) -> Result<Option<Connection>, ClientError> {
    if !path.exists() {
        return Ok(None);
    }
    Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map(Some)
        .map_err(ClientError::from)
}

fn load_imports(connection: &Connection, target: &str) -> Result<Vec<String>, ClientError> {
    let imports: Option<String> = connection
        .query_row(
            "SELECT imports FROM files WHERE path = ?1",
            [target],
            |row| row.get(0),
        )
        .optional()?;
    Ok(imports
        .as_deref()
        .and_then(|value| serde_json::from_str::<Vec<String>>(value).ok())
        .unwrap_or_default())
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum DetailLevel {
    Full,
    Mid,
    Minimal,
}

fn build_enrichment(
    connection: &Connection,
    target: &str,
    config: &HookConfig,
) -> Result<Option<String>, ClientError> {
    let file_row: Option<(Option<String>, Option<String>)> = connection
        .query_row(
            "SELECT layer, imports FROM files WHERE path = ?1",
            [target],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let Some((layer, imports_json)) = file_row else {
        return Ok(None);
    };
    let file_count: i64 =
        connection.query_row("SELECT COUNT(*) FROM files", [], |row| row.get(0))?;
    let (cap, detail) = if file_count > config.tier_mid_max as i64 {
        (config.cap_large, DetailLevel::Minimal)
    } else if file_count > config.tier_full_max as i64 {
        (config.cap, DetailLevel::Mid)
    } else {
        (config.cap, DetailLevel::Full)
    };
    let mut parts = vec![match layer {
        Some(layer) if !layer.is_empty() => format!("[Logic Context] {target} ({layer})"),
        _ => format!("[Logic Context] {target}"),
    }];

    let mut statement = connection.prepare(
        "SELECT DISTINCT callee_qualified FROM edges \
         WHERE source_file = ?1 AND callee_qualified IS NOT NULL LIMIT ?2",
    )?;
    let callees = statement
        .query_map((target, cap as i64), |row| row.get::<_, String>(0))?
        .collect::<Result<Vec<_>, _>>()?;
    if !callees.is_empty() {
        parts.push(format!(
            "  Calls into: {}",
            format_groups(connection, &callees, detail, config.sig_max)?.join(", ")
        ));
    }

    let pattern = format!("{target}::%");
    let mut statement = connection.prepare(
        "SELECT DISTINCT source_file || '::' || caller FROM edges \
         WHERE callee_qualified LIKE ?1 LIMIT ?2",
    )?;
    let callers = statement
        .query_map((&pattern, cap as i64), |row| row.get::<_, String>(0))?
        .collect::<Result<Vec<_>, _>>()?;
    if !callers.is_empty() {
        parts.push(format!(
            "  Called by: {}",
            format_groups(connection, &callers, detail, config.sig_max)?.join(", ")
        ));
    }

    if callees.is_empty() && callers.is_empty() {
        let imports = imports_json
            .as_deref()
            .and_then(|value| serde_json::from_str::<Vec<String>>(value).ok())
            .unwrap_or_default();
        if imports.is_empty() {
            return Ok(None);
        }
        parts.push(format!("  Imports: {}", imports.join(", ")));
    }
    Ok(Some(parts.join("\n")))
}

fn format_groups(
    connection: &Connection,
    qualified_names: &[String],
    detail: DetailLevel,
    sig_max: usize,
) -> Result<Vec<String>, ClientError> {
    let mut groups: Vec<(String, Option<String>, Vec<String>)> = Vec::new();
    for qualified in qualified_names {
        let (file_path, entry, layer) = symbol_detail(connection, qualified, detail, sig_max)?;
        let key = file_path.unwrap_or_else(|| "?".to_owned());
        if let Some((_, _, entries)) = groups.iter_mut().find(|(path, _, _)| path == &key) {
            entries.push(entry);
        } else {
            groups.push((key, layer, vec![entry]));
        }
    }
    Ok(groups
        .into_iter()
        .map(|(file_path, layer, entries)| {
            let layer = layer.map(|value| format!(" ({value})")).unwrap_or_default();
            if entries.len() == 1 {
                format!("{file_path}{layer}::{}", entries[0])
            } else {
                format!("{file_path}{layer}::{{{}}}", entries.join(", "))
            }
        })
        .collect())
}

type SymbolRow = (Option<i64>, Option<i64>, Option<String>, Option<String>);

fn symbol_detail(
    connection: &Connection,
    qualified: &str,
    detail: DetailLevel,
    sig_max: usize,
) -> Result<(Option<String>, String, Option<String>), ClientError> {
    let Some((file_path, name)) = qualified.split_once("::") else {
        return Ok((None, qualified.to_owned(), None));
    };
    let row: Option<SymbolRow> = connection
        .query_row(
            "SELECT lineno, end_lineno, args, layer FROM symbols s \
             JOIN files f ON s.file_path = f.path \
             WHERE s.file_path = ?1 AND s.name = ?2",
            (file_path, name),
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    let Some((line, end_line, args, layer)) = row else {
        return Ok((Some(file_path.to_owned()), name.to_owned(), None));
    };
    let range = match (line, end_line) {
        (Some(line), Some(end_line)) => format!(" [L{line}-L{end_line}]"),
        (Some(line), None) => format!(" [L{line}]"),
        _ => String::new(),
    };
    let entry = match detail {
        DetailLevel::Full => {
            let signature = args
                .as_deref()
                .map(|value| format_signature(value, sig_max))
                .unwrap_or_default();
            if signature.is_empty() {
                format!("{name}{range}")
            } else {
                format!("{name}{range} | {signature}")
            }
        }
        DetailLevel::Mid => format!("{name}{range}"),
        DetailLevel::Minimal => name.to_owned(),
    };
    Ok((Some(file_path.to_owned()), entry, layer))
}

fn format_signature(value: &str, maximum: usize) -> String {
    if value.is_empty() || maximum == 0 {
        return String::new();
    }
    if value.chars().count() <= maximum {
        return value.to_owned();
    }
    let prefix: String = value.chars().take(maximum).collect();
    let count = value.matches(',').count() + 1;
    format!("{prefix}... ({count} args)")
}

fn build_hook_output(
    enrichment: Option<String>,
    mut freshness: BTreeMap<&'static str, Vec<String>>,
) -> Result<Option<Value>, ClientError> {
    let mut lines = Vec::new();
    for status in ["pending", "running", "retrying"] {
        if let Some(files) = freshness.get_mut(status) {
            files.sort();
            files.dedup();
            lines.push(format!(
                "[Index freshness] status={status} files={}",
                files.join(",")
            ));
        }
    }
    if let Some(enrichment) = enrichment {
        lines.push(enrichment);
    }
    if lines.is_empty() {
        return Ok(None);
    }
    Ok(Some(json!({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": format!("<logic_context>\n{}\n</logic_context>", lines.join("\n")),
        }
    })))
}

fn run_python_fallback(kind: HookKind, input: &HookInput) -> Result<Option<Value>, ClientError> {
    let script = match kind {
        HookKind::Dirty => "logic_dirty_tracker.py",
        HookKind::Enrich => "logic_enrichment_hook.py",
    };
    let timeout = match kind {
        HookKind::Dirty => DIRTY_FALLBACK_TIMEOUT,
        HookKind::Enrich => ENRICH_FALLBACK_TIMEOUT,
    };
    let (python, script) = crate::runtime::hook_runtime(script)?;
    let payload = serde_json::to_vec(input)
        .map_err(|error| ClientError::new("fallback_input", error.to_string()))?;
    let mut child = Command::new(python)
        .arg(script)
        .current_dir(&input.cwd)
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    child
        .stdin
        .take()
        .ok_or_else(|| ClientError::new("fallback_stdin", "fallback stdin is unavailable"))?
        .write_all(&payload)?;
    let stdout = read_bounded(
        child
            .stdout
            .take()
            .ok_or_else(|| ClientError::new("fallback_stdout", "fallback stdout is unavailable"))?,
        FALLBACK_STDOUT_LIMIT,
    );
    let stderr = read_bounded(
        child
            .stderr
            .take()
            .ok_or_else(|| ClientError::new("fallback_stderr", "fallback stderr is unavailable"))?,
        FALLBACK_STDERR_LIMIT,
    );
    let status = wait_child(&mut child, timeout)?;
    let stdout = stdout
        .join()
        .map_err(|_| ClientError::new("fallback_stdout", "fallback stdout reader panicked"))?;
    let stderr = stderr
        .join()
        .map_err(|_| ClientError::new("fallback_stderr", "fallback stderr reader panicked"))?;
    if !status.success() {
        return Err(ClientError::new(
            "fallback_exit",
            String::from_utf8_lossy(&stderr.bytes).into_owned(),
        ));
    }
    if stdout.truncated {
        return Err(ClientError::new(
            "fallback_stdout",
            "fallback stdout exceeded 1 MiB",
        ));
    }
    if stderr.truncated {
        return Err(ClientError::new(
            "fallback_stderr",
            "fallback stderr exceeded 64 KiB",
        ));
    }
    let text = std::str::from_utf8(&stdout.bytes)
        .map_err(|error| ClientError::new("fallback_utf8", error.to_string()))?
        .trim();
    if text.is_empty() {
        return Ok(None);
    }
    let output: Value = serde_json::from_str(text)
        .map_err(|error| ClientError::new("fallback_json", error.to_string()))?;
    if !valid_hook_output(&output) {
        return Err(ClientError::new(
            "fallback_json",
            "fallback emitted invalid Hook JSON",
        ));
    }
    Ok(Some(output))
}

fn read_bounded(pipe: impl Read + Send + 'static, limit: usize) -> thread::JoinHandle<Captured> {
    thread::spawn(move || {
        let mut reader = BufReader::new(pipe);
        let mut retained = Vec::new();
        let mut truncated = false;
        let mut buffer = [0_u8; 8192];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) | Err(_) => break,
                Ok(count) => {
                    let remaining = limit.saturating_sub(retained.len());
                    retained.extend_from_slice(&buffer[..count.min(remaining)]);
                    if count > remaining {
                        truncated = true;
                    }
                }
            }
        }
        Captured {
            bytes: retained,
            truncated,
        }
    })
}

fn wait_child(
    child: &mut Child,
    timeout: Duration,
) -> Result<std::process::ExitStatus, ClientError> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(status);
        }
        if Instant::now() >= deadline {
            child.kill()?;
            let _ = child.wait();
            return Err(ClientError::new(
                "fallback_timeout",
                "fallback process timed out",
            ));
        }
        thread::sleep(CHILD_POLL_INTERVAL);
    }
}

fn valid_hook_output(value: &Value) -> bool {
    value
        .get("hookSpecificOutput")
        .and_then(Value::as_object)
        .is_some_and(|output| {
            output.get("hookEventName").and_then(Value::as_str) == Some("PreToolUse")
                && output.get("permissionDecision").and_then(Value::as_str) == Some("allow")
                && output
                    .get("additionalContext")
                    .and_then(Value::as_str)
                    .is_some()
        })
}

fn emit_diagnostic(error: &ClientError) {
    eprintln!("remy-hook: {}: {}", error.kind, sanitize(&error.detail));
}

fn sanitize(message: &str) -> String {
    let mut output = message.to_owned();
    for key in ["REMY_LLM_API_KEY", "OPENAI_API_KEY"] {
        if let Ok(secret) = env::var(key) {
            if !secret.is_empty() {
                output = output.replace(&secret, "<redacted>");
            }
        }
    }
    if let Ok(user_home) = crate::runtime::user_home() {
        output = output.replace(&user_home.to_string_lossy().to_string(), "~");
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typed_input_ignores_write_content() {
        let input: HookInput = serde_json::from_str(
            r#"{"tool_name":"Write","tool_input":{"file_path":"a.py","content":"large"},"cwd":"repo"}"#,
        )
        .unwrap();
        let encoded = serde_json::to_string(&input).unwrap();
        assert!(!encoded.contains("large"));
        assert!(encoded.contains("a.py"));
    }

    #[test]
    fn source_path_normalization_preserves_project_boundary() {
        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("sub").join("main.py");
        fs::create_dir_all(source.parent().unwrap()).unwrap();
        fs::write(&source, "x = 1\n").unwrap();
        assert_eq!(
            normalize_source_path(root.path(), source.to_str().unwrap()).as_deref(),
            Some("sub/main.py")
        );
        assert_eq!(
            normalize_source_path(root.path(), "sub/deleted.py").as_deref(),
            Some("sub/deleted.py")
        );
        assert_eq!(
            normalize_source_path(root.path(), "src/state.rs").as_deref(),
            Some("src/state.rs")
        );
        assert!(normalize_source_path(root.path(), "../outside.py").is_none());
        assert!(normalize_source_path(root.path(), "notes.txt").is_none());
    }

    #[test]
    fn signature_truncation_counts_unicode_characters() {
        assert_eq!(format_signature("甲乙丙,丁", 3), "甲乙丙... (2 args)");
    }

    #[test]
    fn freshness_output_is_sorted_and_structured() {
        let mut freshness = BTreeMap::new();
        freshness.insert("pending", vec!["b.py".to_owned(), "a.py".to_owned()]);
        let output = build_hook_output(None, freshness).unwrap().unwrap();
        assert_eq!(
            output["hookSpecificOutput"]["additionalContext"],
            "<logic_context>\n[Index freshness] status=pending files=a.py,b.py\n</logic_context>"
        );
    }

    #[test]
    fn fallback_reader_enforces_output_bound() {
        let captured = read_bounded(
            std::io::Cursor::new(vec![b'x'; FALLBACK_STDERR_LIMIT + 1]),
            FALLBACK_STDERR_LIMIT,
        )
        .join()
        .unwrap();
        assert_eq!(captured.bytes.len(), FALLBACK_STDERR_LIMIT);
        assert!(captured.truncated);
    }

    #[test]
    fn every_job_status_has_a_locked_action() {
        assert_eq!(job_action(JobStatus::Succeeded), JobAction::None);
        assert_eq!(job_action(JobStatus::Pending), JobAction::Promote);
        assert_eq!(job_action(JobStatus::Running), JobAction::Running);
        for status in [
            JobStatus::CancelRequested,
            JobStatus::Failed,
            JobStatus::Cancelled,
            JobStatus::Superseded,
        ] {
            assert_eq!(job_action(status), JobAction::Retry);
        }
    }

    #[test]
    fn invalid_hook_output_is_rejected() {
        assert!(!valid_hook_output(
            &json!({"message": "not a hook response"})
        ));
    }
}
