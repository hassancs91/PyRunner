# Changelog

All notable changes to the SDK Showcase plugin are documented here. This
project adheres to [Semantic Versioning](https://semver.org/).

## [1.1.0] - Unreleased

### Added
- A **Vectors** card: setup provisions a vectors-only collection
  (`sdk_showcase:demo`) through `VectorAPI`; *Index sample docs* upserts six
  short docs with `add_vectors`, and the search box runs `search_vector` with
  an optional payload filter by topic. The vectors are toy word hashes, so the
  card works with no embedding provider configured and spends nothing.
- The demo worker opens the same collection with the `pyrunner_vectors`
  helper and logs the doc nearest to its configured message.

### Notes
- Targets plugin SDK API `2.6`, which ships with PyRun 2.0.0 — `min_pyrunner`
  moves to `2.0.0` when that release is cut.

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
- Requires PyRun **1.13.0+** (plugin SDK API `2.1`).
- The worker needs **no third-party packages**.
