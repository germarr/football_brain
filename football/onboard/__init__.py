"""Onboard — admit a Competition to the Registry so recurring jobs cover it.

Onboarding is the *decision* that a Competition is ours to collect (CONTEXT.md).
Its effect is forward and permanent: once registered, every later Refresh, parse and
publish includes the Competition without being told again. One-time and idempotent.

Both entrypoints here bundle the admission with the first backfill — `orchestrate`
registers a league and then collects it — but the two acts fail differently, which is
why ADR 0031 names them apart from Backfill.
"""
