# ADR 0012 — Distribution: open source, self-hosted, project site on GitHub Pages

- Status: accepted
- Date: 2026-10-23

## Context

The maintainer will not operate a public service: no server, no domain. GitHub Pages was
suggested as the host, but it serves static files only and cannot run the application
(web, engine, prober, PostgreSQL, Redis).

## Decision

- **Open source under Apache-2.0** (permissive, with an explicit patent grant). `LICENSE`
  is the unmodified text; `NOTICE` carries the copyright line.
- **Self-hosted.** Whoever runs an instance operates it: `docker compose` with the
  production profile, images published to GitHub Container Registry for every release,
  one or more probe nodes of their own. The operator is the data controller under KVKK;
  the project ships the Turkish texts as templates for them to adapt.
- **GitHub Pages hosts the project site**: what it does, screenshots, the self-hosting
  guide, the methodology and the bot page. Built from `docs/` by a workflow.
- **Optional static export of public status pages**: an instance can render an
  organization's public status page to static HTML and publish it to a GitHub Pages
  repository the organization owns, so a status page needs no public server.
- **The repository goes public at the end of Phase 11**, after a final audit: secrets in
  history, placeholders, the `legacy/` directory removed.

## Consequences

- Phase 11 becomes "Release": release images, the Pages site, the static export, the
  install guide and an upgrade path, instead of running production ourselves.
- No telemetry phones home: an instance talks only to what its operator configures.
