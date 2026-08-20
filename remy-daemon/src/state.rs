//! Persistent daemon state: schema ownership, project registration, job state,
//! transactional deduplication, cancellation, and crash recovery.

use std::fmt;
use std::fs;
use std::path::{Component, Path};
use std::sync::Arc;
use std::time::{Duration, UNIX_EPOCH};

use rusqlite::backup::Backup;
use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::clock::Clock;

pub const STATE_DB_FILE: &str = "state.db";
pub const STATE_BACKUP_FILE: &str = "state.db.bak";
pub const STATE_SCHEMA_VERSION: u32 = 2;
pub const PROVIDER_PYTHON: &str = "python";
pub const PROVIDER_RUST: &str = "rust";
pub const JOB_TYPE_INCREMENTAL: &str = "incremental_scan";
pub const JOB_TYPE_FULL_SCAN: &str = "full_scan";
const FULL_SCAN_DEDUPE_KEY: &str = "full_scan";
const BUSY_TIMEOUT: Duration = Duration::from_secs(5);

#[cfg(test)]
const SCHEMA_V1: &str = r#"
CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    project_key TEXT NOT NULL UNIQUE,
    project_path TEXT NOT NULL,
    db_path TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    last_seen_at INTEGER NOT NULL CHECK (last_seen_at >= created_at)
);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    job_type TEXT NOT NULL CHECK (job_type = 'incremental_scan'),
    target_db_path TEXT NOT NULL,
    file_path TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'running', 'cancel_requested', 'succeeded', 'failed',
        'cancelled', 'superseded'
    )),
    progress_current INTEGER CHECK (progress_current IS NULL OR progress_current >= 0),
    progress_total INTEGER CHECK (progress_total IS NULL OR progress_total >= 0),
    progress_message TEXT,
    result_json TEXT,
    error_json TEXT,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    started_at INTEGER,
    finished_at INTEGER,
    dedupe_key TEXT NOT NULL,
    superseded_by_job_id INTEGER REFERENCES jobs(id),
    CHECK (
        progress_total IS NULL OR
        (progress_current IS NOT NULL AND progress_current <= progress_total)
    ),
    CHECK (NOT (result_json IS NOT NULL AND error_json IS NOT NULL)),
    CHECK (
        (status = 'pending' AND started_at IS NULL AND finished_at IS NULL) OR
        (status IN ('running', 'cancel_requested') AND started_at IS NOT NULL AND finished_at IS NULL) OR
        (status IN ('succeeded', 'failed') AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR
        (status IN ('cancelled', 'superseded') AND finished_at IS NOT NULL)
    ),
    CHECK (
        (status = 'succeeded' AND result_json IS NOT NULL AND error_json IS NULL) OR
        (status = 'failed' AND result_json IS NULL AND error_json IS NOT NULL) OR
        (status NOT IN ('succeeded', 'failed') AND result_json IS NULL AND error_json IS NULL)
    ),
    CHECK (
        (status = 'superseded' AND superseded_by_job_id IS NOT NULL) OR
        (status <> 'superseded' AND superseded_by_job_id IS NULL)
    )
);
CREATE UNIQUE INDEX jobs_one_pending
ON jobs(project_id, job_type, dedupe_key)
WHERE status = 'pending';
CREATE UNIQUE INDEX jobs_one_executing
ON jobs(project_id, job_type, dedupe_key)
WHERE status IN ('running', 'cancel_requested');
CREATE INDEX jobs_project_status ON jobs(project_id, status, priority DESC, created_at, id);
PRAGMA user_version = 1;
"#;

const JOBS_DDL_V2: &str = r#"
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    job_type TEXT NOT NULL CHECK (job_type IN ('incremental_scan', 'full_scan')),
    target_db_path TEXT NOT NULL,
    file_path TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority IN (0, 1)),
    provider TEXT NOT NULL DEFAULT 'python' CHECK (provider IN ('python', 'rust')),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'running', 'cancel_requested', 'succeeded', 'failed',
        'cancelled', 'superseded'
    )),
    progress_current INTEGER CHECK (progress_current IS NULL OR progress_current >= 0),
    progress_total INTEGER CHECK (progress_total IS NULL OR progress_total >= 0),
    progress_message TEXT,
    result_json TEXT,
    error_json TEXT,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    started_at INTEGER,
    finished_at INTEGER,
    dedupe_key TEXT NOT NULL,
    superseded_by_job_id INTEGER REFERENCES jobs(id),
    CHECK (
        progress_total IS NULL OR
        (progress_current IS NOT NULL AND progress_current <= progress_total)
    ),
    CHECK (NOT (result_json IS NOT NULL AND error_json IS NOT NULL)),
    CHECK (
        (status = 'pending' AND started_at IS NULL AND finished_at IS NULL) OR
        (status IN ('running', 'cancel_requested') AND started_at IS NOT NULL AND finished_at IS NULL) OR
        (status IN ('succeeded', 'failed') AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR
        (status IN ('cancelled', 'superseded') AND finished_at IS NOT NULL)
    ),
    CHECK (
        (status = 'succeeded' AND result_json IS NOT NULL AND error_json IS NULL) OR
        (status = 'failed' AND result_json IS NULL AND error_json IS NOT NULL) OR
        (status NOT IN ('succeeded', 'failed') AND result_json IS NULL AND error_json IS NULL)
    ),
    CHECK (
        (status = 'superseded' AND superseded_by_job_id IS NOT NULL) OR
        (status <> 'superseded' AND superseded_by_job_id IS NULL)
    )
);
"#;

const JOBS_INDEXES: &str = r#"
CREATE UNIQUE INDEX jobs_one_pending
ON jobs(project_id, job_type, dedupe_key)
WHERE status = 'pending';
CREATE UNIQUE INDEX jobs_one_executing
ON jobs(project_id, job_type, dedupe_key)
WHERE status IN ('running', 'cancel_requested');
CREATE INDEX jobs_project_status ON jobs(project_id, status, priority DESC, created_at, id);
"#;

const PUBLISHED_PROVIDER_DDL: &str = r#"
CREATE TABLE published_provider (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    provider TEXT NOT NULL CHECK (provider IN ('python', 'rust')),
    daemon_version TEXT NOT NULL,
    verified_at INTEGER NOT NULL CHECK (verified_at >= 0),
    probe_summary TEXT NOT NULL
);
"#;

const PROJECTS_DDL: &str = r#"
CREATE TABLE projects (
    id INTEGER PRIMARY KEY,
    project_key TEXT NOT NULL UNIQUE,
    project_path TEXT NOT NULL,
    db_path TEXT NOT NULL,
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    last_seen_at INTEGER NOT NULL CHECK (last_seen_at >= created_at)
);
"#;

#[derive(Debug)]
pub enum StateError {
    Sqlite(rusqlite::Error),
    Io(std::io::Error),
    InvalidInput(String),
    NotFound(i64),
    NotCancellable(JobStatus),
    InvalidTransition { from: JobStatus, to: JobStatus },
    Corrupt(String),
}

impl fmt::Display for StateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Sqlite(error) => write!(f, "state database error: {error}"),
            Self::Io(error) => write!(f, "state file error: {error}"),
            Self::InvalidInput(message) => write!(f, "invalid state input: {message}"),
            Self::NotFound(id) => write!(f, "job {id} not found"),
            Self::NotCancellable(status) => write!(f, "job in state {status} is not cancellable"),
            Self::InvalidTransition { from, to } => {
                write!(f, "invalid job transition: {from} -> {to}")
            }
            Self::Corrupt(message) => write!(f, "state database is corrupt: {message}"),
        }
    }
}

