"""Validated storage primitives for installer metadata and managed paths."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .models import (
    MANIFEST_SCHEMA_VERSION,
    FileRecord,
    MetadataError,
    RootPaths,
    VALID_HOOK_MODES,
    VALID_ROOTS,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def normalize_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise MetadataError("managed path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MetadataError("managed path escapes its declared root")
    return path.as_posix()


def resolve_managed_path(roots: RootPaths, root: str, relative: str) -> Path:
    if root not in VALID_ROOTS:
        raise MetadataError("unsupported managed root: {}".format(root))
    normalized = normalize_relative_path(relative)
    base = roots.for_name(root).resolve()
    target = (base / Path(*PurePosixPath(normalized).parts)).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise MetadataError("managed path escapes its declared root") from exc
    return target


def load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MetadataError("invalid managed metadata: {}".format(path.name)) from exc
    if not isinstance(value, dict):
        raise MetadataError("managed metadata must be a JSON object")
    return value


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_json_bytes(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        if os.name == "posix":
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {"schema_version", "suite_version", "hook_mode", "installed_at", "files", "settings_claim"}
    if set(value) != expected or value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise MetadataError("unsupported manifest schema")
    if value.get("hook_mode") not in VALID_HOOK_MODES:
        raise MetadataError("invalid manifest hook_mode")
    if not isinstance(value.get("suite_version"), str) or not isinstance(value.get("installed_at"), str):
        raise MetadataError("invalid manifest identity fields")
    raw_files = value.get("files")
    if not isinstance(raw_files, list):
        raise MetadataError("manifest files must be an array")
    seen: set[tuple[str, str]] = set()
    files: list[dict[str, str]] = []
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != {"root", "path", "sha256", "owner", "role"}:
            raise MetadataError("invalid manifest file record")
        root = raw.get("root")
        path = raw.get("path")
        digest = raw.get("sha256")
        owner = raw.get("owner")
        role = raw.get("role")
        if root not in VALID_ROOTS or not all(isinstance(item, str) and item for item in (path, digest, owner, role)):
            raise MetadataError("invalid manifest file identity")
        assert isinstance(root, str)
        assert isinstance(path, str)
        assert isinstance(digest, str)
        assert isinstance(owner, str)
        assert isinstance(role, str)
        if owner != "remy-cc":
            raise MetadataError("invalid manifest owner")
        normalized = normalize_relative_path(path)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise MetadataError("invalid manifest sha256")
        key = (root, normalized)
        if key in seen:
            raise MetadataError("duplicate manifest file record")
        seen.add(key)
        files.append(FileRecord(root, normalized, digest, owner, role).to_dict())
    settings_claim = value.get("settings_claim")
    if not isinstance(settings_claim, dict):
        raise MetadataError("manifest settings_claim must be an object")
    hooks = settings_claim.get("hooks")
    permissions = settings_claim.get("permissions")
    if not isinstance(hooks, list) or not isinstance(permissions, list):
        raise MetadataError("invalid manifest settings_claim")
    for hook in hooks:
        if (
            not isinstance(hook, dict)
            or set(hook) != {"event", "matcher", "command"}
            or not all(isinstance(hook.get(key), str) for key in ("event", "matcher", "command"))
        ):
            raise MetadataError("invalid manifest Hook claim")
    if not all(isinstance(permission, str) for permission in permissions):
        raise MetadataError("invalid manifest permission claim")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "suite_version": value["suite_version"],
        "hook_mode": value["hook_mode"],
        "installed_at": value["installed_at"],
        "files": files,
        "settings_claim": {"hooks": hooks, "permissions": permissions},
    }
