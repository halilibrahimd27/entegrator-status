# ADR 0009 — Change sources: snapshots, one fetcher, parsing libraries

- Status: accepted
- Date: 2026-10-20

## Context

Phase 5 adds sources that report *what changed* rather than *whether it is up*: vendor
status pages, RSS/Atom feeds and watched pages. They must be fetched politely (robots
every cycle, conditional requests), parsed safely (XML and HTML from strangers), and
turned into changes exactly once.

## Decision

**One fetcher per change source.** Availability needs several nodes for consensus;
announcements do not. The core gives each change job to one node, chosen by rendezvous
hashing over the nodes seen in the last ten minutes. A provider sees one request per
interval however many nodes run; when a node disappears its jobs move by themselves.

**Snapshots.** An adapter returns the normalised items it found (title, link, date and
a stable id; never article text) and a SHA-256 over the items sorted by id, so order
and whitespace are not changes. The core recomputes the hash on ingest and refuses a
mismatch, caps a snapshot at 512 KB / 100 items, and stores a new `Snapshot` only when
the hash differs from the last one. The prober ships batches of at most 2 MB.

**Conditional requests.** The `ETag` / `Last-Modified` of the latest snapshot travel
back to the node with the job; a `304` costs the provider almost nothing. Only
`Accept`, `If-None-Match` and `If-Modified-Since` may be added to a fetch; values with
control characters are refused.

**Changes.** The engine diffs each new snapshot against the previous one. The first
snapshot is a baseline (what was already published is not news). Every new item id
becomes one `Change`, classified from its title with Turkish and English keyword tables
(`sentinel.sources.classify`), with an effective date when the title carries one. A
change is unique per source and item, so an item that drops out and comes back is not
reported twice.

**Watch state.** `SourceWatch` records the last check, the last change and why a
source needs attention: `blocked_by_robots` (robots.txt disallows; we do not fetch),
`needs_attention` (fetched, but no items or not parseable — never silent "no news"),
`failing` (five failures in a row). Robots.txt is never optional for change sources,
whatever the availability flag says (ADR 0003).

**Parsing libraries.**

- Feeds: `defusedxml` (already in ADR 0001): no DTDs, no entity expansion, no external
  resources. Links are kept only when they are `http(s)`, so a feed cannot place a
  `javascript:` URL in our pages.
- Watched pages (Phase 5c): `lxml.html` (ADR 0001) with **`cssselect`** (new: compiles
  CSS selectors to XPath; pure Python, maintained by the Scrapy project). We chose it
  over BeautifulSoup + soupsieve to stay on the parser ADR 0001 already accepted.

## Consequences

- Adding a change source kind means an adapter that returns a snapshot, plus a diff
  rule in the engine; the protocol does not change.
- Changes found while the engine was down are still found later: snapshots wait until
  processed.
