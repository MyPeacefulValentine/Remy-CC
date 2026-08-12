"""Typed contracts shared by the Remy installer and installed CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Sequence

MANIFEST_SCHEMA_VERSION = 3
RUNTIME_SCHEMA_VERSION = 1
TRANSACTION_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
MANIFEST_RELATIVE_PATH = Path("install") / "manifest.json"
TRANSACTION_RELATIVE_PATH = Path("install") / "transaction.json"
RUNTIME_RELATIVE_PATH = Path("runtime") / "python.json"
LEGACY_MANIFEST_NAME = ".installer_manifest.json"
ROOT_CLAUDE = "claude"
ROOT_REMY = "remy"
VALID_ROOTS = frozenset({ROOT_CLAUDE, ROOT_REMY})
VALID_HOOK_MODES = frozenset({"python", "rust"})
VALID_TRANSACTION_OPERATIONS = frozenset({"install", "uninstall"})
VALID_TRANSACTION_PHASES = frozenset({
    "prepared",
    "staging",
    "applying",
    "publishing_manifest",
    "committed",
    "cleanup",
    "rollback_incomplete",
})
VALID_ACTION_OPERATIONS = frozenset({"write", "delete", "post_commit_delete"})


class InstallRuntimeError(RuntimeError):
    """Base error with a stable machine-facing category."""

    def __init__(self, message: str, *, category: str = "preflight") -> None:
        super().__init__(message)
        self.category = category


class MetadataError(InstallRuntimeError):
    """Raised when managed metadata is missing required structure."""


@dataclass(frozen=True)
class RootPaths:
    claude: Path
    remy: Path

    def for_name(self, name: str) -> Path:
        if name == ROOT_CLAUDE:
            return self.claude
        if name == ROOT_REMY:
            return self.remy
        raise MetadataError("unsupported managed root: {}".format(name))


@dataclass(frozen=True)
class CandidateFile:
    root: str
    path: str
    source: Path
    role: str
    executable: bool = False


@dataclass(frozen=True)
class FileRecord:
    root: str
    path: str
    sha256: str
    owner: str
    role: str

    def to_dict(self) -> dict[str, str]:
        return {
            "root": self.root,
            "path": self.path,
            "sha256": self.sha256,
            "owner": self.owner,
            "role": self.role,
        }


@dataclass(frozen=True)
class RuntimeDescriptor:
    executable: str
    version: tuple[int, int, int]
    implementation: str
    platform: str
    probed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "executable": self.executable,
            "version": list(self.version),
            "implementation": self.implementation,
            "platform": self.platform,
            "probed_at": self.probed_at,
        }


@dataclass
class TransactionAction:
    root: str
    path: str
    operation: str
    old_hash: Optional[str]
    new_hash: Optional[str]
    stage_name: Optional[str]
    backup_name: str
    executable: bool = False
    applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "path": self.path,
            "operation": self.operation,
            "old_hash": self.old_hash,
            "new_hash": self.new_hash,
            "staged_path": self.stage_name,
            "backup_path": self.backup_name,
            "executable": self.executable,
            "applied": self.applied,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransactionAction":
        expected = {
            "root", "path", "operation", "old_hash", "new_hash", "staged_path",
            "backup_path", "executable", "applied",
        }
        if set(value) != expected:
            raise MetadataError("transaction action is missing required fields")
        if not all(isinstance(value[key], str) for key in ("root", "path", "operation", "backup_path")):
            raise MetadataError("transaction action identity fields must be strings")
        root = str(value["root"])
        path = str(value["path"])
        operation = str(value["operation"])
        backup_path = str(value["backup_path"])
        staged_path = _optional_string(value.get("staged_path"))
        old_hash = _optional_string(value.get("old_hash"))
        new_hash = _optional_string(value.get("new_hash"))
        if root not in VALID_ROOTS or operation not in VALID_ACTION_OPERATIONS:
            raise MetadataError("invalid transaction action identity")
        if not _valid_relative_path(path):
            raise MetadataError("invalid transaction action path")
        if not _valid_sibling_name(backup_path) or (
            staged_path is not None and not _valid_sibling_name(staged_path)
        ):
            raise MetadataError("invalid transaction temporary path")
        if not _valid_optional_hash(old_hash) or not _valid_optional_hash(new_hash):
            raise MetadataError("invalid transaction action hash")
        if operation == "write" and (staged_path is None or new_hash is None):
            raise MetadataError("write transaction action is incomplete")
        if operation != "write" and (staged_path is not None or new_hash is not None):
            raise MetadataError("delete transaction action has staged content")
        if not isinstance(value["executable"], bool) or not isinstance(value["applied"], bool):
            raise MetadataError("transaction action flags must be booleans")
        return cls(
            root=root,
            path=path,
            operation=operation,
            old_hash=old_hash,
            new_hash=new_hash,
            stage_name=staged_path,
            backup_name=backup_path,
            executable=bool(value["executable"]),
            applied=bool(value["applied"]),
        )


@dataclass
class TransactionRecord:
    transaction_id: str
    operation: str
    phase: str
    old_manifest_hash: Optional[str]
    new_manifest_hash: Optional[str]
    actions: list[TransactionAction] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "operation": self.operation,
            "phase": self.phase,
            "old_manifest_hash": self.old_manifest_hash,
            "new_manifest_hash": self.new_manifest_hash,
            "actions": [action.to_dict() for action in self.actions],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransactionRecord":
        expected = {
            "schema_version", "transaction_id", "operation", "phase",
            "old_manifest_hash", "new_manifest_hash", "actions",
        }
        if set(value) != expected or value.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
            raise MetadataError("unsupported transaction schema")
        if not all(isinstance(value[key], str) for key in ("transaction_id", "operation", "phase")):
            raise MetadataError("invalid transaction identity fields")
        transaction_id = str(value["transaction_id"])
        operation = str(value["operation"])
        phase = str(value["phase"])
        old_manifest_hash = _optional_string(value.get("old_manifest_hash"))
        new_manifest_hash = _optional_string(value.get("new_manifest_hash"))
        if not transaction_id or operation not in VALID_TRANSACTION_OPERATIONS or phase not in VALID_TRANSACTION_PHASES:
            raise MetadataError("invalid transaction state")
        if not _valid_optional_hash(old_manifest_hash) or not _valid_optional_hash(new_manifest_hash):
            raise MetadataError("invalid transaction manifest hash")
        if not isinstance(value["actions"], list) or not all(
            isinstance(item, dict) for item in value["actions"]
        ):
            raise MetadataError("transaction actions must be an array of objects")
        return cls(
            transaction_id=transaction_id,
            operation=operation,
            phase=phase,
            old_manifest_hash=old_manifest_hash,
            new_manifest_hash=new_manifest_hash,
            actions=[TransactionAction.from_dict(item) for item in value["actions"]],
        )


@dataclass(frozen=True)
class OperationResult:
    operation: str
    status: str
    exit_code: int
    hook_mode: Optional[str] = None
    changed: Sequence[Mapping[str, str]] = ()
    warnings: Sequence[str] = ()
    recovery: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "operation": self.operation,
            "status": self.status,
            "exit_code": self.exit_code,
            "hook_mode": self.hook_mode,
            "changed": list(self.changed),
            "warnings": list(self.warnings),
            "recovery": self.recovery,
        }


def _valid_relative_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _valid_sibling_name(value: str) -> bool:
    return bool(value) and PurePosixPath(value).name == value and value not in {".", ".."}


def _valid_optional_hash(value: Optional[str]) -> bool:
    return value is None or (
        len(value) == 64 and all(character in "0123456789abcdef" for character in value)
    )


def _optional_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MetadataError("expected a string or null")
    return value
