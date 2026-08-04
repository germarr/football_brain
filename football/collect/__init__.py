"""Backfill — bulk-fetch Seasons into the raw cache.

Resumable and quota-bound, and it admits nothing: a backfill cut short resumes for
free, while data in the cache that no Registry names is covered by nothing (CONTEXT.md).
Contrast `football.onboard`, which is the decision these modules carry out.

The shared cache-first helpers live in `football.fetch`, not here (ADR 0031).
"""
