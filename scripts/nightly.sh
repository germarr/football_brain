#!/bin/sh
# The nightly sequence, versioned (ADR 0032).
#
# The crontab holds one path — this repo's root — and nothing else:
#
#   0 4 * * * cd /home/azureuser/alt_data && \
#       flock -n /tmp/football-refresh.lock ./scripts/nightly.sh
#
# `flock` stays in the crontab because the lock guards this whole script against
# overlapping with itself, and a lock a script takes on its own behalf cannot do that.
#
# Because this file is in git, whichever branch is checked out at 04:00 runs its own
# correct version of the sequence — which is what lets module paths move (ADR 0031)
# without the crontab ever being edited again.
#
# Commands are separated by newlines, not `&&`: a failure must not stop the chain.
# Publish has to refresh serve.db even after a partial refresh that still rebuilt
# football.db (ADR 0018/0023). `refresh` keeps its stderr unredirected so a failed
# night emails via MAILTO; the rest are fully logged.
set -u

cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python

# Refuse to spend quota against a broken tree. Nothing below this line runs if a
# Registry is missing, the raw-cache symlink does not resolve, or a command module
# has gone stale (ADR 0033).
"$PY" scripts/preflight.py || exit 1

# The Venue registry path comes from the module that writes it, so the commit below
# cannot drift from where the data actually lands. This is the failure that used to be
# silent: `git add` on a moved path adds nothing, `--quiet` is then true, and the
# registry goes uncommitted for weeks without one error (ADR 0032).
VENUES=$("$PY" -c 'from football import venues; print(venues.REGISTRY_FILE)') || exit 1

"$PY" -m refresh >> refresh/logs/cron.out
"$PY" -m football.publish_pg --heal-venues >> refresh/logs/heal.out 2>&1
{ git add "$VENUES"; git diff --cached --quiet || git commit -m "venues: nightly registry append [cron]"; } >> refresh/logs/venues.out 2>&1
"$PY" -m web.publish >> web/logs/publish-cron.out 2>&1
