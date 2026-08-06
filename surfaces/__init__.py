"""`surfaces/` — the three local surfaces and the root that composes them (ADR 0035/0036).

Each is a subpackage: `viewer/`, `desk/`, `console/`. `app.py` mounts them onto one
port and owns the header they share.

    uv run python -m surfaces          # then open http://127.0.0.1:8001

    /            the Viewer   — read the data
    /desk        the Desk     — write about the data
    /console     the Console  — build the data

They could be grouped because they own **no store between them** — that is ADR 0036's
whole argument, and the reason this is not the role-as-the-tree reorganisation ADR 0031
refused. `desk/` reaches into `football_blog` for the Editorial Store and `viewer/` into
`serving/` and `live/`; both do it with absolute imports, which announce the boundary
crossing that a relative import used to hide.

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
