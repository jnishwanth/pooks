-- pooks schema. Prices are stored in paise (integer) exactly as the Store API
-- returns them; converting to rupees is a presentation concern.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- One row per shop listing. `book_key` links a listing to enrichment data and
-- is deliberately NOT the product id: the shop relists the same title
-- repeatedly, and keying enrichment by book means a relist costs zero API and
-- zero LLM calls.
CREATE TABLE IF NOT EXISTS products (
    product_id          INTEGER PRIMARY KEY,
    book_key            TEXT NOT NULL,
    name                TEXT NOT NULL,
    slug                TEXT,
    permalink           TEXT,
    isbn                TEXT,
    author              TEXT,
    publisher           TEXT,
    book_format         TEXT,
    pages               INTEGER,
    condition           TEXT,
    categories_json     TEXT NOT NULL DEFAULT '[]',
    price_paise         INTEGER,
    -- Recorded for completeness only. NEVER use as a reference price: recon
    -- found regular==sale in 0/40 in-stock items and identical fabricated
    -- "list" prices across unrelated titles. It is a marketing anchor.
    regular_price_paise INTEGER,
    in_stock            INTEGER NOT NULL DEFAULT 0,
    date_created        TEXT,
    date_modified       TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_book_key ON products (book_key);
CREATE INDEX IF NOT EXISTS idx_products_in_stock ON products (in_stock);
CREATE INDEX IF NOT EXISTS idx_products_isbn ON products (isbn);

-- Append-only log of detected changes. `requires_enrichment` and
-- `requires_inference` are set at classification time so the cost policy is
-- inspectable in the DB rather than buried in control flow.
CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL,
    event_type          TEXT NOT NULL,
    details_json        TEXT NOT NULL DEFAULT '{}',
    requires_enrichment INTEGER NOT NULL DEFAULT 0,
    requires_inference  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    processed_at        TEXT,
    FOREIGN KEY (product_id) REFERENCES products (product_id)
);

CREATE INDEX IF NOT EXISTS idx_events_unprocessed ON events (processed_at, created_at);
CREATE INDEX IF NOT EXISTS idx_events_product ON events (product_id);

-- Enrichment cached by book, not by listing. Ratings and comps change slowly;
-- `expires_at` allows targeted refresh without discarding the whole row.
CREATE TABLE IF NOT EXISTS enrichment (
    book_key            TEXT PRIMARY KEY,
    isbn                TEXT,
    resolved_title      TEXT,
    resolved_author     TEXT,
    rating              REAL,
    ratings_count       INTEGER,
    rating_source       TEXT,
    provenance_json     TEXT NOT NULL DEFAULT '{}',
    in_print            INTEGER,

    -- AbeBooks scarcity. Prices from this source are deliberately NOT stored:
    -- it quotes USD, and comparing Indian prices against it made every book
    -- look 87-91% cheaper regardless of what it was. Only the currency-
    -- independent facts survive.
    comp_listing_count  INTEGER,
    scarcity_has_new    INTEGER,

    -- The India-facing baseline the shop is judged against.
    -- `in_available` and `in_price_unknown` are separate on purpose: "no Indian
    -- retailer stocks this" is a meaningful, mildly positive signal, whereas
    -- "every lookup was blocked" means nothing and must not be scored as
    -- scarcity.
    in_price_paise      INTEGER,
    in_price_source     TEXT,
    in_price_url        TEXT,
    in_available        INTEGER,
    in_price_unknown    INTEGER,

    synopsis            TEXT,
    -- Hardcover genre/mood/reader tags. NULL means never asked; '{}' means
    -- asked and it has none, which is settled for ~2 books in 5.
    tags_json           TEXT,
    match_method        TEXT,
    fetched_at          TEXT NOT NULL,
    expires_at          TEXT,

    -- Retry budget for the repair pass. Without a cap, a genuinely obscure
    -- book with no entry on any source would be re-fetched forever.
    refresh_attempts    INTEGER NOT NULL DEFAULT 0,
    last_refresh_at     TEXT
);

-- Keyed by (book_key, role, prompt_version) so bumping prompt_version in
-- config.toml invalidates a role's cache without touching the others.
CREATE TABLE IF NOT EXISTS llm_cache (
    book_key            TEXT NOT NULL,
    role                TEXT NOT NULL,
    prompt_version      INTEGER NOT NULL,
    response_json       TEXT NOT NULL,
    model               TEXT,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (book_key, role, prompt_version)
);

-- Every component is persisted alongside the composite so the dashboard can
-- show the breakdown and `confidence` can gate pushes.
CREATE TABLE IF NOT EXISTS scores (
    product_id          INTEGER PRIMARY KEY,
    score               REAL NOT NULL,
    quality             REAL,
    renown              REAL,
    value               REAL,
    affordability       REAL,
    condition_factor    REAL,
    confidence          REAL,
    breakdown_json      TEXT NOT NULL DEFAULT '{}',
    computed_at         TEXT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products (product_id)
);

CREATE INDEX IF NOT EXISTS idx_scores_score ON scores (score DESC);

-- Records which products have been pushed, so a restock or a rerun never
-- re-notifies for the same listing.
CREATE TABLE IF NOT EXISTS notifications (
    product_id          INTEGER NOT NULL,
    event_id            INTEGER NOT NULL,
    sent_at             TEXT NOT NULL,
    PRIMARY KEY (product_id, event_id)
);

-- Single-row poll state. Holds both the cheap Last-Modified signal and the
-- fallback signals (max product id, in-stock total) used to verify it.
CREATE TABLE IF NOT EXISTS poll_state (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    last_modified           TEXT,
    last_max_product_id     INTEGER,
    last_instock_total      INTEGER,
    last_poll_at            TEXT,
    last_sweep_at           TEXT,
    last_304_at             TEXT
);

INSERT OR IGNORE INTO poll_state (id) VALUES (1);
