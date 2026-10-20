# Shared catalog

The shared catalog holds services many organizations depend on (cloud providers,
payment APIs, e-invoice integrators, GİB, popular packages) together with their
upstream edges. Every organization reads it; only platform admins change it, through
reviewed YAML packs in `catalog/packs/` or by accepting a proposal from an organization.

## Rules for adding entries

1. **Find providers in official sources, by hand.** Open official lists in a browser
   (for e-invoicing: GİB's list of authorised private integrators on
   `ebelge.gib.gov.tr`). That site disallows automated access in robots.txt; never
   scrape it, including the list page.
2. **Public endpoints only.** Add a source only for an address that is public without
   credentials and that you verified by hand: portal home or login page, a publicly
   documented WSDL or OpenAPI document, a public status page, an announcement page or
   feed. Endpoints meant only for customers or integrators are out of scope.
3. **Model what cannot be observed.** Services that are not public (for example GİB's
   central e-Fatura service, reachable only by integrators) are components **without
   sources**. Dependencies and announcements still make their impact visible.
4. **Record provenance.** Every source has a `source:` note: where the URL was found
   (documentation page, status page footer, "checked by hand on 2026-10-20 at …").
5. **No invented data.** Never guess an endpoint. If you are not sure, leave the
   component without sources.
6. **Factual names, no logos.** Use the provider's registered name.

## File format

Each pack is one YAML file; the schema is `catalog/pack.schema.json` (editors that
understand `yaml-language-server` comments validate as you type).

```yaml
id: tr-efatura                 # pack id (slug)
name: Türkiye e-Fatura
services:
  - slug: entegrator-x         # unique across all packs
    name: Entegratör X A.Ş.
    kind: integrator           # saas_api, integrator, authority, cloud, package, …
    components:
      - slug: efatura
        name: e-Fatura servisi
        tags: [efatura_service]          # tag rules add upstream edges
        sources:
          - kind: http                   # http, tls_cert, dns, domain_expiry, status_page,
            config:                      # api_contract, release, feed, page_watch,
              url: https://<PORTAL>/     # advisory, eol — see the schema per kind
            source: "portal adresi, entegratör sitesinin altbilgisi, 2026-10-20 elle kontrol"
edges:
  - from: entegrator-x/efatura
    to: gib/efatura-merkez
tag_rules:
  - tag: efatura_service
    to: gib/efatura-merkez
```

### What validation refuses

`python -m sentinel.catalog validate` (also run by the test suite) rejects:

- duplicate pack ids, duplicate service slugs across packs, duplicate component slugs
- unknown source kinds, unknown fields, invalid configs
- non-`https` URLs without `allow_http: true`; credentials in URLs; private, loopback,
  link-local, CGNAT and metadata IP literals
- intervals below the minimum (60 s for availability, 1 h for content sources)
- edges or tag rules pointing at unknown components; self edges; cycles (tag rules
  included)
- YAML aliases and files over 1 MB

## Importing

```bash
python -m sentinel.catalog validate                # all packs in catalog/packs
python -m sentinel.catalog import                  # dry run: prints the exact changes
python -m sentinel.catalog import --apply          # writes, audit-logged
```

Import is an idempotent upsert keyed by slugs (sources by kind + URL/host/name). It never
deletes: removing an entry from a pack leaves the row in place, because organizations
may depend on it. Retiring an entry is a platform-admin decision (Phase 11).

## Proposals from organizations

An organization can propose one of its private services for the shared catalog. A
platform admin reviews the proposal; accepting copies the service and its components
(never credentials, results or usage data) into the shared catalog.
