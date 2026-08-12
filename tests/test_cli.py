"""Argument parsing and dispatch.

Nothing else in the suite imports `pooks.cli`, so a command wired to nothing —
or to the same handler twice — would only surface when someone ran it. The
parser is cheap to build and touches no database, which makes that checkable
here.
"""

from __future__ import annotations

import argparse
import inspect

import pytest

from pooks import cli
from pooks.cli import build_parser
from pooks.config import load_config
from pooks.db.store import Store
from pooks.ingest.diff import apply, classify
from pooks.models import Product
from pooks.rank.score import ScoreBreakdown


def _command_names() -> list[str]:
    """Every registered subcommand.

    Read off the parser rather than listed again here: a second list would keep
    passing while a newly added command went unchecked. argparse exposes no
    public way to enumerate subcommands, hence `_actions`.
    """
    parser = build_parser()
    action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return list(action.choices)


def test_every_command_dispatches_to_a_distinct_coroutine() -> None:
    handlers = []
    for name in _command_names():
        args = build_parser().parse_args([name])
        assert inspect.iscoroutinefunction(args.handler), name
        handlers.append(args.handler)

    assert len(set(handlers)) == len(handlers)


def test_unknown_command_exits() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["definitely-not-a-command"])


def test_missing_command_exits() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# --- `pooks notify` renders the same card the daemon sends --------------------


def _relisted_cheaper(store: Store, product: Product) -> Product:
    """The same book, listed again at a lower price under a new product id."""
    sold_out = product.model_copy(
        update={"product_id": product.product_id + 500_000, "price_paise": 90_000}
    )
    store.upsert_product(sold_out)
    apply([product], classify([product], store, full_sweep=True), store)
    store.put_enrichment(
        product.book_key,
        {"rating": 4.2, "rating_source": "goodreads", "provenance_json": "{}",
         "refresh_attempts": 0},
    )
    store.put_score(
        product.product_id,
        ScoreBreakdown(
            score=0.8, quality=0.7, renown=0.6, value=0.5,
            condition_factor=0.93, confidence=0.7,
        ).as_dict(),
    )
    return product


async def test_notify_reports_a_book_relisted_cheaper(
    store: Store, products: list[Product], monkeypatch, capsys
) -> None:
    """`notify` re-renders the digest `process` would have sent, so it has to
    populate the same fields. It built its own `ProcessedBook` and left
    `previous_price_paise` unset, which silently dropped the price-drop line —
    the only place a drop surfaces at all, since a relist gets a new product id
    and no same-product PRICE_CHANGE ever fires.
    """
    config = load_config()
    product = _relisted_cheaper(store, products[0])
    monkeypatch.setattr(cli, "_open", lambda: (config, store))

    await cli.cmd_notify(argparse.Namespace(limit=5, dry_run=True))

    digest = capsys.readouterr().out
    assert "cheaper than when last listed" in digest
    assert f"₹{(90_000 - product.price_paise) / 100:.0f} cheaper" in digest
