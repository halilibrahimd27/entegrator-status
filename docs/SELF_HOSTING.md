# Self-hosting

Run your own instance on one Linux server with Docker. Target: a fresh VM to a working
site with HTTPS in under 30 minutes. Whoever runs an instance operates it: you are the
data controller under KVKK for the people who use it (see [KVKK texts](#kvkk-texts)).

## What you need

- A Linux VM with 2 vCPU, 2–4 GB RAM and 20 GB disk; Docker Engine with the Compose
  plugin; ports 80 and 443 reachable from the internet.
- A domain name whose A/AAAA records point at the VM (`<DOMAIN>`).
- An SMTP account for outgoing mail (sign-up, password reset, invitations, alerts).
- A second machine in another network for a second probe node: "down" needs two
  independent probe locations ([PROBE_NODE.md](PROBE_NODE.md)). One node works, but its
  findings stay "doğrulanmadı" (unverified).

## Install

```bash
git clone https://github.com/halilibrahimd27/entegrator-status.git
cd entegrator-status
git checkout v<VERSION>
make env-prod                 # writes .env (mode 600) with fresh secrets
```

Edit `.env` and replace every `<PLACEHOLDER>`: `ENTEGRATOR_VERSION`, `DOMAIN`,
`ACME_EMAIL`, `PUBLIC_BASE_URL` (`https://<DOMAIN>`), `CONTACT_EMAIL` and the SMTP
settings. The app refuses to start while a secret is missing, short, a placeholder or
used twice, so a mistake shows up immediately in `docker compose logs`.

**Back up `CREDENTIALS_MASTER_KEY` now**, somewhere other than this server and other
than your database backups (a password manager). Without it the credentials and alert
channel secrets your organizations stored cannot be decrypted — not even from backups.

```bash
docker compose -f compose.prod.yml up -d
docker compose -f compose.prod.yml ps        # web: healthy, caddy: running
```

Caddy gets a certificate from Let's Encrypt on the first request to
`https://<DOMAIN>`. Then:

1. Open the site, sign up with your own address and verify it.
2. Make yourself platform admin, on the server:
   ```bash
   docker compose -f compose.prod.yml run --rm --no-deps web \
     python -m sentinel.admin promote <YOUR_EMAIL>
   ```
   On your next request the site asks you to set up TOTP; platform admins cannot skip it.
3. Load the shared catalog (reviewed YAML packs, [CATALOG.md](CATALOG.md)):
   ```bash
   docker compose -f compose.prod.yml run --rm --no-deps web \
     python -m sentinel.catalog import --apply
   ```
4. Register the probe nodes ([PROBE_NODE.md](PROBE_NODE.md)) and start the first one.
5. Schedule backups ([BACKUP.md](BACKUP.md)) and run the restore drill once.

If you want nobody else to create accounts, set `SIGNUP_ENABLED=false` in `.env` and
`docker compose -f compose.prod.yml up -d` again after your own account exists.

## What runs

| Service | Role | Network |
|---|---|---|
| `caddy` | HTTPS (Let's Encrypt), HTTP/3, compression; the only published ports | `edge` |
| `web` | the site and the API (`/healthz`, `/metrics` internal only) | `edge`, `data` |
| `engine` | status consensus, changes, risks, alerts, digests, retention | `data`, `egress` |
| `migrate` | runs database migrations, then exits; every start runs it first | `data` |
| `postgres`, `redis` | storage; Redis with an ACL user, no persistence | `data` (no internet) |
| `prober` | optional probe node on the same host (`--profile probe`) | `edge`, `egress` |

Everything runs read-only as a non-root user with all capabilities dropped.

## Settings worth knowing

| Variable | Default | Meaning |
|---|---|---|
| `SIGNUP_ENABLED` | `true` | `false`: the sign-up page refuses new accounts. |
| `ORG_MAX_SOURCES` | `300` | Private sources per organization; admins can set others per organization. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME` | — | Turn on Telegram alert channels (create a bot with @BotFather). |
| `METRICS_TOKEN` | — | Turn on `/metrics` (Prometheus text) for scrapers on the internal network. |
| `CORRELATED_FAILURE_RATIO` | `0.4` | Share of an upstream's dependants that must fail together for the "common cause" hint. |
| `LOG_LEVEL` | `INFO` | Logs never contain secrets, tokens or full webhook URLs. |

## Upgrading

```bash
git fetch --tags && git checkout v<NEW_VERSION>
# read CHANGELOG.md for the versions in between
sed -i 's/^ENTEGRATOR_VERSION=.*/ENTEGRATOR_VERSION=<NEW_VERSION>/' .env
make env-prod                 # adds settings a newer version needs; changes nothing else
tools/backup.sh               # a fresh backup before migrations
docker compose -f compose.prod.yml pull
docker compose -f compose.prod.yml up -d
```

`migrate` applies new migrations before `web` and `engine` start. Migrations only move
forward; to go back, restore the backup taken before the upgrade
([BACKUP.md](BACKUP.md#restoring-for-real)) and start the previous version.

## Monitoring

- `docker compose -f compose.prod.yml ps` shows the health of `web`, `postgres`, `redis`.
- With `METRICS_TOKEN` set, scrape `http://web:8000/metrics` from a container on the
  `data` network with `Authorization: Bearer <METRICS_TOKEN>`; Caddy answers 404 for it
  from outside. Counters: organizations, sources, open incidents, alert deliveries by
  status (24 h), probe nodes, open correction requests.
- Watch `entegrator_alert_deliveries{status="failed"}` and the engine log.

## KVKK texts

The privacy notice (`/aydinlatma`) ships as a draft. Before you open the site to other
people, replace it with your own text as the data controller: what you process (account
data, security logs with shortened IP addresses, alert recipients, public page
subscribers, correction requests), why, for how long (the retention jobs delete
unverified accounts after 48 hours, unconfirmed alert recipients after 48 hours,
unsubscribed ones after 30 days, alert history after 180 days, decided correction
requests after a year) and how people exercise their rights. The copy is in
`src/sentinel/web/i18n/tr.yaml` under `privacy`.

> 🇹🇷 **KVKK notu:** Kurulumu işleten taraf veri sorumlusudur. Yurt dışında bir SMTP
> sağlayıcısı ya da sunucu kullanıyorsan aktarım hükümlerini (KVKK md. 9) değerlendir;
> VERBİS kayıt yükümlülüğünü kendi durumuna göre kontrol et.
