"""Daemon tick priority.

Real arrivals must always beat repair work, and a backlog must always be
drained — the latter because keying processing on `had_changes` alone once left
a cold-start queue of ~630 events stalled the moment the next poll returned 304.
"""

from __future__ import annotations

import pytest

from pooks.config import load_config
from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
from pooks.scheduler import Daemon


@pytest.fixture
def daemon(store: Store, monkeypatch) -> Daemon:
    monkeypatch.setattr(Daemon, "__init__", lambda self, config: None)
    d = Daemon(load_config())
    d.config = load_config()
    d.store = store
    d.calls = []
    return d


def _record(daemon: Daemon, monkeypatch) -> None:
    async def process(self=daemon):
        daemon.calls.append("process")

    async def refresh(self=daemon):
        daemon.calls.append("refresh")

    async def dates(self=daemon):
        daemon.calls.append("dates")

    monkeypatch.setattr(daemon, "_process", process)
    monkeypatch.setattr(daemon, "_refresh", refresh)
    monkeypatch.setattr(daemon, "_backfill_dates", dates)


async def test_backlog_is_processed_even_without_changes(daemon, store, products, monkeypatch):
    _record(daemon, monkeypatch)
    apply(products, classify(products, store, full_sweep=True), store)

    await daemon._process_if_work(had_changes=False)

    assert daemon.calls == ["process"], "a pending queue must be drained"


async def test_idle_tick_backfills_dates_then_refreshes(daemon, monkeypatch):
    """Dates before repair, and both only when there is nothing else to do.

    `date_created` was reachable only from `pooks sweep --with-dates`, so a
    daemon-run install never filled it — 0 of 634 rows on the live database.
    It goes first because it is a couple of requests against the shop's own API
    and stops costing anything once filled, where a refresh spends third-party
    budget on every tick forever.
    """
    _record(daemon, monkeypatch)

    await daemon._process_if_work(had_changes=False)

    assert daemon.calls == ["dates", "refresh"]


async def test_new_arrivals_beat_repair_work(daemon, store, products, monkeypatch):
    """Repair is the lowest priority: it must never displace a real arrival."""
    _record(daemon, monkeypatch)
    apply(products, classify(products, store, full_sweep=True), store)

    await daemon._process_if_work(had_changes=True)

    assert "refresh" not in daemon.calls
    assert "dates" not in daemon.calls
    assert daemon.calls == ["process"]
