"""Performance baseline tool for the frozen Python oracle.

Measures full-scan and incremental-scan wall time, child-process peak
working set (Windows), and database size against a disposable copy of a
sample repository. Scans run as child processes of struct_scan.py with
REMY_LOGIC_INDEX_DB_PATH pointing at a dedicated output database, so a
sample's own project database is never touched.

The sample tree is always copied into the working directory first; the
scanner writes a default .claude/logic_index_config into the copy (an
accepted, recorded side effect) and the incremental phase perturbs K
files of the copy.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

STRUCT_SCAN = Path(__file__).resolve().parents[1] / "skills" / "remy-index" / "struct_scan.py"

_COMMENT_PREFIX = {
    ".py": "# ",
}
_DEFAULT_COMMENT_PREFIX = "// "


class BenchError(RuntimeError):
    """Raised when a benchmark scan does not complete successfully."""


@dataclass(frozen=True)
class ScanSample:
    duration_seconds: float
    peak_commit_bytes: Optional[int]
    db_bytes: int


class _WindowsJob:
    """Tracks a child process tree via a Job Object so memory peaks cover
    descendants too (a venv python.exe is a redirector whose real
    interpreter runs as a grandchild). Reports PeakJobMemoryUsed, i.e. the
    job-wide commit-charge peak."""

    _EXTENDED_INFO_CLASS = 9

    def __init__(self) -> None:
        import ctypes
        import ctypes.wintypes as wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._handle = self._kernel32.CreateJobObjectW(None, None)

    def assign(self, process: subprocess.Popen) -> bool:
        process_handle = getattr(process, "_handle", None)
        if not self._handle or process_handle is None:
            return False
        ok = self._kernel32.AssignProcessToJobObject(
            self._wintypes.HANDLE(self._handle),
            self._wintypes.HANDLE(int(process_handle)),
        )
        return bool(ok)

    def peak_commit_bytes(self) -> Optional[int]:
        ctypes = self._ctypes
        wintypes = self._wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        if not self._handle:
            return None
        info = ExtendedLimits()
        ok = self._kernel32.QueryInformationJobObject(
            wintypes.HANDLE(self._handle),
            self._EXTENDED_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        return int(info.PeakJobMemoryUsed) if ok else None

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._wintypes.HANDLE(self._handle))
            self._handle = 0


def _run_scan(root: Path, db_path: Path, files: Optional[list[str]] = None) -> ScanSample:
    command = [sys.executable, str(STRUCT_SCAN), "--cwd", str(root)]
    if files:
        command += ["--files", *files]
    env = os.environ.copy()
    env["REMY_LOGIC_INDEX_DB_PATH"] = str(db_path)
    job = None
    if sys.platform == "win32":
        job = _WindowsJob()
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(root),
    )
    assigned = job.assign(process) if job is not None else False
    stdout, stderr = process.communicate()
    duration = time.perf_counter() - started
    peak = job.peak_commit_bytes() if job is not None and assigned else None
    if job is not None:
        job.close()
    if process.returncode != 0 or "STRUCT_SCAN_RESULT status=success" not in stdout:
        raise BenchError(
            f"scan failed (exit {process.returncode}): "
            f"stdout={stdout[-500:]!r} stderr={stderr[-500:]!r}"
        )
    return ScanSample(duration, peak, db_path.stat().st_size)


def _indexed_files(db_path: Path) -> list[str]:
    db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [row[0] for row in db.execute("SELECT path FROM files ORDER BY path")]
    finally:
        db.close()


def _perturb(root: Path, relative_paths: list[str]) -> None:
    for relative in relative_paths:
        target = root / relative
        prefix = _COMMENT_PREFIX.get(target.suffix, _DEFAULT_COMMENT_PREFIX)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{prefix}bench perturbation\n")


def measure_sample(
    source_root: Path,
    workdir: Path,
    *,
    reps: int = 5,
    k: int = 50,
    seed: int = 20260815,
) -> dict:
    source_root = Path(source_root).resolve()
    workdir = Path(workdir).resolve()
    full_samples: list[ScanSample] = []
    incremental_samples: list[ScanSample] = []
    file_count = 0
    for rep in range(reps):
        rep_dir = workdir / f"rep{rep}"
        project = rep_dir / "project"
        shutil.copytree(
            source_root, project, ignore=shutil.ignore_patterns(".git")
        )
        db_path = rep_dir / "logic_index.db"
        full_samples.append(_run_scan(project, db_path))
        indexed = _indexed_files(db_path)
        if not indexed:
            raise BenchError(f"no files indexed under {source_root}")
        file_count = len(indexed)
        chosen = random.Random(seed + rep).sample(indexed, min(k, len(indexed)))
        _perturb(project, chosen)
        incremental_samples.append(_run_scan(project, db_path, files=chosen))
    peaks = [
        sample.peak_commit_bytes
        for sample in full_samples
        if sample.peak_commit_bytes is not None
    ]
    return {
        "sample": source_root.name,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "reps": reps,
        "k": k,
        "seed": seed,
        "file_count": file_count,
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "machine": platform.machine(),
        "full_scan_seconds_median": statistics.median(
            sample.duration_seconds for sample in full_samples
        ),
        "incremental_seconds_median": statistics.median(
            sample.duration_seconds for sample in incremental_samples
        ),
        "full_scan_seconds": [sample.duration_seconds for sample in full_samples],
        "incremental_seconds": [
            sample.duration_seconds for sample in incremental_samples
        ],
        "peak_commit_bytes_median": statistics.median(peaks) if peaks else None,
        "db_bytes_median": statistics.median(sample.db_bytes for sample in full_samples),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--k", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260815)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    record = measure_sample(
        args.root, args.workdir, reps=args.reps, k=args.k, seed=args.seed
    )
    encoded = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
