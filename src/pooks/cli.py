"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable

from pooks.config import Config, load_config
from pooks.db.store import Store, connect
from pooks.ingest.pipeline import backfill_dates, build_client, run_poll, run_sweep

log = logging.getLogger("pooks")

Handler = Callable[[argparse.Namespace], Awaitable[int]]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _open() -> tuple[Config, Store]:
    """The config and an open database, which almost every command wants."""
    config = load_config()
    return config, Store(connect(config.db_path))


async def cmd_poll(_: argparse.Namespace) -> int:
    config, store = _open()
    async with build_client(config) as client:
        outcome = await run_poll(store, client)

    if outcome.not_modified:
        print("304 Not Modified — nothing changed, no body transferred.")
    elif outcome.had_changes:
        print(f"changed ({', '.join(outcome.reasons or [])}): {outcome.diff.counts()}")
        print(
            f"  enrichment needed: {outcome.diff.enrichment_count}  "
            f"inference needed: {outcome.diff.inference_count}"
        )
    else:
        print("200 OK, but no changes detected.")
    return 0


async def cmd_sweep(args: argparse.Namespace) -> int:
    config, store = _open()
    async with build_client(config) as client:
        outcome = await run_sweep(store, client)
        if args.with_dates:
            filled = await backfill_dates(store, client)
            print(f"backfilled dates for {filled} products")

    print(f"swept {outcome.products_seen} in-stock products")
    if outcome.diff:
        print(f"  events: {outcome.diff.counts() or 'none'}")
        print(
            f"  enrichment needed: {outcome.diff.enrichment_count}  "
            f"inference needed: {outcome.diff.inference_count}"
        )
    return 0


async def cmd_enrich(args: argparse.Namespace) -> int:
    from pooks.enrich.http import PoliteClient
    from pooks.enrich.pipeline import Enricher
    from pooks.run import product_from_row

    config, store = _open()
    enricher = Enricher(config)

    rows = store.in_stock_products(args.limit, missing_enrichment=not args.force)

    if not rows:
        print("nothing to enrich — every in-stock book is already cached")
        return 0

    print(f"enriching {len(rows)} book(s)\n")
    cached = 0
    async with PoliteClient() as client:
        for row in rows:
            product = product_from_row(row)
            facts, from_cache = await enricher.enrich(
                client, product, store=store, force=args.force
            )
            cached += from_cache
            _print_facts(product, facts, from_cache)

    print(f"\ndone: {len(rows)} book(s), {cached} from cache")
    return 0


def _print_facts(product, facts, from_cache: bool) -> None:
    price = f"Rs {product.price_inr:.0f}" if product.price_inr else "?"
    print(f"  {product.name[:62]}")
    print(f"    {price:<10} isbn={facts.isbn or '-'}  {'(cached)' if from_cache else ''}")

    if facts.has_rating:
        print(
            f"    rating  {facts.rating} from {facts.ratings_count:,} "
            f"[{facts.rating_source}]"
        )
    else:
        skipped = facts.provenance.get("attempts", {})
        reasons = "; ".join(f"{k}: {v.get('result', v)}" for k, v in skipped.items())
        print(f"    rating  none — {reasons or 'no sources tried'}")

    india = facts.indian_price
    if india and india.has_price:
        shop = product.price_inr or 0
        gap = 100 * (1 - shop / india.price_inr)
        delta = f"{gap:.0f}% under" if gap >= 0 else f"{-gap:.0f}% over"
        print(f"    india   Rs {india.price_inr:.0f} via {india.source}  (shop is {delta})")
    elif india and india.unknown:
        print(f"    india   unknown — lookup blocked: {india.attempts}")
    elif india:
        print(f"    india   not sold in India  ({india.attempts})")

    if facts.scarcity and facts.scarcity.has_data:
        s = facts.scarcity
        print(
            f"    world   {s.listing_count} listings, new_offers={s.has_new_offers}  "
            f"in_print={facts.in_print}"
        )
    else:
        print(f"    world   no listings  in_print={facts.in_print}")
    print()


