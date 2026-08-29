//! ScanResult / StageError / scan_result JSON Lines contract replication
//! (index_state.py + struct_scan._scan_result_json, schema_version 1).

use serde::Serialize;
use std::collections::BTreeSet;

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct StageError {
    pub stage: String,
    pub path: Option<String>,
    pub message: String,
}

impl StageError {
    pub fn new(stage: &str, message: impl Into<String>, path: Option<String>) -> StageError {
        StageError {
            stage: stage.to_string(),
            path,
            message: message.into(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunStatus {
    Success,
    Partial,
    Failed,
}

impl RunStatus {
    pub fn value(&self) -> &'static str {
        match self {
            RunStatus::Success => "success",
            RunStatus::Partial => "partial",
            RunStatus::Failed => "failed",
        }
    }

    pub fn exit_code(&self) -> u8 {
        match self {
            RunStatus::Success => 0,
            RunStatus::Partial => 2,
            RunStatus::Failed => 1,
        }
    }
}

#[derive(Debug, Clone)]
pub struct ScanResult {
    pub status: RunStatus,
    pub discovered_paths: Vec<String>,
    pub successful_paths: Vec<String>,
    pub failed_paths: Vec<String>,
    pub deleted_paths: Vec<String>,
    pub errors: Vec<StageError>,
    pub postprocess_complete: bool,
}

impl ScanResult {
    /// index_state.ScanResult.from_parts: sorted-set path tuples plus the
    /// success/partial/failed status rule.
    pub fn from_parts(
        discovered_paths: impl IntoIterator<Item = String>,
        successful_paths: impl IntoIterator<Item = String>,
        failed_paths: impl IntoIterator<Item = String>,
        deleted_paths: impl IntoIterator<Item = String>,
        errors: Vec<StageError>,
        postprocess_complete: bool,
    ) -> ScanResult {
        let discovered: BTreeSet<String> = discovered_paths.into_iter().collect();
        let successful: BTreeSet<String> = successful_paths.into_iter().collect();
        let failed: BTreeSet<String> = failed_paths.into_iter().collect();
        let deleted: BTreeSet<String> = deleted_paths.into_iter().collect();
        let status = if errors.is_empty() {
            RunStatus::Success
        } else if !successful.is_empty() || !deleted.is_empty() {
            RunStatus::Partial
        } else {
            RunStatus::Failed
        };
        ScanResult {
            status,
            discovered_paths: discovered.into_iter().collect(),
            successful_paths: successful.into_iter().collect(),
            failed_paths: failed.into_iter().collect(),
            deleted_paths: deleted.into_iter().collect(),
            errors,
            postprocess_complete,
        }
    }

    /// struct_scan._scan_result_json (compact separators, one line).
    pub fn to_json_line(&self) -> String {
        let value = serde_json::json!({
            "type": "scan_result",
            "schema_version": 1,
            "outcome": self.status.value(),
            "successful_paths": self.successful_paths,
            "failed_paths": self.failed_paths,
            "deleted_paths": self.deleted_paths,
            "postprocess_complete": self.postprocess_complete,
            "errors": self.errors.iter().map(|e| serde_json::json!({
                "stage": e.stage,
                "path": e.path,
                "message": e.message,
            })).collect::<Vec<_>>(),
        });
        value.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_rule_matches_from_parts() {
        let ok = ScanResult::from_parts(
            vec!["a.c".into()],
            vec!["a.c".into()],
            vec![],
            vec![],
            vec![],
            true,
        );
        assert_eq!(ok.status, RunStatus::Success);
        assert_eq!(ok.status.exit_code(), 0);

        let partial = ScanResult::from_parts(
            vec!["a.c".into(), "b.c".into()],
            vec!["a.c".into()],
            vec!["b.c".into()],
            vec![],
            vec![StageError::new("file_scan", "boom", Some("b.c".into()))],
            true,
        );
        assert_eq!(partial.status, RunStatus::Partial);
        assert_eq!(partial.status.exit_code(), 2);

        let failed = ScanResult::from_parts(
            vec!["a.c".into()],
            vec![],
            vec!["a.c".into()],
            vec![],
            vec![StageError::new("file_scan", "boom", Some("a.c".into()))],
            false,
        );
        assert_eq!(failed.status, RunStatus::Failed);
        assert_eq!(failed.status.exit_code(), 1);
    }

    #[test]
    fn json_line_has_schema_v1_shape() {
        let result = ScanResult::from_parts(
            vec!["a.c".into()],
            vec!["a.c".into()],
            vec![],
            vec![],
            vec![],
            true,
        );
        let parsed: serde_json::Value = serde_json::from_str(&result.to_json_line()).unwrap();
        assert_eq!(parsed["type"], "scan_result");
        assert_eq!(parsed["schema_version"], 1);
        assert_eq!(parsed["outcome"], "success");
        assert_eq!(parsed["postprocess_complete"], true);
        assert!(parsed["errors"].as_array().unwrap().is_empty());
    }
}
