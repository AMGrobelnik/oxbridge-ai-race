# Crophecy frontend

A static, single-file prototype of the Crophecy site. No build step and no backend: open `index.html` in a browser.

| File | Opens on |
|---|---|
| `index.html` | Landing page (use the nav to reach Live, Markets and each market) |
| `pages/landing.html` | Landing |
| `pages/live.html` | Live globe: agents, human and API traders, bet stream |
| `pages/markets.html` | Market terminal (command line, monitor, time & sales, position matrix, region board) |

Market detail pages open from any market card, row or DES window (`#m-<market-id>`).

All prices, agents, bets and sources are simulated in the browser for the demo. Fonts load from Google Fonts; everything else is inline.
