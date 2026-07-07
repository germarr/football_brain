Building a sports analysis app is an exciting project, and finding the right starting point for data aggregation is half the battle.
To answer your first question directly: Yes, you will likely need to look at different layers of data platforms. Consumer-facing apps (like Sofascore or FotMob) are fantastic for checking stats manually, but for building your own app, you will ultimately need back-end Data Providers (APIs).
Here is a breakdown of where to find this data as a consumer, followed by how to get it as a developer.
1. Consumer Apps (Where to see the data right now)
If you want to study how this data is visualized and presented to users, these are the gold standards:
Platform	Schedules & Rosters	Per-Game Match Stats	Interactive Heatmaps	Distance Covered (Running)
Sofascore	Premium	Premium	Highly Detailed	✗ Rarely available
FotMob	Premium	Premium (Great xG)	Available	✗ Rarely available
FBref	Premium	Deepest Free Analytics	✗ Text/Table based	✗ Only specific leagues
The "Distance Covered" Catch
You'll notice "Distance Covered" is missing or highly restricted on most general apps. Tracking tracking data (how many kilometers a player ran) requires advanced tracking camera systems (like Second Spectrum or TRACAB) installed in stadium rafters.
Because this data is incredibly expensive, major leagues (like the Premier League or UEFA Champions League) usually keep it locked down for official broadcast partners or sell it at massive premiums. When it is available to consumers, it is usually found directly on the official league apps (e.g., the official Premier League app or UEFA website) rather than third-party aggregators.
2. Developer Data Sources (Where to get the data for your app)
Since you are looking to build your own sports analysis app, you can't scrape consumer apps directly without getting blocked. Instead, you'll want to use specialized sports data APIs.
Step 1: Start Free/Cheap (Schedules, Rosters, Basic Stats)
For a beginner starting an app, these providers give you fixtures, lineups, rosters, and basic match events (goals, cards, shots) for very low cost or free tiers.
API-Football: Incredibly developer-friendly, well-documented, and highly affordable. It's the perfect sandbox to build your first schedule and roster features.
TheSportsDB: A crowd-sourced, open sports database that is great for fetching team logos, player photos, and league schedules.
Step 2: Scale Up to Advanced Stats (Heatmaps & Performance Metrics)
To get the raw coordinate data needed to generate heatmaps or advanced performance metrics (like Expected Goals / xG), you have to go to the industry giants who ingest and clean official Opta or StatsBomb feeds.
Sportradar / Opta (Stats Perform): These are the commercial data feeds behind apps like Sofascore. They provide exact pitch coordinates for every pass, tackle, and shot, allowing you to plot your own heatmaps. Warning: Commercial licenses are expensive.
StatsBomb: They offer an incredible open-data repository on GitHub with free, historical world-class event data (including coordinate maps). This is the perfect playground to learn how to map heatmaps programmatically before buying a live feed.
Want to look into how to programmatically generate heatmaps using open-source football data?

Yes