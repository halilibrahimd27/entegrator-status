# Running a probe node

Probe nodes make the requests; the core decides. A component is "down" only when **at
least two healthy nodes** see failures for three rounds in a row, so a useful instance
has two nodes in different networks (two providers, or one at home and one in a cloud),
preferably both in Turkey if the services you watch serve Turkey.

A node is small and outbound-only: it fetches its jobs from the core, makes the
requests, buffers results locally and ships them back in signed batches. It never
opens a port and never touches the database.

## 1. Register the node on the core

On the core's server:

```bash
docker compose -f compose.prod.yml run --rm --no-deps web \
  python -m sentinel.probe register <NODE_NAME> "<LOCATION>"
```

It prints three lines **once**:

```
PROBE_NODE_ID=<NODE_ID>
PROBE_NODE_TOKEN=<NODE_TOKEN>
PROBE_SIGNING_KEY=<SIGNING_KEY>
```

The core keeps only a hash of the token. If they are lost, register a new node.

Add `--credentials` only for nodes you run yourself on machines you trust: such a node
receives organizations' credentials (sealed for that node alone) and runs the
authenticated checks ([ADR 0011](adr/0011-credentials.md)). Without the flag a node
never sees a credential, and authenticated sources simply skip it.

## 2. Start it on the node machine

The node needs Docker and outbound HTTPS, nothing else. Create a directory with this
`compose.yml`:

```yaml
name: entegrator-probe
services:
  prober:
    image: ghcr.io/halilibrahimd27/entegrator-status:<VERSION>
    command: ["python", "-m", "sentinel.probe"]
    env_file: .env
    environment:
      PROBE_BUFFER_PATH: /data/probe-buffer.sqlite3
    volumes:
      - buffer:/data
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    restart: unless-stopped
volumes:
  buffer:
```

and a `.env` (mode 600) with only what a node needs — no database, Redis or core keys:

```
PUBLIC_BASE_URL=https://<DOMAIN>
CONTACT_EMAIL=<CONTACT_EMAIL>
PROBE_INGEST_URL=https://<DOMAIN>
PROBE_NODE_ID=<NODE_ID>
PROBE_NODE_TOKEN=<NODE_TOKEN>
PROBE_SIGNING_KEY=<SIGNING_KEY>
# Three stable public https URLs this node checks to tell its own network trouble
# from a provider's (comma separated). Recommended.
PROBE_CONTROL_TARGETS=<URL_1>,<URL_2>,<URL_3>
```

```bash
docker compose up -d
docker compose logs -f prober     # "prober started (node …)", then quiet unless something is wrong
```

On the core, `python -m sentinel.probe list` shows when each node was last seen.

`PUBLIC_BASE_URL` and `CONTACT_EMAIL` go into the node's User-Agent, so providers can
see whose bot it is (the instance's `/hakkinda/bot` page explains it).

## Clock

Every batch is signed with a timestamp, and the core refuses batches more than
**5 minutes** off its own clock. Keep the node's clock synchronised (systemd-timesyncd,
chrony): a drifting clock looks like a dead node.

## When the node is offline

Results wait in the local SQLite buffer (bounded by size, kept for up to 6 days) and
are shipped when the core is reachable again; batches are idempotent, so nothing is
counted twice. A node that cannot reach its own control targets marks itself unhealthy
and is left out of consensus until it recovers.

## Retiring a node

Stop it, then revoke it on the core so its token stops working:

```bash
docker compose -f compose.prod.yml run --rm --no-deps web python -m sentinel.probe disable <NODE_NAME>
```

A silent node is left out of consensus after one and a half check intervals anyway;
revoking also makes sure a leaked token is useless.