async def cmd_process(args: argparse.Namespace) -> int:
    from pooks.run import process_pending

    config, store = _open()
    result = await process_pending(
        store, config, limit=args.limit, dry_run=args.dry_run
    )

    print(
        f"events: {result.events_seen}  enriched: {result.enriched} "
        f"({result.cache_hits} cached)  inferred: {result.inferred}  "
        f"silent: {result.silent}"
    )
    if args.dry_run:
        print("(dry run — nothing was written)\n")

    for book in sorted(result.processed, key=lambda b: b.breakdown.score, reverse=True):
        _print_ranked(book)

    if result.to_notify:
        print(f"\n{len(result.to_notify)} book(s) would be pushed to Telegram")
    return 0


def _print_ranked(book) -> None:
    b = book.breakdown
    price = f"Rs {book.product.price_inr:.0f}" if book.product.price_inr else "?"

    def pct(value: float | None) -> str:
        return f"{value:.2f}" if value is not None else "  - "

    print(f"  [{b.score:.3f}] {book.product.name[:60]}")
    print(
        f"      {price:<9} quality={pct(b.quality)} renown={pct(b.renown)} "
        f"value={pct(b.value)} conf={b.confidence:.2f}"
    )
    if book.facts.has_rating:
        print(
            f"      {book.facts.rating} from {book.facts.ratings_count:,} "
            f"[{book.facts.rating_source}]"
        )
    if book.insights.blurb:
        print(f"      {book.insights.blurb}")
    elif book.insights.skipped_reason:
        print(f"      (no blurb: {book.insights.skipped_reason})")
    print()


async def cmd_backfill(args: argparse.Namespace) -> int:
    """Drain the whole event queue, in batches, with progress.

    Separate from `process` because the first run has a very different shape:
    ~630 queued books paced by the slowest enrichment source, which is hours of
    wall clock. Run it under tmux or systemd-run rather than an SSH session.
    Everything caches by ISBN, so interrupting and re-running only picks up
    what is still missing.
    """
    from pooks.run import process_pending

    config, store = _open()

    total = store.pending_event_count()
    if not total:
        print("nothing pending — the queue is empty")
        return 0

    print(f"{total} event(s) pending, {args.batch} per batch")
    if args.fast:
        print("fast profile: skipping Goodreads and Amazon; the daemon upgrades later")
    if args.max_events:
        print(f"stopping after {args.max_events} (--max-events)")
    print()

    done = enriched = cached = inferred = silent = 0
    started = time.monotonic()

    while True:
        remaining = store.pending_event_count()
        if not remaining:
            break
        if args.max_events and done >= args.max_events:
            print(f"\nreached --max-events ({args.max_events}); {remaining} still pending")
            break

        result = await process_pending(
            store, config, limit=args.batch, profile="fast" if args.fast else None
        )
        if not result.events_seen:
            break

        done += result.events_seen
        enriched += result.enriched
        cached += result.cache_hits
        inferred += result.inferred
        silent += result.silent

        elapsed = time.monotonic() - started
        rate = done / elapsed if elapsed else 0
        left = store.pending_event_count()
        eta = f"{left / rate / 60:.0f} min" if rate > 0 and left else "—"
        print(
            f"  {done}/{total} done · {left} left · "
            f"{enriched} enriched ({cached} cached) · {inferred} inferred · eta {eta}"
        )

    print(
        f"\nfinished: {done} event(s) · {enriched} enriched ({cached} from cache) · "
        f"{inferred} inferred · {silent} silent · "
        f"{(time.monotonic() - started) / 60:.1f} min"
    )
    print("run 'pooks calibrate' now that books are scored")
    return 0


