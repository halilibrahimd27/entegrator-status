# ADR 0007 — Authentication and sessions

- Status: accepted
- Date: 2026-10-20

## Context

Accounts hold access to organisations' dependency graphs and, from Phase 7, their
credentials for third-party APIs. Sign-in must resist the usual attacks (credential
stuffing, enumeration, CSRF including login CSRF, session fixation, token leaks) without
JavaScript and without third-party services.

## Decision

- **Passwords:** argon2id (argon2-cffi defaults; cheap parameters only in the test
  environment), 12–128 characters, rehash on sign-in when parameters change. Unknown
  accounts verify against a dummy hash so timing does not reveal them.
- **Enumeration:** sign-up, verification resend and password reset answer identically
  whether or not the address has an account; an existing owner gets a "someone tried to
  sign up" mail instead of a second account.
- **Brute force:** 5 failures lock the account for 15 minutes; Redis fixed-window limits
  per IP and per account on sign-in, sign-up, recovery and the second factor. Redis keys
  are hashed (no e-mail or IP addresses stored).
- **Sessions:** server-side rows; the cookie holds 32 random bytes and the database only
  their HMAC (`SESSION_KEY`). `__Host-` cookies, `Secure; HttpOnly; SameSite=Strict`.
  Idle timeout 2 h, absolute 7 days; a password change ends every session; every sign-in
  and every second-factor success issues a new session (no fixation).
- **CSRF:** derived, not stored — HMAC(`SESSION_KEY`, session token) when signed in,
  HMAC(`SESSION_KEY`, pre-session cookie) on forms shown before sign-in (covers login
  CSRF). A router-level dependency checks every unsafe request; a test fails if any
  unsafe route lacks it.
- **One-time links** (verification, reset, later invitations): the token travels in the
  path, never the query string; the link opens a page whose button POSTs the token (mail
  scanners cannot spend it); those pages send `Referrer-Policy: no-referrer`; the uvicorn
  access log redacts the token segment. Tokens are stored as HMACs (`TOKEN_HASH_KEY`),
  single use, 24 h (verification) or 1 h (reset); a weak new password does not spend a
  reset token.
- **TOTP:** RFC 6238, ±1 step, a step at or before the last accepted one is refused
  (no replay). Secrets are AES-256-GCM encrypted with a key derived from
  `TOTP_ENCRYPTION_KEY`, with the user id as associated data. Mandatory for platform
  admins (cannot be disabled; every page but the account page redirects until enrolled).
- **Mail** is sent after the database transaction commits, never inside it.

## Consequences

- No JavaScript is needed anywhere in the flows; QR codes are inline SVG.
- `SameSite=Strict` means following an e-mail link starts signed out; the verification
  and reset flows do not need a session, invitation acceptance asks to sign in.
- Reverse proxies (Caddy in Phase 11) must redact the same path segments in their logs.
