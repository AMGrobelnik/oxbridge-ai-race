# Harvest: an agentic prediction market for European agriculture under climate stress

## ADDENDUM: required, read first

The coordinator added three requirements after the briefs went out. They bind every
workstream and win over anything below that disagrees. Where the coordinator gave only a
keyword, the architect's reading is written out; build it as written.

### R1. External agent API (owner: market-engine, in `agent/routes/harvest.py`)

Outside agents (other teams' bots, a judge's script, an LLM with HTTP tools) must be able to
join and trade over plain JSON, with no browser, on the same footing as the house world models.

- `POST /harvest/api/agents/register` `{"name", "world_model", "owner"?}` returns
  `{"agent_id": "ext:<slug>-<6 hex>", "token", "credits": 1000, "world_model_sha256"}`. The
  `world_model` text (at most 1200 chars) is hashed with `sha256_json` and appended to the bet
  ledger as a `kind="agent"` entry. This commits the agent's conviction before it trades.
- `GET /harvest/api/agents/markets` lists the open markets in a machine-friendly form (`qid`,
  `title`, `kind`, `p_yes`, `b`, `series`, `visible_history` (the masked window, see R2) and
  `closes`).
- `POST /harvest/api/agents/bet` `{"token", "qid", "side", "spend", "p_yes"?, "rationale"?}`
  uses the same code path as `/harvest/api/bet`. The trader id is the `agent_id`, so external
  agents appear on the world-model leaderboard with `origin: "external"`.
- `GET /harvest/api/agents/{agent_id}` returns positions, PnL, Brier on resolved questions,
  and the world-model hash.
- The API is documented at `GET /harvest/api/agents` (a JSON self-description listing every
  endpoint with an example body), so an LLM agent can discover it.
- It is rate limited with `agent.challenge.ratelimit.RateLimiter` at 30 requests per minute
  per IP for register and 120 for bet. All of these paths are whitelisted in `public_mode.py`
  (already done by the architect for `/harvest/api/...`).
- Acceptance: a test registers an agent, bets on an open market, sees the price move, and
  finds the agent in `/harvest/api/leaderboard` under `world_models` with `origin`
  `"external"`.

### R2. Masking, a recent sealed holdout and data-window hashes (owner: agents-replay, in `replay.py` and `forecaster.py`)

