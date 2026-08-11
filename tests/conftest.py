from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from pooks.db.store import SCHEMA_PATH, Store
from pooks.models import Product

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def raw_products() -> list[dict[str, Any]]:
    """Real Store API payloads captured from oldbookdepot.in."""
    return json.loads((FIXTURES / "instock_page.json").read_text())


@pytest.fixture
def products(raw_products: list[dict[str, Any]]) -> list[Product]:
    return [Product.from_store_api(p) for p in raw_products]


@pytest.fixture
def store() -> Store:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    return Store(conn)


@pytest.fixture
def mutate():
    """Produce a modified copy of a raw Store API payload."""

    def _mutate(
        payload: list[dict[str, Any]],
        *,
        price_paise: dict[int, int] | None = None,
        drop_ids: set[int] | None = None,
        rename: dict[int, str] | None = None,
        out_of_stock: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        result = []
        for item in deepcopy(payload):
            if drop_ids and item["id"] in drop_ids:
                continue
            if price_paise and item["id"] in price_paise:
                item["prices"]["price"] = str(price_paise[item["id"]])
            if rename and item["id"] in rename:
                item["name"] = rename[item["id"]]
            if out_of_stock and item["id"] in out_of_stock:
                item["is_in_stock"] = False
            result.append(item)
        return result

    return _mutate
