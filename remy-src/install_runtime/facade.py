"""High-level installation, verification, and uninstall operations."""

from __future__ import annotations

import importlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .models import (
    LEGACY_MANIFEST_NAME,
    MANIFEST_RELATIVE_PATH,
    RUNTIME_RELATIVE_PATH,
    TRANSACTION_RELATIVE_PATH,
    CandidateFile,
    FileRecord,
    InstallRuntimeError,
    MetadataError,
    OperationResult,
    RootPaths,
)
from .probes import (
    default_daemon_name,
    probe_daemon,
    probe_daemon_version,
    probe_python,
)
from .settings import (
    hook_commands,
    load_settings,
    merge_settings_document,
    remove_settings_claim,
    verify_settings_claim,
)
from .storage import (
    load_json,
    normalize_relative_path,
    resolve_managed_path,
    sha256_bytes,
    sha256_file,
    validate_manifest,
)
from .transaction import FileTransaction, POST_COMMIT_DELETE


@dataclass(frozen=True)
class InstallRequest:
    suite_version: str
    candidates: Sequence[CandidateFile]
    settings_template: Mapping[str, Any]
    python_executable: str
    daemon_candidate: Optional[Path] = None


class InstallRuntime:
    def __init__(self, roots: RootPaths) -> None:
        self.roots = roots
        self.manifest_path = roots.remy / MANIFEST_RELATIVE_PATH
        self.legacy_manifest_path = roots.claude / LEGACY_MANIFEST_NAME
        self.transaction_path = roots.remy / TRANSACTION_RELATIVE_PATH
        self.runtime_path = roots.remy / RUNTIME_RELATIVE_PATH

    def install(self, request: InstallRequest) -> OperationResult:
        recovery_transaction = self._transaction(self.manifest_path)
        recovery = recovery_transaction.recover()
        cleanup_leftovers = list(recovery_transaction.cleanup_leftovers)
        recovery_transaction.sweep_pending_deletes()
        current, _ = self._load_current_manifest()
        self._validate_owned_files(current)
        daemon_path = self.roots.remy / "bin" / default_daemon_name()
        daemon = probe_daemon(daemon_path)
        if daemon.state != "stopped":
            raise InstallRuntimeError(
                "daemon must be stopped before installation; run: remy-cc daemon stop"
            )

        descriptor = probe_python(request.python_executable)
        hook_mode, daemon_source = self._select_daemon(request.daemon_candidate, daemon_path)
        commands = hook_commands(self.roots, hook_mode, descriptor.executable)
        settings_path = self.roots.claude / "settings.json"
        settings = load_settings(settings_path)
        prior_claim = current.get("settings_claim") if current else None
        merged_settings, settings_claim = merge_settings_document(
            settings, request.settings_template, self.roots, commands, prior_claim
        )

        with tempfile.TemporaryDirectory(prefix="remy-install-runtime-") as temporary:
            temp_root = Path(temporary)
            runtime_source = temp_root / "python.json"
            descriptor_document = descriptor.to_dict()
            if self.runtime_path.is_file():
                try:
                    previous_descriptor = load_json(self.runtime_path)
                except MetadataError:
                    previous_descriptor = {}
                identity_fields = {"schema_version", "executable", "version", "implementation", "platform"}
                if all(previous_descriptor.get(key) == descriptor_document.get(key) for key in identity_fields):
                    previous_probed_at = previous_descriptor.get("probed_at")
                    if isinstance(previous_probed_at, str):
                        descriptor_document["probed_at"] = previous_probed_at
            _write_json_file(runtime_source, descriptor_document)
            settings_source = temp_root / "settings.json"
            _write_json_file(settings_source, merged_settings)

            candidates = list(request.candidates)
            candidates.append(
                CandidateFile("remy", RUNTIME_RELATIVE_PATH.as_posix(), runtime_source, "runtime_descriptor")
            )
            if hook_mode == "rust":
                daemon_file = daemon_source or daemon_path
                candidates.append(
                    CandidateFile(
                        "remy",
                        "bin/" + default_daemon_name(),
                        daemon_file,
                        "daemon_binary",
                        executable=True,
                    )
                )
            records, changes = self._build_install_changes(candidates, current)
            settings_bytes = settings_source.read_bytes()
            existing_settings_hash = sha256_file(settings_path) if settings_path.is_file() else None
            if not settings_path.is_file() or sha256_bytes(settings_bytes) != existing_settings_hash:
                changes.append(
                    {
                        "root": "claude",
                        "path": "settings.json",
                        "operation": "write",
                        "source": str(settings_source),
                        "expected_old_hash": existing_settings_hash,
                    }
                )

            manifest = {
                "schema_version": 3,
                "suite_version": request.suite_version,
                "hook_mode": hook_mode,
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "files": [record.to_dict() for record in records],
                "settings_claim": settings_claim,
            }
            if current and _manifest_payload_equal(current, manifest):
                manifest["installed_at"] = current["installed_at"]
            validate_manifest(manifest)
            if self.legacy_manifest_path.exists():
                changes.append(
                    {
                        "root": "claude",
                        "path": LEGACY_MANIFEST_NAME,
                        "operation": POST_COMMIT_DELETE,
                        "expected_old_hash": sha256_file(self.legacy_manifest_path),
                    }
                )
            old_hash = sha256_file(self.manifest_path) if self.manifest_path.is_file() else None
            transaction = self._transaction(self.manifest_path)
            changed = transaction.execute("install", changes, manifest, old_hash)
            cleanup_leftovers.extend(transaction.cleanup_leftovers)
        warnings: list[str] = []
        if cleanup_leftovers:
            warnings.append(
                "cleanup deferred for {} locked file(s); a later install removes them".format(
                    len(cleanup_leftovers)
                )
            )
        return OperationResult(
            operation="install",
            status="ok",
            exit_code=0,
            hook_mode=hook_mode,
            changed=changed,
            warnings=warnings,
            recovery=recovery,
        )

    def load_manifest(self) -> dict[str, Any]:
        manifest, _ = self._load_current_manifest(require=True)
        if manifest is None:
            raise InstallRuntimeError("install manifest is missing")
        return manifest

    def verify(self) -> OperationResult:
        warnings: list[str] = []
        manifest: Optional[dict[str, Any]] = None
        if self.transaction_path.exists():
            warnings.append("an installation transaction requires recovery")
        try:
            manifest, _ = self._load_current_manifest(require=True)
            if manifest is None:
                raise InstallRuntimeError("install manifest is missing")
            self._validate_owned_files(manifest)
            settings = load_settings(self.roots.claude / "settings.json")
            verify_settings_claim(settings, manifest["settings_claim"])
            descriptor_value = load_json(self.runtime_path)
            executable = _validate_runtime_descriptor(descriptor_value)
            probe_python(executable)
        except InstallRuntimeError as exc:
            warnings.append(str(exc))
        return OperationResult(
            operation="verify",
            status="ok" if not warnings else "preflight_rejected",
            exit_code=0 if not warnings else 1,
            hook_mode=manifest.get("hook_mode") if manifest else None,
            warnings=warnings,
        )

    def verify_environment(self) -> OperationResult:
        result = self.verify()
        warnings = list(result.warnings)
        try:
            importlib.import_module("mcp")
        except (ImportError, ValueError):
            warnings.append("MCP SDK (mcp) is not installed")
        try:
            remy_config = importlib.import_module("remy_config")
            remy_config.validate_document(
                self.roots.claude / remy_config.CONFIG_FILE_NAME, project=False
            )
        except (ImportError, OSError, ValueError):
            warnings.append("Remy configuration is missing or invalid")
        return OperationResult(
            operation="verify",
            status="ok" if not warnings else "preflight_rejected",
            exit_code=0 if not warnings else 1,
            hook_mode=result.hook_mode,
            warnings=warnings,
        )

    def uninstall(self, purge_state: bool = False) -> OperationResult:
        recovery = None
        if self.transaction_path.exists():
            recovery_manifest = (
                self.manifest_path
                if self.manifest_path.exists() or not self.legacy_manifest_path.exists()
                else self.legacy_manifest_path
            )
            recovery = self._transaction(recovery_manifest).recover()
            if not self.manifest_path.exists() and not self.legacy_manifest_path.exists():
                if purge_state:
                    shutil.rmtree(self.roots.remy, ignore_errors=False)
                return OperationResult(
                    operation="uninstall",
                    status="ok",
                    exit_code=0,
                    changed=[],
                    recovery=recovery,
                )
        manifest, current_path = self._load_current_manifest(require=True)
        if manifest is None:
            raise InstallRuntimeError("install manifest is missing")
        self._validate_owned_files(manifest)
        transaction = self._transaction(current_path)
        daemon_path = self.roots.remy / "bin" / default_daemon_name()
        daemon = probe_daemon(daemon_path)
        if daemon.state != "stopped":
            raise InstallRuntimeError(
                "daemon must be stopped before uninstall; run: remy-cc daemon stop"
            )
        settings_path = self.roots.claude / "settings.json"
        settings = load_settings(settings_path)
        cleaned_settings = remove_settings_claim(settings, manifest["settings_claim"])
        with tempfile.TemporaryDirectory(prefix="remy-uninstall-runtime-") as temporary:
            settings_source = Path(temporary) / "settings.json"
            _write_json_file(settings_source, cleaned_settings)
            changes: list[dict[str, Any]] = []
            for raw in manifest["files"]:
                target = resolve_managed_path(self.roots, raw["root"], raw["path"])
                changes.append(
                    {
                        "root": raw["root"],
                        "path": raw["path"],
                        "operation": "delete",
                        "expected_old_hash": sha256_file(target),
                    }
                )
            current_settings_hash = sha256_file(settings_path) if settings_path.is_file() else None
            if sha256_bytes(settings_source.read_bytes()) != current_settings_hash:
                changes.append(
                    {
                        "root": "claude",
                        "path": "settings.json",
                        "operation": "write",
                        "source": str(settings_source),
                        "expected_old_hash": current_settings_hash,
                    }
                )
            changed = transaction.execute(
                "uninstall", changes, None, sha256_file(current_path)
            )
        if purge_state:
            try:
                shutil.rmtree(self.roots.remy)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise InstallRuntimeError(
                    "uninstall committed but engine-state cleanup is incomplete",
                    category="cleanup",
                ) from exc
        return OperationResult(
            operation="uninstall",
            status="ok",
            exit_code=0,
            hook_mode=manifest.get("hook_mode"),
            changed=changed,
            recovery=recovery,
        )

    def _transaction(self, manifest_path: Path) -> FileTransaction:
        return FileTransaction(self.roots, self.transaction_path, manifest_path)

    def _load_current_manifest(
        self, require: bool = False
    ) -> tuple[Optional[dict[str, Any]], Path]:
        if self.manifest_path.exists():
            current = validate_manifest(load_json(self.manifest_path))
            if self.legacy_manifest_path.exists():
                self._parse_legacy_manifest(load_json(self.legacy_manifest_path))
            return current, self.manifest_path
        if self.legacy_manifest_path.exists():
            return self._parse_legacy_manifest(load_json(self.legacy_manifest_path)), self.legacy_manifest_path
        if require:
            raise InstallRuntimeError("install manifest is missing")
        return None, self.manifest_path

    def _parse_legacy_manifest(self, value: Mapping[str, Any]) -> dict[str, Any]:
        raw_files = value.get("files")
        if not isinstance(raw_files, list):
            raise MetadataError("legacy manifest files must be an array")
        records: list[dict[str, str]] = []
        for raw in raw_files:
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                raise MetadataError("invalid legacy manifest file record")
            digest = raw.get("sha256")
            if not isinstance(digest, str) or len(digest) != 64:
                raise MetadataError("legacy manifest record lacks sha256")
            path = Path(raw["path"])
            if path.is_absolute():
                root, relative = self._classify_absolute(path)
            else:
                root, relative = "claude", normalize_relative_path(raw["path"])
            records.append(FileRecord(root, relative, digest, "remy-cc", "legacy").to_dict())
        # Pre-dual-root installers wrote the CLI shim without recording it in
        # their manifest; adopt the on-disk shim so install can overwrite it.
        recorded_paths = {(record["root"], record["path"]) for record in records}
        for shim_name in ("bin/remy-cc.cmd", "bin/remy-cc"):
            if ("claude", shim_name) in recorded_paths:
                continue
            shim_target = self.roots.claude / shim_name
            if shim_target.is_file():
                records.append(
                    FileRecord(
                        "claude", shim_name, sha256_file(shim_target), "remy-cc", "legacy"
                    ).to_dict()
                )
        claim = _legacy_settings_claim(
            value.get("injected_hooks"), value.get("injected_permissions"), self.roots
        )
        return {
            "schema_version": 3,
            "suite_version": str(value.get("version", "unknown")),
            "hook_mode": "python",
            "installed_at": str(value.get("installed_at", "legacy")),
            "files": records,
            "settings_claim": claim,
        }

    def _classify_absolute(self, path: Path) -> tuple[str, str]:
        resolved = path.resolve(strict=False)
        for name, root in (("claude", self.roots.claude), ("remy", self.roots.remy)):
            try:
                relative = resolved.relative_to(root.resolve())
            except ValueError:
                continue
            return name, normalize_relative_path(relative.as_posix())
        raise MetadataError("legacy manifest contains a path outside managed roots")

    def _validate_owned_files(self, manifest: Optional[Mapping[str, Any]]) -> None:
        if not manifest:
            return
        for raw in manifest["files"]:
            target = resolve_managed_path(self.roots, raw["root"], raw["path"])
            if not target.is_file() or sha256_file(target) != raw["sha256"]:
                raise InstallRuntimeError("a managed file is missing or modified")

    def _select_daemon(
        self, candidate: Optional[Path], deployed: Path
    ) -> tuple[str, Optional[Path]]:
        candidate_version = probe_daemon_version(candidate) if candidate else None
        deployed_version = probe_daemon_version(deployed)
        if candidate and candidate_version:
            if deployed_version and candidate_version == deployed_version:
                if sha256_file(candidate) != sha256_file(deployed):
                    raise InstallRuntimeError("same-version daemon binaries have different hashes")
                return "rust", None
            return "rust", candidate
        if deployed_version:
            return "rust", None
        raise InstallRuntimeError(
            "no usable remy-cc binary was found; build it with "
            "'cargo build --release' under remy-cc/ (install.py deploys "
            "target/release) or download a release with the daemon binary"
        )

    def _build_install_changes(
        self,
        candidates: Sequence[CandidateFile],
        current: Optional[Mapping[str, Any]],
    ) -> tuple[list[FileRecord], list[dict[str, Any]]]:
        old_records = {
            (raw["root"], raw["path"]): raw for raw in (current or {}).get("files", [])
        }
        records: list[FileRecord] = []
        changes: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            relative = normalize_relative_path(candidate.path)
            key = (candidate.root, relative)
            if key in seen:
                raise InstallRuntimeError("duplicate candidate file")
            seen.add(key)
            digest = sha256_file(candidate.source)
            record = FileRecord(candidate.root, relative, digest, "remy-cc", candidate.role)
            records.append(record)
            target = resolve_managed_path(self.roots, candidate.root, relative)
            current_hash = sha256_file(target) if target.is_file() else None
            old = old_records.pop(key, None)
            if old is None and current_hash not in {None, digest}:
                raise InstallRuntimeError("an unmanaged target has different content")
            if old is not None and current_hash != old["sha256"]:
                raise InstallRuntimeError("a managed target changed after preflight")
            if current_hash != digest:
                changes.append(
                    {
                        "root": candidate.root,
                        "path": relative,
                        "operation": "write",
                        "source": str(candidate.source),
                        "expected_old_hash": current_hash,
                        "executable": candidate.executable,
                    }
                )
        for key, old in old_records.items():
            target = resolve_managed_path(self.roots, key[0], key[1])
            current_hash = sha256_file(target) if target.is_file() else None
            if current_hash != old["sha256"]:
                raise InstallRuntimeError("an obsolete managed target was modified")
            changes.append(
                {
                    "root": key[0],
                    "path": key[1],
                    "operation": "delete",
                    "expected_old_hash": current_hash,
                }
            )
        records.sort(key=lambda item: (item.root, item.path))
        return records, changes


