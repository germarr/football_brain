"""Thin httpx wrapper around PocketBase's REST API for our specific collections.

Auth: admin superuser via /api/collections/_superusers/auth-with-password.
Token cached on the client instance for the process lifetime.

The three collections we own are `publication`, `team_slug` and `match_post` — the
**Editorial Store** (CONTEXT.md). Both of the odd names are deliberate:

- **`match_post`, not `posts`.** This instance is the personal site's backend and
  already has a `posts` collection — a generic blog post
  (title/slug/content/excerpt/cover/tags) with live records and its own Astro
  pages. A **Narrative** shares only the word "post" with it; every field here
  (`postgres_fixture_id`, `narrative_md`, `ai_drafted`, the `publication`
  relation) is ours alone. Sharing the name would have meant sharing the schema.

- **`publication`, not `league`.** A Publication is not a Competition — it is our
  editorial decision to cover one, plus how it renders. The project glossary bans
  the bare word "league" as a canonical noun because a Competition may be a *cup*,
  and the World Cup is one of the three Competitions published here: a record
  named `league` would have configured a cup. See ADR 0029.
"""
from typing import Any, Optional

import httpx

from .config import require_env, optional_env

# Collection names — change them here, not at the call sites.
POST_COLLECTION = "match_post"
PUBLICATION_COLLECTION = "publication"
#: The forward half (ADR 0040). Named for `match_post`'s reason, not a new one: the
#: co-tenanted personal site owns generic nouns here, and `preview` is generic enough
#: that it could plausibly want it later.
PREVIEW_COLLECTION = "match_preview"
#: The two read-feeds the blog used to get from Postgres (ADR 0044). They answer different
#: questions and the split is load-bearing: `match_bundle` is "render THIS Fixture", keyed
#: by Fixture id and existing only for Fixtures that have a Match Post; `fixture_row` is
#: "which Fixtures?", a narrow moving window. Neither can serve the other's question —
#: filtering `match_bundle` by date assumes a history that is deliberately not there.
BUNDLE_COLLECTION = "match_bundle"
ROW_COLLECTION = "fixture_row"


