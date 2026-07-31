"""Tests for force-recompute counter thresholds and zeroing on propagation."""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "remy-index"))
from struct_scan import SCHEMA_SQL, VERSION


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "logic_index.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO meta (key, value) VALUES ('version', ?)", (VERSION,))
    conn.execute(
        "INSERT INTO node_change_counters (node_kind, node_ref, child_change_count, leaf_descendant_count) "
        "VALUES ('cluster', 'c1', 0, 0)"
    )
    conn.commit()
    yield conn
    conn.close()


def _bump_counter(db, kind, ref, child_delta=1, leaf_delta=0):
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT OR IGNORE INTO node_change_counters (node_kind, node_ref) VALUES (?, ?)",
        (kind, ref),
    )
    db.execute(
        "UPDATE node_change_counters SET "
        "child_change_count = child_change_count + ?, "
        "leaf_descendant_count = leaf_descendant_count + ? "
        "WHERE node_kind = ? AND node_ref = ?",
        (child_delta, leaf_delta, kind, ref),
    )
    db.commit()


def _read_counter(db, kind, ref):
    row = db.execute(
        "SELECT child_change_count, leaf_descendant_count, last_force_recompute_at "
        "FROM node_change_counters WHERE node_kind = ? AND node_ref = ?",
        (kind, ref),
    ).fetchone()
    return row


def _force_recompute(db, kind, ref):
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "UPDATE node_change_counters SET "
        "child_change_count = 0, leaf_descendant_count = 0, "
        "last_force_recompute_at = ? WHERE node_kind = ? AND node_ref = ?",
        (datetime.now().isoformat(timespec="seconds"), kind, ref),
    )
    db.commit()


def _should_force(db, kind, ref, threshold_primary, threshold_backup, interval_days):
    row = _read_counter(db, kind, ref)
    if row is None:
        return False
    child, leaf, last_force = row
    if threshold_primary > 0 and child >= threshold_primary:
        return True
    if threshold_backup >= 0 and leaf >= threshold_backup:
        return True
    if last_force:
        elapsed = datetime.now() - datetime.fromisoformat(last_force)
        if elapsed >= timedelta(days=interval_days):
            return True
    return False


class TestCounterIncrement:
    def test_initial_zero(self, db):
        child, leaf, last_force = _read_counter(db, "cluster", "c1")
        assert child == 0
        assert leaf == 0
        assert last_force is None

    def test_increment_child(self, db):
        for _ in range(5):
            _bump_counter(db, "cluster", "c1", child_delta=1)
        child, _, _ = _read_counter(db, "cluster", "c1")
        assert child == 5

    def test_increment_both(self, db):
        _bump_counter(db, "cluster", "c1", child_delta=2, leaf_delta=10)
        child, leaf, _ = _read_counter(db, "cluster", "c1")
        assert child == 2
        assert leaf == 10

    def test_concurrent_increments_serial(self, db):
        for _ in range(100):
            _bump_counter(db, "cluster", "c1", child_delta=1)
        child, _, _ = _read_counter(db, "cluster", "c1")
        assert child == 100


class TestForceTrigger:
    def test_primary_threshold_triggers(self, db):
        for _ in range(3):
            _bump_counter(db, "cluster", "c1", child_delta=1)
        assert _should_force(db, "cluster", "c1", threshold_primary=3, threshold_backup=-1, interval_days=30)

    def test_below_threshold_no_trigger(self, db):
        for _ in range(2):
            _bump_counter(db, "cluster", "c1", child_delta=1)
        assert not _should_force(db, "cluster", "c1", threshold_primary=3, threshold_backup=-1, interval_days=30)

    def test_backup_disabled_with_minus_one(self, db):
        _bump_counter(db, "cluster", "c1", child_delta=0, leaf_delta=1000)
        assert not _should_force(db, "cluster", "c1", threshold_primary=10000, threshold_backup=-1, interval_days=30)

    def test_backup_triggers_when_enabled(self, db):
        _bump_counter(db, "cluster", "c1", child_delta=0, leaf_delta=5)
        assert _should_force(db, "cluster", "c1", threshold_primary=100, threshold_backup=5, interval_days=30)


class TestZeroingOnPropagation:
    def test_force_recompute_clears_counters(self, db):
        for _ in range(5):
            _bump_counter(db, "cluster", "c1", child_delta=1, leaf_delta=2)
        _force_recompute(db, "cluster", "c1")
        child, leaf, last_force = _read_counter(db, "cluster", "c1")
        assert child == 0
        assert leaf == 0
        assert last_force is not None

    def test_post_recompute_below_threshold(self, db):
        for _ in range(3):
            _bump_counter(db, "cluster", "c1", child_delta=1)
        assert _should_force(db, "cluster", "c1", threshold_primary=3, threshold_backup=-1, interval_days=30)
        _force_recompute(db, "cluster", "c1")
        assert not _should_force(db, "cluster", "c1", threshold_primary=3, threshold_backup=-1, interval_days=30)