- **Masking (stops hindsight leakage from the LLM's memory).** Gemini has read the news up to
  its training cutoff, so a prompt saying "wheat, March 2022" leaks the answer. Every
  forecaster prompt is masked:
  - commodities become `Commodity A`, `B` and so on, with a per-replay seeded shuffle;
  - dates become relative months (`t-11 … t0`, horizon `t+6`), never calendar dates;
  - prices are rebased to an index (`100` at t-12);
  - soft questions are never masked, because they are only live and never scored in replay.

  The mask (`{"commodities": {"PWHEAMTUSDM": "Commodity C", ...}, "seed": int}`) is stored
  in the replay result and revealed only after resolution. `forecaster.mask_window(...)`
  and `forecaster.unmask(...)` are pure functions with unit tests. The persona text keeps
  its world model, but its examples must not name specific dated events.
- **Recent sealed holdout.** The holdout is the most recent resolvable quarters, the ones
  least likely to be in the model's training data: `HOLDOUT_FROM = "2025-09"`, so cutoffs at
  or after 2025-09 that still resolve inside the fixture. Their forecasts are generated,
  hashed and anchored (`harvest:v1:holdout:<sha>`) BEFORE `questions.resolve` is called for
  any holdout question. The code order is enforced by `replay.run` and asserted in a test that
  monkeypatches `questions.resolve` to fail if it is called before the commit exists.
- **Data-window hashes.** For every (cutoff, series) pair the replay stores
  `window_sha256 = sha256_json(points_visible_at_cutoff)`, where the points are the masked
  window the agents saw. The holdout commitment includes the list of window hashes, so a
  verifier can prove that the agents saw only data up to t0. `GET /harvest/api/verify`
  (market-engine route, calling `replay.verify_commitments`) recomputes them from the
  fixture and reports `windows_ok`.
- Acceptance:
  - a test proves that no calendar year (regex `20\d\d`) and no commodity name appears in any
    masked prompt;
  - the holdout commit precedes resolution;
  - tampering one fixture point inside a holdout window makes `windows_ok` false.

### R3. Global map and bets-flow visuals (owner: UI workstream, in `agent/routes/harvest_pages.py` and `agent/harvest/static/*`)

- **Global map.** A Leaflet map (reuse the CDN and tile setup from `/map`, `agent/verify`)
  on `/harvest` and `/harvest/board`:
  - European risk regions are circles sized by Resilience Fund allocation and coloured by
    market-implied risk;
  - Gulf buyer origins (Saudi Arabia, UAE, Qatar) are joined by animated arcs to the regions
    where they buy farmland (soft land-grab questions);
  - growing regions for each commodity are also shown (wheat: Black Sea / France; olive oil:
    Andalusia / Puglia / Crete; cocoa: Ivory Coast / Ghana; coffee: Brazil / Vietnam).

  The coordinates come from `GET /harvest/api/geo` (market-engine serves it from
  `agent/harvest/data/geo.json`, which the UI workstream owns and writes).
- **Bets-flow visuals.** These show live trades as flows:
  - every trade from `/harvest/api/state` `recent_trades` animates as a particle from the
    trader (world-model chip or human avatar) into the market's YES or NO pool;
  - a Sankey-style band runs from world models to markets, with width equal to the credits
    staked;
  - the price line steps on every trade, and the fund counter ticks up by the fee.

  The board polls `/harvest/api/state?since=<seq>` every 2 s, and `since` returns only newer
  trades (market-engine implements it).
- Acceptance: `/harvest` and `/harvest/board` render the map with at least 6 regions and at
  least 3 Gulf arcs, and a bet placed from `/harvest/play` appears as a flow on the board
  within 3 s. This is checked with a Playwright screenshot.

---

## 1. Module layout

| File | Owner |
| --- | --- |
| `agent/harvest/SPEC.md`, `types.py`, `__init__.py` | architect (frozen) |
| `agent/harvest/prices.py`, `questions.py` | data-questions |
| `agent/harvest/data/prices.json` | data-questions |
| `agent/harvest/data/questions.json` | data-questions |
| `tests/test_harvest_data.py` | data-questions |
| `agent/harvest/lmsr.py`, `market.py`, `ledger.py` | market-engine |
| `agent/harvest/scoring.py`, `fund.py` | market-engine |
| `agent/routes/harvest.py` (JSON API) | market-engine |
| `tests/test_harvest_market.py` | market-engine |
| `agent/harvest/personas.py`, `forecaster.py` | agents-replay |
| `agent/harvest/replay.py`, `__main__.py` | agents-replay |
| `agent/harvest/data/personas.json` | agents-replay |
| `agent/harvest/data/demo_replay.json` | agents-replay |
| `agent/harvest/data/llm_cache.json` | agents-replay |
| `tests/test_harvest_replay.py` | agents-replay |
| `agent/routes/harvest_pages.py`, `agent/harvest/static/*` | UI (later) |
| `agent/harvest/data/geo.json` | UI (later) |

- Banks are JSON, not YAML: the repo has no PyYAML, and JSON adds no dependency.
- Runtime state (user accounts, the live bet ledger) lives in SQLite at `HARVEST_DB` or
  `.harvest/harvest.sqlite3`, which is gitignored like `.challenge/`.
- Do not edit `agent/sealed/static|templates` or `agent/routes/sealed.py`.
- All shared shapes are TypedDicts in `agent/harvest/types.py`; import them, never redefine.

## 2. Data fixture (`data/prices.json`, type `PriceFixture`)

```json
{"v": 1, "source": "IMF primary commodity prices via FRED", "fetched_at": "2026-10-25T13:00:00Z",
 "series": {"PWHEAMTUSDM": {"id": "PWHEAMTUSDM", "name": "Wheat", "unit": "USD/mt",
   "source_url": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=PWHEAMTUSDM",
   "points": [["2015-01", 254.1], ["2015-02", 249.9]]}}}
```

- The series are PWHEAMTUSDM, PMAIZMTUSDM, POLVOILUSDM, PCOFFOTMUSDM, PCOCOUSDM, PSUGAISAUSDM,
  PSUNOUSDM and PRICENPQUSDM.
- Points run monthly from 2015-01, with months as `YYYY-MM` and missing values (`.`) dropped.
- `prices.fetch_fixture()` writes the fixture (network, run once, committed).
- `prices.load_fixture()` reads it.
- `prices.window(fixture, series_id, upto, months)` returns the last `months` points with
  month at most `upto`. This is the only way agents see data.
- `prices.value_at(fixture, series_id, month)` returns `float | None`.
- `prices.last_month(fixture)` returns the latest month present in every series.

## 3. Questions (`data/questions.json`, `questions.py`)

The bank has 12–20 hard specs and 8 soft specs, of type `QuestionSpec`.

- **Hard spec:**

  ```json
  {"id": "wheat-up10-6m", "kind": "hard", "title": "Will wheat be more than 10% higher in 6 months?",
   "series": "PWHEAMTUSDM", "rule": {"type": "pct_change_gt", "threshold": 0.10, "horizon_months": 6},
   "region": "Italy", "crop": "durum wheat", "adverse_if": "yes", "why": "one line"}
  ```

  - `rule.type` is either `pct_change_gt` (with `threshold` as a fraction, which may be negative
    for "lower" via `pct_change_lt`) or `level_gt` / `level_lt` (with `level` in USD).
  - `horizon_months` is 3 or 6.
- **Soft spec:**

  ```json
  {"id": "it-drought-2027", "kind": "soft", "title": "...", "deadline": "2027-12-31",
   "region": "Italy", "crop": "durum wheat", "adverse_if": "yes", "prior": 0.4, "why": "..."}
  ```

  Soft specs have no rule, are never auto-resolved and are for governments.
- **Instance** (`QuestionInstance`, from `questions.instantiate(spec, cutoff, fixture)`):
  - `qid = f"{spec_id}@{cutoff}"` for hard instances, and `spec_id` for soft;
  - `base_month = cutoff`, `base_value = value_at(series, cutoff)`;
  - `resolve_month = cutoff + horizon`;
  - `target` is the absolute price threshold: `base*(1+threshold)` or `level`;
  - the `title` is rendered with numbers, e.g. "Will wheat (now $331/mt) be above $364/mt by
    2022-09?".
- **Resolution** (`questions.resolve(instance, fixture) -> Resolution`) is
  `{"qid", "outcome": 1|0|None, "resolved_value", "resolve_month"}`:
  - the outcome is `None` when `resolve_month` is later than the fixture's last month (the
    market stays open live);
  - `pct_change_gt` and `level_gt` give 1 iff value > target, and `_lt` gives 1 iff
    value < target;
  - the outcome is deterministic and pure.

