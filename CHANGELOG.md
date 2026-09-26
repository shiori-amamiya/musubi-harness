# Changelog

All notable changes to `musubi-harness` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-09-26

### Added
- `musubi-memory-data`: a public, stdlib-only HTTP client for the six Musubi
  operations the harness calls (`status`, `recent`, `search`, `get`,
  `capture-durable`, `receipt-lookup`). It speaks the same argv and JSON as the
  operator `memory-data` tool, so the capture → outbox → drainer → receipt →
  readback contract is unchanged. Endpoint and credential come from
  `MUSUBI_API_URL` and `MUSUBI_TOKEN` in the child environment, which plugins
  fill from their own settings.
- `PluginRuntime.memory_data_bin()` falls back to that client **last**, after
  every existing resolution step. Before 1.1.0 an install without the private
  operator tool raised `memory_data_unavailable` and could not capture or recall.
- `tests/test_memory_data_parity.py`: both binaries run against one fake Musubi
  and must send identical requests and print identical JSON. It runs when
  `MUSUBI_PARITY_MEMORY_DATA` points at an operator `memory-data`.

### Security
- The bundled client refuses HTTP redirects, so the bearer token is never sent
  to a `Location` target. It accepts only `http`/`https` URLs without
  credentials, query or fragment, and caps responses at 16 MiB.

### Unchanged
- Where an operator `memory-data` is configured, on `PATH`, beside
  `musubi-harness`, or in the development root, it is still the one used.

## [1.0.1] - 2026-09-26

### Added
- `py.typed` marker (PEP 561) so downstream mypy sees the package as
  type-complete. The wheel now ships the marker file.

## [1.0.0] - 2026-09-26

### Added
- Initial public release of `musubi-harness` — the host-neutral Musubi
  memory runtime extracted from the `lib/musubi_harness/` source tree in
  the fleet-tools workspace.

### Notes
- This is the same code that the `musubi-claude`, `musubi-codex`,
  `musubi-livekit`, `musubi-hermes`, and `musubi-openclaw` seat adapters
  have been depending on at runtime via the workspace-internal
  `sys.path.insert(...)` shim. The extraction here makes that runtime
  dependency a real, versioned PyPI package so the adapters can pin
  against `musubi-harness>=1.0.0` instead of the workspace layout.

[Unreleased]: https://github.com/sourceblender/musubi-harness/compare/HEAD
[1.0.1]: https://github.com/sourceblender/musubi-harness/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/sourceblender/musubi-harness/compare/HEAD...v1.0.0
