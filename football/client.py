"""Cache-first API-Football client (Layer 1 of docs/adr/0002).

Every call is keyed by endpoint + params and cached to data/raw/<endpoint>/.
The network is only touched on a cache miss, so each fixture is fetched once,
ever, and re-runs of downstream parsing cost zero API requests.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

import requests

from . import config


class QuotaExceeded(RuntimeError):
    """Raised when a live request would exceed the per-run request budget."""


class ApiError(RuntimeError):
    """Raised when the API returns a non-empty `errors` payload."""


def _is_rate_limit(errors) -> bool:
    """True if an API `errors` payload is a per-minute rate-limit rejection."""
    text = str(errors).lower()
    return "ratelimit" in text or "requests per minute" in text


def _is_daily_limit(errors) -> bool:
    """True if an API `errors` payload is the daily request-quota rejection."""
    text = str(errors).lower()
    return "for the day" in text or "daily" in text or "request limit for" in text


def _cache_key(params: dict) -> str:
    """Stable, human-readable filename from sorted params (e.g. `season=2024&team=529`)."""
    if not params:
        return "_all"
    return "&".join(f"{k}={params[k]}" for k in sorted(params))


class CachedClient:
    def __init__(self, max_live_requests: int | None = None) -> None:
        self._key = config.load_api_key()
        self._session = requests.Session()
        self._session.headers[config.KEY_HEADER] = self._key
        # Default the per-run cap to the daily limit; the API's own daily-cap
        # rejection (handled as a clean stop) is the real backstop. Pass 0 for a
        # strictly cache-only client (parse.py).
        self._max_live = config.DAILY_LIMIT if max_live_requests is None else max_live_requests
        self.live_requests = 0     # network calls made this run
        self.cache_hits = 0
        # live requests this run, broken down by endpoint — powers the Refresh
        # per-Competition/per-endpoint call accounting (refresh package, ADR 0018).
        self.live_by_endpoint: dict[str, int] = defaultdict(int)
        self._last_call_ts = 0.0

    def get(self, endpoint: str, params: dict | None = None, force: bool = False) -> dict:
        """Return the parsed JSON for `endpoint`, from cache if present.

        `force=True` bypasses a cached entry and re-fetches live, overwriting the
        cache. The nightly Refresh uses it for the two things cache-first would
        otherwise freeze — a Competition's /leagues record and its current Season's
        fixture list — and to heal a fixture whose per-match data was cached empty
        while it was still unplayed (refresh package, ADR 0018). Every other caller
        stays purely cache-first: an already-collected fixture is fetched once, ever.
        """
        params = params or {}
        cache_dir = config.RAW_DIR / endpoint.replace("/", "_")
        cache_path = cache_dir / f"{_cache_key(params)}.json"

        if cache_path.exists() and not force:
            self.cache_hits += 1
            return json.loads(cache_path.read_text())

        payload = self._fetch_live(endpoint, params)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        return payload

    def peek(self, endpoint: str, params: dict | None = None) -> dict | None:
        """The cached payload for a key, or None — without fetching or counting.

        Lets a caller inspect what is already cached (e.g. whether a fixture's events
        came back empty) before deciding whether to force a live re-fetch. Used by the
        Refresh to distinguish a stale-empty cache (fetched while a match was unplayed)
        from a legitimately empty one (refresh package, ADR 0018)."""
        params = params or {}
        cache_path = config.RAW_DIR / endpoint.replace("/", "_") / f"{_cache_key(params)}.json"
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        return None

    def _fetch_live(self, endpoint: str, params: dict, _attempt: int = 1) -> dict:
        if self.live_requests >= self._max_live:
            raise QuotaExceeded(
                f"Hit per-run budget of {self._max_live} live requests; "
                f"stopping to protect the {config.DAILY_LIMIT}/day cap. "
                "Cached data is intact — re-run to resume for free."
            )
        # rate limiting (10/min on the free plan)
        wait = config.MIN_REQUEST_INTERVAL_S - (time.monotonic() - self._last_call_ts)
        if wait > 0:
            time.sleep(wait)

        url = f"{config.BASE_URL}/{endpoint}"
        # A transient network blip or 5xx shouldn't abort a multi-thousand-call run.
        # Retry with exponential backoff (like the rate-limit path), giving up only
        # after several attempts — cached data stays intact for a resume either way.
        try:
            resp = self._session.get(url, params=params, timeout=30)
        except (requests.ConnectionError, requests.Timeout) as e:
            return self._retry_transient(endpoint, params, _attempt, e.__class__.__name__)
        self._last_call_ts = time.monotonic()
        self.live_requests += 1
        self.live_by_endpoint[endpoint] += 1
        if resp.status_code >= 500:
            self.live_requests -= 1  # a server error returned no data
            self.live_by_endpoint[endpoint] -= 1
            return self._retry_transient(endpoint, params, _attempt, f"HTTP {resp.status_code}")
        resp.raise_for_status()
        payload = resp.json()

        errors = payload.get("errors")
        if errors:  # API returns [] on success, or a dict/list of messages on failure
            # A per-minute rate-limit rejection doesn't count as data — wait out
            # the window and retry rather than aborting the whole run.
            if _is_rate_limit(errors) and _attempt <= 3:
                print(f"    rate-limited; waiting 61s then retrying (attempt {_attempt})")
                time.sleep(61)
                self.live_requests -= 1  # the rejected call returned no data
                self.live_by_endpoint[endpoint] -= 1
                return self._fetch_live(endpoint, params, _attempt + 1)
            if _is_daily_limit(errors):
                raise QuotaExceeded(
                    "Daily request limit reached. Cached data is intact — "
                    "re-run tomorrow to resume the backfill for free."
                )
            raise ApiError(f"{endpoint} {params} -> {errors}")
        return payload

    def _retry_transient(self, endpoint: str, params: dict, attempt: int, reason: str) -> dict:
        """Back off and retry a transient network/5xx failure; give up after 5 tries."""
        if attempt > 5:
            raise ApiError(f"{endpoint} {params} -> gave up after {attempt} attempts ({reason})")
        backoff = min(2 ** attempt, 30)
        print(f"    transient failure ({reason}); waiting {backoff}s then retrying "
              f"(attempt {attempt})")
        time.sleep(backoff)
        return self._fetch_live(endpoint, params, attempt + 1)

    def status(self) -> dict:
        """Live account/quota status (always a network call; not cached)."""
        return self._fetch_live("status", {})["response"]
