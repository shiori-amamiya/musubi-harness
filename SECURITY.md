# Security Policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 1.x.y   | :white_check_mark: |
| < 1.0   | :x:                |

The `1.x` line of `musubi-harness` is the supported stable line. Older
versions are not maintained and will not receive security fixes.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Email `ericmey@gmail.com` with:

- A clear description of the vulnerability and its impact.
- A reproducer — input, environment, observed behavior.
- Whether you intend to disclose, and on what timeline.

You will receive an acknowledgement within 3 business days. We aim to
produce a fix or a mitigation plan within 14 days for high-impact
issues.

## What we will not do

- We will not request, accept, or store bearer tokens, presence ids,
  zone ids, or any other identity material in this repository or in
  issue trackers. If your reproducer includes real credentials, redact
  them before sending.
- We will not publish a security advisory that includes
  conversation text, transcripts, or any captured memory payload.
  The harness is intentionally built to keep captured content out of
  diagnostic sinks, and security work here should not undo that.

## Scope

In scope for security reports:

- The `musubi_harness` Python package and its console scripts
  (`musubi-harness`, `musubi-harness-conformance`).
- The shipped configuration model (`$PLUGIN_DATA/config.json`,
  `MUSUBI_*` environment variables) — these are the only knobs an
  operator touches, and a misuse here is a real attack surface.

Out of scope:

- Host adapter implementations (`musubi-claude`, `musubi-codex`,
  `musubi-livekit`, `musubi-hermes`, `musubi-openclaw`). Each adapter
  has its own security policy and contact path.
- The downstream Musubi core service. That has its own repository
  and its own disclosure channel.
