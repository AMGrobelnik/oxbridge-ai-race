# Crophecy frontend

A static prototype of the Crophecy site. No build step and no backend: open `index.html` in a browser.

| File | Opens on |
|---|---|
| `index.html` | Landing page (nav reaches Live, Markets, Farmers and each market) |
| `pages/landing.html` | Landing: waitlist look, katakana logo, who trades, farmer tools, Powered by Solana |
| `pages/live.html` | Live globe: 18 agents, human and API traders, bet stream, behaviour notes |
| `pages/markets.html` | Market terminal: command line, monitor, time & sales, position matrix, region board, behaviour wire |
| `waitlist/` | The waitlist page from the `waitlist` branch. Every "Join the waitlist" button links here. |

Market detail pages open from any market card, row or DES window (`#m-<market-id>`).

## Traders

- **World models (8):** Agronomist, Adaptation Optimist, Macro / Dollar, Efficient-Market Skeptic, Geopolitics Hawk, Climate Pessimist, Land-Grab Realist, Crowd Follower.
- **Trackers (5):** Commission Watcher, Farm-Lobby Tracker, Minister Monitor, Supply-Report Reader, Grain-House Tracker. They follow public signals from EU institutions, ministers, farm unions, supply reports and the big grain houses. No statements are attributed to real people.
- **Behavioural (5):** Contrarian, Anchorer, Herd Detector, Loss-Averse Hedger, Recency Chaser.
- **People:** growers, merchants, analysts and students, some hedging their own region; plus external API bots.

All prices, agents, bets, behaviour notes and sources are simulated in the browser for the demo. The farmer calculators are illustrative, not financial advice.
