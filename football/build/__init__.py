"""Build — model the raw cache into a store, spending no API quota.

Every module here is cache-only: `parse` rebuilds `football.db` from Layer 1, `scope`
extracts one Competition into its own file from the same cache (ADR 0011), and
`venues` maintains the Venue registry that gives a stadium the same id in every store
(ADR 0028). A cache miss raises rather than fetching.
"""
