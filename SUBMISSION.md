# Submission answers (ready to paste)

## Title

Harvest: which world model is right about European farming under heat?

## Problem solved

Heat and drought are cutting European harvests, and nobody can say which theory of the world to trust. In summer 2026 the EU's JRC MARS bulletin reported moderate to severe yield losses across France, southern Germany, northern and central Italy and Hungary; Coldiretti says about 60% of Italian farmland is hit by drought. Olive oil rose 149% in two years (IMF series, 4,125 to 10,281 USD/t, Dec 2021 to Jan 2024) and then fell back. Meanwhile Gulf-controlled firms bank EU farm subsidies: DeSmog counts over EUR 71 million for Al Nahyan-controlled farms in Romania, Spain and Italy (May 2026, desmog.com/2026/05/06/gulf-royal-family-banks-over-e70-million-in-eu-farming-funds). Sources: brusselstimes.com/2287095, fred.stlouisfed.org/series/POLVOILUSDM, claimsjournal.com/news/national/2026/08/31/339851.htm.

Traders, governments, policymakers and lawyers all act on competing stories: "heat means supply shocks", "markets mean-revert", "irrigation and technology will offset it", "export bans drive prices". Pundits are never scored, and AI forecasters hide their assumptions. Existing prediction markets (Kalshi, Polymarket) cover US weather and not European agriculture, policy or any link to those who suffer the damage. Harvest scores the stories themselves and sends part of every market's volume to farmers' heat adaptation.

## Project description

Harvest is an agentic prediction market for European agriculture under climate stress. Eight AI agents each carry an explicit WORLD MODEL: Climate Pessimist, Efficient-Market Skeptic, Geopolitics Hawk, Adaptation Optimist, Macro/Dollar, Land-Grab Realist, Agronomist and Crowd Follower. Each forecasts and bets on every question according to its world model. The leaderboard therefore ranks theories of the world, not just bots: the best world model is the one to trust.

Questions come in two kinds. HARD questions are grounded in IMF commodity prices from FRED (wheat, maize, olive oil, coffee, cocoa, sugar, sunflower oil, rice) and resolve automatically: "Will wheat be more than 10% higher in 6 months?". SOFT questions are policy and geopolitics for governments, such as "Will Italy declare a national drought emergency in 2027?" or "Will an EU state restrict farmland purchases by non-EU sovereign funds by 2028?". They stay open and show live odds.

A time machine makes it resolve today. We replay 19 quarterly cutoffs (2022Q1 to 2026Q3). At each cutoff agents see only data up to then, forecast and bet in an LMSR market, and questions resolve against the real later prices. The three most recent resolvable quarters (2025Q3 to 2026Q1) are a sealed holdout. Every agent's world model and every holdout forecast is hashed and committed to Solana devnet as a memo BEFORE resolution, as "proof of conviction" with no hindsight edits. Verify and "tamper one number" are on the page.

Humans join with a QR code, no signup, with play credits, and bet alongside the agents ("can you beat the world models?"). The user count is chained on-chain. Outside agents can register a world model and trade over a JSON API. Every fee flows into a Resilience Fund, a devnet treasury earmarked for farmers' heat adaptation, whose allocation across regions and crops is weighted by what the markets predict as highest risk (futarchy-lite). Pages: /harvest (map, charts, leaderboard, fund), /harvest/play (phone) and /harvest/board (projector).

Real result on the frozen replay: on the sealed holdout the Crowd Follower (Brier 0.153), Adaptation Optimist (0.158) and Macro/Dollar (0.161) lead; the Climate Pessimist is last (0.218, worse than climatology at 0.216). We report this as it is. The devnet payer is unfunded, so memos show "pending on-chain" and go on chain with one command once it is funded.

Team: Adrian Mladenic Grobelnik, Antonio Aciob and Eashan Rautaray (Cambridge).

## Technical architecture

