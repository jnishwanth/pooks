"""A periodic summary of whether the pipeline is actually working.

Almost every failure mode here is silent. A source can stay blocked for a week,
a backlog can stop draining, the LLM key can lapse — and the only symptom is a
digest that quietly gets worse. Nothing raises an error, because degrading
gracefully is the whole design.

Everything below is derived from the database, so it costs no network calls and
cannot itself fail in the way it is meant to detect.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pooks.config import Config
from pooks.db.store import Store
from pooks.enrich.quality import MAX_REFRESH_ATTEMPTS


@dataclass
class Health:
    in_stock: int = 0
    enriched: int = 0
    with_rating: int = 0
    with_indian_price: int = 0
    price_unknown: int = 0
    fallback_rating: int = 0
    improvable: int = 0
    exhausted: int = 0
    pending_events: int = 0
    scored: int = 0
    with_blurb: int = 0
    notified_7d: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def rating_coverage(self) -> float:
        return self.with_rating / self.in_stock if self.in_stock else 0.0

    @property
    def price_coverage(self) -> float:
        return self.with_indian_price / self.in_stock if self.in_stock else 0.0


def collect(store: Store, config: Config) -> Health:
    primary = config.primary_rating_source
    version = config.prompt_version

    row = store.conn.execute(
        """
        SELECT
          COUNT(*) AS in_stock,
          SUM(e.book_key IS NOT NULL) AS enriched,
          SUM(e.rating IS NOT NULL) AS with_rating,
          SUM(e.in_price_paise IS NOT NULL) AS with_price,
          SUM(COALESCE(e.in_price_unknown, 0) = 1) AS price_unknown,
          SUM(e.rating_source IS NOT NULL AND e.rating_source != ?) AS fallback_rating,
          SUM(COALESCE(e.refresh_attempts, 0) >= ?) AS exhausted,
          SUM(s.score IS NOT NULL) AS scored
        FROM products p
        LEFT JOIN enrichment e ON e.book_key = p.book_key
        LEFT JOIN scores s ON s.product_id = p.product_id
        WHERE p.in_stock = 1
        """,
        (primary, MAX_REFRESH_ATTEMPTS),
    ).fetchone()

    health = Health(
        in_stock=row["in_stock"] or 0,
        enriched=row["enriched"] or 0,
        with_rating=row["with_rating"] or 0,
        with_indian_price=row["with_price"] or 0,
        price_unknown=row["price_unknown"] or 0,
        fallback_rating=row["fallback_rating"] or 0,
        exhausted=row["exhausted"] or 0,
        scored=row["scored"] or 0,
        pending_events=store.pending_event_count(),
        improvable=len(store.improvable_books(10_000, primary)),
    )

    health.with_blurb = store.conn.execute(
        """
        SELECT COUNT(*) n FROM products p
        JOIN llm_cache l ON l.book_key = p.book_key
        WHERE p.in_stock = 1 AND l.role = 'blurb' AND l.prompt_version = ?
        """,
        (version,),
    ).fetchone()["n"]

    health.notified_7d = store.conn.execute(
        "SELECT COUNT(*) n FROM notifications WHERE sent_at >= datetime('now', '-7 days')"
    ).fetchone()["n"]

    health.warnings = _warnings(health)
    return health


def _warnings(health: Health) -> list[str]:
    """Only things worth acting on. A summary nobody trusts gets ignored."""
    out: list[str] = []
    if health.in_stock and health.enriched < health.in_stock * 0.9:
        out.append(
            f"only {health.enriched}/{health.in_stock} in-stock books enriched — "
            "run 'pooks backfill'"
        )
    if health.in_stock and health.rating_coverage < 0.6:
        out.append(
            f"rating coverage {health.rating_coverage:.0%} — a source may be blocked"
        )
    if health.price_unknown > health.in_stock * 0.25:
        out.append(
            f"{health.price_unknown} books have an unknown price — Amazon may be "
            "throttling; the repair pass will retry"
        )
    if health.pending_events > 200:
        out.append(f"{health.pending_events} events pending — the queue is not draining")
    if health.exhausted > health.in_stock * 0.1:
        out.append(f"{health.exhausted} books past the refresh retry cap")
    return out


def render(health: Health) -> str:
    """HTML for Telegram."""
    lines = [
        "<b>pooks weekly health</b>",
        "",
        f"in stock       {health.in_stock}",
        f"enriched       {health.enriched} ({health.enriched / max(health.in_stock, 1):.0%})",
        f"with rating    {health.with_rating} ({health.rating_coverage:.0%})",
        f"indian price   {health.with_indian_price} ({health.price_coverage:.0%})",
        f"with blurb     {health.with_blurb}",
        f"scored         {health.scored}",
        "",
        f"improvable     {health.improvable}",
        f"price unknown  {health.price_unknown}",
        f"fallback rtg   {health.fallback_rating}",
        f"past retry cap {health.exhausted}",
        f"queue pending  {health.pending_events}",
        f"pushed (7d)    {health.notified_7d}",
    ]
    if health.warnings:
        lines += ["", "<b>needs attention</b>"]
        lines += [f"· {w}" for w in health.warnings]
    return "\n".join(lines)
