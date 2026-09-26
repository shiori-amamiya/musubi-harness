# Changelog

All notable changes to `musubi-harness` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
