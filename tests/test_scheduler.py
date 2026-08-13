"""Daemon tick priority.

Real arrivals must always beat repair work, and a backlog must always be
drained — the latter because keying processing on `had_changes` alone once left
a cold-start queue of ~630 events stalled the moment the next poll returned 304.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from pooks import scheduler as scheduler_module
from pooks.config import load_config
from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
from pooks.ingest.pipeline import IngestOutcome
from pooks.scheduler import Daemon


@pytest.fixture
def daemon(store: Store, monkeypatch) -> Daemon:
    monkeypatch.setattr(Daemon, "__init__", lambda self, config: None)
    d = Daemon(load_config())
    d.config = load_config()
    d.store = store
    d.calls = []
    d.date_clients = []
    d._lock = asyncio.Lock()
    return d


def _record(daemon: Daemon, monkeypatch) -> None:
    async def process(self=daemon):
        daemon.calls.append("process")

    async def refresh(self=daemon):
        daemon.calls.append("refresh")

    async def dates(client=None, self=daemon):
        daemon.calls.append("dates")
        daemon.date_clients.append(client)

    monkeypatch.setattr(daemon, "_process", process)
    monkeypatch.setattr(daemon, "_refresh", refresh)
    monkeypatch.setattr(daemon, "_backfill_dates", dates)


async def test_backlog_is_processed_even_without_changes(daemon, store, products, monkeypatch):
    _record(daemon, monkeypatch)
    apply(products, classify(products, store, full_sweep=True), store)

    await daemon._process_if_work(had_changes=False)

    assert daemon.calls == ["process"], "a pending queue must be drained"


async def test_an_idle_tick_repairs_but_does_not_ask_for_dates(daemon, monkeypatch):
    """The five-minute poll must not carry the date backfill.

    The backfill never converges — wp/v2 answers for published posts only, and
    a delisted book is kept forever — so ids it cannot resolve are re-asked
    every time it runs. On the poll that is a request every five minutes, for
    ever, against a source this client is deliberately polite to.
    """
    _record(daemon, monkeypatch)

    await daemon._process_if_work(had_changes=False)

    assert daemon.calls == ["refresh"]


async def test_the_sweep_backfills_dates_on_the_client_it_already_has(daemon, monkeypatch):
    """The hourly reconciliation pass is where dates are repaired instead, and
    it lends the backfill its own client rather than opening a second one."""
    _record(daemon, monkeypatch)
    sweep_clients = []
    opened = []

    @asynccontextmanager
    async def build_client(config):
        client = object()
        opened.append(client)
        yield client

    async def run_sweep(store, client):
        sweep_clients.append(client)
        return IngestOutcome()

    monkeypatch.setattr(scheduler_module, "build_client", build_client)
    monkeypatch.setattr(scheduler_module, "run_sweep", run_sweep)

    await daemon.sweep_tick()

    assert daemon.calls == ["dates", "refresh"]
    assert len(opened) == 1
    assert daemon.date_clients == sweep_clients


async def test_a_failed_date_fetch_does_not_take_the_sweep_down(daemon, store, products):
    """Dates are not load-bearing. wp/v2 can fail in ways `fetch_dates` does
    not anticipate, and that must cost nothing more than a log line — not the
    sold-out detection and event processing the sweep exists for.
    """

    class _Exploding:
        async def fetch_dates(self, product_ids):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    apply(products, classify(products, store, full_sweep=True), store)

    await daemon._backfill_dates(_Exploding())

    assert all(row["date_created"] is None for row in store.in_stock_products())


async def test_new_arrivals_beat_repair_work(daemon, store, products, monkeypatch):
    """Repair is the lowest priority: it must never displace a real arrival."""
    _record(daemon, monkeypatch)
    apply(products, classify(products, store, full_sweep=True), store)

    await daemon._process_if_work(had_changes=True)

    assert "refresh" not in daemon.calls
    assert "dates" not in daemon.calls
    assert daemon.calls == ["process"]


async def test_an_idle_tick_repairs_before_it_writes_prose(daemon, monkeypatch):
    """Data quality before blurbs, and both behind real arrivals.

    A blurb written from a thin record has to be regenerated once the record
    improves, and the only way to regenerate is to bump `prompt_version` —
    which discards every cached role for every book.
    """
    _record(daemon, monkeypatch)

    async def blurbs(self=daemon):
        daemon.calls.append("blurbs")

    monkeypatch.setattr(daemon, "_blurbs", blurbs)

    await daemon._process_if_work(had_changes=False)

    assert daemon.calls == ["refresh", "blurbs"]


async def test_blurbs_are_skipped_when_the_budget_is_zero(daemon, store, monkeypatch):
    """`blurbs_per_tick = 0` must cost nothing at all — not even the scan, and
    not the LLM client construction that would log a credential complaint on
    every tick of an install that deliberately has no key."""
    daemon.config = replace(
        daemon.config, schedule={**daemon.config.schedule, "blurbs_per_tick": 0}
    )

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("nothing should be selected when the budget is zero")

    monkeypatch.setattr("pooks.run.blurb_candidates", explode)

    await daemon._blurbs()
