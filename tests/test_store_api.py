"""The Store API client, exercised over httpx.MockTransport — no network.

The property worth pinning is that the cheap poll and the full sweep read the
same window. The poll's change signals are comparisons against numbers the last
*sweep* stored, so a filter that drifted apart from the sweep's would report a
change on every poll (or, worse, never report one) with nothing in the logs to
say why.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from pooks.ingest.store_api import StoreAPIClient

HEADER = "Tue, 11 Aug 2026 17:37:19 GMT"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> StoreAPIClient:
    return StoreAPIClient(
        base_url="https://shop.test",
        user_agent="pooks-test",
        min_request_interval_s=0.0,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://shop.test"
        ),
    )


def _payload(product_id: int) -> dict[str, Any]:
    return {"id": product_id, "name": f"Book {product_id}"}


def _window(params: httpx.QueryParams) -> dict[str, str]:
    """The filter, without the paging that legitimately differs."""
    return {k: v for k, v in params.items() if k not in {"per_page", "page"}}


async def test_poll_and_sweep_ask_for_the_same_window() -> None:
    seen: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params)
        return httpx.Response(200, json=[_payload(1)], headers={"x-wp-total": "1"})

    async with _client(handler) as client:
        await client.poll()
        await client.fetch_in_stock()

    poll_params, sweep_params = seen
    assert _window(poll_params) == _window(sweep_params)
    assert _window(poll_params) == {"orderby": "date", "order": "desc", "stock_status": "instock"}
    # The poll reads the head of that window, which is only the newest listings
    # because the ordering above puts them there.
    assert poll_params["page"] == "1"


async def test_fetch_in_stock_pages_until_the_total_is_reached() -> None:
    pages = {1: [_payload(3), _payload(2)], 2: [_payload(1)]}
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        requested.append(page)
        return httpx.Response(
            200,
            json=pages.get(page, []),
            headers={"x-wp-total": "3", "last-modified": HEADER},
        )

    async with _client(handler) as client:
        products, last_modified = await client.fetch_in_stock(page_size=2)

    assert requested == [1, 2]
    assert [p.product_id for p in products] == [3, 2, 1]
    assert last_modified == HEADER


async def test_poll_reads_304_as_no_change_not_as_an_empty_catalogue() -> None:
    """The conditional GET is the whole point of the poll: an idle check
    transfers no body, and must not look like the shop selling out."""
    sent: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("if-modified-since"))
        return httpx.Response(304)

    async with _client(handler) as client:
        result = await client.poll(last_modified=HEADER)

    assert sent == [HEADER]
    assert result.not_modified and not result.changed
    assert result.products == []
    # The caller's header survives, so a 304 does not clear the poll state.
    assert result.last_modified == HEADER


async def test_fetch_dates_survives_a_200_that_is_not_json() -> None:
    """A shop behind a WAF or a login redirect answers 200 with HTML, which
    fails in the decode rather than in the status — and dates are best-effort,
    so the chunk is skipped rather than raised out into the caller's tick."""
    chunks: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chunks.append(request.url.params["include"])
        if len(chunks) == 1:
            return httpx.Response(200, html="<html>Attention Required! | Cloudflare</html>")
        return httpx.Response(200, json=[{"id": 2, "date_gmt": "2026-08-01T09:00:00"}])

    async with _client(handler) as client:
        dates = await client.fetch_dates(list(range(1, 102)))

    # Both chunks were attempted: one bad response must not abort the rest.
    assert len(chunks) == 2
    assert dates == {2: {"date_created": "2026-08-01T09:00:00", "date_modified": None}}


async def test_fetch_dates_survives_a_200_that_decodes_to_an_object() -> None:
    """The nastier shape, because it decodes cleanly.

    A WP REST error envelope served with a 200 by a cache or WAF is valid JSON,
    so guarding only the decode lets it through — and iterating a dict yields
    its *keys*, so reading `id` off one is a TypeError on a string. Nothing
    catches that in `cli.cmd_sweep`, which calls `backfill_dates` without the
    daemon's guard, so it surfaces as a traceback and abandons every later
    chunk.
    """
    chunks: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chunks.append(request.url.params["include"])
        if len(chunks) == 1:
            return httpx.Response(
                200,
                json={"code": "rest_forbidden", "message": "Sorry", "data": {"status": 401}},
            )
        return httpx.Response(200, json=[{"id": 2, "date_gmt": "2026-08-01T09:00:00"}])

    async with _client(handler) as client:
        dates = await client.fetch_dates(list(range(1, 102)))

    assert len(chunks) == 2, "a bad chunk must not abandon the rest"
    assert dates == {2: {"date_created": "2026-08-01T09:00:00", "date_modified": None}}


async def test_fetch_dates_skips_an_entry_with_no_id() -> None:
    """`_fields` asks for id, but a filtered or partial row is not worth
    raising over when the other rows in the batch are fine."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"date_gmt": "2026-08-01T09:00:00"},
                {"id": 5, "date_gmt": "2026-08-02T09:00:00"},
            ],
        )

    async with _client(handler) as client:
        dates = await client.fetch_dates([4, 5])

    assert dates == {5: {"date_created": "2026-08-02T09:00:00", "date_modified": None}}


async def test_poll_reports_change_from_totals_the_header_missed() -> None:
    """`changed` is derived from three signals precisely so a Last-Modified
    that fails to advance cannot hide a real change."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_payload(7)],
            headers={"x-wp-total": "9", "last-modified": HEADER},
        )

    async with _client(handler) as client:
        quiet = await client.poll(last_modified=HEADER, known_total=9, known_max_id=7)
        total_moved = await client.poll(last_modified=HEADER, known_total=8, known_max_id=7)
        id_moved = await client.poll(last_modified=HEADER, known_total=9, known_max_id=6)

    assert not quiet.changed and quiet.reasons == []
    assert total_moved.changed and total_moved.reasons == ["instock-total 8->9"]
    assert id_moved.changed and id_moved.reasons == ["max-id 6->7"]
