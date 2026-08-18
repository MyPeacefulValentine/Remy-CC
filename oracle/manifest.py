"""Oracle manifest: machine-readable identity of a frozen Python oracle.

Records everything that determines the scanner's fact output: source
revision, interpreter and grammar versions, parser registry identity
(language ids, extensions, cache contract versions), schema version,
configuration snapshot, fixture content hashes, and the registered
known-gap entries. The comparator refuses to compare databases whose
manifests carry different environment identities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import classification

MANIFEST_SCHEMA_VERSION = 2

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INDEX_DIR = _REPO_ROOT / "skills" / "remy-index"
_REMY_SRC = _REPO_ROOT / "remy-src"
FIXTURES_ROOT = Path(__file__).resolve().parent / "fixtures"

GRAMMAR_DISTRIBUTIONS = (
    "tree-sitter",
    "tree-sitter-c",
    "tree-sitter-cpp",
    "tree-sitter-typescript",
    "tree-sitter-rust",
)

CONFIG_SNAPSHOT_KEYS = (
    "REMY_LOGIC_INDEX_FILTER_SMALL",
    "REMY_SCAN_COMMIT_BATCH_SIZE",
    "REMY_CLUSTER_DENSITY_THRESHOLD",
    "REMY_CLUSTER_MAX_SIZE",
    "REMY_CLUSTER_ENTRY_COUNT",
    "REMY_SYNTH_INTERFACE_FANOUT_CAP",
    "REMY_SYNTH_EVENT_FANOUT_CAP",
)

# Divergences from the desired semantics that are frozen into this oracle,
# classified per R3 dev-plan §7.3.
KNOWN_GAPS = (
    {
        "id": "python-docstring-in-hash",
        "classification": "category-1-frozen-compat",
        "description": (
            "PythonParser.symbol_hash_input strips only '#' comments, so "
            "docstrings participate in the symbol hash and docstring-only "
            "edits invalidate symbol summaries. Rust must replicate this "
            "behavior until a cross-language ruling changes it."
        ),
        "fix_window": None,
        "fix_procedure": None,
    },
)

# Identity anchors gating cross-database comparison: the fields that define
# the fact-semantics contract shared by every scanner implementation.
# Producer-private details (interpreter, package versions) are recorded in
# the manifest but do not gate comparison — schema v2 moved python_version
# and packages out of this set so a Rust scanner's output can be compared
# against the frozen Python oracle.
_ENVIRONMENT_IDENTITY_FIELDS = (
    "registry",
    "schema_version",
    "classification_version",
    "comparator_version",
    "fixtures",
)

_REQUIRED_FIELDS_V1 = (
    "manifest_schema_version",
    "generated_at",
    "commit",
    "python_version",
    "platform",
    "packages",
    "registry",
    "schema_version",
    "classification_version",
    "comparator_version",
    "config_snapshot",
    "fixtures",
    "known_gaps",
)

_REQUIRED_FIELDS_V2 = (
    "manifest_schema_version",
    "generated_at",
    "commit",
    "producer",
    "platform",
    "registry",
    "schema_version",
    "classification_version",
    "comparator_version",
    "config_snapshot",
    "fixtures",
    "known_gaps",
)


def _ensure_import_paths() -> None:
    for directory in (_INDEX_DIR, _REMY_SRC):
        value = str(directory)
        if value not in sys.path:
            sys.path.insert(0, value)


def _git_commit(repo_root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _package_versions() -> dict[str, Optional[str]]:
    from importlib import metadata

    versions: dict[str, Optional[str]] = {}
    for distribution in GRAMMAR_DISTRIBUTIONS:
        try:
            versions[distribution] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            versions[distribution] = None
    return versions


def _registry_identity() -> list[dict]:
    _ensure_import_paths()
    from parsers import build_default_registry

    return [
        {
            "language_id": parser.language_id,
            "extensions": sorted(parser.get_extensions()),
            "cache_contract_version": getattr(parser, "CACHE_CONTRACT_VERSION", None),
        }
        for parser in sorted(
            build_default_registry().all(), key=lambda parser: parser.language_id
        )
    ]


def _config_snapshot(repo_root: Path) -> dict[str, str]:
    _ensure_import_paths()
    import remy_config

    config = remy_config.load_config(repo_root, strict=False)
    return {key: str(config.get(key)) for key in CONFIG_SNAPSHOT_KEYS}


def fixture_hashes(fixtures_root: Path = FIXTURES_ROOT) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(fixtures_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(fixtures_root).as_posix()
        if any(part.startswith(".") for part in Path(relative).parts):
            continue
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def generate(
    repo_root: Path = _REPO_ROOT, fixtures_root: Path = FIXTURES_ROOT
) -> dict:
    _ensure_import_paths()
    import schema as index_schema

    from . import comparator

    packages = _package_versions()
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit": _git_commit(repo_root),
        "producer": {
            "implementation": "python-oracle",
            "backend_versions": packages,
        },
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "packages": packages,
        "registry": _registry_identity(),
        "schema_version": index_schema.VERSION,
        "classification_version": classification.CLASSIFICATION_VERSION,
        "comparator_version": comparator.COMPARATOR_VERSION,
        "config_snapshot": _config_snapshot(repo_root),
        "fixtures": fixture_hashes(fixtures_root),
        "known_gaps": [dict(gap) for gap in KNOWN_GAPS],
    }


def environment_identity(manifest: dict) -> dict:
    return {field: manifest.get(field) for field in _ENVIRONMENT_IDENTITY_FIELDS}


def upgrade(manifest: dict) -> dict:
    """Return the in-memory v2 form of a manifest loaded from disk.

    v1 manifests predate the producer field and were only ever written by
    the Python oracle, so the upgrade is unambiguous. The on-disk file is
    left untouched.
    """
    if manifest["manifest_schema_version"] == MANIFEST_SCHEMA_VERSION:
        return manifest
    upgraded = dict(manifest)
    upgraded["manifest_schema_version"] = MANIFEST_SCHEMA_VERSION
    upgraded["producer"] = {
        "implementation": "python-oracle",
        "backend_versions": dict(manifest["packages"]),
    }
    return upgraded


def validate(manifest: dict) -> dict:
    version = manifest.get("manifest_schema_version")
    if version == 1:
        required = _REQUIRED_FIELDS_V1
    elif version == 2:
        required = _REQUIRED_FIELDS_V2
    else:
        raise ValueError(f"unsupported oracle manifest schema version: {version}")
    missing = [field for field in required if field not in manifest]
    if missing:
        raise ValueError(f"oracle manifest lacks required fields: {missing}")
    return manifest


def write(manifest: dict, path: Path) -> None:
    validate(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load(path: Path) -> dict:
    return upgrade(validate(json.loads(Path(path).read_text(encoding="utf-8"))))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)
    write(generate(args.repo_root), args.output)
    print(f"ORACLE_MANIFEST written={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