async def cmd_blurbs(args: argparse.Namespace) -> int:
    """Generate blurb and renown for top-ranked books that lack them.

    Cold-start inference is suppressed by design, so after a backfill the
    catalogue is ranked but has no blurb text. Only the books near the top are
    worth the calls — you do not need a description for something you would
    never buy.
    """
    from pooks.llm.client import LLMClient
    from pooks.llm.pipeline import InsightGenerator
    from pooks.run import load_cached

    config, store = _open()
    client = LLMClient.from_config(config)
    if problem := client.credential_problem():
        print(f"NOT configured: {problem}")
        return 1

    version = config.llm.get("prompt_version", 1)
    generator = InsightGenerator(client, version)

    # "the top N books", not "N books that need one" — so a second run is a
    # no-op rather than quietly walking deeper into the ranking.
    todo = []
    skipped_ungrounded = 0
    for row in store.ranked_in_stock(limit=args.top):
        if row["score"] is None:
            continue
        cached = load_cached(store, config, row)
        if cached is None:
            continue
        product, facts, insights = cached
        if insights.blurb:
            continue
        if not (facts.synopsis or "").strip():
            skipped_ungrounded += 1
            continue
        todo.append((product, facts))

    if skipped_ungrounded:
        print(
            f"skipping {skipped_ungrounded} book(s) with no synopsis to ground a "
            "blurb — run 'pooks refresh' first to try for one"
        )
    if not todo:
        print(f"nothing to generate in the top {args.top}")
        return 0

    print(f"generating for {len(todo)} book(s)\n")
    made = 0
    for product, facts in todo:
        insights = await generator.generate(store, product, facts)
        if insights.blurb and not insights.skipped_reason:
            made += 1
            print(f"  {product.work_title[:52]}")
            print(f"    {insights.blurb[:150]}")
            if insights.spoiler_flagged:
                print("    (spoiler check flagged this; text suppressed)")
        else:
            print(f"  {product.work_title[:52]} — {insights.skipped_reason or 'failed'}")
        print()

    print(f"done: {made}/{len(todo)} generated")
    return 0


async def cmd_health(args: argparse.Namespace) -> int:
    from pooks.notify.telegram import TelegramNotifier
    from pooks.rank.health import collect, render

    config, store = _open()
    health = collect(store, config)
    text = render(health)
    print(text.replace("<b>", "").replace("</b>", ""))

    if args.push:
        notifier = TelegramNotifier(
            config.secrets.telegram_bot_token, config.secrets.telegram_chat_id
        )
        if await notifier.send_text(text):
            print("\npushed to telegram")
    return 0


async def cmd_refresh(args: argparse.Namespace) -> int:
    from pooks.run import refresh_improvable

    config, store = _open()
    result = await refresh_improvable(store, config, limit=args.limit)

    if not result.attempted:
        print("nothing improvable — every in-stock book is from primary sources")
        return 0
    print(
        f"attempted {result.attempted} · improved {result.improved} · "
        f"unchanged {result.unchanged}"
    )
    return 0


async def cmd_rescore(_: argparse.Namespace) -> int:
    from pooks.run import rescore_in_stock

    config, store = _open()
    count = await rescore_in_stock(store, config)
    print(f"rescored {count} in-stock books from cache (no API or LLM calls)")
    return 0


async def cmd_top(args: argparse.Namespace) -> int:
    _, store = _open()
    rows = store.ranked_in_stock(limit=args.limit)

    scored = [r for r in rows if r["score"] is not None]
    if not scored:
        print("nothing scored yet — run 'pooks process' first")
        return 0

    print(f"top {len(scored)} in-stock books\n")
    for index, row in enumerate(scored, start=1):
        price = f"Rs {row['price_paise'] / 100:.0f}" if row["price_paise"] else "?"
        rating = (
            f"{row['rating']} ({row['ratings_count']:,})" if row["rating"] else "no rating"
        )
        print(f"{index:>3}. [{row['score']:.3f}] {row['name'][:58]}")
        print(f"      {price:<9} {rating:<22} conf={row['confidence'] or 0:.2f}")
        if row["tags_json"]:
            tags = json.loads(row["tags_json"])
            flat = [t for facet in ("genre", "mood") for t in tags.get(facet, [])][:6]
            if flat:
                print(f"      {' · '.join(flat)}")
    return 0


async def cmd_serve(_: argparse.Namespace) -> int:
    import os

    import uvicorn

    serve = load_config().serve
    # Environment wins over config.toml so a packaged install can set the bind
    # address without rewriting a config file the operator may have hand-written.
    host = os.environ.get("POOKS_SERVE_HOST") or serve["host"]
    port = int(os.environ.get("POOKS_SERVE_PORT") or serve["port"])

    print(f"dashboard on http://{host}:{port}")
    server = uvicorn.Server(
        uvicorn.Config("pooks.serve.app:app", host=host, port=port, log_level="warning")
    )
    await server.serve()
    return 0


async def cmd_daemon(_: argparse.Namespace) -> int:
    from pooks.scheduler import run_forever

    await run_forever(load_config())
    return 0