Python 3.12 service on the Superteam Agent Studio starter (FastAPI, Solana devnet). No new dependencies beyond the starter; the market maths, ledger and scoring are pure stdlib.

Data (agent/harvest/prices.py): monthly IMF primary commodity prices for eight series, fetched once from FRED (fredgraph.csv, no key) into a committed fixture. Agents see data only through prices.window(series, upto, months).

Questions (questions.py): 16 hard specs (percent change or level rules over 3 or 6 months) that resolve deterministically from the fixture, and 8 soft specs (deadline, prior, region, crop) that never auto-resolve.

Market (lmsr.py, market.py): a logarithmic market scoring rule maker per question with liquidity b=100, play credits (1,000 each) and a 2% fee that goes entirely to the fund. Agents size bets by risk times edge; a bet is placed only when the forecast differs from the price by more than the agent's edge threshold.

Ledger (ledger.py): a hash-chained SQLite ledger of join, agent, bet, resolve and fund entries, mirrored on the challenge ledger. Batches are anchored as harvest:v1:ledger:<count>:<head>, and the user count as harvest:v1:users:<n>:<head16>. Hashing is Sealed's canonical JSON plus SHA-256.

Replay (replay.py, forecaster.py): for each cutoff, mask the window, forecast with each persona, trade round-robin in a fresh book, settle dev cutoffs at once, then commit the holdout (harvest:v1:models:<sha> before the first cutoff, harvest:v1:holdout:<sha> before any holdout resolution; the order is asserted in a test). Each (cutoff, series) window has its own SHA-256 inside the commitment, so a verifier can prove agents saw nothing after t0.

Sealed layer: reuses agent/sealed hashing and chain.anchor (pay_usdc with a memo). It never raises; an unfunded wallet gives a pending receipt.

Fund (fund.py): risk per (region, crop) is the mean adverse probability of its open markets; share = risk / sum(risk); amount = share times treasury; memo harvest:v1:fund:<treasury>:<sha>.

Interfaces: JSON API under /harvest/api (state, markets, leaderboard, fund, geo, ledger, verify, join, bet, forecast, external agents register/bet), public-mode whitelisted and rate limited. A Leaflet map shows risk regions, Gulf-to-Europe buyer arcs and growing regions, with trade-flow animations. The Sealed Backtest harness at /sealed stays as the integrity layer. Tests run offline with no keys.

## AI capabilities

What the AI does: Gemini (JSON mode, through the starter's ask_gemini) is the forecaster. One call per world model per cutoff receives that agent's world-model paragraph, beliefs and the masked data window, and returns a probability and a one-line rationale for every question. The agent then bets according to its edge and risk. Deterministic fallbacks per world model (momentum, mean reversion, shock, base rate, seasonal, crowd) keep the market running offline or if the API fails.

Which models: Gemini flash (gemini-flash-latest) for the frozen replay, cached by prompt hash so the demo is reproducible. Eight personas differ only in their declared world model.

How we checked it works: (1) Hindsight masking: the model has read the news, so prompts hide commodity names (Commodity A, B, ...), use relative months instead of calendar dates and rebase prices to an index; a test proves no year or commodity name appears in any prompt. (2) Sealed holdout: forecasts for the last three resolvable quarters are hashed and anchored before resolution, and the commit-before-resolve order is asserted in a test. (3) Scoring: Brier score and PnL per world model against baselines: coin flip 0.25, climatology 0.216 and the final market price 0.161. Over 272 resolved forecasts per agent, the holdout ranking is Crowd Follower 0.153, Adaptation Optimist 0.158, Macro/Dollar 0.161 and Climate Pessimist last at 0.218. (4) Tamper test: changing one holdout forecast makes verification fail. (5) Offline pytest suite for data, market, replay and pages. Limits: few quarters and eight commodities give wide error bars; the leaderboard is evidence and not proof; LLM training data may leak, which the masking and live soft questions reduce; devnet memos are pending until the wallet is funded; play credits only, no financial advice.
