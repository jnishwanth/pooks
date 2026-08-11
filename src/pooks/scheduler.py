"""The long-running daemon.

Two cadences, deliberately different in cost:

  poll   every 5 minutes, a conditional GET that transfers no body on 304
  sweep  hourly, the only place sold-out detection is valid, and the safety net
         if Last-Modified ever stops advancing

Processing only follows a poll or sweep that actually found something, so a
quiet shop costs one HTTP request every five minutes and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from pooks.config import Config, load_config
from pooks.db.store import Store, connect
from pooks.ingest.pipeline import build_client, run_poll, run_sweep
from pooks.notify.telegram import TelegramNotifier
from pooks.run import process_pending, refresh_improvable

log = logging.getLogger(__name__)


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.store = Store(connect(config.db_path))
        self.notifier = TelegramNotifier(
            config.secrets.telegram_bot_token,
            config.secrets.telegram_chat_id,
            config.notify.get("max_books_per_message", 10),
        )
        # Serialises poll and sweep: they mutate the same rows, and a sweep
        # overlapping a poll could classify a half-applied state.
        self._lock = asyncio.Lock()

    async def poll_tick(self) -> None:
        async with self._lock:
            async with build_client(self.config) as client:
                outcome = await run_poll(self.store, client)
            await self._process_if_work(outcome.had_changes)

    async def sweep_tick(self) -> None:
        async with self._lock:
            async with build_client(self.config) as client:
                outcome = await run_sweep(self.store, client)
            await self._process_if_work(outcome.had_changes)

    async def _process_if_work(self, had_changes: bool) -> None:
        """Process when something changed *or* when a backlog is waiting.

        The backlog half matters: a cold-start sweep queues one event per
        in-stock book (~630), and only a fraction fits in one batch. Keying
        this on `had_changes` alone meant every later poll returned 304, found
        no change, and never revisited the queue — the backfill stalled with
        hundreds of books permanently unenriched.
        """
        pending = self.store.pending_event_count()
        if not had_changes and not pending:
            # Idle: spend the tick repairing records that fell back to a worse
            # source, or whose lookup was blocked. Lowest priority by design —
            # real arrivals always come first.
            await self._refresh()
            return
        if pending:
            log.info("%d event(s) pending", pending)
        await self._process()

    async def _refresh(self) -> None:
        limit = self.config.schedule.get("refresh_per_tick", 3)
        if limit <= 0:
            return
        result = await refresh_improvable(self.store, self.config, limit=limit)
        if result.attempted:
            log.info(
                "refresh: %d attempted, %d improved, %d unchanged",
                result.attempted,
                result.improved,
                result.unchanged,
            )

    async def _process(self) -> None:
        # Bounded per tick. Enrichment is paced at ~90s per book by the slowest
        # source, so an unbounded drain would occupy the scheduler for hours and
        # starve the poll it shares a lock with.
        batch = self.config.schedule.get("process_batch_size", 25)
        result = await process_pending(self.store, self.config, limit=batch)
        if result.to_notify:
            sent = await self.notifier.send(self.store, result.to_notify)
            log.info("pushed %d book(s) to telegram", sent)


async def run_forever(config: Config | None = None) -> None:
    config = config or load_config()
    daemon = Daemon(config)
    scheduler = AsyncIOScheduler()

    poll_interval = config.schedule.get("poll_interval_s", 300)
    sweep_interval = config.schedule.get("full_sweep_interval_s", 3600)

    scheduler.add_job(
        daemon.poll_tick, "interval", seconds=poll_interval, id="poll", max_instances=1
    )
    scheduler.add_job(
        daemon.sweep_tick, "interval", seconds=sweep_interval, id="sweep", max_instances=1
    )
    scheduler.start()

    log.info(
        "pooks daemon started: poll every %ds, sweep every %ds. "
        "Telegram %s, LLM provider %s.",
        poll_interval,
        sweep_interval,
        "configured" if daemon.notifier.configured else "NOT configured",
        config.llm.get("provider"),
    )

    # Do one sweep immediately so a restart does not wait an hour to reconcile.
    await daemon.sweep_tick()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - not on POSIX
            pass

    await stop.wait()
    scheduler.shutdown(wait=False)
    log.info("pooks daemon stopped")
