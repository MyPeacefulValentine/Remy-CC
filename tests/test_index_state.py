"""Tests for index run results and the project scan lock."""

import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "remy-index"))
from index_state import (
    SCAN_LOCK_FILE,
    InterProcessFileLock,
    LockTimeoutError,
    RunStatus,
    ScanResult,
    StageError,
    normalize_source_path,
)

_TARGET_DIR = Path(__file__).resolve().parent.parent / "remy-daemon" / "target"


def _daemon_binary():
    name = "remy-daemon.exe" if sys.platform == "win32" else "remy-daemon"
    candidates = [_TARGET_DIR / profile / name for profile in ("release", "debug")]
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


BINARY = _daemon_binary()


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


@pytest.mark.skipif(BINARY is None, reason="remy-daemon binary not built")
class TestScanLockInterop:
    """Python msvcrt/flock byte lock vs Rust std File::try_lock on the same
    `.claude/logic_index_scan.lock` must exclude each other both ways."""

    @staticmethod
    def _write_corpus(root):
        (root / "a.c").write_text("int a(void) { return 1; }\n", encoding="utf-8")

    def test_python_holder_blocks_rust_scanner(self, tmp_path):
        self._write_corpus(tmp_path)
        lock_path = tmp_path / SCAN_LOCK_FILE
        with InterProcessFileLock(str(lock_path), 1):
            completed = subprocess.run(
                [
                    str(BINARY), "scan",
                    "--root", str(tmp_path),
                    "--db", str(tmp_path / "out.db"),
                    "--result-json",
                    "--lock-timeout", "0",
                ],
                capture_output=True,
                text=True,
            )
        assert completed.returncode == 1, completed.stderr
        report = json.loads(completed.stdout.strip().splitlines()[-1])
        assert report["outcome"] == "failed"
        assert "scan lock" in report["errors"][0]["message"].lower()

    def test_rust_holder_blocks_python(self, tmp_path):
        self._write_corpus(tmp_path)
        lock_path = tmp_path / SCAN_LOCK_FILE
        env = dict(os.environ, REMY_SCAN_LOCK_HOLD_MS="5000")
        process = subprocess.Popen(
            [
                str(BINARY), "scan",
                "--root", str(tmp_path),
                "--db", str(tmp_path / "out.db"),
                "--result-json",
                "--progress-json",
            ],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            assert process.stdout is not None
            first = json.loads(process.stdout.readline())
            assert first == {
                "type": "progress",
                "stage": "lock_acquired",
                "elapsed_ms": first["elapsed_ms"],
            }
            with pytest.raises(LockTimeoutError):
                InterProcessFileLock(str(lock_path), 0).acquire()
        finally:
            process.kill()
            process.wait(10)
        with InterProcessFileLock(str(lock_path), 5):
            pass
