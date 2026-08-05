"""`surfaces/` composes all three local surfaces onto one port (ADR 0035).

It contains **none of them**, and that is the point rather than an oversight. The
Viewer is `web/`, the Desk is `football_blog/desk/`, the Console is `console/`, and
all three stay exactly where ADR 0023, 0031 and 0034 put them. This package only
mounts them and lends them a header.

    uv run python -m surfaces          # then open http://127.0.0.1:8001

    /            the Viewer   — read the data
    /desk        the Desk     — write about the data
    /console     the Console  — build the data

The import points *this* way on purpose. Had `web/` mounted the Desk, the package
whose whole identity in ADR 0023 is "reads only its own stores" would have imported
`football_blog`, and with it PocketBase and the Postgres loader — exactly the spread
ADR 0034 forbade. Mounting is teaching, so the package that belongs to no context
does the mounting.

This is not the merge ADR 0023 rejected. That rejection was about a shared *database
handle* — `football.db`, dropped and rebuilt wholesale for ~13 minutes at a time.
**No surface here holds one.** The Viewer reads `serve.db`/`live.db`; the Desk reads
the Editorial and Published Stores; and the Console, since ADR 0023 stripped its
reads, holds no handle at all — its options come from `competitions.json`, it probes
the build lock and releases it immediately, and `football.db` is written by the
subprocesses it spawns, which are separate OS processes however this is hosted.

Reads no store, belongs to no context — which is the README's stated reason the
Console sits at the root, and why this does too.
"""
