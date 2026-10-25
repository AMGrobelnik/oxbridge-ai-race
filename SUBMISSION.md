# Submission answers (ready to paste)

## Title

The Crophecy: AI world models bet on Europe's food under heat

## Problem solved

Heat and drought are cutting European harvests, and nobody can say which theory of the world to trust. In summer 2026 the EU's JRC MARS bulletin reported moderate to severe yield losses across France, southern Germany, northern and central Italy and Hungary; Coldiretti says about 60% of Italian farmland is hit by drought. Olive oil rose 149% in two years (IMF series, 4,125 to 10,281 USD/t, Dec 2021 to Jan 2024) and then fell back. Meanwhile Gulf-controlled firms bank EU farm subsidies: DeSmog counts over EUR 71 million for Al Nahyan-controlled farms in Romania, Spain and Italy (May 2026, desmog.com/2026/05/06/gulf-royal-family-banks-over-e70-million-in-eu-farming-funds). Sources: brusselstimes.com/2287095, fred.stlouisfed.org/series/POLVOILUSDM, claimsjournal.com/news/national/2026/08/31/339851.htm.

Traders, governments, policymakers and lawyers all act on competing stories: "heat means supply shocks", "markets mean-revert", "irrigation and technology will offset it", "export bans drive prices". Pundits are never scored, and AI forecasters hide their assumptions. Existing prediction markets (Kalshi, Polymarket) cover US weather and not European agriculture, policy or any link to those who suffer the damage. The Crophecy scores the stories themselves, lets anyone submit their own, and points a sponsor-funded Resilience Fund at the regions the market says are most at risk.

## Project description

The Crophecy is an agentic prediction market for European agriculture under climate stress. Eight AI agents each carry an explicit WORLD MODEL: Climate Pessimist, Efficient-Market Skeptic, Geopolitics Hawk, Adaptation Optimist, Macro/Dollar, Land-Grab Realist, Agronomist and Crowd Follower. Each forecasts and bets on every question according to its world model. The leaderboard therefore ranks theories of the world, not just bots: the best world model is the one to trust.

Questions come in two kinds. HARD questions are grounded in IMF commodity prices from FRED (wheat, maize, olive oil, coffee, cocoa, sugar, sunflower oil, rice) and resolve automatically: "Will wheat be more than 10% higher in 6 months?". SOFT questions are policy and geopolitics for governments, such as "Will Italy declare a national drought emergency in 2027?" or "Will an EU state restrict farmland purchases by non-EU sovereign funds by 2028?". They stay open and show live odds.

A time machine makes it resolve today. We replay 19 quarterly cutoffs (2022Q1 to 2026Q3). At each cutoff agents see only data up to then, forecast and bet in an LMSR market, and questions resolve against the real later prices. The three most recent resolvable quarters (2025Q3 to 2026Q1) are a sealed holdout. Every agent's world model and every holdout forecast is hashed and committed to Solana devnet as a memo BEFORE resolution, as "proof of conviction" with no hindsight edits. Verify and "tamper one number" are on the page.

Humans join with a QR code, no signup, with play credits, and bet alongside the agents. The user count is chained on-chain. Bring your own world model: type a one-paragraph theory on the Theory tab; it is hashed onto the ledger before it sees a question, forecasts the sealed holdout with Gemini and is ranked against the agents and the market. Outside agents can register a world model and trade over a JSON API. Live markets keep moving: every few minutes an agent re-forecasts all open markets with Gemini and NOAA climate context (El Nino, Europe temperature anomaly) and trades, its rationale on the feed. The Resilience Fund is sponsor-funded: play-credit fees only illustrate it, and its split across regions and crops follows what the markets price as highest risk (futarchy-lite). Pages: /harvest (map, charts, leaderboard, fund), /harvest/play (phone) and /harvest/board (projector).

Real result on the frozen replay: on the sealed holdout the Crowd Follower (Brier 0.153), Adaptation Optimist (0.158) and Macro/Dollar (0.161) lead; the Climate Pessimist is last (0.218, worse than climatology at 0.216). Bootstrap error bars (90%, 48 sealed questions) put the leader within noise of the market (0.161) and only the Climate Pessimist clearly behind. We report this as it is. The devnet payer is unfunded, so memos show "pending on-chain" and go on chain with one command once it is funded.

Team: Adrian Mladenic Grobelnik, Antonio Aciob and Eashan Rautaray (Cambridge).

## Technical architecture

Python 3.12 service on the Superteam Agent Studio starter (FastAPI, Solana devnet). No new dependencies; market maths, ledger and scoring are pure stdlib.

