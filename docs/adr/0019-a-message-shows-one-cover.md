# 19. A message shows one cover, and only when it is about one book

Status: accepted

## Context

The shop photographs the copy it is actually selling, and the Store API returns
that photograph on every listing — `images[0].src`, populated for 574 of 574
in-stock products (measured 2026-09-03), in a payload the poll and the sweep
already fetch. `Product.from_store_api` discarded it.

It cannot be recovered from the page instead. The shop's product pages carry no
OpenGraph tags at all, so pointing a Telegram link preview at `permalink`
renders an empty card rather than the cover. The URL has to be ingested to be
usable.

Telegram renders at most one link preview per message, and messages are grouped:
arrivals come in bulk uploads, so a drop of eight must not become eight
notifications (ADR 2 decides *whether* to push; this is about what a push looks
like once it happens). A digest of ten books therefore has no honest way to show
ten covers.

## Decision

`Product.image_url` is ingested and stored, and only absolute `http(s)` URLs are
kept — Telegram answers a preview URL it cannot resolve with a 400 that drops
the whole message.

A message shows a large preview above its text exactly when it carries one book,
and disables the preview otherwise. The rule is a property of the *message*, not
of the drop: anything else would attach one book's photograph to a card listing
nine others, and it means a chunk that ended up alone because of the length
budget still gets its cover rather than the outcome depending on why it was
alone.

The listing is reached by the title, which is now the link, and by an inline
keyboard button per book. The separate inline `buy` link they replace is gone —
it was a third copy of the same URL.

`image_url` is deliberately absent from `ingest.diff.METADATA_FIELDS`. Every
member of that tuple describes the *book*, and a change to one is worth an audit
row a human might read; a cover URL changes when WordPress regenerates a media
filename, so including it would emit an event per product on a re-import and
record nothing anyone would act on.

## Consequences

A bulk drop stays text and stays scannable. A single arrival — the case that
most wants a decision made — arrives with a photograph of the actual copy.

The cover is presentation only. It is not evidence, reaches no score, and no
fact appears only in the preview: Telegram fetches the URL itself, and a fetch
that fails is silent, so the message still sends and the card is complete
without it. Telegram also caches preview results per URL, so a first fetch that
times out against a small WooCommerce host can render nothing for a while
afterwards.

Existing rows carry NULL until the hourly sweep re-reads them, so covers appear
over the first hour after deploy rather than immediately. A genuinely new
arrival is unaffected, since `run.process_pending` reads the row the same sweep
wrote.

The grouping now has a second cap. `max_books_per_message` is editorial; the
4,096-character message limit is Telegram's, and exceeding it is not a
truncation but a rejection that loses the whole chunk permanently, because the
events were marked processed before the push was attempted. Both are enforced in
`chunk_books`, and the blurb — the card's only unbounded field — is capped so a
single book can never overrun a message on its own.
