"""Crash-recoverable file transaction for the two managed user roots."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .models import (
    InstallRuntimeError,
    MetadataError,
    RootPaths,
    TransactionAction,
    TransactionRecord,
)
from .storage import (
    atomic_write_json,
    canonical_json_bytes,
    load_json,
    resolve_managed_path,
    sha256_bytes,
    sha256_file,
)

POST_COMMIT_DELETE = "post_commit_delete"
PRIVATE_SETTINGS_TARGET = ("claude", "settings.json")
PRIVATE_FILE_MODE = 0o600


class FileTransaction:
    def __init__(self, roots: RootPaths, journal_path: Path, manifest_path: Path) -> None:
        self.roots = roots
        self.journal_path = journal_path
        self.manifest_path = manifest_path
        self.pending_deletes_path = journal_path.with_name("pending_deletes.json")
        self.cleanup_leftovers: list[str] = []

    def recover(self) -> Optional[str]:
        if not self.journal_path.exists():
            return None
        try:
            record = TransactionRecord.from_dict(load_json(self.journal_path))
            committed = self._is_committed(record)
            if committed:
                self._complete_post_commit(record)
                self._cleanup(record)
                return "completed_committed_cleanup"
            self._rollback(record)
            return "rolled_back_incomplete_transaction"
        except MetadataError:
            raise
        except Exception as exc:
            raise InstallRuntimeError(
                "installation transaction recovery is incomplete", category="recovery"
            ) from exc

    def execute(
        self,
        operation: str,
        changes: Sequence[Mapping[str, Any]],
        manifest_document: Optional[Mapping[str, Any]],
        old_manifest_hash: Optional[str],
    ) -> list[dict[str, str]]:
        if self.journal_path.exists():
            raise MetadataError("an installation transaction is already present")
        transaction_id = uuid.uuid4().hex
        actions = [self._plan_action(transaction_id, change) for change in changes]
        record = TransactionRecord(
            transaction_id=transaction_id,
            operation=operation,
            phase="prepared",
            old_manifest_hash=old_manifest_hash,
            new_manifest_hash=(
                sha256_bytes(canonical_json_bytes(manifest_document))
                if manifest_document is not None
                else None
            ),
            actions=actions,
        )
        self._write_record(record)
        try:
            record.phase = "staging"
            self._write_record(record)
            for action, change in zip(record.actions, changes):
                self._stage_action(action, change)
            record.phase = "applying"
            self._write_record(record)
            for action in record.actions:
                if action.operation == POST_COMMIT_DELETE:
                    continue
                self._apply(action)
                action.applied = True
                self._write_record(record)
            record.phase = "publishing_manifest"
            self._write_record(record)
            current_manifest_hash = sha256_file(self.manifest_path) if self.manifest_path.is_file() else None
            if current_manifest_hash != old_manifest_hash:
                raise InstallRuntimeError("install manifest changed during commit")
            if manifest_document is None:
                removed_manifest = self._removed_manifest_path(record)
                if removed_manifest.exists():
                    raise InstallRuntimeError("uninstall manifest tombstone already exists")
                os.replace(self.manifest_path, removed_manifest)
            else:
                atomic_write_json(self.manifest_path, manifest_document)
                if sha256_file(self.manifest_path) != record.new_manifest_hash:
                    raise InstallRuntimeError("published manifest hash is invalid")
            record.phase = "committed"
            self._write_record(record)
        except Exception as exc:
            if self._is_committed(record):
                raise InstallRuntimeError(
                    "installation committed but cleanup is incomplete", category="cleanup"
                ) from exc
            current_manifest_hash = sha256_file(self.manifest_path) if self.manifest_path.is_file() else None
            if record.phase == "publishing_manifest" and current_manifest_hash != old_manifest_hash:
                raise InstallRuntimeError(
                    "manifest publication state requires recovery", category="recovery"
                ) from exc
            try:
                self._rollback(record)
            except Exception as rollback_exc:
                raise InstallRuntimeError(
                    "installation failed and rollback is incomplete", category="recovery"
                ) from rollback_exc
            raise InstallRuntimeError("installation failed and was rolled back", category="rollback") from exc

        try:
            self._complete_post_commit(record)
            changed = [
                {"root": action.root, "path": action.path, "operation": action.operation}
                for action in record.actions
            ]
            self._cleanup(record)
        except Exception as exc:
            raise InstallRuntimeError(
                "installation committed but cleanup is incomplete", category="cleanup"
            ) from exc
        return changed

    def _plan_action(self, transaction_id: str, change: Mapping[str, Any]) -> TransactionAction:
        root = str(change["root"])
        relative = str(change["path"])
        operation = str(change["operation"])
        target = resolve_managed_path(self.roots, root, relative)
        token = transaction_id[:12]
        stage_name: Optional[str] = None
        if operation == "write":
            source = Path(str(change["source"]))
            if not source.is_file():
                raise InstallRuntimeError("candidate file is missing")
            stage_name = ".{}.{}.stage".format(target.name, token)
            new_hash = sha256_file(source)
        elif operation in {"delete", POST_COMMIT_DELETE}:
            new_hash = None
        else:
            raise InstallRuntimeError("unsupported transaction operation")
        old_hash = sha256_file(target) if target.is_file() else None
        if change.get("expected_old_hash") != old_hash:
            raise InstallRuntimeError("managed target changed after preflight")
        return TransactionAction(
            root=root,
            path=relative,
            operation=operation,
            old_hash=old_hash,
            new_hash=new_hash,
            stage_name=stage_name,
            backup_name=".{}.{}.backup".format(target.name, token),
            executable=bool(change.get("executable", False)),
        )

    def _stage_action(self, action: TransactionAction, change: Mapping[str, Any]) -> None:
        if action.operation != "write":
            return
        if action.stage_name is None:
            raise MetadataError("write action is missing a staged path")
        source = Path(str(change["source"]))
        target = resolve_managed_path(self.roots, action.root, action.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = target.with_name(action.stage_name)
        if stage.exists():
            raise InstallRuntimeError("transaction stage path already exists")
        shutil.copy2(source, stage)
        if sha256_file(stage) != action.new_hash:
            raise InstallRuntimeError("candidate file changed during staging")
        if os.name == "posix":
            if target.exists():
                os.chmod(stage, target.stat().st_mode & 0o777)
            elif (action.root, action.path) == PRIVATE_SETTINGS_TARGET:
                os.chmod(stage, PRIVATE_FILE_MODE)
            elif action.executable:
                os.chmod(stage, 0o755)

    def _apply(self, action: TransactionAction) -> None:
        target = resolve_managed_path(self.roots, action.root, action.path)
        backup = target.with_name(action.backup_name)
        current_hash = sha256_file(target) if target.is_file() else None
        if backup.exists():
            if action.operation in {"delete", POST_COMMIT_DELETE} and not target.exists():
                return
            if action.operation == "write" and current_hash == action.new_hash:
                return
            raise InstallRuntimeError("transaction action has an inconsistent backup")
        if current_hash != action.old_hash:
            raise InstallRuntimeError("managed target changed during commit")
        if target.exists():
            os.replace(target, backup)
        if action.operation == "write":
            if action.stage_name is None:
                raise MetadataError("write action is missing a staged file")
            stage = target.with_name(action.stage_name)
            if not stage.is_file() or sha256_file(stage) != action.new_hash:
                raise InstallRuntimeError("staged candidate changed before commit")
            os.replace(stage, target)
            if action.executable and os.name == "posix":
                os.chmod(target, 0o755)

    def _complete_post_commit(self, record: TransactionRecord) -> None:
        record.phase = "cleanup"
        self._write_record(record)
        for action in record.actions:
            if action.operation != POST_COMMIT_DELETE or action.applied:
                continue
            self._apply(action)
            action.applied = True
            self._write_record(record)

    def _is_committed(self, record: TransactionRecord) -> bool:
        if record.operation == "uninstall":
            return (
                record.phase in {"publishing_manifest", "committed", "cleanup"}
                and not self.manifest_path.exists()
                and self._removed_manifest_path(record).is_file()
            )
        return (
            record.phase in {"publishing_manifest", "committed", "cleanup"}
            and self.manifest_path.is_file()
            and record.new_manifest_hash is not None
            and sha256_file(self.manifest_path) == record.new_manifest_hash
        )

    def _rollback(self, record: TransactionRecord) -> None:
        errors: list[OSError] = []
        for action in reversed(record.actions):
            if action.operation == POST_COMMIT_DELETE:
                continue
            target = resolve_managed_path(self.roots, action.root, action.path)
            backup = target.with_name(action.backup_name)
            stage = target.with_name(action.stage_name) if action.stage_name else None
            try:
                if backup.exists():
                    if target.exists():
                        target.unlink()
                    os.replace(backup, target)
                elif action.old_hash is None and target.is_file() and action.new_hash == sha256_file(target):
                    target.unlink()
                if stage and stage.exists():
                    stage.unlink()
            except OSError as exc:
                errors.append(exc)
        if errors:
            record.phase = "rollback_incomplete"
            self._write_record(record)
            raise errors[0]
        self.journal_path.unlink(missing_ok=True)

    def _cleanup(self, record: TransactionRecord) -> None:
        """Best-effort residue removal after commit.

        A locked backup (e.g. the previous daemon binary still mapped by a
        running process on Windows) must not fail the committed install:
        undeletable paths are registered for a later sweep and the journal is
        released. Only a double failure — undeletable AND unregistrable —
        keeps the journal and propagates, so recover() retries the full chain.
        """
        self.cleanup_leftovers = []
        residues: list[Path] = []
        for action in record.actions:
            target = resolve_managed_path(self.roots, action.root, action.path)
            residues.append(target.with_name(action.backup_name))
            if action.stage_name:
                residues.append(target.with_name(action.stage_name))
        if record.operation == "uninstall":
            residues.append(self._removed_manifest_path(record))
        leftovers: list[Path] = []
        for path in residues:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                leftovers.append(path)
        if leftovers:
            self._register_pending_deletes(leftovers)
            self.cleanup_leftovers = [str(path) for path in leftovers]
        self.journal_path.unlink(missing_ok=True)

    def _register_pending_deletes(self, paths: Sequence[Path]) -> None:
        existing: list[str] = []
        if self.pending_deletes_path.exists():
            try:
                document = load_json(self.pending_deletes_path)
            except MetadataError:
                document = None
            entries = document.get("paths") if isinstance(document, dict) else None
            if isinstance(entries, list):
                existing = [str(entry) for entry in entries]
        merged = list(dict.fromkeys(existing + [str(path) for path in paths]))
        atomic_write_json(
            self.pending_deletes_path, {"schema_version": 1, "paths": merged}
        )

    def _is_managed(self, path: Path) -> bool:
        for root in (self.roots.claude, self.roots.remy):
            try:
                path.resolve().relative_to(Path(root).resolve())
                return True
            except ValueError:
                continue
        return False

    def sweep_pending_deletes(self) -> None:
        """Best-effort deletion of previously registered residues.

        Entries outside the two managed roots are dropped without deletion.
        Any failure keeps the entry (or the register file) for the next
        sweep; sweeping never blocks the surrounding operation.
        """
        if not self.pending_deletes_path.exists():
            return
        try:
            document = load_json(self.pending_deletes_path)
        except MetadataError:
            document = None
        entries = document.get("paths") if isinstance(document, dict) else None
        paths = [str(entry) for entry in entries] if isinstance(entries, list) else []
        remaining: list[str] = []
        for text in paths:
            path = Path(text)
            if not self._is_managed(path):
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                remaining.append(text)
        try:
            if remaining:
                atomic_write_json(
                    self.pending_deletes_path,
                    {"schema_version": 1, "paths": remaining},
                )
            else:
                self.pending_deletes_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _removed_manifest_path(self, record: TransactionRecord) -> Path:
        return self.manifest_path.with_name(
            ".{}.{}.removed".format(self.manifest_path.name, record.transaction_id[:12])
        )

    def _write_record(self, record: TransactionRecord) -> None:
        atomic_write_json(self.journal_path, record.to_dict())
