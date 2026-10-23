# Backups, restore drill and key rotation

Three things make an instance recoverable: the PostgreSQL database, the `.env` file
and — separately — `CREDENTIALS_MASTER_KEY`. Redis holds nothing that must survive.

| What | How | Where to keep it |
|---|---|---|
| Database | `tools/backup.sh`, nightly | off this server, encrypted |
| `.env` | copy once, again after every change | a password manager or a secrets store |
| `CREDENTIALS_MASTER_KEY` | the value in `.env` | **apart from the database backups** |

A database dump alone cannot reveal organizations' credentials: they are sealed with
keys that only `CREDENTIALS_MASTER_KEY` unwraps. That is also why losing the master key
loses those secrets for good.

## Nightly backup

```bash
tools/backup.sh            # backups/sentinel-<UTC time>.dump, mode 600; keeps the newest 14
KEEP=30 tools/backup.sh    # keep 30
```

Cron (`crontab -e` as the user that runs the stack):

```
15 3 * * * cd <PATH_TO_ENTEGRATOR_STATUS> && tools/backup.sh >> backups/backup.log 2>&1
```

Then copy the newest dump off the machine, encrypted, for example with
[age](https://github.com/FiloSottile/age) to an object store:

```bash
age -r <AGE_PUBLIC_KEY> -o sentinel.dump.age backups/sentinel-<UTC time>.dump
```

A dump contains personal data (accounts, alert recipients, subscribers): keep the
retention of your copies in line with your privacy notice.

## Restore drill (monthly)

```bash
tools/restore-drill.sh backups/sentinel-<UTC time>.dump
```

The drill loads the dump into a scratch database next to the live one, never touching
the live data, and checks three things against it:

```
✓ schema 0015_org_quotas (latest 0015_org_quotas)
rows: organizations 12, users 40, products 31
✓ secrets: 12 data keys, 9 credentials, 7 alert channels
✓ pages: /: 200, /yontem: 200, /s/<ORG_SLUG>: 200
```

It exits non-zero if the schema is behind, any secret fails to decrypt with the current
`CREDENTIALS_MASTER_KEY`, or a page does not render. The scratch database is dropped at
the end. An older dump shows an older schema: that is expected; restore it for real and
`migrate` brings it up to date.

## Restoring for real

```bash
docker compose -f compose.prod.yml stop web engine
docker compose -f compose.prod.yml exec -T postgres dropdb --username sentinel sentinel
docker compose -f compose.prod.yml exec -T postgres createdb --username sentinel sentinel
docker compose -f compose.prod.yml exec -T postgres \
  pg_restore --username sentinel --dbname sentinel --no-owner --exit-on-error \
  < backups/sentinel-<UTC time>.dump
docker compose -f compose.prod.yml up -d     # migrate runs first, then web and engine
```

Use the `.env` (and so the master key) that belonged to the instance when the dump was
taken; if the key was rotated since, use the key that was current then.

## Checking the master key

```bash
docker compose -f compose.prod.yml run --rm --no-deps web python -m sentinel.tenancy.keys check
```

Opens every organization's data key and every sealed secret, and names the rows that
do not open (ids only, never values).

## Rotating the master key

Rotate when the key may have been exposed, or on a schedule. Data keys are re-wrapped
under the new key in one transaction; the secrets themselves are not touched, so this
takes seconds.

```bash
tools/backup.sh
NEW_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
docker compose -f compose.prod.yml run --rm --no-deps \
  -e NEW_CREDENTIALS_MASTER_KEY="$NEW_KEY" web python -m sentinel.tenancy.keys rewrap
```

When it prints `re-wrapped N data key(s)` and every secret opens under the new key:

1. put the new value into `.env` as `CREDENTIALS_MASTER_KEY`;
2. store it where you keep the master key, and keep the old one until the backups made
   with it have aged out;
3. `docker compose -f compose.prod.yml up -d` (web and engine pick up the new key).

If anything fails, nothing was changed and the old key stays valid. The rotation is
recorded in the platform audit log.