impl StateError {
    pub fn is_fatal(&self) -> bool {
        match self {
            Self::Corrupt(_) | Self::Io(_) => true,
            Self::Sqlite(rusqlite::Error::SqliteFailure(error, _)) => matches!(
                error.code,
                rusqlite::ErrorCode::DatabaseCorrupt
                    | rusqlite::ErrorCode::NotADatabase
                    | rusqlite::ErrorCode::SystemIoFailure
                    | rusqlite::ErrorCode::CannotOpen
            ),
            Self::Sqlite(_)
            | Self::InvalidInput(_)
            | Self::NotFound(_)
            | Self::NotCancellable(_)
            | Self::InvalidTransition { .. } => false,
        }
    }
}

impl std::error::Error for StateError {}

impl From<rusqlite::Error> for StateError {
    fn from(value: rusqlite::Error) -> Self {
        Self::Sqlite(value)
    }
}

impl From<std::io::Error> for StateError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

pub type StateResult<T> = Result<T, StateError>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum JobPriority {
    Background,
    Interactive,
}

impl JobPriority {
    fn as_db(self) -> i64 {
        match self {
            Self::Background => 0,
            Self::Interactive => 1,
        }
    }

    fn from_db(value: i64) -> StateResult<Self> {
        match value {
            0 => Ok(Self::Background),
            1 => Ok(Self::Interactive),
            _ => Err(StateError::Corrupt(format!("invalid priority {value}"))),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum JobStatus {
    Pending,
    Running,
    CancelRequested,
    Succeeded,
    Failed,
    Cancelled,
    Superseded,
}

impl JobStatus {
    pub(crate) fn as_db(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Running => "running",
            Self::CancelRequested => "cancel_requested",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::Superseded => "superseded",
        }
    }

    fn from_db(value: &str) -> StateResult<Self> {
        match value {
            "pending" => Ok(Self::Pending),
            "running" => Ok(Self::Running),
            "cancel_requested" => Ok(Self::CancelRequested),
            "succeeded" => Ok(Self::Succeeded),
            "failed" => Ok(Self::Failed),
            "cancelled" => Ok(Self::Cancelled),
            "superseded" => Ok(Self::Superseded),
            _ => Err(StateError::Corrupt(format!("invalid job status {value}"))),
        }
    }
}

impl fmt::Display for JobStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_db())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
pub struct Job {
    pub id: i64,
    pub project_id: i64,
    pub project_path: String,
    pub target_db_path: String,
    pub job_type: String,
    pub file_path: String,
    pub priority: JobPriority,
    pub provider: String,
    pub status: JobStatus,
    pub progress_current: Option<i64>,
    pub progress_total: Option<i64>,
    pub progress_message: Option<String>,
    pub result: Option<Value>,
    pub error: Option<Value>,
    pub created_at: i64,
    pub started_at: Option<i64>,
    pub finished_at: Option<i64>,
    pub superseded_by_job_id: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubmitJob {
    pub project_path: String,
    pub db_path: String,
    pub file_path: String,
    pub priority: JobPriority,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubmitResult {
    pub job: Job,
    pub created: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PromoteResult {
    pub job: Job,
    pub changed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ListJobs {
    pub project_path: Option<String>,
    pub file_path: Option<String>,
    pub status: Option<JobStatus>,
    pub job_type: Option<String>,
    pub limit: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProgressUpdate {
    pub current: i64,
    pub total: i64,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CancelResult {
    pub job: Job,
    pub changed: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
pub struct PublishedProvider {
    pub provider: String,
    pub daemon_version: String,
    pub verified_at: i64,
    pub probe_summary: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct RecoveryReport {
    pub requeued: usize,
    pub cancelled: usize,
    pub superseded: usize,
}

pub struct StateStore {
    connection: Connection,
    clock: Arc<dyn Clock>,
}

impl StateStore {
    pub fn open(home: &Path, clock: Arc<dyn Clock>) -> StateResult<(Self, RecoveryReport)> {
        fs::create_dir_all(home)?;
        let path = home.join(STATE_DB_FILE);
        let existed = path.exists();
        let mut connection = Connection::open(&path)?;
        connection.busy_timeout(BUSY_TIMEOUT)?;
        connection.pragma_update(None, "foreign_keys", "ON")?;

        initialize_or_migrate(&mut connection, &path, existed)?;
        connection.pragma_update(None, "journal_mode", "WAL")?;
        connection.pragma_update(None, "synchronous", "FULL")?;
        set_private_permissions(&path)?;
        let mut store = Self { connection, clock };
        let report = store.recover()?;
        Ok((store, report))
    }

    pub fn submit(&mut self, request: SubmitJob) -> StateResult<SubmitResult> {
        let project_path = normalize_existing_project(&request.project_path)?;
        let project_key = project_key(&project_path);
        let db_path = normalize_database_path(&request.db_path)?;
        let file_path = normalize_source_path(&request.file_path)?;
        let dedupe_key = format!("incremental_scan:{file_path}");
        let now = self.now_millis()?;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;

        tx.execute(
            "INSERT INTO projects(project_key, project_path, db_path, created_at, last_seen_at) \
             VALUES (?1, ?2, ?3, ?4, ?4) \
             ON CONFLICT(project_key) DO UPDATE SET \
                 project_path = excluded.project_path, \
                 db_path = excluded.db_path, \
                 last_seen_at = excluded.last_seen_at",
            params![project_key, project_path, db_path, now],
        )?;
        let project_id: i64 = tx.query_row(
            "SELECT id FROM projects WHERE project_key = ?1",
            params![project_key],
            |row| row.get(0),
        )?;

        let absorbing_full_scan: Option<i64> = tx
            .query_row(
                "SELECT id FROM jobs \
                 WHERE project_id = ?1 AND job_type = 'full_scan' AND status = 'pending'",
                params![project_id],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(full_scan_id) = absorbing_full_scan {
            tx.execute(
                "INSERT INTO jobs( \
                    project_id, job_type, target_db_path, file_path, priority, status, \
                    created_at, finished_at, dedupe_key, superseded_by_job_id \
                 ) VALUES (?1, 'incremental_scan', ?2, ?3, ?4, 'superseded', ?5, ?5, ?6, ?7)",
                params![
                    project_id,
                    db_path,
                    file_path,
                    request.priority.as_db(),
                    now,
                    dedupe_key,
                    full_scan_id
                ],
            )?;
            let job = load_job(&tx, tx.last_insert_rowid())?;
            tx.commit()?;
            return Ok(SubmitResult { job, created: true });
        }

        let existing_id: Option<i64> = tx
            .query_row(
                "SELECT id FROM jobs \
                 WHERE project_id = ?1 AND job_type = 'incremental_scan' \
                   AND dedupe_key = ?2 AND status = 'pending'",
                params![project_id, dedupe_key],
                |row| row.get(0),
            )
            .optional()?;

        let (job_id, created) = if let Some(job_id) = existing_id {
            tx.execute(
                "UPDATE jobs SET priority = MAX(priority, ?1) WHERE id = ?2",
                params![request.priority.as_db(), job_id],
            )?;
            (job_id, false)
        } else {
            tx.execute(
                "INSERT INTO jobs( \
                    project_id, job_type, target_db_path, file_path, priority, status, \
                    created_at, dedupe_key \
                 ) VALUES (?1, 'incremental_scan', ?2, ?3, ?4, 'pending', ?5, ?6)",
                params![
                    project_id,
                    db_path,
                    file_path,
                    request.priority.as_db(),
                    now,
                    dedupe_key
                ],
            )?;
            (tx.last_insert_rowid(), true)
        };
        let job = load_job(&tx, job_id)?;
        tx.commit()?;
        Ok(SubmitResult { job, created })
    }

    pub fn get(&self, job_id: i64) -> StateResult<Job> {
        load_job(&self.connection, job_id)
    }

    /// Queue-level supersede fold: a pending full_scan absorbs every pending
    /// incremental job of the project, and later incremental submissions are
    /// recorded as superseded by it (see submit).
    pub fn submit_full_scan(&mut self, project_id: i64) -> StateResult<SubmitResult> {
        let now = self.now_millis()?;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let project: Option<(String, String)> = tx
            .query_row(
                "SELECT project_path, db_path FROM projects WHERE id = ?1",
                params![project_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((_, db_path)) = project else {
            return Err(StateError::InvalidInput(format!(
                "project {project_id} is not registered"
            )));
        };

        let existing_id: Option<i64> = tx
            .query_row(
                "SELECT id FROM jobs \
                 WHERE project_id = ?1 AND job_type = 'full_scan' \
                   AND status IN ('pending', 'running', 'cancel_requested')",
                params![project_id],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(job_id) = existing_id {
            let job = load_job(&tx, job_id)?;
            tx.commit()?;
            return Ok(SubmitResult {
                job,
                created: false,
            });
        }

        tx.execute(
            "INSERT INTO jobs( \
                project_id, job_type, target_db_path, file_path, priority, status, \
                created_at, dedupe_key \
             ) VALUES (?1, 'full_scan', ?2, '.', 0, 'pending', ?3, ?4)",
            params![project_id, db_path, now, FULL_SCAN_DEDUPE_KEY],
        )?;
        let full_scan_id = tx.last_insert_rowid();
        tx.execute(
            "UPDATE jobs SET status = 'superseded', finished_at = ?1, \
                 superseded_by_job_id = ?2 \
             WHERE project_id = ?3 AND job_type = 'incremental_scan' AND status = 'pending'",
            params![now, full_scan_id, project_id],
        )?;
        let job = load_job(&tx, full_scan_id)?;
        tx.commit()?;
        Ok(SubmitResult { job, created: true })
    }

    pub fn project_ids(&self) -> StateResult<Vec<i64>> {
        let mut statement = self
            .connection
            .prepare("SELECT id FROM projects ORDER BY id")?;
        let ids = statement
            .query_map([], |row| row.get(0))?
            .collect::<Result<Vec<i64>, _>>()?;
        Ok(ids)
    }

    pub fn published_provider(&self) -> StateResult<Option<PublishedProvider>> {
        self.connection
            .query_row(
                "SELECT provider, daemon_version, verified_at, probe_summary \
                 FROM published_provider WHERE id = 1",
                [],
                |row| {
                    Ok(PublishedProvider {
                        provider: row.get(0)?,
                        daemon_version: row.get(1)?,
                        verified_at: row.get(2)?,
                        probe_summary: row.get(3)?,
                    })
                },
            )
            .optional()
            .map_err(StateError::from)
    }

    pub fn publish_provider(
        &mut self,
        provider: &str,
        daemon_version: &str,
        probe_summary: &str,
    ) -> StateResult<PublishedProvider> {
        if !matches!(provider, PROVIDER_PYTHON | PROVIDER_RUST) {
            return Err(StateError::InvalidInput(format!(
                "unsupported provider {provider}"
            )));
        }
        let now = self.now_millis()?;
        self.connection.execute(
            "INSERT INTO published_provider(id, provider, daemon_version, verified_at, probe_summary) \
             VALUES (1, ?1, ?2, ?3, ?4) \
             ON CONFLICT(id) DO UPDATE SET \
                 provider = excluded.provider, \
                 daemon_version = excluded.daemon_version, \
                 verified_at = excluded.verified_at, \
                 probe_summary = excluded.probe_summary",
            params![provider, daemon_version, now, probe_summary],
        )?;
        Ok(PublishedProvider {
            provider: provider.to_string(),
            daemon_version: daemon_version.to_string(),
            verified_at: now,
            probe_summary: probe_summary.to_string(),
        })
    }

    pub fn promote(&mut self, job_id: i64, priority: JobPriority) -> StateResult<PromoteResult> {
        let changed = self.connection.execute(
            "UPDATE jobs SET priority = MAX(priority, ?1) \
             WHERE id = ?2 AND status = 'pending' AND priority < ?1",
            params![priority.as_db(), job_id],
        )? == 1;
        Ok(PromoteResult {
            job: self.get(job_id)?,
            changed,
        })
    }

    pub fn claim_next_pending(&mut self, provider: &str) -> StateResult<Option<Job>> {
        if !matches!(provider, PROVIDER_PYTHON | PROVIDER_RUST) {
            return Err(StateError::InvalidInput(format!(
                "unsupported provider {provider}"
            )));
        }
        let now = self.now_millis()?;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let job_id: Option<i64> = tx
            .query_row(
                "SELECT j.id FROM jobs j \
                 WHERE j.status = 'pending' \
                   AND NOT EXISTS ( \
                       SELECT 1 FROM jobs active \
                       WHERE active.project_id = j.project_id \
                         AND active.job_type = j.job_type \
                         AND active.dedupe_key = j.dedupe_key \
                         AND active.status IN ('running', 'cancel_requested') \
                   ) \
                 ORDER BY j.priority DESC, j.created_at ASC, j.id ASC LIMIT 1",
                [],
                |row| row.get(0),
            )
            .optional()?;
        let Some(job_id) = job_id else {
            tx.commit()?;
            return Ok(None);
        };
        if !transition_job(
            &tx,
            job_id,
            JobStatus::Pending,
            JobStatus::Running,
            now,
            None,
        )? {
            return Err(StateError::Corrupt(format!(
                "pending job {job_id} changed during claim"
            )));
        }
        tx.execute(
            "UPDATE jobs SET provider = ?1, progress_current = 0, progress_total = 3, \
                 progress_message = 'runtime_validation' WHERE id = ?2",
            params![provider, job_id],
        )?;
        let job = load_job(&tx, job_id)?;
        tx.commit()?;
        Ok(Some(job))
    }

    pub fn update_progress(&mut self, job_id: i64, update: ProgressUpdate) -> StateResult<Job> {
        if update.current < 0 || update.total < 0 || update.current > update.total {
            return Err(StateError::InvalidInput(
                "progress must satisfy 0 <= current <= total".to_string(),
            ));
        }
        let changed = self.connection.execute(
            "UPDATE jobs SET progress_current = ?1, progress_total = ?2, \
                 progress_message = ?3 \
             WHERE id = ?4 AND status IN ('running', 'cancel_requested') \
               AND (progress_current IS NULL OR progress_current <= ?1)",
            params![update.current, update.total, update.message, job_id],
        )?;
        if changed != 1 {
            let current = self.get(job_id)?;
            return Err(StateError::InvalidTransition {
                from: current.status,
                to: current.status,
            });
        }
        self.get(job_id)
    }

    pub fn complete_success(&mut self, job_id: i64, result: Value) -> StateResult<Job> {
        self.complete(job_id, JobStatus::Succeeded, result)
    }

    pub fn complete_failure(&mut self, job_id: i64, error: Value) -> StateResult<Job> {
        self.complete(job_id, JobStatus::Failed, error)
    }

    pub fn confirm_cancelled(&mut self, job_id: i64) -> StateResult<Job> {
        let now = self.now_millis()?;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current = load_job(&tx, job_id)?;
        if current.status != JobStatus::CancelRequested {
            return Err(StateError::InvalidTransition {
                from: current.status,
                to: JobStatus::Cancelled,
            });
        }
        transition_job(
            &tx,
            job_id,
            JobStatus::CancelRequested,
            JobStatus::Cancelled,
            now,
            None,
        )?;
        let job = load_job(&tx, job_id)?;
        tx.commit()?;
        Ok(job)
    }

    pub fn list(&self, filter: ListJobs) -> StateResult<Vec<Job>> {
        let limit = filter.limit.clamp(1, 200) as i64;
        let project_key = filter
            .project_path
            .as_deref()
            .map(normalize_project_filter)
            .transpose()?;
        let file_path = filter
            .file_path
            .as_deref()
            .map(normalize_source_path)
            .transpose()?;
        let status = filter.status.map(JobStatus::as_db);
        let job_type = filter.job_type.as_deref();
        if let Some(value) = job_type {
            if !matches!(value, JOB_TYPE_INCREMENTAL | JOB_TYPE_FULL_SCAN) {
                return Err(StateError::InvalidInput(format!(
                    "unsupported job_type {value}"
                )));
            }
        }
        let mut statement = self.connection.prepare(
            "SELECT j.id FROM jobs j JOIN projects p ON p.id = j.project_id \
             WHERE (?1 IS NULL OR p.project_key = ?1) \
               AND (?2 IS NULL OR j.file_path = ?2) \
               AND (?3 IS NULL OR j.status = ?3) \
               AND (?4 IS NULL OR j.job_type = ?4) \
             ORDER BY j.created_at DESC, j.id DESC LIMIT ?5",
        )?;
        let ids = statement
            .query_map(
                params![project_key, file_path, status, job_type, limit],
                |row| row.get(0),
            )?
            .collect::<Result<Vec<i64>, _>>()?;
        ids.into_iter()
            .map(|job_id| load_job(&self.connection, job_id))
            .collect()
    }

    pub fn recent_failed(&self, limit: usize) -> StateResult<Vec<Job>> {
        self.list(ListJobs {
            status: Some(JobStatus::Failed),
            limit: limit.clamp(1, 200),
            ..ListJobs::default()
        })
    }

    fn complete(&mut self, job_id: i64, status: JobStatus, payload: Value) -> StateResult<Job> {
        if !matches!(status, JobStatus::Succeeded | JobStatus::Failed) {
            return Err(StateError::InvalidInput(
                "completion status must be succeeded or failed".to_string(),
            ));
        }
        let now = self.now_millis()?;
        let encoded = serde_json::to_string(&payload)
            .map_err(|error| StateError::InvalidInput(error.to_string()))?;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current = load_job(&tx, job_id)?;
        if current.status != JobStatus::Running {
            return Err(StateError::InvalidTransition {
                from: current.status,
                to: status,
            });
        }
        let (result, error) = if status == JobStatus::Succeeded {
            (Some(encoded), None)
        } else {
            (None, Some(encoded))
        };
        let changed = tx.execute(
            "UPDATE jobs SET status = ?1, progress_current = 3, progress_total = 3, \
                 progress_message = 'finalizing', result_json = ?2, error_json = ?3, \
                 finished_at = ?4 WHERE id = ?5 AND status = 'running'",
            params![status.as_db(), result, error, now, job_id],
        )?;
        if changed != 1 {
            return Err(StateError::Corrupt(format!(
                "running job {job_id} changed during completion"
            )));
        }
        let job = load_job(&tx, job_id)?;
        tx.commit()?;
        Ok(job)
    }

    pub fn cancel(&mut self, job_id: i64) -> StateResult<CancelResult> {
        let now = self.now_millis()?;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let current = load_job(&tx, job_id)?;
        let changed = match current.status {
            JobStatus::Pending => transition_job(
                &tx,
                job_id,
                JobStatus::Pending,
                JobStatus::Cancelled,
                now,
                None,
            )?,
            JobStatus::Running => transition_job(
                &tx,
                job_id,
                JobStatus::Running,
                JobStatus::CancelRequested,
                now,
                None,
            )?,
            JobStatus::CancelRequested | JobStatus::Cancelled => false,
            JobStatus::Succeeded | JobStatus::Failed | JobStatus::Superseded => {
                let status = current.status;
                drop(tx);
                return Err(StateError::NotCancellable(status));
            }
        };
        let job = load_job(&tx, job_id)?;
        tx.commit()?;
        Ok(CancelResult { job, changed })
    }

    #[cfg(test)]
    fn transition_for_test(
        &mut self,
        job_id: i64,
        from: JobStatus,
        to: JobStatus,
    ) -> StateResult<bool> {
        let now = self.now_millis()?;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let changed = transition_job(&tx, job_id, from, to, now, None)?;
        tx.commit()?;
        Ok(changed)
    }

    fn recover(&mut self) -> StateResult<RecoveryReport> {
        let now = self.now_millis()?;
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let executing: Vec<(i64, i64, String, String)> = {
            let mut statement = tx.prepare(
                "SELECT id, project_id, job_type, dedupe_key FROM jobs WHERE status = 'running'",
            )?;
            let rows = statement.query_map([], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            })?;
            rows.collect::<Result<_, _>>()?
        };

        let mut report = RecoveryReport::default();
        for (job_id, project_id, job_type, dedupe_key) in executing {
            let successor: Option<i64> = tx
                .query_row(
                    "SELECT id FROM jobs \
                     WHERE project_id = ?1 AND job_type = ?2 AND dedupe_key = ?3 \
                       AND status = 'pending' ORDER BY id LIMIT 1",
                    params![project_id, job_type, dedupe_key],
                    |row| row.get(0),
                )
                .optional()?;
            if let Some(successor_id) = successor {
                if transition_job(
                    &tx,
                    job_id,
                    JobStatus::Running,
                    JobStatus::Superseded,
                    now,
                    Some(successor_id),
                )? {
                    report.superseded += 1;
                }
            } else if transition_job(
                &tx,
                job_id,
                JobStatus::Running,
                JobStatus::Pending,
                now,
                None,
            )? {
                report.requeued += 1;
            }
        }

        let cancel_ids: Vec<i64> = {
            let mut statement =
                tx.prepare("SELECT id FROM jobs WHERE status = 'cancel_requested'")?;
            let rows = statement.query_map([], |row| row.get(0))?;
            rows.collect::<Result<_, _>>()?
        };
        for job_id in cancel_ids {
            if transition_job(
                &tx,
                job_id,
                JobStatus::CancelRequested,
                JobStatus::Cancelled,
                now,
                None,
            )? {
                report.cancelled += 1;
            }
        }

        tx.commit()?;
        Ok(report)
    }

    fn now_millis(&self) -> StateResult<i64> {
        let millis = self
            .clock
            .now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| StateError::Corrupt("clock is before Unix epoch".to_string()))?
            .as_millis();
        i64::try_from(millis)
            .map_err(|_| StateError::Corrupt("clock value exceeds SQLite INTEGER".to_string()))
    }
}

fn initialize_or_migrate(
    connection: &mut Connection,
    path: &Path,
    existed_before_open: bool,
) -> StateResult<()> {
    let version: u32 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
    if version > STATE_SCHEMA_VERSION {
        return Err(StateError::Corrupt(format!(
            "state schema {version} is newer than supported {STATE_SCHEMA_VERSION}"
        )));
    }
    if version == STATE_SCHEMA_VERSION {
        return Ok(());
    }

    let object_count: i64 = connection.query_row(
        "SELECT COUNT(*) FROM sqlite_master \
         WHERE name NOT LIKE 'sqlite_%' AND type IN ('table', 'index', 'trigger', 'view')",
        [],
        |row| row.get(0),
    )?;
    if version == 0 && object_count != 0 {
        return Err(StateError::Corrupt(
            "unversioned state database contains unknown objects".to_string(),
        ));
    }

    if existed_before_open {
        backup_database(connection, &path.with_file_name(STATE_BACKUP_FILE))?;
    }
    match version {
        0 => {
            let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
            tx.execute_batch(PROJECTS_DDL)?;
            tx.execute_batch(JOBS_DDL_V2)?;
            tx.execute_batch(JOBS_INDEXES)?;
            tx.execute_batch(PUBLISHED_PROVIDER_DDL)?;
            tx.pragma_update(None, "user_version", STATE_SCHEMA_VERSION)?;
            tx.commit()?;
            Ok(())
        }
        1 => migrate_v1_to_v2(connection),
        _ => Err(StateError::Corrupt(format!(
            "state schema {version} has no migration handler"
        ))),
    }
}

/// v1 -> v2: jobs gains the provider claim-snapshot column and the
/// full_scan job_type (table rebuild), plus the published_provider table.
/// Runs with foreign_keys off around a single transaction; row ids are
/// preserved so a foreign_key_check afterwards must stay empty.
fn migrate_v1_to_v2(connection: &mut Connection) -> StateResult<()> {
    connection.pragma_update(None, "foreign_keys", "OFF")?;
    let result = (|| -> StateResult<()> {
        let tx = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute("ALTER TABLE jobs RENAME TO jobs_v1", [])?;
        tx.execute_batch(JOBS_DDL_V2)?;
        tx.execute(
            "INSERT INTO jobs( \
                id, project_id, job_type, target_db_path, file_path, priority, \
                provider, status, progress_current, progress_total, progress_message, \
                result_json, error_json, created_at, started_at, finished_at, \
                dedupe_key, superseded_by_job_id) \
             SELECT id, project_id, job_type, target_db_path, file_path, priority, \
                'python', status, progress_current, progress_total, progress_message, \
                result_json, error_json, created_at, started_at, finished_at, \
                dedupe_key, superseded_by_job_id FROM jobs_v1",
            [],
        )?;
        tx.execute("DROP TABLE jobs_v1", [])?;
        tx.execute_batch(JOBS_INDEXES)?;
        tx.execute_batch(PUBLISHED_PROVIDER_DDL)?;
        let violations: i64 =
            tx.query_row("SELECT COUNT(*) FROM pragma_foreign_key_check", [], |row| {
                row.get(0)
            })?;
        if violations != 0 {
            return Err(StateError::Corrupt(format!(
                "v1 to v2 migration produced {violations} foreign key violations"
            )));
        }
        tx.pragma_update(None, "user_version", STATE_SCHEMA_VERSION)?;
        tx.commit()?;
        Ok(())
    })();
    connection.pragma_update(None, "foreign_keys", "ON")?;
    result
}

fn backup_database(source: &mut Connection, backup_path: &Path) -> StateResult<()> {
    if backup_path.exists() {
        fs::remove_file(backup_path)?;
    }
    let mut destination = Connection::open(backup_path)?;
    {
        let backup = Backup::new(source, &mut destination)?;
        backup.run_to_completion(64, Duration::from_millis(10), None)?;
    }
    set_private_permissions(backup_path)?;
    Ok(())
}

fn transition_job(
    tx: &Transaction<'_>,
    job_id: i64,
    from: JobStatus,
    to: JobStatus,
    now: i64,
    superseded_by: Option<i64>,
) -> StateResult<bool> {
    if !allowed_transition(from, to) {
        return Err(StateError::InvalidTransition { from, to });
    }
    let changed = match (from, to) {
        (JobStatus::Pending, JobStatus::Running) => tx.execute(
            "UPDATE jobs SET status = ?1, started_at = ?2 \
             WHERE id = ?3 AND status = ?4",
            params![to.as_db(), now, job_id, from.as_db()],
        )?,
        (JobStatus::Running, JobStatus::Pending) => tx.execute(
            "UPDATE jobs SET status = ?1, started_at = NULL, finished_at = NULL, \
                 progress_current = NULL, progress_total = NULL, progress_message = NULL \
             WHERE id = ?2 AND status = ?3",
            params![to.as_db(), job_id, from.as_db()],
        )?,
        (JobStatus::Running, JobStatus::CancelRequested) => tx.execute(
            "UPDATE jobs SET status = ?1 WHERE id = ?2 AND status = ?3",
            params![to.as_db(), job_id, from.as_db()],
        )?,
        (JobStatus::Pending, JobStatus::Cancelled)
        | (JobStatus::CancelRequested, JobStatus::Cancelled) => tx.execute(
            "UPDATE jobs SET status = ?1, finished_at = ?2 \
             WHERE id = ?3 AND status = ?4",
            params![to.as_db(), now, job_id, from.as_db()],
        )?,
        (JobStatus::Pending, JobStatus::Superseded)
        | (JobStatus::Running, JobStatus::Superseded) => tx.execute(
            "UPDATE jobs SET status = ?1, finished_at = ?2, superseded_by_job_id = ?3 \
             WHERE id = ?4 AND status = ?5",
            params![to.as_db(), now, superseded_by, job_id, from.as_db()],
        )?,
        (JobStatus::Running, JobStatus::Succeeded) => {
            return Err(StateError::InvalidInput(
                "succeeded transition requires a result payload".to_string(),
            ));
        }
        (JobStatus::Running, JobStatus::Failed) => {
            return Err(StateError::InvalidInput(
                "failed transition requires an error payload".to_string(),
            ));
        }
        _ => return Err(StateError::InvalidTransition { from, to }),
    };
    Ok(changed == 1)
}

fn allowed_transition(from: JobStatus, to: JobStatus) -> bool {
    matches!(
        (from, to),
        (JobStatus::Pending, JobStatus::Running)
            | (JobStatus::Pending, JobStatus::Cancelled)
            | (JobStatus::Pending, JobStatus::Superseded)
            | (JobStatus::Running, JobStatus::Pending)
            | (JobStatus::Running, JobStatus::Succeeded)
            | (JobStatus::Running, JobStatus::Failed)
            | (JobStatus::Running, JobStatus::CancelRequested)
            | (JobStatus::Running, JobStatus::Superseded)
            | (JobStatus::CancelRequested, JobStatus::Cancelled)
    )
}

fn load_job(connection: &Connection, job_id: i64) -> StateResult<Job> {
    let row = connection
        .query_row(
            "SELECT j.id, j.project_id, p.project_path, j.target_db_path, j.job_type, \
                    j.file_path, j.priority, j.provider, j.status, j.progress_current, \
                    j.progress_total, j.progress_message, j.result_json, j.error_json, \
                    j.created_at, j.started_at, j.finished_at, j.superseded_by_job_id \
             FROM jobs j JOIN projects p ON p.id = j.project_id WHERE j.id = ?1",
            params![job_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, i64>(6)?,
                    row.get::<_, String>(7)?,
                    row.get::<_, String>(8)?,
                    row.get::<_, Option<i64>>(9)?,
                    row.get::<_, Option<i64>>(10)?,
                    row.get::<_, Option<String>>(11)?,
                    row.get::<_, Option<String>>(12)?,
                    row.get::<_, Option<String>>(13)?,
                    row.get::<_, i64>(14)?,
                    row.get::<_, Option<i64>>(15)?,
                    row.get::<_, Option<i64>>(16)?,
                    row.get::<_, Option<i64>>(17)?,
                ))
            },
        )
        .optional()?
        .ok_or(StateError::NotFound(job_id))?;
    Ok(Job {
        id: row.0,
        project_id: row.1,
        project_path: row.2,
        target_db_path: row.3,
        job_type: row.4,
        file_path: row.5,
        priority: JobPriority::from_db(row.6)?,
        provider: row.7,
        status: JobStatus::from_db(&row.8)?,
        progress_current: row.9,
        progress_total: row.10,
        progress_message: row.11,
        result: parse_json(row.12, "result_json")?,
        error: parse_json(row.13, "error_json")?,
        created_at: row.14,
        started_at: row.15,
        finished_at: row.16,
        superseded_by_job_id: row.17,
    })
}

fn parse_json(value: Option<String>, field: &str) -> StateResult<Option<Value>> {
    value
        .map(|raw| {
            serde_json::from_str(&raw)
                .map_err(|error| StateError::Corrupt(format!("invalid {field}: {error}")))
        })
        .transpose()
}

fn normalize_project_filter(value: &str) -> StateResult<String> {
    let path = Path::new(value);
    if !path.is_absolute() {
        return Err(StateError::InvalidInput(
            "project_path filter must be absolute".to_string(),
        ));
    }
    let normalized = if path.exists() {
        display_path(&fs::canonicalize(path)?)
    } else {
        path.to_string_lossy().into_owned()
    };
    Ok(project_key(&normalized))
}

fn normalize_existing_project(value: &str) -> StateResult<String> {
    let path = Path::new(value);
    if !path.is_absolute() || !path.is_dir() {
        return Err(StateError::InvalidInput(
            "project_path must be an existing absolute directory".to_string(),
        ));
    }
    Ok(display_path(&fs::canonicalize(path)?))
}

fn display_path(path: &Path) -> String {
    let value = path.to_string_lossy();
    #[cfg(windows)]
    {
        value.strip_prefix(r"\\?\").unwrap_or(&value).to_string()
    }
    #[cfg(not(windows))]
    {
        value.into_owned()
    }
}

fn project_key(project_path: &str) -> String {
    #[cfg(windows)]
    {
        project_path
            .strip_prefix(r"\\?\")
            .unwrap_or(project_path)
            .to_lowercase()
    }
    #[cfg(not(windows))]
    {
        project_path.to_string()
    }
}

fn normalize_database_path(value: &str) -> StateResult<String> {
    let path = Path::new(value);
    if !path.is_absolute() {
        return Err(StateError::InvalidInput(
            "db_path must be absolute".to_string(),
        ));
    }
    let mut existing = path;
    let mut suffix = Vec::new();
    while !existing.exists() {
        let name = existing.file_name().ok_or_else(|| {
            StateError::InvalidInput("db_path has no existing parent".to_string())
        })?;
        suffix.push(name.to_os_string());
        existing = existing.parent().ok_or_else(|| {
            StateError::InvalidInput("db_path has no existing parent".to_string())
        })?;
    }
    let mut normalized = fs::canonicalize(existing)?;
    for component in suffix.iter().rev() {
        normalized.push(component);
    }
    Ok(display_path(&normalized))
}

fn normalize_source_path(value: &str) -> StateResult<String> {
    let path = Path::new(value);
    if path.is_absolute() || value.is_empty() {
        return Err(StateError::InvalidInput(
            "file_path must be a non-empty relative path".to_string(),
        ));
    }
    let mut parts = Vec::new();
    for component in path.components() {
        match component {
            Component::Normal(part) => parts.push(part.to_string_lossy().into_owned()),
            _ => {
                return Err(StateError::InvalidInput(
                    "file_path may contain only normal relative components".to_string(),
                ));
            }
        }
    }
    let normalized = parts.join("/");
    let extension = Path::new(&normalized)
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase);
    if !matches!(
        extension.as_deref(),
        Some("py" | "c" | "h" | "cpp" | "hpp" | "cc" | "cxx" | "hh" | "hxx" | "ts" | "tsx" | "rs")
    ) {
        return Err(StateError::InvalidInput(
            "file_path has an unsupported source extension".to_string(),
        ));
    }
    Ok(normalized)
}

#[cfg(unix)]
fn set_private_permissions(path: &Path) -> StateResult<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

#[cfg(not(unix))]
fn set_private_permissions(_path: &Path) -> StateResult<()> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::time::{Duration, SystemTime};

    use super::*;
    use crate::clock::fake::FakeClock;

    fn store_in(home: &Path) -> StateStore {
        let clock = Arc::new(FakeClock::new(
            SystemTime::UNIX_EPOCH + Duration::from_secs(1_750_000_000),
        ));
        StateStore::open(home, clock as Arc<dyn Clock>).unwrap().0
    }

    fn request(project: &Path, db: &Path, file: &str, priority: JobPriority) -> SubmitJob {
        SubmitJob {
            project_path: project.to_string_lossy().into_owned(),
            db_path: db.to_string_lossy().into_owned(),
            file_path: file.to_string(),
            priority,
        }
    }

    #[test]
    fn submit_reuses_pending_and_promotes_priority() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join(".claude").join("logic_index.db");
        let mut store = store_in(home.path());

        let first = store
            .submit(request(
                project.path(),
                &db,
                "src/a.py",
                JobPriority::Background,
            ))
            .unwrap();
        let second = store
            .submit(request(
                project.path(),
                &db,
                "src/a.py",
                JobPriority::Interactive,
            ))
            .unwrap();

        assert!(first.created);
        assert!(!second.created);
        assert_eq!(first.job.id, second.job.id);
        assert_eq!(second.job.priority, JobPriority::Interactive);
    }

    #[test]
    fn failed_submit_rolls_back_project_registration() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        store
            .connection
            .execute_batch(
                "CREATE TRIGGER reject_jobs BEFORE INSERT ON jobs \
                 BEGIN SELECT RAISE(ABORT, 'injected job failure'); END;",
            )
            .unwrap();

        let result = store.submit(request(
            project.path(),
            &db,
            "a.py",
            JobPriority::Interactive,
        ));

        assert!(matches!(result, Err(StateError::Sqlite(_))));
        assert_eq!(
            store
                .connection
                .query_row("SELECT COUNT(*) FROM projects", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            store
                .connection
                .query_row("SELECT COUNT(*) FROM jobs", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            0
        );
    }

    #[test]
    fn running_job_gets_one_pending_successor() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        let first = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        let now = store.now_millis().unwrap();
        let tx = store
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .unwrap();
        assert!(transition_job(
            &tx,
            first.job.id,
            JobStatus::Pending,
            JobStatus::Running,
            now,
            None
        )
        .unwrap());
        tx.commit().unwrap();

        let successor = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Background,
            ))
            .unwrap();
        let duplicate = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();

        assert!(successor.created);
        assert!(!duplicate.created);
        assert_ne!(successor.job.id, first.job.id);
        assert_eq!(duplicate.job.id, successor.job.id);
    }

    #[test]
    fn cancel_is_idempotent_by_state() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        let pending = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();

        let first = store.cancel(pending.job.id).unwrap();
        let second = store.cancel(pending.job.id).unwrap();
        assert!(first.changed);
        assert_eq!(first.job.status, JobStatus::Cancelled);
        assert!(!second.changed);
    }

    #[test]
    fn running_cancel_is_requested_and_repeated_cancel_is_idempotent() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        let submitted = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        assert!(store
            .transition_for_test(submitted.job.id, JobStatus::Pending, JobStatus::Running)
            .unwrap());

        let first = store.cancel(submitted.job.id).unwrap();
        let second = store.cancel(submitted.job.id).unwrap();
        assert!(first.changed);
        assert_eq!(first.job.status, JobStatus::CancelRequested);
        assert!(!second.changed);
        assert_eq!(second.job.status, JobStatus::CancelRequested);
    }

    #[test]
    fn recovery_requeues_running_and_cancels_requested_jobs() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        let running = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        let cancelling = store
            .submit(request(
                project.path(),
                &db,
                "b.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        assert!(store
            .transition_for_test(running.job.id, JobStatus::Pending, JobStatus::Running)
            .unwrap());
        assert!(store
            .transition_for_test(cancelling.job.id, JobStatus::Pending, JobStatus::Running)
            .unwrap());
        assert!(store.cancel(cancelling.job.id).unwrap().changed);
        drop(store);

        let clock = Arc::new(FakeClock::new(
            SystemTime::UNIX_EPOCH + Duration::from_secs(1_750_000_100),
        ));
        let (store, report) = StateStore::open(home.path(), clock as Arc<dyn Clock>).unwrap();
        assert_eq!(report.requeued, 1);
        assert_eq!(report.cancelled, 1);
        assert_eq!(
            store.get(running.job.id).unwrap().status,
            JobStatus::Pending
        );
        let requeued = store.get(running.job.id).unwrap();
        assert!(requeued.started_at.is_none());
        assert!(requeued.finished_at.is_none());
        assert!(requeued.progress_current.is_none());
        assert!(requeued.progress_total.is_none());
        assert!(requeued.progress_message.is_none());
        assert_eq!(
            store.get(cancelling.job.id).unwrap().status,
            JobStatus::Cancelled
        );
        assert!(store.get(cancelling.job.id).unwrap().finished_at.is_some());
    }

    #[test]
    fn recover_supersedes_running_when_pending_successor_exists() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        let running = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        let now = store.now_millis().unwrap();
        let tx = store
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .unwrap();
        transition_job(
            &tx,
            running.job.id,
            JobStatus::Pending,
            JobStatus::Running,
            now,
            None,
        )
        .unwrap();
        tx.commit().unwrap();
        let successor = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        drop(store);

        let clock = Arc::new(FakeClock::new(
            SystemTime::UNIX_EPOCH + Duration::from_secs(1_750_000_100),
        ));
        let (store, report) = StateStore::open(home.path(), clock as Arc<dyn Clock>).unwrap();
        let old = store.get(running.job.id).unwrap();
        assert_eq!(report.superseded, 1);
        assert_eq!(old.status, JobStatus::Superseded);
        assert_eq!(old.superseded_by_job_id, Some(successor.job.id));
    }

    #[test]
    fn invalid_transition_is_rejected_without_state_change() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        let submitted = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();

        let error = store
            .transition_for_test(submitted.job.id, JobStatus::Pending, JobStatus::Succeeded)
            .unwrap_err();
        assert!(matches!(error, StateError::InvalidTransition { .. }));
        assert_eq!(
            store.get(submitted.job.id).unwrap().status,
            JobStatus::Pending
        );
    }

    #[test]
    fn unknown_unversioned_database_is_preserved() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join(STATE_DB_FILE);
        let connection = Connection::open(&path).unwrap();
        connection
            .execute("CREATE TABLE foreign_data(value TEXT)", [])
            .unwrap();
        drop(connection);
        let clock = Arc::new(FakeClock::new(SystemTime::UNIX_EPOCH));

        let result = StateStore::open(home.path(), clock as Arc<dyn Clock>);
        assert!(matches!(result, Err(StateError::Corrupt(_))));
        let connection = Connection::open(path).unwrap();
        assert_eq!(
            connection
                .query_row("SELECT COUNT(*) FROM foreign_data", [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }

    #[test]
    fn empty_existing_database_is_backed_up_before_initialization() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join(STATE_DB_FILE);
        let connection = Connection::open(&path).unwrap();
        drop(connection);
        let clock = Arc::new(FakeClock::new(SystemTime::UNIX_EPOCH));

        let (_store, report) = StateStore::open(home.path(), clock as Arc<dyn Clock>).unwrap();

        assert_eq!(report, RecoveryReport::default());
        let backup_path = home.path().join(STATE_BACKUP_FILE);
        assert!(backup_path.exists());
        let backup = Connection::open(backup_path).unwrap();
        let version: u32 = backup
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .unwrap();
        assert_eq!(version, 0);
    }

    #[test]
    fn new_database_initialization_does_not_create_backup() {
        let home = tempfile::tempdir().unwrap();
        let clock = Arc::new(FakeClock::new(SystemTime::UNIX_EPOCH));

        let (_store, report) = StateStore::open(home.path(), clock as Arc<dyn Clock>).unwrap();

        assert_eq!(report, RecoveryReport::default());
        assert!(!home.path().join(STATE_BACKUP_FILE).exists());
    }

    #[test]
    fn higher_schema_version_is_rejected_without_change() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join(STATE_DB_FILE);
        let connection = Connection::open(&path).unwrap();
        connection.pragma_update(None, "user_version", 99).unwrap();
        drop(connection);
        let clock = Arc::new(FakeClock::new(SystemTime::UNIX_EPOCH));

        let result = StateStore::open(home.path(), clock as Arc<dyn Clock>);
        assert!(matches!(result, Err(StateError::Corrupt(_))));
        let connection = Connection::open(path).unwrap();
        let version: u32 = connection
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .unwrap();
        assert_eq!(version, 99);
    }

    #[test]
    fn claim_orders_interactive_before_background_and_fifo_within_priority() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        let background = store
            .submit(request(
                project.path(),
                &db,
                "background.py",
                JobPriority::Background,
            ))
            .unwrap();
        let first_interactive = store
            .submit(request(
                project.path(),
                &db,
                "first.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        let second_interactive = store
            .submit(request(
                project.path(),
                &db,
                "second.py",
                JobPriority::Interactive,
            ))
            .unwrap();

        assert_eq!(
            store
                .claim_next_pending(PROVIDER_PYTHON)
                .unwrap()
                .unwrap()
                .id,
            first_interactive.job.id
        );
        store
            .complete_success(first_interactive.job.id, serde_json::json!({"ok": true}))
            .unwrap();
        assert_eq!(
            store
                .claim_next_pending(PROVIDER_PYTHON)
                .unwrap()
                .unwrap()
                .id,
            second_interactive.job.id
        );
        store
            .complete_success(second_interactive.job.id, serde_json::json!({"ok": true}))
            .unwrap();
        assert_eq!(
            store
                .claim_next_pending(PROVIDER_PYTHON)
                .unwrap()
                .unwrap()
                .id,
            background.job.id
        );
    }

    #[test]
    fn completion_and_list_preserve_structured_payload_and_bounds() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        let submitted = store
            .submit(request(
                project.path(),
                &db,
                "main.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        let claimed = store.claim_next_pending(PROVIDER_PYTHON).unwrap().unwrap();
        assert_eq!(claimed.progress_current, Some(0));
        assert_eq!(claimed.progress_total, Some(3));
        store
            .update_progress(
                claimed.id,
                ProgressUpdate {
                    current: 2,
                    total: 3,
                    message: "scanning".to_string(),
                },
            )
            .unwrap();
        let payload = serde_json::json!({"schema_version": 1, "outcome": "success"});
        let completed = store.complete_success(claimed.id, payload.clone()).unwrap();
        assert_eq!(completed.status, JobStatus::Succeeded);
        assert_eq!(completed.result, Some(payload));
        assert_eq!(completed.progress_current, Some(3));
        let jobs = store
            .list(ListJobs {
                project_path: Some(project.path().to_string_lossy().into_owned()),
                file_path: None,
                status: Some(JobStatus::Succeeded),
                job_type: Some("incremental_scan".to_string()),
                limit: 500,
            })
            .unwrap();
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].id, submitted.job.id);
    }

    #[test]
    fn promote_only_updates_pending_without_creating_successor() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join(".claude").join("logic_index.db");
        let mut store = store_in(home.path());
        let submitted = store
            .submit(request(
                project.path(),
                &db,
                "main.py",
                JobPriority::Background,
            ))
            .unwrap();
        let promoted = store
            .promote(submitted.job.id, JobPriority::Interactive)
            .unwrap();
        assert!(promoted.changed);
        assert_eq!(promoted.job.priority, JobPriority::Interactive);
        let running = store.claim_next_pending(PROVIDER_PYTHON).unwrap().unwrap();
        let unchanged = store.promote(running.id, JobPriority::Interactive).unwrap();
        assert!(!unchanged.changed);
        assert_eq!(unchanged.job.status, JobStatus::Running);
        let pending = store
            .list(ListJobs {
                project_path: Some(project.path().to_string_lossy().into_owned()),
                file_path: Some("main.py".to_string()),
                status: Some(JobStatus::Pending),
                job_type: Some("incremental_scan".to_string()),
                limit: 10,
            })
            .unwrap();
        assert!(pending.is_empty());
    }