def _validate_runtime_descriptor(value: Mapping[str, Any]) -> str:
    expected = {"schema_version", "executable", "version", "implementation", "platform", "probed_at"}
    version = value.get("version")
    if (
        set(value) != expected
        or value.get("schema_version") != 1
        or not isinstance(value.get("executable"), str)
        or not isinstance(version, list)
        or len(version) != 3
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in version)
        or tuple(version) < (3, 10, 0)
        or not all(isinstance(value.get(key), str) and value.get(key) for key in ("implementation", "platform", "probed_at"))
    ):
        raise MetadataError("invalid Python runtime descriptor")
    executable = str(value["executable"])
    if not Path(executable).is_absolute():
        raise MetadataError("Python runtime executable must be absolute")
    return executable


def result_for_error(operation: str, error: InstallRuntimeError) -> OperationResult:
    codes = {"preflight": 1, "rollback": 2, "cleanup": 3, "recovery": 4}
    code = codes.get(error.category, 1)
    statuses = {
        1: "preflight_rejected",
        2: "rolled_back",
        3: "committed_cleanup_pending",
        4: "recovery_incomplete",
    }
    return OperationResult(
        operation=operation,
        status=statuses[code],
        exit_code=code,
        warnings=[str(error)],
    )


def _write_json_file(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _manifest_payload_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    keys = {"schema_version", "suite_version", "hook_mode", "files", "settings_claim"}
    return all(left.get(key) == right.get(key) for key in keys)


def _legacy_settings_claim(raw_hooks: Any, raw_permissions: Any, roots: RootPaths) -> dict[str, Any]:
    hooks: list[dict[str, str]] = []
    if isinstance(raw_hooks, dict):
        for event, entries in raw_hooks.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                matcher = str(entry.get("matcher", ""))
                for hook in entry.get("hooks", []):
                    if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                        command = hook["command"].strip().replace(
                            "~/.claude/", str(roots.claude).replace("\\", "/") + "/"
                        )
                        hooks.append(
                            {"event": str(event), "matcher": matcher, "command": command}
                        )
    permissions = [item for item in raw_permissions or [] if isinstance(item, str)]
    return {"hooks": hooks, "permissions": permissions}
