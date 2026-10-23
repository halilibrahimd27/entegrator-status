# Webhooks — receiving alerts

An alert channel of kind **İmzalı webhook** sends every alert as a signed JSON `POST` to
a URL you choose. This page is for whoever writes the receiving end: what arrives, how to
check that it came from your Entegratör Status installation, and how retries work.

A channel of kind **Slack uyumlu webhook** is simpler: it posts `{"text": "…"}` to an
incoming-webhook URL (Slack, or anything that accepts the same body, such as Mattermost).
It is not signed; the secret is the URL itself.

## What arrives

```http
POST /<YOUR_PATH> HTTP/1.1
Host: <YOUR_HOST>
Content-Type: application/json
User-Agent: EntegratorStatus/<VERSION> (+<PUBLIC_BASE_URL>/hakkinda/bot; <CONTACT_EMAIL>)
X-Sentinel-Timestamp: 1792400000
X-Sentinel-Signature: sha256=<HEX_HMAC>
X-Sentinel-Event: <EVENT_ID>

{"event":"<EVENT_ID>","type":"change","phase":"new","severity":"high", …}
```

| Header | Meaning |
|---|---|
| `X-Sentinel-Timestamp` | Unix time (seconds) when this attempt was signed. |
| `X-Sentinel-Signature` | `sha256=` + hex HMAC-SHA256 of `<timestamp>.<raw body>` with the channel's signing secret. |
| `X-Sentinel-Event` | Stable id of the alert. A retry carries the same id: use it to drop duplicates. |

### Body

One alert:

| Field | Example | Notes |
|---|---|---|
| `event` | `"3f0c…"` | Same as `X-Sentinel-Event`. |
| `type` | `"incident"`, `"change"`, `"risk"`, `"test"` | `test`: sent from the panel's *Test mesajı gönder* button. |
| `phase` | `"opened"`, `"resolved"`, `"new"`, `"test"` | Incidents send `opened` and later `resolved`. |
| `severity` | `"low"`, `"medium"`, `"high"` | Impact on the product, not the vendor's own label. |
| `reason` | `"used_operation_breaking"` | Why it matters; the panel shows it in Turkish. |
| `title` | `"openapi"` | Short subject. |
| `path` | `["Fatura", "Entegratör X — API", "GİB — e-Fatura"]` | From your product down to where it happened (up to three hops). |
| `link` | `"https://…"` or `null` | The vendor's page about it, when there is one. |
| `occurred_at` | `"2026-10-19T09:00:00+00:00"` | ISO 8601, UTC. |
| `product_id` | `"9a1d…"` or `null` | `null` for test messages. |

A channel in **daily mode** sends one request a day at 09:00 (Turkey time) with
`{"events": [<alert>, …]}`; `X-Sentinel-Event` is the id of the first alert in it.
A channel with the **weekly digest** gets
`{"type": "weekly_digest", "start": …, "end": …, "changes": 3, "outages": 1, "due": 2, "planned": 0}`
on Mondays at 09:00, with `X-Sentinel-Event: weekly-<YYYY-MM-DD>`.

## Verify every request

The signing secret is shown **once**, when the channel is created (or when you press
*Yeni imza anahtarı üret*). Store it with your other secrets. Then, for every request:

1. Read the **raw** body bytes before any JSON parsing — re-serialised JSON will not match.
2. Reject a timestamp more than 5 minutes away from your clock (stops replays).
3. Compute the HMAC over `<timestamp>.` + raw body and compare it to the header in
   constant time.
4. Drop an `X-Sentinel-Event` you have already processed (retries resend it).

Reference implementation in Python (the test suite runs this exact block against real
signed requests, so it stays correct):

```python
import hashlib
import hmac
import time

TOLERANCE_S = 300


def verify(
    secret: str, body: bytes, timestamp: str, signature: str, now: float | None = None
) -> bool:
    """True when the request was signed with ``secret`` within the last five minutes."""
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - signed_at) > TOLERANCE_S:
        return False
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return hmac.compare_digest("sha256=" + mac.hexdigest(), signature)
```

Node.js (with the raw body, e.g. `express.raw({ type: "application/json" })`):

```javascript
const crypto = require("node:crypto");

function verify(secret, rawBody, timestamp, signature) {
  const age = Math.abs(Date.now() / 1000 - Number(timestamp));
  if (!Number.isFinite(age) || age > 300) return false;
  const expected = "sha256=" + crypto
    .createHmac("sha256", secret)
    .update(`${timestamp}.`)
    .update(rawBody)
    .digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(signature);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

## Answer quickly, retry safely

- Any `2xx` within 20 seconds counts as delivered; the response body is ignored.
- Anything else (other status, timeout, TLS or DNS error) is retried after 1 min,
  5 min, 30 min, 2 h and 6 h, then given up. Each retry is signed again with a new
  timestamp; the event id stays the same.
- Redirects are not followed: point the channel at the final URL.
- Ten failed sends in a row switch the channel off. The panel shows *Kapalı: üst üste
  10 gönderim başarısız oldu*; fix the receiver, then tick *Kanal açık* again.
- The panel's *Son gönderimler* list shows each attempt's result as a short code
  (`http_500`, `connect_timeout`, `blocked_address`, …), never the URL.

## Network rules

The URL must be public `https`. Addresses in private, loopback, link-local and other
special ranges are refused, and the host name is resolved and checked again on every
send, so a DNS change cannot point an existing channel at an internal service. The
complete URL, including any token in its path, is stored encrypted in the
organization's vault and never shown again or written to logs; the panel shows its host
only. To change it, enter a new URL (*Adresi değiştir*).
