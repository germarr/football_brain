# Target the 2024/25 season (`season=2024`), not the requested 2025/26

> **Status: superseded by [ADR-0003](0003-full-league-multi-season.md).**
> The account was upgraded to the Pro plan (7,500/day), which unlocks the current
> season. 2025/26 is now collected directly; 2024/25 is retained as a second
> season for comparison rather than as a substitute. Kept for history.

The project brief asks for La Liga 2025/26, but the API-Football **free plan
only serves seasons 2022–2024** (verified: requesting `season=2025` returns
`"Free plans do not have access to this season, try from 2022 to 2024."`). We
target `season=2024` (the 2024/25 campaign) — the most recent reachable season —
so all collection code runs against real, complete data (38 finished Barcelona
fixtures). Upgrading the plan later requires changing a single constant
`2024 → 2025`.

Note: seasons are identified by their **starting year**, so `2024` = the 2024/25
season. `season=2026` exists in the API but is the *2026/27* season and has no
coverage yet.
