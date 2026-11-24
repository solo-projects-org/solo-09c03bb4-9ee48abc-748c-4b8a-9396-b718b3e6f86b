"""Concurrency tests for serialized take-down transactions.

These call the transaction layer directly from real threads. The key
invariant: two batches that are each valid alone but jointly invalid must
never both commit, because coverage is re-validated inside the
``BEGIN IMMEDIATE`` write transaction after the lock is acquired.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from app import database

from conftest import _img


def _seed(catalog_paths, images):
    _, write = catalog_paths
    write(images)
    return database.initialize_database(force=True)


def test_parallel_batches_serialize_without_lost_updates(catalog_paths):
    # One fine sheet under six duplicate coarser sheets: every coarser sheet
    # is removable as long as the fine one and the others survive.
    images = [_img("FINE", [0, 0, 100, 100], 0.25)]
    images += [_img(f"S{i}", [0, 0, 100, 100], 0.5) for i in range(6)]
    _seed(catalog_paths, images)

    errors: list[Exception] = []

    def worker(sheet: str) -> None:
        try:
            database.retry_on_locked(database.withdraw_batch, [sheet])
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"S{i}",)) for i in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    active = [r.sheet_no for r in database.list_images(active_only=True)]
    assert active == ["FINE"]
    committed = [b for b in database.list_batches(100) if b["status"] == "committed"]
    assert len(committed) == 6


def test_jointly_invalid_batches_exactly_one_commits(catalog_paths):
    # X and Y are duplicate sheets with no other imagery. Either batch alone
    # is valid (the other survives and covers it); both together are invalid.
    for round_no in range(5):
        _seed(
            catalog_paths,
            [
                _img("X", [0, 0, 100, 100], 0.5),
                _img("Y", [0, 0, 100, 100], 0.5),
            ],
        )
        barrier = threading.Barrier(2)
        outcomes: dict[str, str] = {}

        def worker(sheet: str) -> None:
            barrier.wait()
            try:
                database.withdraw_batch([sheet])
                outcomes[sheet] = "committed"
            except database.WithdrawalValidationError:
                outcomes[sheet] = "rejected"

        t1 = threading.Thread(target=worker, args=("X",))
        t2 = threading.Thread(target=worker, args=("Y",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert sorted(outcomes.values()) == ["committed", "rejected"], (
            f"round {round_no}: both batches slipped through: {outcomes}"
        )
        active = [r.sheet_no for r in database.list_images(active_only=True)]
        assert len(active) == 1
        rejected = next(
            b for b in database.list_batches(10) if b["status"] == "rejected"
        )
        assert rejected["reason"] == "coverage gap after withdrawal"
        assert rejected["uncovered_rect"] is not None


def test_same_sheet_withdrawn_concurrently_only_first_commits(catalog_paths):
    _seed(
        catalog_paths,
        [
            _img("X", [0, 0, 100, 100], 0.5),
            _img("Y", [0, 0, 100, 100], 0.5),
        ],
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            database.withdraw_batch(["X"])
            with lock:
                outcomes.append("committed")
        except database.WithdrawalValidationError as exc:
            with lock:
                outcomes.append(f"rejected:{exc.reason}")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("committed") == 1
    assert any(o.startswith("rejected:") for o in outcomes)


def test_retry_helper_succeeds_after_transient_lock(monkeypatch):
    # Keep the test fast: neutralize the exponential backoff.
    monkeypatch.setattr(database.time, "sleep", lambda _seconds: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert database.retry_on_locked(flaky, attempts=5) == "ok"
    assert calls["n"] == 3


def test_retry_helper_gives_up_after_exhausting_attempts(monkeypatch):
    monkeypatch.setattr(database.time, "sleep", lambda _seconds: None)

    def always_locked():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        database.retry_on_locked(always_locked, attempts=3)
