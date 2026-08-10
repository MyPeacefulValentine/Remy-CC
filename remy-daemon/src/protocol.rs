//! IPC protocol types: JSON Lines requests and tagged responses.
//!
//! PROTOCOL_VERSION history: 1 (R1.2 initial), 2 (R2.1 persistent jobs).

use serde::{Deserialize, Serialize};

use crate::state::{Job, JobPriority, STATE_SCHEMA_VERSION};

pub const PROTOCOL_VERSION: u32 = 2;
pub const MAX_LINE_BYTES: u64 = 65536;

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(tag = "cmd", rename_all = "snake_case")]
pub enum Request {
    Hello {
        protocol_version: u32,
        token: String,
    },
    Ping {
        token: String,
    },
    Shutdown {
        token: String,
    },
    SubmitJob {
        protocol_version: u32,
        state_schema_version: u32,
        token: String,
        project_path: String,
        db_path: String,
        file_path: String,
        priority: JobPriority,
    },
    GetJob {
        protocol_version: u32,
        state_schema_version: u32,
        token: String,
        job_id: i64,
    },
    CancelJob {
        protocol_version: u32,
        state_schema_version: u32,
        token: String,
        job_id: i64,
    },
}

impl Request {
    pub fn token(&self) -> &str {
        match self {
            Self::Hello { token, .. }
            | Self::Ping { token }
            | Self::Shutdown { token }
            | Self::SubmitJob { token, .. }
            | Self::GetJob { token, .. }
            | Self::CancelJob { token, .. } => token,
        }
    }

    pub fn business_versions(&self) -> Option<(u32, u32)> {
        match self {
            Self::SubmitJob {
                protocol_version,
                state_schema_version,
                ..
            }
            | Self::GetJob {
                protocol_version,
                state_schema_version,
                ..
            }
            | Self::CancelJob {
                protocol_version,
                state_schema_version,
                ..
            } => Some((*protocol_version, *state_schema_version)),
            Self::Hello { .. } | Self::Ping { .. } | Self::Shutdown { .. } => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Response {
    Hello {
        protocol_version: u32,
        state_schema_version: u32,
        daemon_version: String,
    },
    Ack,
    Submitted {
        job: Job,
        created: bool,
    },
    Job {
        job: Job,
    },
    Cancelled {
        job: Job,
        changed: bool,
    },
    Error {
        code: String,
        message: String,
    },
}

impl Response {
    pub fn hello(daemon_version: String) -> Self {
        Self::Hello {
            protocol_version: PROTOCOL_VERSION,
            state_schema_version: STATE_SCHEMA_VERSION,
            daemon_version,
        }
    }

    pub fn error(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self::Error {
            code: code.into(),
            message: message.into(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::JobStatus;

    fn sample_job() -> Job {
        Job {
            id: 7,
            project_id: 3,
            project_path: "/repo".to_string(),
            target_db_path: "/repo/.claude/logic_index.db".to_string(),
            job_type: "incremental_scan".to_string(),
            file_path: "src/main.py".to_string(),
            priority: JobPriority::Interactive,
            status: JobStatus::Pending,
            progress_current: None,
            progress_total: None,
            progress_message: None,
            result: None,
            error: None,
            created_at: 1_000,
            started_at: None,
            finished_at: None,
            superseded_by_job_id: None,
        }
    }

    #[test]
    fn request_variants_roundtrip() {
        let requests = [
            Request::Hello {
                protocol_version: PROTOCOL_VERSION,
                token: "token".to_string(),
            },
            Request::Ping {
                token: "token".to_string(),
            },
            Request::Shutdown {
                token: "token".to_string(),
            },
            Request::SubmitJob {
                protocol_version: PROTOCOL_VERSION,
                state_schema_version: STATE_SCHEMA_VERSION,
                token: "token".to_string(),
                project_path: "/repo".to_string(),
                db_path: "/repo/.claude/logic_index.db".to_string(),
                file_path: "src/main.py".to_string(),
                priority: JobPriority::Interactive,
            },
            Request::GetJob {
                protocol_version: PROTOCOL_VERSION,
                state_schema_version: STATE_SCHEMA_VERSION,
                token: "token".to_string(),
                job_id: 7,
            },
            Request::CancelJob {
                protocol_version: PROTOCOL_VERSION,
                state_schema_version: STATE_SCHEMA_VERSION,
                token: "token".to_string(),
                job_id: 7,
            },
        ];
        for request in requests {
            let json = serde_json::to_string(&request).unwrap();
            assert_eq!(serde_json::from_str::<Request>(&json).unwrap(), request);
        }
    }

    #[test]
    fn response_variants_roundtrip() {
        let responses = [
            Response::hello("0.1.0".to_string()),
            Response::Ack,
            Response::Submitted {
                job: sample_job(),
                created: true,
            },
            Response::Job { job: sample_job() },
            Response::Cancelled {
                job: sample_job(),
                changed: true,
            },
            Response::error("not_found", "job missing"),
        ];
        for response in responses {
            let json = serde_json::to_string(&response).unwrap();
            assert_eq!(serde_json::from_str::<Response>(&json).unwrap(), response);
        }
    }

    #[test]
    fn tagged_response_does_not_depend_on_optional_field_disambiguation() {
        let json = serde_json::to_value(Response::error("bad_token", "token rejected")).unwrap();
        assert_eq!(json["type"], "error");
        assert_eq!(json["code"], "bad_token");
        assert!(json.get("ok").is_none());
    }
}
