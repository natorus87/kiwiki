# Security Policy

## Supported Versions

Security fixes are handled on the `main` branch until versioned release branches exist.

## Reporting a Vulnerability

Please do not open public issues for suspected vulnerabilities.

Report security issues privately to the repository owner. Include:

- A short description of the issue
- Steps to reproduce
- Affected version or commit
- Any relevant logs, payloads, or screenshots

## Secrets

Do not commit real `KIWIKI_USERS` API keys, OAuth token secrets, `.env` files, local wiki data, or deployment-specific credentials.

Use strong random values for API keys and for `KIWIKI_OAUTH_TOKEN_SECRET` in shared or public deployments.

## Known Limitations

- **Refresh-token replay protection is process-local.** Refresh tokens are
  rotated on every use, and a token that was already redeemed is rejected with
  `invalid_grant`. The list of redeemed token IDs lives in memory only: after a
  restart, or on another replica, an already-rotated refresh token (valid for up
  to 30 days) can be redeemed once more. Rotate `KIWIKI_OAUTH_TOKEN_SECRET` (or
  the affected API key) to invalidate all outstanding tokens after a suspected
  leak.
- **OAuth client registrations, authorization codes and the per-source
  registration limit are also in memory** and reset on restart. Run a single
  replica unless a shared store is configured (see `docs/architecture.md`).
- **Legacy cookie authentication.** For backwards compatibility the
  `kiwiki_session` cookie still accepts a raw API key instead of a session
  token. This path logs a deprecation warning and will be removed in a future
  major version; clients should send `Authorization: Bearer` instead.
