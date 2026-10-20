# ADR 0010 — Registries and advisory APIs: front doors and robots.txt

- Status: accepted
- Date: 2026-10-20

## Context

Phase 6 tracks releases, advisories and end-of-life dates through public APIs (ADR 0003
rule 4: GitHub, npm, PyPI, OSV, endoflife.date). These fetches store content (release
lists), so they are content watchers and robots.txt applies to them (ADR 0003 rule 6).
We read each host's robots.txt on 2026-10-20 instead of assuming:

| Host | robots.txt | Consequence |
|---|---|---|
| `api.github.com` | 404 (none) | allowed (RFC 9309) |
| `registry.npmjs.org` | not a robots file (the `robots.txt` npm package) | no rules: allowed |
| `pypi.org` | `Disallow: /pypi/*/json`, `/simple/` | the JSON API is closed to bots; `/rss/project/<name>/releases.xml` is allowed |
| `api.osv.dev` | 404 (none) | allowed |
| `endoflife.date` | `Allow: /` | allowed |

The same check exposed that `urllib.robotparser` ignores RFC 9309 wildcards (`*`, `$`):
it would have allowed `/pypi/requests/json`. `sentinel.probe.robots_rules` now
implements RFC 9309 for every content watcher.

## Decision

- robots.txt is checked every cycle for registry and advisory fetches too, with the
  RFC 9309 matcher. A host that disallows us shows `blocked_by_robots` on the source.
- Front doors per ecosystem:
  - **GitHub**: `GET /repos/{owner}/{repo}/releases` (tags when there are no releases),
    unauthenticated for now (60 requests / hour / IP is enough at a ≥ 1 h interval with
    deduplication). A platform `GITHUB_TOKEN` waits for Phase 7, where credentials are
    dropped on cross-host redirects.
  - **npm**: abbreviated metadata (`application/vnd.npm.install-v1+json`): versions,
    dist-tags and deprecations; no publish dates.
  - **PyPI**: the per-project releases RSS feed (the JSON API is disallowed).
  - **OCI**: Docker Hub tags only; other registries need token flows (later).
  - **OSV** (Phase 6c): `POST /v1/query` is the documented way to ask about a package.
    It is a read query in the sense of ADR 0003 rule 2: allowed only for that exact URL,
    no redirects followed, JSON body built by us.
- Release notes are read for "breaking" markers and dropped; snapshots keep version,
  title, link, date and flags only.

## Consequences

- PyPI releases have dates and links but no yanked flags (RSS does not carry them).
- If a registry changes its robots.txt to disallow us, its sources stop and say so
  instead of silently fetching.
