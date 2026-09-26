## What this PR does

<!--
The harness is host-neutral and shared by every musubi-* seat adapter.
Describe the contract change and which adapters are affected.
-->

## Why

<!--
Link to an issue if one exists. State the problem and the chosen
solution.
-->

## Contract impact

- [ ] No change to `shadow` / `pending` / `accepted` / `verified` / `dead`
- [ ] No change to receipt shape (`object_id`, `Readback`, etc.)
- [ ] No change to identity policy (env-or-config, all-or-nothing)
- [ ] No change to namespace policy (`actor == presence-prefix`)
- [ ] No new runtime dependency

If any of the above is checked off as "change", describe it here and
note every adapter affected:

## Adapters affected

- [ ] musubi-claude
- [ ] musubi-codex
- [ ] musubi-livekit
- [ ] musubi-hermes
- [ ] musubi-openclaw
- [ ] None / N/A (consumers of the harness only)

## Checklist

- [ ] `ruff check src tests` is clean
- [ ] `ruff format --check src tests` is clean
- [ ] `mypy src` is clean
- [ ] `pytest` is green
- [ ] New public symbols are listed in `src/musubi_harness/__init__.py`