## 4. World-model personas (`data/personas.json`, type `Persona`)

```json
{"id": "climate-pessimist", "name": "Climate Pessimist", "color": "#d9480f",
 "world_model": "Paragraph: how society, climate and markets work in this agent's view.",
 "beliefs": ["Heat waves cause supply shocks", "..."],
 "heuristic": {"type": "momentum", "lookback": 6, "tilt": 0.10},
 "soft_lean": 0.15, "risk": 0.25, "edge": 0.03}
```

- There are exactly 8 personas:
  - climate-pessimist
  - efficient-market-skeptic
  - geopolitics-hawk
  - adaptation-optimist
  - macro-dollar
  - land-grab-realist
  - agronomist
  - crowd-follower
- `heuristic.type` is one of `momentum`, `mean_revert`, `shock`, `base_rate`, `seasonal` or
  `crowd`, and is used by the deterministic fallback.
- `soft_lean` is added to a soft question's prior when the question is adverse.
- `risk` is the fraction of bankroll per unit of edge.
- `edge` is the minimum |p - price| needed to trade.
- Every persona starts with `STARTING_CREDITS = 1000`.

## 5. Forecast and bet output

- **Forecast** (`Forecast`): `{"persona_id", "qid", "p_yes": 0.01..0.99, "rationale": "<=200
  chars", "source": "gemini"|"fallback"|"cache"}`.
- **Gemini structured output** (one call per persona per cutoff, all questions batched, JSON
  mode via `agent.ai.gemini.ask_gemini(prompt, json_output=True)`):
  `{"forecasts": [{"qid": str, "p_yes": number, "rationale": str}]}`.
  - Cache key: `sha256_json({"persona": id, "cutoff": c, "prompt_sha": sha256(prompt)})`.
  - On any error, or when `HARVEST_OFFLINE=1`, use the deterministic fallback.
- **Bet decision** (`market.decide(persona, p_yes, price) -> BetIntent | None`):
  - side YES if `p_yes - price > edge`, NO if `price - p_yes > edge`;
  - `spend = min(risk * bankroll * |p_yes - price|, 0.2 * bankroll)`.
