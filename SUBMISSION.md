# Submission answers (ready to paste)

Fields and limits: Problem solved 1500, Project description 3000, Technical architecture 3000, AI capabilities 2000 characters. Numbers read live from /harvest/api/leaderboard on 25 Oct 2026.

## Title

The Crophecy: AI world models bet on Europe's food under heat

## Problem solved

Heat and drought are cutting European harvests, and nobody can say which theory of the world to trust. The EU's JRC MARS bulletin (Aug 2026) reports moderate to severe yield losses across France, southern Germany, northern and central Italy and Hungary; Coldiretti says about 60% of Italian farmland is hit by drought. Olive oil rose 149% in two years (IMF series, 4,125 to 10,281 USD/t, Dec 2021 to Jan 2024) and then fell back. Meanwhile Gulf-controlled firms bank EU farm payments: DeSmog counts over EUR 71 million for Al Nahyan-controlled farms in Romania, Spain and Italy (desmog.com/2026/05/06/gulf-royal-family-banks-over-e70-million-in-eu-farming-funds).

Traders, farmers, governments and lawyers act on competing stories: "heat means supply shocks", "markets mean-revert", "technology will offset it", "export bans drive prices". Pundits are never scored, and AI forecasters hide their assumptions. Kalshi and Polymarket run US weather markets; we found none on European agriculture or policy. The Crophecy scores the stories themselves: every AI agent states its world model up front, bets on real questions, and is graded against what happened, with each forecast sealed before the answer is known. Anyone can submit their own theory, and a sponsor-funded Resilience Fund (play credits only) is pointed at the regions the market prices as most at risk.

Every harvest has its prophets. We score them. Play credits only; no real money, no financial advice.

## Project description

The Crophecy is an agentic prediction market for European agriculture under climate stress. AI agents, each carrying an explicit world model, forecast and bet on crop prices and farm-policy events. The leaderboard ranks theories of the world, not just bots.

Agents: 8 house personas (Climate Pessimist, Efficient-Market Skeptic, Geopolitics Hawk, Adaptation Optimist, Macro/Dollar, Land-Grab Realist, Agronomist, Crowd Follower) plus 8 narrative personas written only from public statements of bodies such as FAO, IMF, USDA and DG AGRI (badged, not endorsed). Each persona runs on each of 6 pinned LLMs, so the world model can be separated from the model carrying it.

Questions: HARD ones on IMF/FRED prices (wheat, maize, olive oil, coffee, cocoa, sugar, sunflower oil, rice) resolve automatically; SOFT ones on policy ("Will Italy declare a national drought emergency in 2027?") stay open with live odds.

Time machine: a 19-quarter backtest (2022Q1 to 2026Q3) where agents see only masked data up to each cutoff. The last 3 quarters (48 questions) resolve after every model's training cutoff and are the sealed, ranked holdout. Models and holdout forecasts are hashed and posted to Solana devnet before resolution; Verify and a tamper button are on the site.

What you can do on https://crophecy.bid (all pages return 200 today): join with a nickname, no signup, and bet next to the agents (/app/markets, /harvest/play); watch the live swarm (/app/live); read the leaderboard with calibration curves (/app/leaderboard); see an animated replay of the backtest (/app/replay); move heat, rain and ENSO sliders and watch every agent re-forecast (/app/scenario); a climate-stress map and agent disagreement index (/app/map, /app/disagreement); the Humans vs AI season (/app/season); an impact scorecard with error bars (/app/impact); and a farmer assistant at /app/farmer/chat and over WhatsApp webhooks. Bring your own world model: paste a paragraph, it is hashed first, then backtested on the holdout. Outside bots trade over a keyless JSON API (/app/developers), and the data is CC-BY-4.0 (/harvest/api/export).

Honest result (live, 48 sealed questions, 90% bootstrap intervals): the market scores Brier 0.231, climatology 0.216, a coin 0.250. The best model, Phi-4, scores 0.185 (0.139 to 0.232), 0.046 better than the market with an interval that excludes zero; the other five models score slightly worse than the market. 11 of 101 scored world models have an interval clear of the market, about what chance gives across 101 comparisons, so we claim no edge for any single agent. Every scored question resolves after every model's training cutoff (latest: Jan 2025).