class PocketBaseClient:
    def __init__(self) -> None:
        self.base_url = optional_env("POCKETBASE_URL", "http://localhost:8090").rstrip("/")
        self.email = require_env("POCKETBASE_ADMIN_EMAIL")
        self.password = require_env("POCKETBASE_ADMIN_PASSWORD")
        self._token: Optional[str] = None
        self._client = httpx.Client(timeout=10.0)

    def _auth(self) -> str:
        if self._token is not None:
            return self._token
        r = self._client.post(
            f"{self.base_url}/api/collections/_superusers/auth-with-password",
            json={"identity": self.email, "password": self.password},
        )
        r.raise_for_status()
        self._token = r.json()["token"]
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._auth(), "Content-Type": "application/json"}

    # --- publications ---
    def list_publications(self, only_published: bool = False) -> list[dict[str, Any]]:
        params = {"perPage": 200, "sort": "postgres_competition_id"}
        if only_published:
            params["filter"] = 'published = true'
        r = self._client.get(
            f"{self.base_url}/api/collections/{PUBLICATION_COLLECTION}/records",
            headers=self._headers(),
            params=params,
        )
        r.raise_for_status()
        return r.json()["items"]

    def published_publications(self) -> dict[int, dict[str, Any]]:
        """Published Publications only, keyed by Competition id.

        The set every public-facing builder works from, and the one definition of it.
        Deliberately unlike `candidates.py`, which passes `only_published=False`: the
        Desk's board is an internal work list where the gate is irrelevant, while these
        feeds are read by the public site, where the gate is the entire point.

        Never a hardcoded competition list — ADR 0037 records `DEFAULT_LEAGUE_IDS` as a
        trap that silently drops the World Cup and Leagues Cup.
        """
        return {int(p["postgres_competition_id"]): p
                for p in self.list_publications(only_published=True)}

    def get_publication_by_competition_id(self, competition_id: int) -> Optional[dict[str, Any]]:
        r = self._client.get(
            f"{self.base_url}/api/collections/{PUBLICATION_COLLECTION}/records",
            headers=self._headers(),
            params={"filter": f"postgres_competition_id = {competition_id}", "perPage": 1},
        )
        r.raise_for_status()
        items = r.json()["items"]
        return items[0] if items else None

    def set_publication_published(self, publication_id: str, published: bool) -> None:
        r = self._client.patch(
            f"{self.base_url}/api/collections/{PUBLICATION_COLLECTION}/records/{publication_id}",
            headers=self._headers(),
            json={"published": published},
        )
        r.raise_for_status()

    # --- match_post ---
    def get_post_by_fixture_id(self, fixture_id: int) -> Optional[dict[str, Any]]:
        r = self._client.get(
            f"{self.base_url}/api/collections/{POST_COLLECTION}/records",
            headers=self._headers(),
            params={"filter": f"postgres_fixture_id = {fixture_id}", "perPage": 1},
        )
        r.raise_for_status()
        items = r.json()["items"]
        return items[0] if items else None

    def upsert_post(self, data: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_post_by_fixture_id(data["postgres_fixture_id"])
        if existing:
            r = self._client.patch(
                f"{self.base_url}/api/collections/{POST_COLLECTION}/records/{existing['id']}",
                headers=self._headers(),
                json=data,
            )
        else:
            r = self._client.post(
                f"{self.base_url}/api/collections/{POST_COLLECTION}/records",
                headers=self._headers(),
                json=data,
            )
        r.raise_for_status()
        return r.json()

    #: Fixture ids per request. The filter is a `||` chain in the query string, and
    #: PocketBase answers 400 once it grows too long — at ~87 ids in practice, which is
    #: an ordinary two-week board. Found when the Desk first rendered (ADR 0034); the
    #: sweep in `draft.find_undrafted_fixtures` passes every candidate id too, so it was
    #: reachable from the CLI as well. 80 keeps the chain near 3 KB.
    _FIXTURE_FILTER_CHUNK = 80

    def _paged(self, collection: str,
               params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """Every record matching `params`, following `totalPages` to exhaustion.

        Exhaustive rather than capped everywhere it is used, and for one reason: a
        truncated page here never raises. It returns a shorter list that every caller
        treats as the whole answer — a Fixture left in `upcoming` forever, a post left
        without a bundle, a stale Fixture Row nobody deletes.
        """
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            r = self._client.get(
                f"{self.base_url}/api/collections/{collection}/records",
                headers=self._headers(),
                params={**(params or {}), "perPage": 200, "page": page})
            r.raise_for_status()
            body = r.json()
            items.extend(body["items"])
            if page >= body["totalPages"] or not body["items"]:
                return items
            page += 1

    def _by_fixture_ids(self, collection: str,
                        fixture_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Batched lookup in any Fixture-keyed collection — Map<fixture_id, record>.

        Chunked because the filter travels in the URL. `perPage` is set to the chunk
        size, so a chunk cannot silently return a partial page: a Fixture missing from
        the result must mean "no record", never "page two".

        One implementation for every collection keyed on `postgres_fixture_id`, because
        the 80-id ceiling is a property of PocketBase's URL handling rather than of any
        one collection — a second copy would be a second place to forget it.
        """
        if not fixture_ids:
            return {}
        found: dict[int, dict[str, Any]] = {}
        for i in range(0, len(fixture_ids), self._FIXTURE_FILTER_CHUNK):
            chunk = fixture_ids[i:i + self._FIXTURE_FILTER_CHUNK]
            filter_expr = " || ".join(f"postgres_fixture_id = {fid}" for fid in chunk)
            r = self._client.get(
                f"{self.base_url}/api/collections/{collection}/records",
                headers=self._headers(),
                params={"filter": filter_expr, "perPage": len(chunk)},
            )
            r.raise_for_status()
            found.update({p["postgres_fixture_id"]: p for p in r.json()["items"]})
        return found

    def list_posts_by_fixture_ids(self, fixture_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Every Match Post for these Fixtures, by Fixture id."""
        return self._by_fixture_ids(POST_COLLECTION, fixture_ids)

    def list_posts(self) -> list[dict[str, Any]]:
        """Every Match Post, paged to exhaustion.

        Paged rather than capped for `list_previews`' reason: a cap would not error, it
        would silently leave the oldest posts out of whatever the caller is building — and
        a post page that renders nothing looks like a content problem, not a short page.
        """
        return self._paged(POST_COLLECTION, {"sort": "postgres_fixture_id"})

    # --- match_preview ---
    def list_previews(self, *, lifecycle: Optional[str] = None,
                      extra_filter: str = "") -> list[dict[str, Any]]:
        """Every Match Preview, optionally narrowed by lifecycle.

        Paged to exhaustion rather than capped: the builder decides what to freeze from
        this list, and a truncated page would silently leave kicked-off Fixtures sitting
        in `upcoming` forever.
        """
        clauses = [c for c in (f'lifecycle = "{lifecycle}"' if lifecycle else "",
                               extra_filter) if c]
        params: dict[str, Any] = {"sort": "kickoff_utc"}
        if clauses:
            params["filter"] = " && ".join(clauses)
        return self._paged(PREVIEW_COLLECTION, params)

    def list_previews_by_fixture_ids(self, fixture_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Map<fixture_id, preview record>."""
        return self._by_fixture_ids(PREVIEW_COLLECTION, fixture_ids)

    def upsert_preview(self, data: dict[str, Any],
                       existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Create or replace one Match Preview, keyed on its Fixture id.

        Unlike `upsert_post` there is no refuse-to-overwrite guard, and that asymmetry is
        the point: a Match Preview is **derived**, so overwriting it costs one rebuild,
        while a published Match Post carries hand-edited prose nothing regenerates
        (ADR 0029/0040). The guard that matters here is the *caller's* — a `settled`
        record must never be passed to this method.
        """
        found = existing if existing is not None else \
            self.list_previews_by_fixture_ids([data["postgres_fixture_id"]]).get(
                data["postgres_fixture_id"])
        if found:
            r = self._client.patch(
                f"{self.base_url}/api/collections/{PREVIEW_COLLECTION}/records/{found['id']}",
                headers=self._headers(), json=data)
        else:
            r = self._client.post(
                f"{self.base_url}/api/collections/{PREVIEW_COLLECTION}/records",
                headers=self._headers(), json=data)
        r.raise_for_status()
        return r.json()

    def settle_preview(self, record_id: str) -> None:
        """Freeze a Match Preview at kickoff — the lifecycle flip, and nothing else.

        Deliberately patches only `lifecycle`. The record's last **Quote** was read before
        kickoff and nothing reconstructs it, so the freeze must not be an opportunity to
        rewrite the market half with whatever the exchange says now (ADR 0040).
        """
        r = self._client.patch(
            f"{self.base_url}/api/collections/{PREVIEW_COLLECTION}/records/{record_id}",
            headers=self._headers(), json={"lifecycle": "settled"})
        r.raise_for_status()

    # --- match_bundle / fixture_row (ADR 0044) ---
    def list_bundles_by_fixture_ids(self, fixture_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Map<fixture_id, bundle record>.

        Deliberately the only lookup this collection offers. There is no `list_bundles()`
        and no date-filtered variant, because a Match Bundle exists only for a Fixture
        that has a Match Post — a query that asked this collection "which Fixtures?" would
        get a silently partial answer. That question belongs to `fixture_row`.
        """
        return self._by_fixture_ids(BUNDLE_COLLECTION, fixture_ids)

    def upsert_bundle(self, data: dict[str, Any],
                      existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Create or replace one Match Bundle, keyed on its Fixture id.

        No overwrite guard, for `upsert_preview`'s reason: a Match Bundle is **derived**,
        so overwriting one costs a rebuild while a published Match Post carries
        hand-edited prose nothing regenerates. Unlike a preview there is not even a
        settled state to respect — a bundle is history, and history stays derivable, so
        an upstream correction *should* flow through on the next run.
        """
        found = existing if existing is not None else \
            self.list_bundles_by_fixture_ids([data["postgres_fixture_id"]]).get(
                data["postgres_fixture_id"])
        if found:
            r = self._client.patch(
                f"{self.base_url}/api/collections/{BUNDLE_COLLECTION}/records/{found['id']}",
                headers=self._headers(), json=data)
        else:
            r = self._client.post(
                f"{self.base_url}/api/collections/{BUNDLE_COLLECTION}/records",
                headers=self._headers(), json=data)
        r.raise_for_status()
        return r.json()

    def list_rows(self) -> list[dict[str, Any]]:
        """Every Fixture Row currently stored.

        Unfiltered on purpose: the caller's job is to diff this against the window it just
        built and delete the difference. A capped page would hide exactly the rows that
        need deleting — the ones nobody is writing any more — and the collection would
        grow without bound while looking correct.
        """
        return self._paged(ROW_COLLECTION, {"sort": "kickoff_utc"})

    def upsert_row(self, data: dict[str, Any],
                   existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Create or replace one Fixture Row, keyed on its Fixture id."""
        found = existing if existing is not None else \
            self._by_fixture_ids(ROW_COLLECTION, [data["postgres_fixture_id"]]).get(
                data["postgres_fixture_id"])
        if found:
            r = self._client.patch(
                f"{self.base_url}/api/collections/{ROW_COLLECTION}/records/{found['id']}",
                headers=self._headers(), json=data)
        else:
            r = self._client.post(
                f"{self.base_url}/api/collections/{ROW_COLLECTION}/records",
                headers=self._headers(), json=data)
        r.raise_for_status()
        return r.json()

    def delete_row(self, record_id: str) -> None:
        """Drop one Fixture Row that has left the window.

        The only delete this client performs anywhere. It is safe precisely because a
        Fixture Row carries nothing authored and nothing that is not re-derivable from
        the Published Store — which is why the same method does not exist for Match Posts.
        """
        r = self._client.delete(
            f"{self.base_url}/api/collections/{ROW_COLLECTION}/records/{record_id}",
            headers=self._headers())
        r.raise_for_status()

    # --- team_slug ---
    def upsert_team_slug(self, postgres_team_id: int, slug: str, display_name: str) -> dict[str, Any]:
        r = self._client.get(
            f"{self.base_url}/api/collections/team_slug/records",
            headers=self._headers(),
            params={"filter": f"postgres_team_id = {postgres_team_id}", "perPage": 1},
        )
        r.raise_for_status()
        items = r.json()["items"]
        payload = {"postgres_team_id": postgres_team_id, "slug": slug, "display_name": display_name}
        if items:
            r = self._client.patch(
                f"{self.base_url}/api/collections/team_slug/records/{items[0]['id']}",
                headers=self._headers(), json=payload,
            )
        else:
            r = self._client.post(
                f"{self.base_url}/api/collections/team_slug/records",
                headers=self._headers(), json=payload,
            )
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()