- **Trade** (`Trade`, as returned by `market.buy`): `{"seq", "trader", "qid", "side":
  "YES"|"NO", "spend", "fee", "shares", "price_before", "price_after", "at", "cutoff"}`.
  - Trader ids are `agent:<persona_id>`, `ext:<slug>` or `user:<sha256(nick)[:12]>`.

## 6. LMSR market (`lmsr.py` pure, `market.py` stateful)

- **Pure functions** (`lmsr.py`, implemented by the architect; keep them unchanged):
  - `cost(q_yes, q_no, b)`
  - `price_yes(q_yes, q_no, b)`
  - `cost_to_buy(q_yes, q_no, b, side, shares)`
  - `shares_for_spend(q_yes, q_no, b, side, spend)`
  - `initial_q(prior, b) -> (q_yes, q_no)`, which sets the opening price
- **Book** (`market.Book`), with `DEFAULT_B = 100.0`, `FEE_RATE = 0.02` and `FUND_SHARE = 1.0`
  (every fee goes to the Resilience Fund):
  - `Book.open(qid, prior=0.5, b=DEFAULT_B) -> MarketState`
  - `Book.buy(trader, qid, side, spend, *, cutoff=None) -> Trade` debits credits, charges the
    fee to the fund and moves q; it raises `MarketError` on insufficient credits or a closed
    market
  - `Book.price(qid) -> float`
  - `Book.resolve(qid, outcome) -> list[Payout]` pays 1 credit per winning share
  - `Book.balance(trader) -> float`
  - `Book.positions(trader)`
  - `Book.snapshot() -> dict`
- The replay uses an in-memory `Book` per cutoff.
- The live server uses `market.live_book()`, which is backed by SQLite and replays the ledger
  on start.

## 7. Ledger, commitments and memos (reusing Sealed)

- **Hashing.** Hashes use `agent.sealed.hashing.sha256_json` (canonical JSON with floats
  rounded to 6 places).
- **Bet ledger** (`ledger.py`, which mirrors `agent/challenge/ledger.py`). An entry
  `LedgerEntry` is `{"seq", "prev", "kind": "join"|"agent"|"bet"|"resolve"|"fund", "trader",
  "payload", "at", "hash"}`.
  - `hash = sha256_json({"v":1, seq, prev, kind, trader, payload, at})`.
  - `GENESIS = sha256_json({"v":1,"harvest":"ledger"})`.
  - Batch anchor memo: `harvest:v1:ledger:<count>:<head>`, via `agent.sealed.chain.anchor`.
    It is anchored every `HARVEST_ANCHOR_EVERY` (default 10) entries in the background, and
    each receipt is stored.
  - User count on chain: `harvest:v1:users:<n>:<head[:16]>`, anchored when a user joins
    (batched like the ledger).
- **Commitments** (`Commitment`, produced by the replay):
  - **World models:** `harvest:v1:models:<sha256_json(personas)>`, anchored before the first
    cutoff.
  - **Holdout forecasts:** `harvest:v1:holdout:<sha256_json({"personas_sha", "forecasts":
    holdout_forecasts, "windows": window_hashes, "mask_sha"})>`, anchored before any holdout
    resolution.
  - Each commitment is `{"kind", "sha256", "memo", "payload_ref", "receipt": ChainReceipt,
    "committed_at"}`.
- An unfunded wallet gives `receipt.status = "pending"`, with the error "pending on-chain:
  ...". This is not a failure; show it as is.
- **Verify** (`replay.verify_commitments(result, fixture, *, tamper=False)`) recomputes every
  sha and compares it to the memo (and to the on-chain memo via `chain.fetch_memo` when
  confirmed). `tamper=True` flips one holdout forecast on a copy and must report a mismatch.

## 8. Replay / backtest protocol (`replay.py`)

- `CUTOFFS` are the quarter-end months from `2022-03` through `prices.last_month(fixture)`
  (rounded down to a quarter end).
- `split(cutoff)` returns `"holdout"` if `cutoff >= HOLDOUT_FROM ("2025-09")` and every hard
  question at that cutoff resolves; `"live"` for cutoffs whose questions do not all resolve
  (the latest ones, which are open markets now); and `"dev"` otherwise.