async def cmd_notify(args: argparse.Namespace) -> int:
    """Re-render the digest for the current top books without re-processing."""
    from pooks.notify.telegram import TelegramNotifier, render_digest
    from pooks.rank.score import ScoreBreakdown
    from pooks.run import ProcessedBook, load_cached

    config, store = _open()

    books = []
    for row in store.ranked_in_stock(limit=args.limit):
        if row["score"] is None:
            continue
        cached = load_cached(store, config, row)
        if cached is None:
            continue
        product, facts, insights = cached

        books.append(
            ProcessedBook(
                product=product,
                facts=facts,
                insights=insights,
                breakdown=ScoreBreakdown(
                    score=row["score"],
                    quality=row["quality"],
                    renown=row["renown"],
                    value=row["value"],
                    condition_factor=1.0,
                    confidence=row["confidence"] or 0.0,
                ),
                event_id=-1,
                event_type="MANUAL",
                notify=True,
            )
        )

    if not books:
        print("nothing scored yet — run 'pooks process' first")
        return 0

    if args.dry_run:
        print(render_digest(books[: args.limit]))
        return 0

    notifier = TelegramNotifier(
        config.secrets.telegram_bot_token,
        config.secrets.telegram_chat_id,
        config.notify.get("max_books_per_message", 10),
    )
    sent = await notifier.send(store, books)
    print(f"pushed {sent} book(s)")
    return 0


