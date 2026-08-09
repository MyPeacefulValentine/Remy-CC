//! IPC protocol types (R1.2): JSON Lines request/response for hello, ping, shutdown.
//!
//! PROTOCOL_VERSION history: 1 (R1.2 initial).

use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: u32 = 1;
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
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(untagged)]
pub enum Response {
    Ok(OkResponse),
    Err(ErrorResponse),
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OkResponse {
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub protocol_version: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub daemon_version: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
pub struct ErrorResponse {
    pub ok: bool,
    pub error: String,
}

impl Response {
    pub fn ok() -> Self {
        Response::Ok(OkResponse {
            ok: true,
            protocol_version: None,
            daemon_version: None,
        })
    }

    pub fn hello(daemon_version: String) -> Self {
        Response::Ok(OkResponse {
            ok: true,
            protocol_version: Some(PROTOCOL_VERSION),
            daemon_version: Some(daemon_version),
        })
    }

    pub fn error(msg: impl Into<String>) -> Self {
        Response::Err(ErrorResponse {
            ok: false,
            error: msg.into(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn request_hello_roundtrip() {
        let req = Request::Hello {
            protocol_version: 1,
            token: "abc123".to_string(),
        };
        let json = serde_json::to_string(&req).unwrap();
        let parsed: Request = serde_json::from_str(&json).unwrap();
        assert_eq!(req, parsed);
    }

    #[test]
    fn request_ping_roundtrip() {
        let req = Request::Ping {
            token: "xyz".to_string(),
        };
        let json = serde_json::to_string(&req).unwrap();
        let parsed: Request = serde_json::from_str(&json).unwrap();
        assert_eq!(req, parsed);
    }

    #[test]
    fn request_shutdown_roundtrip() {
        let req = Request::Shutdown {
            token: "token".to_string(),
        };
        let json = serde_json::to_string(&req).unwrap();
        let parsed: Request = serde_json::from_str(&json).unwrap();
        assert_eq!(req, parsed);
    }

    #[test]
    fn response_ok_roundtrip() {
        let resp = Response::ok();
        let json = serde_json::to_string(&resp).unwrap();
        let parsed: Response = serde_json::from_str(&json).unwrap();
        assert_eq!(resp, parsed);
    }

    #[test]
    fn response_hello_roundtrip() {
        let resp = Response::hello("0.1.0".to_string());
        let json = serde_json::to_string(&resp).unwrap();
        let parsed: Response = serde_json::from_str(&json).unwrap();
        assert_eq!(resp, parsed);
    }

    #[test]
    fn response_error_roundtrip() {
        let resp = Response::error("bad_token");
        let json = serde_json::to_string(&resp).unwrap();
        let parsed: Response = serde_json::from_str(&json).unwrap();
        assert_eq!(resp, parsed);
    }

    #[test]
    fn response_ok_serializes_without_optional_fields() {
        let resp = Response::ok();
        let json = serde_json::to_string(&resp).unwrap();
        assert!(!json.contains("protocol_version"));
        assert!(!json.contains("daemon_version"));
    }
}
