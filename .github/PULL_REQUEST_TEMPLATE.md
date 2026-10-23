## What and why

<!-- One or two sentences. Link the plan phase: "Phase N — …" -->

## How to test by hand

<!-- Commands or steps a reviewer can run. -->

## Checklist

- [ ] `make check` passes locally
- [ ] Behaviour is covered by tests (bug fixes start with a failing test); no real network in tests
- [ ] Outbound HTTP goes through `sentinel.security.http`
- [ ] No secrets, no secret defaults; new settings added to `.env.example` as placeholders
- [ ] User-facing copy is in `sentinel/web/i18n/tr.yaml`, not in templates
- [ ] Docs updated (README, METHODOLOGY, ADR for any stack or rule change)
