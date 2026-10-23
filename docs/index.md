---
title: Entegratör Status
---

# Entegratör Status

Status and change tracking for everything your product depends on — and for what
*those* depend on. Open source ([Apache-2.0](https://github.com/halilibrahimd27/entegrator-status/blob/main/LICENSE)),
self-hosted, no telemetry. [Türkçe özet aşağıda.](#türkçe)

![The organization dashboard](img/dashboard.png)

Map your dependencies — third-party APIs, SaaS vendors, e-invoice integrators, cloud
services, packages, certificates, domains — and a curated shared catalog adds the
dependencies of those dependencies. Entegratör Status answers three questions for each
of them:

1. **Is it up?** Consensus from at least two independent probe locations, next to the
   vendor's own status page.
2. **Did it change?** API contract drift, releases, advisories, end-of-life dates and
   announcements, classified by what they mean for you.
3. **Does it affect me?** Impact up to the third degree, with the reason path and a
   severity that knows which operations you use.

## Run your own instance

- [Self-hosting guide](SELF_HOSTING.md) — one server, Docker Compose, automatic HTTPS
- [Backups, restore drill, key rotation](BACKUP.md)
- [Running a probe node](PROBE_NODE.md)
- [A public status page on GitHub Pages](STATIC_EXPORT.md)
- [Receiving alert webhooks](WEBHOOKS.md)
- [KVKK text templates for operators](KVKK.md)

## How it measures

- [Methodology](METHODOLOGY.md): what is observed, the status and impact rules, what
  public pages show, how our bot behaves and how corrections work.
- [The shared catalog](CATALOG.md): how entries are curated (by hand, from official
  sources, never scraped).
- [Architecture decisions](https://github.com/halilibrahimd27/entegrator-status/tree/main/docs/adr)

| Product page | Public status page |
|---|---|
| ![Product page](img/product.png) | ![Public status page](img/public-page.png) |

Source, issues and releases: [github.com/halilibrahimd27/entegrator-status](https://github.com/halilibrahimd27/entegrator-status).

## Türkçe

Ürününün bağımlı olduğu her şeyin — ve onların bağımlı olduklarının — durum ve
değişiklik takibi. Açık kaynak, kendi sunucunda çalışır, telemetri yok. Kesintileri,
sürüm ve API sözleşmesi değişikliklerini, sertifika ve destek bitişi gibi tarihli
riskleri üç dereceye kadar izler; ürününü neyin, neden etkilediğini söyler. Kurulum için
[kendi sunucunda çalıştırma rehberi](SELF_HOSTING.md), işletmeciler için
[KVKK metin şablonları](KVKK.md).
