# Security policy

Entegratör Status fetches URLs that other people configure and publishes the results,
so it is itself an attack surface. Please report problems privately.

## How to report

Use GitHub's **private vulnerability reporting** on this repository
(Security → Report a vulnerability). Do not open a public issue.

Please include the affected component, steps to reproduce and the impact you expect.
You will get an acknowledgement within 7 days.

## In scope

- SSRF: any way to make the prober or webhook sender reach a private, loopback,
  link-local, CGNAT or metadata address (including via DNS rebinding or redirects)
- XML parsing (XXE, entity expansion, oversized documents)
- Cross-tenant access: reading or changing another organisation's graph, results,
  credentials, alerts or subscribers
- Credential vault: any way to read back, log or exfiltrate stored credentials, or to
  make them reach a host other than the configured one
- Authentication, session or CSRF weaknesses in the panel or platform admin
- Forged or replayed probe result batches; webhook signature bypass
- XSS or content injection on public pages (e.g. through provider names)
- Leaks of personal data: accounts, members, alert recipients, subscribers (KVKK)
- Secrets in the repository or in logs; insecure defaults

## Out of scope

- Findings that require a compromised host or a malicious maintainer
- Rate limits on public pages that do not lead to resource exhaustion
- Problems in the third-party services we observe; contact the vendor directly