- `replay.run(*, fixture, specs, personas, offline: bool, anchor: bool) -> ReplayResult`:
  1. Commit the world models.
  2. For each cutoff in order:
     - instantiate the hard questions;
     - build the masked windows (R2);
     - forecast with each persona;
     - open a fresh `Book` (prior 0.5) and let the personas bet in a fixed round-robin order
       (persona order shuffled per cutoff with seed `hash(cutoff)`);
     - record the trades and final prices.
  3. Resolve and settle each dev cutoff immediately.
  4. Collect all holdout forecasts and window hashes, commit them, and only then resolve and
     settle the holdout.
  5. Leave the live cutoff unresolved; its final prices seed `live_book()`.
  6. Score.
- `ReplayResult` is `{"v":1, "generated_at", "model", "mask", "personas", "cutoffs":
  [CutoffResult], "commitments", "scores": Leaderboard}`.
- `CutoffResult` is `{"cutoff", "split", "questions", "windows": {series: window_sha256},
  "forecasts", "trades", "final_prices", "resolutions", "payouts"}`.
- `replay.freeze(result)` writes `data/demo_replay.json`, and `replay.load_demo()` reads it.
- CLI: `python -m agent.harvest replay [--offline] [--anchor] [--freeze]`, and
  `python -m agent.harvest fetch` (which calls `prices.fetch_fixture`).

## 9. Scoring (`scoring.py`)

- `brier(p, outcome) = (p - outcome)^2`.
- A trader's Brier is the mean over its resolved forecasts. Lower is better; 0.25 is the coin
  flip.
- PnL is the sum of payouts minus spend minus fees.
- `scoring.leaderboard(result_like) -> Leaderboard` gives
  `{"world_models": [{"persona_id", "name", "origin": "house"|"external", "brier_dev",
  "brier_holdout", "pnl_dev", "pnl_holdout", "n", "rank"}], "baselines": {"coin": 0.25,
  "market": brier_of_final_prices, "climatology": brier_of_base_rate}, "humans": [...]}`.
- Ranking is by `brier_holdout` ascending, ties broken by `pnl_holdout`.
- The headline insight is `scoring.insight(lb) -> str`, e.g. "On the sealed holdout, the
  Geopolitics Hawk world model beat the market by 0.031 Brier".

## 10. Resilience Fund (`fund.py`)

- The treasury is the sum of every fee (`FEE_RATE` of every spend, all flowing to the fund).
- **Allocation (futarchy-lite):**
  - for each open market (live hard and soft), take the adverse probability
    `risk_q = p_yes if adverse_if == "yes" else 1 - p_yes`;
  - group by `(region, crop)`: `risk = mean(risk_q)`;
  - `share = risk / sum(risk)`, and `amount = share * treasury`.
- `fund.allocate(open_markets, treasury) -> FundState` gives `{"treasury", "fee_rate",
  "allocations": [{"region", "crop", "risk", "share", "amount", "questions": [qid]}], "memo",
  "receipt"}`.
- Memo: `harvest:v1:fund:<treasury rounded to 2 places>:<sha256_json(allocations)[:32]>`.
- An optional devnet USDC settlement uses `chain.anchor(memo, amount_usdc=...)`, which stays
  pending while unfunded.

## 11. HTTP API (JSON, `agent/routes/harvest.py`; every path is public in PUBLIC_MODE)

| Method and path | Result |
| --- | --- |
| GET `/harvest/api/state?since=<seq>` | state summary |
| GET `/harvest/api/markets` | list of `MarketView` |
| GET `/harvest/api/markets/{qid}` | one market in detail |
| GET `/harvest/api/replay` | `ReplayResult` |
| GET `/harvest/api/prices` | `PriceFixture` |
| GET `/harvest/api/leaderboard` | `Leaderboard` |
| GET `/harvest/api/fund` | `FundState` |
| GET `/harvest/api/geo` | contents of `geo.json` |
| GET `/harvest/api/ledger` | entries and anchors |
| GET `/harvest/api/verify` | `{chain_ok, commitments_ok, windows_ok, details}` |
| POST `/harvest/api/verify` `{"tamper": bool}` | the same, on a tampered copy if asked |
| POST `/harvest/api/join` `{"nickname"}` | `{"user_id", "token", "credits"}` |
| GET `/harvest/api/me?token=` | balance, positions and Brier |
| POST `/harvest/api/bet` `{"token","qid","side","spend"}` | `{"trade", "market", "balance"}` |
| POST `/harvest/api/forecast` `{"token","answers":[{qid,p_yes}]}` | see below |
| GET/POST `/harvest/api/agents/*` | see R1 |

