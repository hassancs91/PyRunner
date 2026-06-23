# Changelog

All notable changes to the SDK Showcase plugin are documented here. This
project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-06-23

First release — a reference plugin that demonstrates the whole plugin SDK
(`core.plugins.api`) on one page.

### Added
- One **Set up demo** save that idempotently provisions a data store, an
  owner-scoped secret, a managed script, and a schedule.
- Capability cards, each demonstrating one SDK surface with the code behind it:
  **data store** (counter), **secrets** (encrypted + selected-mode injection),
  **run lifecycle** (`queue_run` / `latest_run` / `runs` / `cancel_latest_run`
  with live progress + Stop), **worker output**, **schedule** (`sync`/`list`),
  and **ownership** (the owned-resource inventory).
- A standard-library-only demo worker, so it runs in any environment.

### Notes
- Requires PyRunner **1.13.0+** (plugin SDK API `2.1`).
- The worker needs **no third-party packages**.
