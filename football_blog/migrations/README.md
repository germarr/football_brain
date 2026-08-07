# PocketBase migrations for the collections this package writes

The schema for **`match_bundle`** and **`fixture_row`** — the two collections
`football_blog/bundle.py` fills and the blog reads (ADR 0044).

They are here because this is where they belong. The collections are this
package's feature: its builder writes them, its ADR argues for their shape, its
tests guard them, and its field names are the wire contract with
`blog/la-cancha`. A schema whose only definition lived in another repo was a
contract with no home.

## The thing to understand before editing one

**PocketBase does not read these files.** It reads
`personal_site/pocketbase/pb_migrations/`, and applies each once, tracked by
filename. These are copies.

That is a duplication, and this project's own rule is that duplication which
*agrees until it doesn't* is the dangerous kind — `football/status.py` exists
because six copies of four status strings agreed right up until two drifted, and
the drift made the Desk hide work with no error anywhere (ADR 0033).

So editing a file here changes **nothing**. To change the schema:

1. Edit the file in `personal_site/pocketbase/pb_migrations/` — or add a new
   `<timestamp>_updated_<collection>.js` there, which is what PocketBase's
   apply-once model actually wants for a change to a live collection.
2. Restart PocketBase so it applies.
3. Copy the result back here, and commit both repos.

## What stops these copies becoming a lie

`tests/test_pocketbase_schema.py` compares the fields declared in **these
files** against the fields the **live instance** actually has, and fails if they
disagree. It skips when PocketBase is unreachable, so it does not break an
offline run.

Checking against the live schema rather than against the other repo's copy is
deliberate, and catches strictly more: a field edited in the PocketBase admin UI
touches no migration file at all, so a file-to-file comparison would pass while
the schema had genuinely moved underneath the builder.

## Why only these two

The instance holds other collections this package writes — `match_post`,
`match_preview`, `publication`, `team_slug` — and by the same argument they
belong here too. They are not, only because they were created inside
`1785093364_created_football_collections.js`, a single migration also carrying
collections that are the personal site's. Splitting that file is a rewrite of
applied history for no functional gain. If those collections' schema ever needs
changing, the new migration is the moment to bring it here.
