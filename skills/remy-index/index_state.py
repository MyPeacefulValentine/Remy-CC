"""Shared run results, process locks, and dirty-path queue state."""

from __future__ import annotations

import errno
import glob
import importlib
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Tuple


SOURCE_EXTENSIONS = frozenset(
    (".py", ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hh", ".hxx", ".ts", ".tsx")
)
DIRTY_FILE = os.path.join(".claude", "logic_index_dirty")
DIRTY_PROCESSING_FILE = DIRTY_FILE + ".processing"
DIRTY_PENDING_PREFIX = DIRTY_FILE + ".pending."
SCAN_LOCK_FILE = os.path.join(".claude", "logic_index_scan.lock")
QUEUE_LOCK_FILE = os.path.join(".claude", "logic_index_dirty.lock")


class RunStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"

    @property
    def exit_code(self) -> int:
        return {self.SUCCESS: 0, self.PARTIAL: 2, self.FAILED: 1}[self]


@dataclass(frozen=True)
class StageError:
    stage: str
    message: str
    path: Optional[str] = None


@dataclass(frozen=True)
class ScanResult:
    status: RunStatus
    discovered_paths: Tuple[str, ...] = ()
    successful_paths: Tuple[str, ...] = ()
    failed_paths: Tuple[str, ...] = ()
    deleted_paths: Tuple[str, ...] = ()
    errors: Tuple[StageError, ...] = ()
    postprocess_complete: bool = True

    @property
    def exit_code(self) -> int:
        return self.status.exit_code

    @classmethod
    def from_parts(
        cls,
        *,
        discovered_paths: Iterable[str] = (),
        successful_paths: Iterable[str] = (),
        failed_paths: Iterable[str] = (),
        deleted_paths: Iterable[str] = (),
        errors: Iterable[StageError] = (),
        postprocess_complete: bool = True,
    ) -> "ScanResult":
        discovered = tuple(sorted(set(discovered_paths)))
        successful = tuple(sorted(set(successful_paths)))
        failed = tuple(sorted(set(failed_paths)))
        deleted = tuple(sorted(set(deleted_paths)))
        error_items = tuple(errors)
        if not error_items:
            status = RunStatus.SUCCESS
        elif successful or deleted:
            status = RunStatus.PARTIAL
        else:
            status = RunStatus.FAILED
        return cls(
            status=status,
            discovered_paths=discovered,
            successful_paths=successful,
            failed_paths=failed,
            deleted_paths=deleted,
            errors=error_items,
            postprocess_complete=postprocess_complete,
        )


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    scan: Optional[ScanResult] = None
    errors: Tuple[StageError, ...] = ()
    symbol_requested: int = 0
    symbol_completed: int = 0
    file_requested: int = 0
    file_completed: int = 0
    cluster_requested: int = 0
    cluster_completed: int = 0

    @property
    def exit_code(self) -> int:
        return self.status.exit_code


class LockTimeoutError(TimeoutError):
    pass


def _env_timeout(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def scan_lock_timeout() -> float:
    return _env_timeout("INDEX_SCAN_LOCK_TIMEOUT", 30.0)


def queue_lock_timeout() -> float:
    return _env_timeout("INDEX_QUEUE_LOCK_TIMEOUT", 1.0)


def _lock_byte(handle, nonblocking: bool) -> None:
    try:
        msvcrt = importlib.import_module("msvcrt")
    except ImportError:
        fcntl = importlib.import_module("fcntl")
        mode = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(handle.fileno(), mode)
    else:
        mode = msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
        msvcrt.locking(handle.fileno(), mode, 1)


def _unlock_byte(handle) -> None:
    try:
        msvcrt = importlib.import_module("msvcrt")
    except ImportError:
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    else:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class InterProcessFileLock:
    """Advisory one-byte lock implemented with the platform standard library."""

    def __init__(self, path: str, timeout: float):
        self.path = path
        self.timeout = max(0.0, timeout)
        self._handle = None

    def acquire(self) -> "InterProcessFileLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle.seek(0)
                _lock_byte(handle, nonblocking=True)
                self._handle = handle
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13):
                    handle.close()
                    raise
                if time.monotonic() >= deadline:
                    handle.close()
                    raise LockTimeoutError(f"Timed out acquiring lock: {self.path}") from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            _unlock_byte(handle)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "InterProcessFileLock":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def project_scan_lock(root_dir: str, timeout: Optional[float] = None) -> InterProcessFileLock:
    if timeout is None:
        timeout = scan_lock_timeout()
    return InterProcessFileLock(os.path.join(os.path.abspath(root_dir), SCAN_LOCK_FILE), timeout)


def normalize_source_path(root_dir: str, file_path: str) -> Optional[str]:
    if not file_path:
        return None
    root = os.path.realpath(os.path.abspath(root_dir))
    candidate = file_path if os.path.isabs(file_path) else os.path.join(root, file_path)
    candidate = os.path.realpath(os.path.abspath(candidate))
    try:
        if os.path.commonpath((os.path.normcase(root), os.path.normcase(candidate))) != os.path.normcase(root):
            return None
    except ValueError:
        return None
    if os.path.isdir(candidate):
        return None
    if os.path.splitext(candidate)[1].lower() not in SOURCE_EXTENSIONS:
        return None
    return os.path.relpath(candidate, root).replace(os.sep, "/")


def _read_paths(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return {line.strip() for line in handle if line.strip()}
    except FileNotFoundError:
        return set()


def _atomic_write_paths(path: str, paths: Iterable[str]) -> None:
    values = sorted(set(paths))
    if not values:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp.{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(values) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


@dataclass(frozen=True)
class DirtyClaim:
    paths: Tuple[str, ...]


class DirtyQueue:
    """Crash-recoverable dirty path queue guarded by a short-lived file lock."""

    def __init__(self, root_dir: str):
        self.root_dir = os.path.abspath(root_dir)
        self.dirty_path = os.path.join(self.root_dir, DIRTY_FILE)
        self.processing_path = os.path.join(self.root_dir, DIRTY_PROCESSING_FILE)
        self.pending_prefix = os.path.join(self.root_dir, DIRTY_PENDING_PREFIX)
        self.lock_path = os.path.join(self.root_dir, QUEUE_LOCK_FILE)

    def _lock(self) -> InterProcessFileLock:
        return InterProcessFileLock(self.lock_path, queue_lock_timeout())

    def _merge_pending_locked(self) -> set[str]:
        paths = _read_paths(self.dirty_path)
        for pending_path in glob.glob(self.pending_prefix + "*"):
            paths.update(_read_paths(pending_path))
            try:
                os.remove(pending_path)
            except FileNotFoundError:
                pass
        _atomic_write_paths(self.dirty_path, paths)
        return paths

    def _recover_processing_locked(self) -> set[str]:
        paths = self._merge_pending_locked()
        paths.update(_read_paths(self.processing_path))
        _atomic_write_paths(self.dirty_path, paths)
        try:
            os.remove(self.processing_path)
        except FileNotFoundError:
            pass
        return paths

    def record(self, file_path: str) -> Optional[str]:
        normalized = normalize_source_path(self.root_dir, file_path)
        if normalized is None:
            return None
        try:
            with self._lock():
                paths = self._merge_pending_locked()
                paths.add(normalized)
                _atomic_write_paths(self.dirty_path, paths)
        except LockTimeoutError:
            os.makedirs(os.path.dirname(self.dirty_path), exist_ok=True)
            pending_path = self.pending_prefix + str(os.getpid())
            with open(pending_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(normalized + "\n")
        return normalized

    def peek(self) -> set[str]:
        with self._lock():
            return set(self._merge_pending_locked())

    def claim(self, paths: Optional[Iterable[str]] = None) -> DirtyClaim:
        with self._lock():
            available = self._recover_processing_locked()
            if paths is None:
                selected = available
            else:
                requested = {
                    normalized
                    for item in paths
                    for normalized in (normalize_source_path(self.root_dir, item),)
                    if normalized is not None
                }
                selected = available & requested
            _atomic_write_paths(self.dirty_path, available - selected)
            _atomic_write_paths(self.processing_path, selected)
            return DirtyClaim(tuple(sorted(selected)))

    def finish(
        self,
        claim: DirtyClaim,
        successful_paths: Iterable[str] = (),
        *,
        retry_all: bool = False,
    ) -> None:
        claimed = set(claim.paths)
        successful = set(successful_paths)
        with self._lock():
            processing = _read_paths(self.processing_path)
            active = processing & claimed
            retry = active if retry_all else active - successful
            queued = self._merge_pending_locked()
            queued.update(retry)
            _atomic_write_paths(self.dirty_path, queued)
            remaining_processing = processing - active
            _atomic_write_paths(self.processing_path, remaining_processing)

    def recover(self) -> set[str]:
        with self._lock():
            return set(self._recover_processing_locked())
