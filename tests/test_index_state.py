"""Tests for index run results, process locks, and dirty queue recovery."""

import multiprocessing
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "remy-index"))
from index_state import (
    DirtyQueue,
    InterProcessFileLock,
    LockTimeoutError,
    RunStatus,
    ScanResult,
    StageError,
    normalize_source_path,
)


def _hold_lock(path, ready):
    with InterProcessFileLock(path, 1):
        ready.set()
        time.sleep(10)


def test_status_exit_codes():
    assert RunStatus.SUCCESS.exit_code == 0
    assert RunStatus.PARTIAL.exit_code == 2
    assert RunStatus.FAILED.exit_code == 1


def test_scan_result_status_from_parts():
    assert ScanResult.from_parts(successful_paths=["a.py"]).status == RunStatus.SUCCESS
    partial = ScanResult.from_parts(
        successful_paths=["a.py"], failed_paths=["b.py"],
        errors=[StageError("file_scan", "bad", "b.py")],
    )
    assert partial.status == RunStatus.PARTIAL
    failed = ScanResult.from_parts(
        failed_paths=["b.py"], errors=[StageError("file_scan", "bad", "b.py")],
    )
    assert failed.status == RunStatus.FAILED


def test_normalize_rejects_external_and_non_source(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    source = root / "a.py"
    source.write_text("x=1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("x=2\n", encoding="utf-8")
    text = root / "note.txt"
    text.write_text("x", encoding="utf-8")
    assert normalize_source_path(str(root), str(source)) == "a.py"
    assert normalize_source_path(str(root), str(outside)) is None
    assert normalize_source_path(str(root), str(text)) is None


def test_dirty_queue_preserves_new_generation(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("x=1\n", encoding="utf-8")
    queue = DirtyQueue(str(tmp_path))
    queue.record(str(source))
    claim = queue.claim(["a.py"])
    queue.record(str(source))
    queue.finish(claim, successful_paths=["a.py"])
    assert queue.peek() == {"a.py"}


def test_dirty_queue_partial_ack_requeues_failed_path(tmp_path):
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("x=1\n", encoding="utf-8")
    queue = DirtyQueue(str(tmp_path))
    queue.record("a.py")
    queue.record("b.py")
    claim = queue.claim()
    queue.finish(claim, successful_paths=["a.py"])
    assert queue.peek() == {"b.py"}


def test_dirty_queue_requeues_failed_claim(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("x=1\n", encoding="utf-8")
    queue = DirtyQueue(str(tmp_path))
    queue.record("a.py")
    claim = queue.claim()
    queue.finish(claim, retry_all=True)
    assert queue.peek() == {"a.py"}


def test_dirty_queue_recovers_processing_and_pending(tmp_path):
    source_a = tmp_path / "a.py"
    source_b = tmp_path / "b.py"
    source_a.write_text("x=1\n", encoding="utf-8")
    source_b.write_text("x=2\n", encoding="utf-8")
    queue = DirtyQueue(str(tmp_path))
    Path(queue.processing_path).parent.mkdir(parents=True, exist_ok=True)
    Path(queue.processing_path).write_text("a.py\n", encoding="utf-8")
    Path(queue.pending_prefix + "123").write_text("b.py\n", encoding="utf-8")
    assert queue.recover() == {"a.py", "b.py"}
    assert not Path(queue.processing_path).exists()
    assert not Path(queue.pending_prefix + "123").exists()


def test_dirty_queue_recovery_is_idempotent(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("x=1\n", encoding="utf-8")
    queue = DirtyQueue(str(tmp_path))
    Path(queue.processing_path).parent.mkdir(parents=True, exist_ok=True)
    Path(queue.processing_path).write_text("a.py\n", encoding="utf-8")
    first = queue.recover()
    second = queue.recover()
    assert first == second == {"a.py"}


def test_file_lock_blocks_and_releases_after_process_exit(tmp_path):
    lock_path = str(tmp_path / "lock")
    ready = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_lock, args=(lock_path, ready))
    process.start()
    try:
        assert ready.wait(5)
        with pytest.raises(LockTimeoutError):
            InterProcessFileLock(lock_path, 0).acquire()
    finally:
        process.terminate()
        process.join(5)
    with InterProcessFileLock(lock_path, 1):
        assert True


def test_tracker_fallback_pending_on_queue_lock(tmp_path, monkeypatch):
    source = tmp_path / "a.py"
    source.write_text("x=1\n", encoding="utf-8")
    queue = DirtyQueue(str(tmp_path))
    monkeypatch.setenv("REMY_INDEX_QUEUE_LOCK_TIMEOUT", "0")
    with InterProcessFileLock(queue.lock_path, 1):
        assert queue.record("a.py") == "a.py"
    pending = list(Path(tmp_path / ".claude").glob("logic_index_dirty.pending.*"))
    assert len(pending) == 1
    assert pending[0].read_text(encoding="utf-8") == "a.py\n"
