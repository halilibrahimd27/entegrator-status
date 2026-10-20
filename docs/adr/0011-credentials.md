# ADR 0011 — Credential vault and authenticated checks on probe nodes

- Status: accepted
- Date: 2026-10-20

## Context

ADR 0004 brought authenticated checks into scope: an organization attaches **its own**
credentials to sources of services it is entitled to use (CLAUDE.md "Credentials").
The secrets must stay write-only and must only ever reach the host they were made for,
yet the requests are made by probe nodes, which may run on other networks.

## Decision

**Vault (core).**

- `CREDENTIALS_MASTER_KEY` is a new fail-closed secret (like the other keys in
  `SECRET_FIELDS`). `make env` adds it to an existing `.env` without touching the
  values already there.
- Each organization has a random 256-bit data key, stored wrapped with AES-256-GCM
  under a key derived from the master key, with the organization id as associated data.
- Each secret is sealed with AES-256-GCM under its organization's data key, with the
  organization and credential ids as associated data: a ciphertext copied onto another
  row does not decrypt.
- Secrets are write-only: never returned by the API, rendered, logged or stored in
  snapshots. Users can replace a secret (rotation) or delete the credential; the
  vault records when it was set and last used. Every change is audit-logged without
  the value.
- A credential is bound to one host. Credential kinds: API key in a request header,
  bearer token, basic auth, OAuth2 client credentials (with its token URL).

**Delivery to nodes.**

- Only nodes registered as trusted for credentials (`--credentials`) receive jobs of
  authenticated sources; other nodes never see them.
- The secret travels inside the job, sealed with AES-256-GCM under a key derived (HKDF)
  from that node's signing key, with the job key as associated data. The node already
  holds its signing key; nothing new is distributed, and one node cannot open another
  node's jobs.
- The node opens the secret in memory right before the request. It never enters the
  result buffer, snapshots, details or logs.
- Authenticated sources are never grouped with other organizations' sources (the
  credential is part of the job identity).

**On the wire.**

- Credentials are attached only to requests whose host is the credential's host; a
  redirect to another host drops them (each hop is checked).
- API keys in query strings are refused; keys travel in headers only.
- The only non-GET requests are the OAuth2 token request (to the configured token URL,
  through the SSRF guard, no redirects) and flagged read queries (ADR 0003 rule 2).
  Tokens are cached in node memory until shortly before they expire.

## Consequences

- Rotating the master key re-wraps the data keys only (`python -m sentinel.security
  rewrap`), not every secret.
- A canary secret is tested against logs, API responses, error pages, snapshots and
  diffs.
- Running an authenticated availability check with consensus needs two trusted nodes.