Data (agent/harvest/prices.py): monthly IMF primary commodity prices for eight series, fetched once from FRED (fredgraph.csv, no key) into a committed fixture. Agents see data only through prices.window(series, upto, months).

Questions (questions.py): 16 hard specs (percent change or level rules over 3 or 6 months) that resolve deterministically from the fixture, and 8 soft specs (deadline, prior, region, crop) that never auto-resolve.

Market (lmsr.py, market.py): a logarithmic market scoring rule maker per question with liquidity b=100 in the replay and b=400 in the live book, play credits (1,000 each) and a 2% play-credit fee shown in the illustrative fund. Agents size bets by risk times edge; a bet is placed only when the forecast differs from the price by more than the agent's edge threshold.

Ledger (ledger.py): a hash-chained SQLite ledger of join, agent, bet, resolve and fund entries, mirrored on the challenge ledger. Batches are anchored as harvest:v1:ledger:<count>:<head>, and the user count as harvest:v1:users:<n>:<head16>. Hashing is Sealed's canonical JSON plus SHA-256.

Replay (replay.py, forecaster.py): for each cutoff, mask the window, forecast with each persona, trade round-robin in a fresh book, settle dev cutoffs at once, then commit the holdout (harvest:v1:models:<sha> before the first cutoff, harvest:v1:holdout:<sha> before any holdout resolution; the order is asserted in a test). Each (cutoff, series) window has its own SHA-256 inside the commitment, so a verifier can prove agents saw nothing after t0.

Sealed layer: reuses agent/sealed hashing and chain.anchor (pay_usdc with a memo). It never raises; an unfunded wallet gives a pending receipt.

Live forecasting (live_forecast.py, climate.py): a daemon thread re-forecasts one persona every 240 s in one batched Gemini call (60 s timeout, call cap) with NOAA climate lines in the prompt; on error the old forecasts stay. Bring your own (byo.py): POST /harvest/api/byo hashes a paragraph into a persona, forecasts the holdout cutoffs concurrently (45 s timeouts, fallbacks) and stores the score on the ledger; rate limited, cached by paragraph hash. Error bars (scoring.py): 2000 paired bootstrap resamples give each Brier and its gap to the market a 90% interval.

Fund (fund.py): risk per (region, crop) is the mean adverse probability of its open markets; share = risk / sum(risk); amount = share times treasury; memo harvest:v1:fund:<treasury>:<sha>.

Interfaces: JSON API under /harvest/api (state, markets, leaderboard, fund, geo, ledger, verify, join, bet, forecast, byo, external agents register/bet), public-mode whitelisted and rate limited. A Leaflet map shows risk regions, Gulf-to-Europe buyer arcs and trade flows. The Sealed Backtest harness at /sealed stays as the integrity layer. Tests run offline with no keys.

## AI capabilities

What the AI does: Gemini (JSON mode) is the forecaster. One call per world model per cutoff receives that agent's world-model paragraph, beliefs and the masked data window, and returns a probability and a one-line rationale for every question. The agent then bets according to its edge and risk. The same forecaster scores human-written world models (bring your own) on the sealed holdout and re-forecasts live markets with NOAA climate context (El Nino, Europe temperature anomaly). Deterministic per-model fallbacks (momentum, mean reversion, shock, base rate, seasonal, crowd) keep it running offline or if the API fails.

Which models: Gemini flash (gemini-flash-latest), cached by prompt hash so the replay is reproducible. Eight personas differ only in their declared world model.

How we checked it works: (1) Hindsight masking: the model has read the news, so prompts hide commodity names (Commodity A, B, ...), use relative months instead of calendar dates and rebase prices to an index; a test proves no year or commodity name appears in any prompt. (2) Sealed holdout: forecasts for the last three resolvable quarters are hashed and anchored before resolution, and the commit-before-resolve order is asserted in a test. (3) Scoring: Brier score and PnL per world model against baselines: coin flip 0.25, climatology 0.216 and the final market price 0.161. Over 272 resolved forecasts per agent, Crowd Follower leads the holdout at 0.153 and Climate Pessimist is last at 0.218. (4) Tamper test: changing one holdout forecast makes verification fail. (5) Offline pytest suite. (6) Bootstrap error bars: 90% intervals on each Brier and its gap to the market show no model clearly beats the market yet. Limits: few quarters and eight commodities give wide intervals; the leaderboard is evidence and not proof; LLM training data may leak, which the masking and live soft questions reduce; devnet memos are pending until the wallet is funded; play credits only, no financial advice.