async def _check_model_available(*models: str | None) -> list[str]:
    """Warn about models OpenRouter no longer lists.

    Free models are withdrawn without notice — both of this project's original
    defaults (qwen-2.5-72b, llama-3.3-70b) had vanished by the time a key was
    configured, and the resulting error says nothing about the cause.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get("https://openrouter.ai/api/v1/models")
            response.raise_for_status()
            available = {m["id"] for m in response.json().get("data", [])}
    except (httpx.HTTPError, ValueError, KeyError):
        return ["  (could not verify model availability)"]

    notes = []
    for model in models:
        if not model:
            continue
        bare = model.removeprefix("openrouter/")
        mark = "ok" if bare in available else "NOT LISTED — it may have been withdrawn"
        notes.append(f"  {bare}: {mark}")
    return notes


async def cmd_probe_llm(_: argparse.Namespace) -> int:
    """Verify the configured provider actually works, end to end."""
    from pooks.llm.client import LLMClient, LLMUnavailableError
    from pooks.llm.roles import generate_blurb, judge_renown

    config = load_config()
    client = LLMClient.from_config(config)

    print(f"provider : {client.provider}\nmodel    : {client.model}")
    if problem := client.credential_problem():
        print(f"\nNOT configured: {problem}")
        return 1

    if client.provider == "openrouter":
        for note in await _check_model_available(client.model, client.fallback_model):
            print(note)

    sample = {
        "title": "Memoirs of a Dutiful Daughter",
        "author": "Simone de Beauvoir",
        "synopsis": (
            "The first volume of Simone de Beauvoir's autobiography, covering her "
            "childhood in a bourgeois Parisian family, her Catholic upbringing, her "
            "education, and her growing intellectual independence."
        ),
        "categories": ["Non Fiction", "Biography"],
        "rating": 4.13,
        "ratings_count": 19192,
    }

    try:
        blurb, verdict = await generate_blurb(client, **sample)
        print(f"\nblurb        : {blurb.blurb}")
        status = f"FLAGGED - {verdict.reason}" if verdict.has_spoilers else "clean"
        print(f"spoiler check: {status}")

        renown = await judge_renown(
            client,
            title=sample["title"],
            author=sample["author"],
            publisher="Penguin",
            year=1958,
            categories=sample["categories"],
            rating=sample["rating"],
            ratings_count=sample["ratings_count"],
        )
        print(f"renown       : tier={renown.tier} score={renown.score} "
              f"abstained={renown.abstained}")
        print(f"  evidence   : {renown.evidence}")
    except LLMUnavailableError as exc:
        print(f"\nfailed: {exc}")
        return 1

    print("\nLLM layer is working.")
    return 0


async def cmd_calibrate(args: argparse.Namespace) -> int:
    from pooks.rank.calibrate import calibrate, summarise, would_notify

    config, store = _open()
    min_confidence = args.min_confidence or config.notify.get("push_min_confidence", 0.5)
    threshold = args.threshold or config.notify.get("push_score_threshold", 0.62)

    result = calibrate(store, min_confidence)
    for line in summarise(result, threshold, min_confidence, store):
        print(line)

    if books := would_notify(store, threshold, min_confidence):
        print(f"\nwould push right now ({len(books)}):")
        for book in books[:10]:
            print(
                f"  [{book['score']:.3f}] conf {book['confidence']:.2f}  "
                f"Rs {book['price_inr']:.0f}  {book['name'][:52]}"
            )
    return 0


async def cmd_status(_: argparse.Namespace) -> int:
    _, store = _open()
    state = store.poll_state()

    counts = store.product_counts()

    print(f"products tracked : {counts['tracked']}  (in stock: {counts['in_stock']})")
    print(f"unprocessed events: {store.pending_event_count()}")
    print(f"last poll        : {state['last_poll_at'] or 'never'}")
    print(f"last sweep       : {state['last_sweep_at'] or 'never'}")
    print(f"last 304         : {state['last_304_at'] or 'never'}")
    print(f"last-modified    : {state['last_modified'] or 'unset'}")
    if by_type := store.event_counts_by_type():
        print("events by type:")
        for row in by_type:
            print(f"  {row['event_type']:<18} {row['n']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pooks", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    # `dest` is unread — it is what makes argparse say "argument command:
    # invalid choice" rather than repeating the whole list of choices.
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(name: str, handler: Handler, help: str) -> argparse.ArgumentParser:
        """Declare a subcommand and bind its handler in one place.

        The handler rides on the parser rather than being looked up in a
        name-keyed dispatch table, so a command cannot exist with nothing
        behind it.
        """
        sub = subparsers.add_parser(name, help=help)
        sub.set_defaults(handler=handler)
        return sub

    command("poll", cmd_poll, "cheap conditional-GET change check")

    sweep = command("sweep", cmd_sweep, "full in-stock sweep")
    sweep.add_argument("--with-dates", action="store_true", help="also backfill wp/v2 dates")

    enrich = command("enrich", cmd_enrich, "fetch ratings and price comps")
    enrich.add_argument("--limit", type=int, default=5)
    enrich.add_argument("--force", action="store_true", help="ignore the cache")

    process = command("process", cmd_process, "enrich, infer, and score pending events")
    process.add_argument("--limit", type=int, default=20)
    process.add_argument("--dry-run", action="store_true", help="compute but write nothing")

    backfill = command(
        "backfill", cmd_backfill,
        "drain the whole event queue in batches (hours on first run)",
    )
    backfill.add_argument("--batch", type=int, default=25)
    backfill.add_argument(
        "--fast", action="store_true",
        help="skip the slow-paced sources for a quick first pass",
    )
    backfill.add_argument(
        "--max-events", type=int, default=0, help="stop after this many (0 = all)"
    )

    blurbs = command(
        "blurbs", cmd_blurbs, "generate blurbs for top-ranked books that lack them"
    )
    blurbs.add_argument("--top", type=int, default=25)

    health = command("health", cmd_health, "pipeline health summary")
    health.add_argument("--push", action="store_true", help="also send it to Telegram")

    refresh = command(
        "refresh", cmd_refresh,
        "re-enrich books stuck on a fallback source or a blocked lookup",
    )
    refresh.add_argument("--limit", type=int, default=25)

    command("rescore", cmd_rescore, "recompute scores from cache after tuning weights")

    top = command("top", cmd_top, "show the ranked in-stock list")
    top.add_argument("--limit", type=int, default=25)

    command("serve", cmd_serve, "run the local dashboard")
    command("daemon", cmd_daemon, "run the scheduler (poll + sweep + notify)")
    command("probe-llm", cmd_probe_llm, "verify the configured LLM provider works")

    notify = command("notify", cmd_notify, "push the current top books")
    notify.add_argument("--limit", type=int, default=10)
    notify.add_argument("--dry-run", action="store_true", help="print instead of sending")

    calibrate = command(
        "calibrate", cmd_calibrate,
        "measure the score distribution and tune push thresholds",
    )
    calibrate.add_argument("--threshold", type=float, default=None)
    calibrate.add_argument("--min-confidence", type=float, default=None)

    command("status", cmd_status, "show pipeline state")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return asyncio.run(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
