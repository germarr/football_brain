"""`surfaces/` composes the Viewer and the Desk onto one port (ADR 0035).

It contains **neither surface**, and that is the point rather than an oversight. The
Viewer is `web/`, the Desk is `football_blog/desk/`, and both stay exactly where ADR
0023 and ADR 0034 put them. This package only mounts them and lends them a header.

    uv run python -m surfaces          # then open http://127.0.0.1:8001

The import points *this* way on purpose. Had `web/` mounted the Desk, the package
whose whole identity in ADR 0023 is "reads only its own stores" would have imported
`football_blog`, and with it PocketBase and the Postgres loader — exactly the spread
ADR 0034 forbade. Mounting is teaching, so the package that belongs to no context
does the mounting.

This is not the merge ADR 0023 rejected. That rejection was about a shared *database
handle* — `football.db`, dropped and rebuilt wholesale for ~13 minutes at a time. The
Viewer and the Desk share no store at all: `serve.db`/`live.db` on one side, the
Editorial and Published Stores on the other. The Console keeps its own port precisely
because 0023's argument still binds it.

Reads no store, belongs to no context — which is the README's stated reason the
Console sits at the root, and why this does too.
"""