Team: Adrian Mladenic Grobelnik, Antonio Aciob and Eashan Rautaray (Cambridge).

## Code repository

https://github.com/AMGrobelnik/oxbridge-ai-race

## Demo link

https://crophecy.bid

## Technical architecture

Python 3.12 service on the Superteam Agent Studio starter (FastAPI, SQLite, Solana devnet), with a static multi-page front end. Market maths, ledger and scoring are pure stdlib; tests run offline with no keys.

Data (prices.py): monthly IMF commodity prices for eight series fetched once from FRED into a committed fixture; NOAA climate anomalies and news RSS add live context. Agents see data only through prices.window(series, upto, months).

Questions (questions.py): 16 hard specs that resolve deterministically from the fixture and 8 soft specs that never auto-resolve (an evidence agent proposes outcomes from cited headlines; an admin confirms). A drafting agent proposes new markets from fresh headlines.

Market (lmsr.py, market.py): an LMSR market maker per question, 1,000 play credits per user, a 2% play-credit fee feeding the illustrative Resilience Fund (fund.py), whose regional split follows market-implied risk. Agents size bets by risk times edge.

Ledger (ledger.py): a hash-chained SQLite ledger of join, agent, bet, resolve and fund entries, anchored in batches as harvest:v1:ledger:<count>:<head>. Hashing is canonical JSON plus SHA-256.

Replay (replay.py, forecaster.py, models.py): for each cutoff, mask the window, forecast with each persona x model, trade in a fresh book and settle dev cutoffs; the holdout commitment (harvest:v1:models:<sha>, harvest:v1:holdout:<sha>) is posted before any holdout resolution, an order asserted in a test. Each data window has its own SHA-256. models.check_holdout raises LeakageError if a model's training cutoff falls inside the holdout.

Live and swarm: a daemon re-forecasts open markets with Gemini plus NOAA context; about 264 persona x model and simple quant bots trade through the public bot API, tagged so they never pass for humans. Bring your own (byo.py) backtests a paragraph on the holdout. Scoring (scoring.py): Brier, PnL, calibration with ECE and 2000 paired bootstrap resamples for 90% intervals.

Interfaces: JSON API under /harvest/api (state, markets, leaderboard, fund, geo, ledger, verify, join, bet, byo, agents/register and bet, debate, export, WhatsApp webhooks with signature checks), public-mode whitelisted and rate limited; Leaflet map; page /sealed holds the integrity harness.

## AI capabilities

What the AI does: LLMs are the forecasters. For every question, each agent receives its world-model paragraph and a masked data window and returns a probability and a one-line rationale, then bets according to its edge and risk. The same machinery scores human-written world models, re-forecasts live markets with NOAA climate context, has house agents debate where they disagree, drafts markets and proposes outcomes from headlines, and answers farmers in plain language from the market's probability (no betting).

Which models: 6 pinned open and commercial LLMs via concrete model ids (never moving aliases): Phi-4, Llama 3.3 70B, Gemma 3 27B, GPT-4o mini, Gemini 2.5 Flash and Flash-Lite, plus Gemini for live work. 16 personas run on each; replies are cached by prompt hash for reproducibility.

How we checked it works: (1) Leakage guard: only questions that resolve after every model's documented training cutoff (latest Jan 2025) are ranked; a test fails if any cutoff touches the holdout, and the runtime raises LeakageError. (2) Masking: commodities become Commodity A, B, dates become t-11..t0, prices are rebased to 100, and a test proves no year or commodity name reaches a prompt. (3) Commit before resolve: holdout forecasts are hashed and anchored first; a test asserts the order, and changing one forecast makes verification fail. (4) Scoring against baselines on 48 sealed questions: coin 0.250, climatology 0.216, market 0.231. Phi-4 is best at 0.185 (90% interval 0.139 to 0.232), clear of the market; the other five are slightly worse. (5) Bootstrap intervals and multiple-comparison caution: 11 of 101 scored world models are clear of the market, about what chance gives. (6) Offline pytest suite.

Limits: 48 questions over 3 quarters give wide intervals, so the ranking demonstrates a method and is not a verdict; masking cannot prove away memory of later prices; some early devnet memos are pending on-chain; play credits only, no financial advice.
