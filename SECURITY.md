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
