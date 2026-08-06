"""The Desk — the local surface for deciding what to write about (ADR 0034).

    uv run python -m surfaces          # the Desk at http://127.0.0.1:8001/desk

Third of the three local applications, and the only one that reaches the **Editorial
Store**: the Operator Console (`console`, :8000) runs the jobs that build the stores,
the Viewer (`web`) reads the serving copy, and the Desk lists **Drafting Candidates**,
shows the prompt the model will be given, and fires the pipeline.

Since ADR 0035 the Desk and the Viewer answer on one port, mounted by `surfaces/` at
`/desk` and `/` — they share no store, so the split ADR 0023 enforced between the
Console and the Viewer does not apply between these two. Still three applications;
two of them now behind one address. `python -m surfaces.desk` remains as a debug
entrypoint, and every template path here is built from `base_url(request)` so both
modes serve correctly.

It lives inside `football_blog/` rather than beside `console/` and `web/` because
context is the tree and role is declared in the registry (ADR 0031): this is
Control-role code inside a Publish-role package, which is the arrangement that ADR
intends rather than an exception to it. Everything here imports `football_blog`.

It is a **launcher, not an editor**. It stops at the run's log and links out to the
PocketBase admin. Reviewing a **Narrative**, editing it and publishing it stay there,
because publishing is a separate deliberate act and the separation is what makes it one.
"""
