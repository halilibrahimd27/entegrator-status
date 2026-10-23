# Static export — a public status page without a public server

An instance does not need to be reachable from the internet to give an organization a
public status page. The exporter renders the page as static files, and a small script
pushes them to a GitHub Pages repository the organization owns (any static host works
the same way). This is the setup [ADR 0012](adr/0012-distribution.md) describes.

What you get: `index.html`, `yontem.html` (methodology), `akis.atom`, `rozet.svg`,
`static/app.css`, `static/favicon.svg` and `.nojekyll`. Links between them are
relative. The page is as fresh as your schedule (every 5 minutes is reasonable). E-mail subscriptions need a running
instance that the internet can reach; without one the page has no subscribe link.

## 1. Switch the page on

In the panel: **Durum sayfası** → choose products → tick *Sayfa yayında* → *Kaydet*.
The live page at `<PUBLIC_BASE_URL>/s/<ORG_SLUG>` is what will be exported.

## 2. Create the Pages repository

1. Create a repository, e.g. `<OWNER>/<REPO>`, with one commit on `main`.
2. *Settings → Pages*: deploy from branch `main`, folder `/ (root)`.
3. Create a deploy key on the instance's host and add it to the repository
   (*Settings → Deploy keys*) **with write access**. It can push to this repository
   only.

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/<REPO>_deploy -C "<REPO> status export"
git clone git@github.com:<OWNER>/<REPO>.git ~/status-pages/<REPO>
git -C ~/status-pages/<REPO> config core.sshCommand "ssh -i ~/.ssh/<REPO>_deploy -o IdentitiesOnly=yes"
```

A custom domain is a `CNAME` file in the repository plus a DNS record; the exporter
never touches files it did not write.

## 3. Export and push

`publish-status-page.sh` on the instance's host:

```bash
#!/usr/bin/env bash
# Export one organization's status page and push it when it changed.
set -euo pipefail

COMPOSE_DIR=<PATH_TO_ENTEGRATOR_STATUS>
PAGES_DIR=~/status-pages/<REPO>
SLUG=<ORG_SLUG>
SITE_URL=https://<OWNER>.github.io/<REPO>

docker compose --project-directory "$COMPOSE_DIR" run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  --volume "$PAGES_DIR:/export" \
  web python -m sentinel.public.export "$SLUG" /export --site-url "$SITE_URL"

cd "$PAGES_DIR"
git add -A
if ! git diff --cached --quiet; then
  git commit --quiet -m "status: $(date -u +%Y-%m-%dT%H:%MZ)"
  git push --quiet origin main
fi
```

Run it every 5 minutes (`crontab -e`):

```
*/5 * * * * <PATH_TO>/publish-status-page.sh >> ~/status-pages/publish.log 2>&1
```

The "last updated" time on the page is rounded down to the hour (`--stamp-minutes`,
default 60), so the files change — and the script commits — only when something on the
page changed, plus once an hour. That keeps the repository history small.

If the instance's public URL is reachable from the internet, add
`--subscribe-url <PUBLIC_BASE_URL>` to the export command: the static page then links
to the instance's subscription form.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--site-url` | required | Where the files are served; used for the canonical link and the feed id. Must be `https`. |
| `--subscribe-url` | none | This instance's public URL, for the subscribe link. |
| `--stamp-minutes` | `60` | Round the "last updated" time down to this many minutes (1–1440). |

The exporter exits with status 1 when the organization has no enabled page.

## What is published

Exactly what the live page shows: the chosen products, the organization's own incidents
of the last 30 days, and — marked as independent measurement — whether a component a
product uses directly is down. Nothing from the panel, no changes, no risks, no
subscriber data.
