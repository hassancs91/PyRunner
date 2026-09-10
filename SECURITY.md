# Security Policy

## Supported versions

PyRun is under active development. Security fixes are applied to the latest
release on the `main` branch. Please make sure you are running the most recent
version before reporting an issue.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or the Discord server.**

Instead, use one of the private channels below:

- **GitHub private vulnerability reporting** (preferred) — open a report via the
  repository's **Security → Report a vulnerability** tab. This keeps the details
  private to the maintainers until a fix is available.
- Alternatively, reach the maintainer privately via a direct message on
  [Discord](https://discord.gg/BjkmTn7XSd).

Please include:

- A description of the vulnerability and its impact.
- Steps to reproduce (proof-of-concept, affected endpoint/feature, config).
- The PyRun version (`pyrunner/version.py`) and your deployment mode
  (SQLite/Postgres, Docker/bare, behind a proxy, etc.).

## What to expect

- We aim to acknowledge a report within a few days.
- We will confirm the issue, keep you updated on remediation, and credit you in
  the release notes if you would like.
- Please give us a reasonable window to release a fix before any public
  disclosure.

## Scope notes

PyRun is **self-hosted software that runs arbitrary user-supplied Python
scripts by design**. Script execution having access to the host is an intended
capability, not a vulnerability — isolation is opt-in via the sandbox and per-run
resource limits (see the Isolation settings and `.env.example`). Reports about
the operator's own scripts having host access are out of scope; reports about one
user/tenant escaping their boundary, auth bypass, secret disclosure, or remote
code execution by an unauthenticated party are in scope.