    #[test]
    fn list_filters_by_normalized_file_path() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join(".claude").join("logic_index.db");
        let mut store = store_in(home.path());
        let first = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Background,
            ))
            .unwrap();
        store
            .submit(request(
                project.path(),
                &db,
                "b.py",
                JobPriority::Background,
            ))
            .unwrap();
        let jobs = store
            .list(ListJobs {
                project_path: Some(project.path().to_string_lossy().into_owned()),
                file_path: Some("a.py".to_string()),
                status: None,
                job_type: Some("incremental_scan".to_string()),
                limit: 10,
            })
            .unwrap();
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].id, first.job.id);
        assert!(store
            .list(ListJobs {
                file_path: Some("../outside.py".to_string()),
                limit: 10,
                ..ListJobs::default()
            })
            .is_err());
    }

    #[test]
    fn path_normalization_rejects_parent_components_and_accepts_deleted_source() {
        assert!(normalize_source_path("src/../outside.py").is_err());
        assert_eq!(
            normalize_source_path("src/deleted.py").unwrap(),
            "src/deleted.py"
        );
        assert_eq!(
            normalize_source_path("src/state.rs").unwrap(),
            "src/state.rs"
        );
        assert!(normalize_source_path("README.md").is_err());
    }

    fn v1_database(home: &Path) {
        let connection = Connection::open(home.join(STATE_DB_FILE)).unwrap();
        connection.execute_batch(SCHEMA_V1).unwrap();
    }

    #[test]
    fn migration_v1_to_v2_defaults_provider_and_backs_up() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        v1_database(home.path());
        let connection = Connection::open(home.path().join(STATE_DB_FILE)).unwrap();
        let project_path = fs::canonicalize(project.path()).unwrap();
        connection
            .execute(
                "INSERT INTO projects(project_key, project_path, db_path, created_at, last_seen_at) \
                 VALUES (?1, ?2, ?3, 100, 100)",
                params![
                    project_key(&display_path(&project_path)),
                    display_path(&project_path),
                    display_path(&project_path.join("logic_index.db")),
                ],
            )
            .unwrap();
        connection
            .execute(
                "INSERT INTO jobs(project_id, job_type, target_db_path, file_path, priority, \
                     status, created_at, dedupe_key) \
                 VALUES (1, 'incremental_scan', 'db', 'a.py', 1, 'pending', 100, \
                     'incremental_scan:a.py')",
                [],
            )
            .unwrap();
        drop(connection);

        let store = store_in(home.path());
        let job = store.get(1).unwrap();
        assert_eq!(job.provider, PROVIDER_PYTHON);
        assert_eq!(job.status, JobStatus::Pending);
        assert!(home.path().join(STATE_BACKUP_FILE).exists());
        let version: u32 = store
            .connection
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .unwrap();
        assert_eq!(version, STATE_SCHEMA_VERSION);
        assert!(store.published_provider().unwrap().is_none());
        let violations: i64 = store
            .connection
            .query_row("SELECT COUNT(*) FROM pragma_foreign_key_check", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(violations, 0);
    }

    #[test]
    fn claim_snapshots_provider_onto_job() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        let claimed = store.claim_next_pending(PROVIDER_RUST).unwrap().unwrap();
        assert_eq!(claimed.provider, PROVIDER_RUST);
        assert!(store.claim_next_pending("other").is_err());
    }

    #[test]
    fn full_scan_supersedes_pending_incrementals_and_absorbs_later_submissions() {
        let home = tempfile::tempdir().unwrap();
        let project = tempfile::tempdir().unwrap();
        let db = project.path().join("logic_index.db");
        let mut store = store_in(home.path());
        let pending = store
            .submit(request(
                project.path(),
                &db,
                "a.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        let full = store.submit_full_scan(pending.job.project_id).unwrap();
        assert!(full.created);
        assert_eq!(full.job.job_type, JOB_TYPE_FULL_SCAN);

        let old = store.get(pending.job.id).unwrap();
        assert_eq!(old.status, JobStatus::Superseded);
        assert_eq!(old.superseded_by_job_id, Some(full.job.id));

        let absorbed = store
            .submit(request(
                project.path(),
                &db,
                "b.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        assert_eq!(absorbed.job.status, JobStatus::Superseded);
        assert_eq!(absorbed.job.superseded_by_job_id, Some(full.job.id));

        let duplicate = store.submit_full_scan(pending.job.project_id).unwrap();
        assert!(!duplicate.created);
        assert_eq!(duplicate.job.id, full.job.id);

        let claimed = store.claim_next_pending(PROVIDER_RUST).unwrap().unwrap();
        assert_eq!(claimed.id, full.job.id);
        let after_running = store
            .submit(request(
                project.path(),
                &db,
                "c.py",
                JobPriority::Interactive,
            ))
            .unwrap();
        assert_eq!(after_running.job.status, JobStatus::Pending);
    }

    #[test]
    fn publish_provider_upserts_single_row() {
        let home = tempfile::tempdir().unwrap();
        let mut store = store_in(home.path());
        assert!(store.published_provider().unwrap().is_none());
        let first = store
            .publish_provider(PROVIDER_RUST, "0.2.0", "{}")
            .unwrap();
        assert_eq!(first.provider, PROVIDER_RUST);
        let second = store
            .publish_provider(PROVIDER_PYTHON, "0.2.0", "{}")
            .unwrap();
        assert_eq!(second.provider, PROVIDER_PYTHON);
        let stored = store.published_provider().unwrap().unwrap();
        assert_eq!(stored.provider, PROVIDER_PYTHON);
        assert!(store.publish_provider("other", "0.2.0", "{}").is_err());
        let count: i64 = store
            .connection
            .query_row("SELECT COUNT(*) FROM published_provider", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 1);
    }
}