- **State summary** (`/harvest/api/state`): `{"personas", "markets": [MarketView],
  "recent_trades", "leaderboard", "fund", "users", "ledger_head", "insight"}`.
- **`MarketView`**: `{qid, title, kind, region, crop, p_yes, volume, history: [[seq, p]],
  agent_forecasts}`.
- **Join:** no signup, rate limited, and the nickname is hashed as in the challenge.
- **Bet:** only on live or soft markets.
- **Forecast** is the time-machine round on holdout questions. It returns
  `{"brier", "rank_vs_world_models", "reveal": [resolutions]}`, appends a ledger entry, and the
  user then appears in `humans`.
- Errors are `{"error": str}` with 4xx; inputs are clamped (spend 1..500 and
  p 0.01..0.99).

## 12. Pages (UI workstream, `agent/routes/harvest_pages.py`, `agent/harvest/static/`)

- **`/harvest`** is the main market and visualisation page:
  - commodity price charts with the replay cutoffs and the holdout shaded;
  - the world-model leaderboard with the insight line;
  - the market cards with price sparklines and agent chips;
  - the Resilience Fund allocation;
  - the global map (R3);
  - the proof strip (commitments, memos, explorer links, verify and tamper).
- **`/harvest/play`** is the phone page reached from the QR code:
  - nickname join;
  - the "Can you beat the world models?" time-machine round;
  - live market bets with credits.
- **`/harvest/board`** is the projector page: map, bets flow, leaderboard, user count and QR.
- **`/harvest/qr.svg`** is the QR code of `PUBLIC_BASE_URL/harvest/play`.

## 13. Proof the scoring is real (`proof.py`, `agent/routes/harvest_proof.py`)

All three read the current frozen backtest through `harvest.replay_result()` (so a re-frozen
`data/demo_replay.json` shows up without a code change). Only dev and holdout quarters count.
Intervals are the seeded 90% percentile bootstrap of `scoring`.

| Method and path | Result |
| --- | --- |
| GET `/harvest/api/replay/timeline` | `{"has_replay", "agents": [{id, name, color}], "steps": [{cutoff, split, n_questions, yes_rate, market_brier_cum, "agents": {id: {bankroll, pnl, brier, brier_cum}}}], "final": [{id, name, bankroll, brier_cum, rank_bankroll, rank_brier}], "note"}` |
| GET `/harvest/api/calibration?split=all\|dev\|holdout&bins=10` | `{"agents": [{id, name, color, n, ece, ece_lo, ece_hi, brier}]}` sorted by ECE |
| GET `/harvest/api/calibration?agent=<id>&split=&bins=` | `{"agent", "n", "bins": [{lo, hi, n, mean_p, freq, freq_lo, freq_hi}], "ece", "ece_lo", "ece_hi", "brier", "note"}`; 404 for an unknown agent |
| GET `/harvest/api/impact` | `{"summary", "forecasters", "holdout"/"all_scored": {n, market, consensus, base_rate: {brier, lo, hi, diff_vs_base, diff_lo, diff_hi, beats_base_ci}, coin}, "by_commodity": [...], "early_warning": {holdout, all_scored: {market, consensus, base_rate: {events, flagged, hit_rate, false_alarms, false_alarm_rate, lead_months_avg}}}, "payers": [{who, why}], "caveats"}` |

- **Bankroll** is 1,000 plus the agent's profit carried across quarters (each quarter is a fresh
  LMSR book), so it can go below zero.
- **Calibration agents** are the world models plus `market` (final LMSR price) and `consensus`
  (mean of the world models). ECE is the count-weighted |mean forecast - observed frequency|.
- **Impact baseline** is the historical base rate: the share of earlier questions of the same spec
  that resolved YES by the cutoff (0.5 with no history), so it uses only what was known then.
  An adverse move is the outcome named by `adverse_if`; flagged means adverse odds >= 50% when the
  question opened, so the lead is the question's horizon.
- Pages: `/app/replay` (about 30 s animation, play/pause/speed, `?at=<step>` jumps,
  `?autoplay=0` waits), `/app/impact`, and a calibration section under `/app/leaderboard`
  (`#calibration`), all in `static/app/proof.js`.
