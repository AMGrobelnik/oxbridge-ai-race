# Harvest: an agentic prediction market for European agriculture under climate stress

Oxford and Cambridge AI Race, Build track, 25 Oct 2026. Team: Adrian Mladenic Grobelnik, Antonio Aciob and Eashan Rautaray (Cambridge). Python, FastAPI and Solana devnet on the Superteam Agent Studio starter. Pages: `/harvest` (map, charts, world-model leaderboard, Resilience Fund), `/harvest/play` (phone, reached by QR code) and `/harvest/board` (projector). Sealed Backtest (below, `/sealed`) stays underneath as the integrity layer.

## Why

Heat and drought are cutting European yields: the JRC MARS bulletin of Aug 2026 reports moderate to severe losses across France, southern Germany, northern and central Italy and Hungary, and Coldiretti says about 60% of Italian farmland is hit. Olive oil rose 149% between Dec 2021 and Jan 2024 (IMF series POLVOILUSDM, 4,125 to 10,281 USD/t) and then fell back. Gulf-controlled firms collect EU farm payments: DeSmog counts over EUR 71 million for Al Nahyan-controlled farms in Romania, Spain and Italy ([DeSmog, 6 May 2026](https://www.desmog.com/2026/05/06/gulf-royal-family-banks-over-e70-million-in-eu-farming-funds/)). Kalshi and Polymarket run US weather markets, but we found none on European agriculture or policy.

## What it is

- **Agents are world models.** Eight agents each carry an explicit conviction about how society and markets work: Climate Pessimist, Efficient-Market Skeptic, Geopolitics Hawk, Adaptation Optimist, Macro/Dollar, Land-Grab Realist, Agronomist and Crowd Follower. Each forecasts (Gemini, with a deterministic fallback) and bets in an LMSR market. The best performers show which world model is right, which matters to traders and to governments, policymakers and lawyers.
- **Hard questions** (16 specs) are grounded in IMF commodity prices from FRED (wheat, maize, olive oil, coffee, cocoa, sugar, sunflower oil, rice) and resolve automatically. **Soft questions** (8 specs) are policy and geopolitics ("Will Italy declare a national drought emergency in 2027?"); they stay open with live odds.
- **Time machine.** 19 quarterly cutoffs (2022Q1 to 2026Q3): agents see a masked window of data up to the cutoff only, then questions resolve against the real later prices. Prompts hide commodity names and calendar dates to stop hindsight leakage from the LLM's memory.
- **Proof of conviction.** World models, and the forecasts of the sealed holdout (2025Q3 to 2026Q1), are hashed and posted as Solana devnet memos (`harvest:v1:models:<sha>`, `harvest:v1:holdout:<sha>`) before resolution. Per-window data hashes prove agents saw nothing later. Verify and tamper work on `/harvest`.
- **Humans and outside agents.** Anyone joins via QR with no signup and play credits, bets next to the agents and gets a leaderboard; the user count is chained (`harvest:v1:users:<n>:<head>`). Outside agents register a world model and trade through `/harvest/api/agents/*`. Bets form a hash-chained ledger anchored by memo.
- **Resilience Fund.** Every fee (2% of each bet) goes into a devnet treasury earmarked for farmers' heat adaptation. Its split across regions and crops follows market-implied risk (futarchy-lite), shown on the page and anchored (`harvest:v1:fund:...`).

## Result on the frozen replay

Brier score on the sealed holdout, lower is better (coin flip 0.25, climatology 0.216, final market price 0.161): Crowd Follower 0.153, Adaptation Optimist 0.158, Macro/Dollar 0.161, Efficient-Market Skeptic 0.168, Geopolitics Hawk 0.176, Land-Grab Realist 0.184, Agronomist 0.201, Climate Pessimist 0.218. Each agent made 272 resolved forecasts. The honest reading: "heat means price spikes" did not beat the market, and the differences are small with few quarters.

## Run

```bash
python -m agent serve                          # open http://localhost:3000/harvest
python -m agent.harvest replay --offline       # deterministic replay without Gemini
python -m agent.harvest replay --anchor        # commit memos (pending until the wallet is funded)
PYTHONPATH=. .venv/bin/python -m pytest -q tests   # 433 tests, offline, no keys
```

Code lives in `agent/harvest/` (spec in `SPEC.md`), `agent/routes/harvest.py` and `agent/routes/harvest_pages.py`. Limits: the devnet wallet is unfunded, so memos show "pending on-chain"; few quarters give wide error bars; LLMs may remember post-cutoff prices, which masking and the live soft questions reduce; play credits only, not financial or policy advice. Sources: [JRC MARS coverage](https://www.brusselstimes.com/2287095/drought-extreme-heat-cut-summer-crop-yields-across-western-and-central-europe), [IMF olive oil series on FRED](https://fred.stlouisfed.org/series/POLVOILUSDM), [Kalshi weather volume](https://www.claimsjournal.com/news/national/2026/08/31/339851.htm).

---

# Sealed Backtest (the integrity layer): AI quant-research agents whose success criteria are sealed on Solana before they start, so they can't fool themselves

Oxford and Cambridge AI Race, Build track, 25 Oct 2026. Team: Adrian Mladenic Grobelnik, Antonio Aciob and Eashan Rautaray (Cambridge). A Python agent built on the Superteam Agent Studio starter (FastAPI and Solana devnet). Sealed Backtest lives at `/sealed`. The starter's AI Race adapter and its six task handlers (`/agent-studio/v1/*`, `/race`) run unchanged in the same service.

## Problem

AI research agents grade their own homework. A quant agent that iterates against a backtest it can see will find the parameters that fit that history, and the best-looking result is usually the most overfit: the bug that looks like a discovery. Nobody can tell afterwards whether the test was moved, the data was cherry-picked or the metric was swapped mid-run, and the agent's own logs can't settle it because the agent wrote them. Onchain finance will only hand real money to AI agents that come with guardrails: limited autonomy, human sign-off on big actions and an onchain audit trail.

## What it does

Sealed Backtest is a research agent with an audit trail. The success criteria are agreed in advance and sealed so any later edit is detectable.

**The headline demo: SOL/USDC strategy research on real Kraken data.** An LLM (Gemini) proposes trading rules; the harness scores them on dev data only. A grid-tuned dip-buyer scores **103.3 on dev but 91.0 on the sealed 2026 holdout** and is rejected as `OVERFIT`. A slow trend filter with staking passes (**91.0 on dev, 95.6 on the holdout**) and is selected. The harness catches the bug that looks like a discovery.

**Same harness, any scored environment.** An environment is a plug-in of about three files. Three ship today:

| Plug-in | Goal |
| --- | --- |
| `strategy` (headline) | SOL/USDC/staking allocation rule that maximises risk-adjusted value after fees and slippage, on real Kraken daily data |
| `tokenomics` | Token launch design (vesting, emissions, float, pool depth, buybacks) that stays stable for 18 months without draining the treasury |
| `epidemic` | Age-structured SEIRV winter-epidemic policy (hospital-days, budget, school closures) |

## How it works

1. **Seal.** Before iteration 1 it hashes the locked evaluation code (`sim.py` and `eval.py`), the dev seeds, and a salted commitment to the held-out data, seeds and scenario shifts. The hash goes on Solana devnet as a memo, and the block timestamp proves the test existed, unchanged, before the first experiment.
2. **Search.** The proposer states one falsifiable hypothesis per iteration and submits a full candidate, which is JSON validated against a schema, never code. The harness scores it on the dev data only and keeps it if it beats the champion; otherwise it reverts. The proposer's context has no holdout field.
3. **Reveal.** The salt is revealed and every kept candidate is re-scored on the sealed holdout. A fixed gate, which is part of the sealed code, labels each candidate `GENERALISES` or `OVERFIT`. The best-scoring candidate that generalises is selected. A second memo anchors the result hash and the verdict.
4. **Verify.** Anyone can download the run bundle and re-hash it against the sealed record and the on-chain memos. Change one number in one candidate and the check turns red.

## Guardrails

- **Per-round onchain hash log.** Every iteration, the baseline included, is one link of a hash chain that starts at the preregistration commitment, and each round's hash is posted as a Solana memo (`sealed:v1:round:<run_id>:<index>:<hash>`). No round can be edited, dropped or reordered without breaking every later link; `verify` reports the first broken round.
- **Human co-sign above a payment cap.** Any agent payment over `COSIGN_CAP_USD` (default 0.50 devnet USDC) is not sent. It is queued as an approval, the agent gets "awaiting human co-sign", and a human approves or rejects it through `/api/cosign/*`. State and an append-only `audit.jsonl` live in `.cosign/`. A co-signed payment still counts against the total budget.
- **Limited autonomy.** Per-payment and total spending caps from the starter stay in force, and the LLM never grades its own work: keep/revert comes from the locked simulator, the verdict from the sealed holdout and a fixed gate.
- **Honest on-chain state.** With an unfunded wallet, every memo shows "pending on-chain" and goes on chain once the wallet is funded (`sealed anchor`).

Simulation and backtest evidence, not financial or policy advice.

## Architecture

```
agent/sealed/
  harness.py      seal -> baseline -> N x (propose, validate, dev score, KEEP/REVERT/INVALID) -> reveal -> anchor
  proposer.py     GeminiProposer (one JSON-mode call per iteration, retries transient errors) | ScriptedProposer (replay)
  prereg.py       code hash, salted holdout commitment, commitment, result hash, memos, verify_bundle
  audit.py        per-round hash chain bound to the commitment
  chain.py        devnet memo anchor via the starter's pay_usdc(memo=...); never raises, "pending on-chain" when unfunded
  gate.py         holdout gate: relative gap limit + "no shift worse than the naive baseline"
  hashing.py      canonical JSON (sorted keys, 6-dp floats, 33.0 == 33) + SHA-256
  store.py        run records as JSON, atomic writes; frozen demo in demo/
  environment.py  plug-in loader: sim.py, eval.py, holdout.json, handbook.md, demo_trajectory.json
  envs/           strategy (Kraken SOL/USDC), tokenomics, epidemic (SEIRV); pure stdlib
  static/         the demo page (plain HTML/CSS/JS, no build step)
agent/payments/cosign.py  human co-sign queue and audit log
agent/routes/sealed.py    HTTP API under /sealed/api
agent/routes/cosign.py    HTTP API under /api/cosign
agent/commands/sealed.py  python -m agent sealed run|anchor|verify
```

| Endpoint | What it does |
| --- | --- |
| `GET /sealed` | Demo page: replays the frozen run (seal, search, reveal, verify) in about 45 s |
| `POST /sealed/api/runs` | Starts a live run, `{"env": "strategy", "iterations": 8, "proposer": "gemini", "anchor": true}`, and returns 202 `{"run_id"}` |
| `GET /sealed/api/runs/{id}` | The run record as it grows; `/bundle` downloads it |
| `POST /sealed/api/runs/{id}/anchor` | Retries pending devnet memos once the wallet is funded |
| `POST /sealed/api/verify` | `{"record": <bundle>}` returns the re-hash report, including the audit chain, fetching on-chain memos when confirmed |
| `GET /sealed/api/demo`, `/sealed/api/envs` | The frozen demo run and the plug-ins |
| `GET /api/cosign/pending`, `POST /api/cosign/{id}/approve` or `/reject`, `GET /api/cosign/audit` | The human co-sign queue |

Hashes and memos (SPEC: `agent/sealed/SPEC.md`):
- `commitment = sha256({v, run_id, env, code_sha256, dev_seeds, holdout_commitment})`, where `holdout_commitment = sha256({salt, holdout})`.
- The commit memo is `sealed:v1:commit:<run_id>:<commitment>`.
- The reveal memo is `sealed:v1:reveal:<run_id>:<salt>:<result_sha256>:<verdict>:<selected>`.

## AI capabilities

- **An LLM as the research proposer.** Gemini (`agent/ai/gemini.py`, JSON mode) reads the goal, the domain handbook, the candidate's JSON schema, the champion with its dev score breakdown and the last six experiments. It returns a one-line falsifiable hypothesis, a strategy (`continue`, `pivot` or `widen`) and a full candidate. It is told to change one idea at a time.
- **An external oracle, not an LLM judge.** Keep and revert decisions use the locked score on dev data. The final verdict uses the sealed holdout and a fixed gate.
- **Robustness.** Proposals are data validated against a JSON schema, never code. Each evaluation has a 10 s timeout. A bad, over-budget or failed proposal becomes an `INVALID` iteration and the loop carries on.
- **The same service also answers the AI Race tasks:** respond, extract, use_tools, answer_from_source, converse and run_workflow (`agent/race/`).

## How it was verified

**End to end through the HTTP API on the live service** (`POST /sealed/api/runs`, proposer `gemini`, 8 iterations, anchoring on), shown on the epidemic plug-in. That run is `20261025T125119Z-epidemic-d428` and is the frozen demo in `agent/sealed/demo/epidemic_demo.json`.

- **Sealed first.** The commitment `26cea50b…7f530a` was sealed at 12:51:19 UTC, before the baseline (12:51:20) and iteration 1 (12:51:35). The devnet memo is honestly marked "pending on-chain: agent wallet not funded".
- **The loop improves on dev, and overfitting is caught.** The dev score rose from 17.59 to 25.68 (+46%), the holdout rejected 6 of the 8 kept candidates as `OVERFIT`, including the dev champion (25.68 on dev, 17.73 on holdout), and a candidate that holds above the naive baseline under every shift was selected.
- **Verify.** `POST /sealed/api/verify` on the downloaded bundle gives code, commitment and result hash all matching. On the page, "Tamper one number" changes one candidate field and only the result hash fails.
- **Strategy headline.** The dip-buyer (103.3 dev, 91.0 holdout) is rejected as `OVERFIT`; the trend filter with staking (91.0, 95.6) is selected.
- **Tests.** `PYTHONPATH=. .venv/bin/python -m pytest -q tests` runs offline with no keys: harness, prereg and verify, audit chain, co-sign, all three plug-ins, routes and the starter and race tests.
- **The page in a real browser.** Chromium via Playwright, at 1280 and 390 px, light and dark, with no console errors and no horizontal scroll.

**Known limits:**
- Until the wallet is funded the memos are pending and verification compares against the recorded commitment only. `python -m agent sealed anchor <run_id> --freeze` upgrades a run once it is funded.
- The simulators are calibrated stand-ins, and a backtest is not a forecast.
- The holdout file ships in the repo, so the seal proves the test did not change during the run, not that the author never looked at it.

## Run Sealed

```bash
python -m agent serve                     # then open http://localhost:3000/sealed (PORT in .env)
python -m agent sealed run --env strategy --proposer gemini --iterations 8          # live run in the terminal
python -m agent sealed run --env strategy --proposer scripted --iterations 8 --no-anchor   # offline replay
python -m agent sealed anchor <run_id> --freeze   # retry pending memos (after funding) and refresh the demo
python -m agent sealed verify <run_id>            # re-hash a stored run against its memos
```

For real on-chain memos, fund the agent wallet (`python -m agent wallets` shows it) with devnet SOL and USDC, then run `sealed anchor`.


---

# Running the agent (Superteam Agent Studio starter)

A Python autonomous agent generated by **Superteam Agent Studio**: a command line (`python -m agent`) plus a small HTTP service built with [FastAPI](https://fastapi.tiangolo.com). You don't need any blockchain experience to run it.

## Quick start

You need [Python](https://www.python.org/downloads/) 3.10 or newer (3.12 recommended). Check with `python3 --version`.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in the values described below
python -m agent                    # lists every command and feature
python -m agent serve --reload     # HTTP service on http://localhost:3000
```

Open http://localhost:3000 (or `curl http://localhost:3000`). It answers with JSON listing every feature in this project. http://localhost:3000/docs shows every endpoint.

| Command | What it does |
| --- | --- |
| `python -m agent` | Lists the agent's commands and features |
| `python -m agent serve --reload` | Runs the HTTP service and restarts it when you save a file |
| `python -m agent serve` | Runs the HTTP service (`PORT`, default 3000; `HOST`, default `127.0.0.1`, set `0.0.0.0` to deploy) |
| `python -m unittest discover -s tests -v` | Runs the tests (offline, no keys needed) |
| `pip install -r requirements-dev.txt && pyright` | Checks types (optional) |

Always activate the virtual environment first (`source .venv/bin/activate`) in every new terminal.

## Keeping secrets secret

- Put keys only in **`.env`**. It is in `.gitignore`, so git won't commit it. The agent loads it on start; variables already set in your environment win.
- Never send a key back in a response, and never print or log it.
- If a key ever ends up in git or a screenshot, revoke it and make a new one.

## Project layout

```
agent/__main__.py   the command line (python -m agent); loads every file in agent/commands
agent/server.py     the HTTP service; loads every file in agent/routes
agent/commands/     one file per command
agent/routes/       one file per group of endpoints
agent/…             helpers for each feature
tests/              offline tests
studio.manifest.json   what Agent Studio generated, for reference
```

To add your own endpoint, create `agent/routes/hello.py`:

```python
from fastapi import FastAPI


def register(app: FastAPI) -> None:
    @app.get("/api/hello")
    async def hello() -> dict[str, str]:
        return {"hello": "world"}
```

To add your own command, create `agent/commands/hello.py` (see `agent/commands/__init__.py`). Then `python -m agent hello`.

---

## Internet-native payments (x402 on Solana devnet)

**What it is:** [x402](https://www.x402.org) brings back the HTTP status code `402 Payment Required`. A server answers "this costs $0.01"; the caller pays and repeats the request; the server checks the payment and responds. No sign-ups, cards or API keys. Programs like your agent can pay for things on their own.

**No real money.** This template only works on **Solana devnet**, a test network where tokens are free and worthless. It refuses any other network on purpose.

### Words you'll see

| Word | Meaning |
| --- | --- |
| Wallet / keypair | A public **address** (safe to share, like an account number) plus a **secret key** (like a password; never share it). |
| Devnet | Solana's test network. Free test tokens, no value. |
| USDC | A token worth $1 on mainnet. Here you use **devnet USDC**, which is free test money. |
| Facilitator | A service that checks a payment and submits it to Solana for you. This agent uses the free test facilitator at `https://x402.org/facilitator`. It also pays the small network fee, so you don't need SOL. |
| `PAYMENT-REQUIRED` / `PAYMENT-SIGNATURE` / `PAYMENT-RESPONSE` | The HTTP headers x402 V2 uses for the price, the signed payment and the receipt. |

### Setup

1. **Where payments arrive (seller).** Run `python -m agent keygen --pay-to`. This puts a fresh devnet address in `PAY_TO_ADDRESS` in `.env`. (Or paste your own devnet address from a wallet app such as Phantom or Solflare.)
2. **Who pays (the agent).** Run `python -m agent keygen`. This creates a throwaway devnet key in `AGENT_PRIVATE_KEY` and prints its address.
3. **Get free test USDC.** Go to https://faucet.circle.com, choose **Solana Devnet**, and request USDC for **both** addresses: the agent (so it can pay) *and* the seller (so it has a USDC account to receive into; x402 does not create one for you).
4. **Check:** `python -m agent wallets` should show ✓ for both.
5. Restart the service.

> ⚠️ `AGENT_PRIVATE_KEY` is a secret. Use a throwaway key made for this project, never a wallet that holds real money.

**Code:** `agent/payments/server.py` (charge for an endpoint with `@paid_route(...)`, built on the [`x402`](https://pypi.org/project/x402/) package's FastAPI middleware), `agent/payments/client.py` (an `httpx` client that pays automatically), `agent/payments/config.py` (devnet-only settings).

**Paying an address directly (the AI Race's on-chain payment stage):** `agent/payments/transfer.py` has `await pay_usdc(load_agent_keypair(), to=..., amount_usdc=..., memo=...)`. It sends devnet USDC from the agent wallet to any address, with an optional memo, creates the recipient's USDC account if needed, and waits until devnet confirms. It returns the signature. Pass the spending module's tracker as `guard=` to keep it within your budget. The agent wallet needs devnet SOL for fees (faucet.solana.com) and devnet USDC (faucet.circle.com); if it doesn't, the error says which.

**Charge for your own endpoint** in any file in `agent/routes/`:

```python
from fastapi import FastAPI

from agent.payments.server import PaidRouteOptions, paid_route


def register(app: FastAPI) -> None:
    @paid_route(app, "GET", "/my-data", lambda: PaidRouteOptions(price="$0.02", description="My data"))
    async def my_data() -> dict[str, str]:
        return {"secret": "only for paying callers"}
```

`paid_route` adds the endpoint and its price together, so they can't drift apart. `{id}` style path parameters are fine. The payment is only settled if your handler answers with a status below 400.

---

## Spending controls

An agent that pays automatically needs limits. Before every payment the agent checks:

- **Per-payment cap** (`MAX_PAYMENT_USD`, default `0.10`): anything more expensive is refused before it is signed.
- **Total budget** (`AGENT_BUDGET_USD`, default `1.00`): once this much has been spent, further payments are refused.
- **Only devnet USDC**: prices in any other token are refused.

A payment counts against the budget as soon as it is signed. It is handed back if the seller rejects it. If the network fails halfway, it stays counted, so the budget is never under-counted. The running total is kept in memory and **resets when the agent restarts**. Save it to a database if you need it to persist.

**Code:** `agent/spending/policy.py` (reads the limits), `agent/spending/tracker.py` (the running total), `agent/spending/attach.py` (plugs the limits into the x402 client, alongside x402's own built-in per-payment cap).

**Test it (no wallet needed):** `python -m unittest discover -s tests -v`. `tests/test_spending_x402.py` drives the real x402 client against a fake seller.

---

## Paid API example

**What you get:**

- `GET /premium-data`: an endpoint that costs `$0.01` (devnet USDC) per call. Change the price with `PAID_API_PRICE_USD`. Replace the handler in `agent/routes/premium_data.py` with whatever you want to sell, such as Gemini output or data.
- `python -m agent buy`: the agent buys from a paid URL. It pays with `AGENT_PRIVATE_KEY` within its spending limits and prints the data and the receipt. It can buy from any x402 seller on Solana devnet the same way.
- `POST /api/buy`: the same buyer running inside your service. It only ever buys from this service's own `/premium-data`. `GET /api/buy` shows the budget.

**Try it** (after the payments setup above, with `python -m agent serve` running in another terminal):

```bash
curl -i http://localhost:3000/premium-data                      # 402 + PAYMENT-REQUIRED header (the price)
python -m agent buy                                             # pays once, prints the data + receipt
python -m agent buy http://localhost:3000/premium-data -n 5     # pays 5 times; watch the budget go down
curl -X POST http://localhost:3000/api/buy                      # the service's own agent pays once
```

The `PAYMENT-REQUIRED` header is base64 JSON. To read it: `curl -si http://localhost:3000/premium-data | grep -i payment-required | cut -d' ' -f2 | base64 -d`.

A successful payment prints a `receipt` with a `transaction` id. Paste it into https://explorer.solana.com/?cluster=devnet to see it on-chain.

**If it fails:**

| Message | Fix |
| --- | --- |
| `PAY_TO_ADDRESS is not set` | Run `python -m agent keygen --pay-to`, then restart the service |
| `AGENT_PRIVATE_KEY is not set` | Run `python -m agent keygen` |
| `…simulation_failed…` or `insufficient_funds` | Run `python -m agent wallets`. The agent needs devnet USDC, and the seller address needs a USDC account (send it faucet USDC once) |
| `…above the per-payment cap…` / `max_amount_per_payment` | The price is higher than `MAX_PAYMENT_USD` |
| `…would exceed the budget…` | `AGENT_BUDGET_USD` is used up. Restart the agent to reset it, or raise it |

---

## Gemini AI

**What you get:** your agent can ask Google Gemini, using the official [`google-genai`](https://pypi.org/project/google-genai/) SDK:

- `python -m agent ask "your question"` on the command line
- `POST /api/chat` on the HTTP service

**Setup (2 minutes, free):**

1. Go to https://aistudio.google.com/apikey and sign in with a Google account.
2. Click **Create API key** and copy it.
3. In `.env` set `GEMINI_API_KEY=your-key`, then restart the service.

**Try it:**

```bash
python -m agent ask "Say hi"
curl -X POST http://localhost:3000/api/chat -H "Content-Type: application/json" -d '{"prompt":"Say hi"}'
```

**How it works:** only your agent talks to Gemini, so the key never leaves your machine or server. The code is in `agent/ai/gemini.py` (call `await ask_gemini(prompt)` from your own code), `agent/routes/chat.py` and `agent/commands/ask.py`. To use a different model set `GEMINI_MODEL` (default `gemini-flash-latest`).

**If it fails:** "GEMINI_API_KEY is not set" means `.env` is missing the key or you didn't restart. A 502 means Gemini rejected the call; the terminal running the service shows the reason (often an invalid key or a free-tier rate limit).

---

## AI Race adapter

**What you get:** the two endpoints the AI Race verifier calls, `GET /agent-studio/v1/health` and `POST /agent-studio/v1/tasks`, already speaking the race protocol (ASRP/1): they check the verifier token, echo the nonce and wrap your answers. `GET /race` shows which task types you've built.

**What you build:** the AI logic, one function per task type in `agent/race/handlers.py`. Each race layer tests one capability (answering with a model, structured output, tool calling, knowledge, memory, reliability, workflows). They all start as "not built yet" and return a clear error until you write them. `agent/race/tools.py` has `call_tool` and `search_source` for calling the race's tools.

**Setup:**

1. On Agent Studio, open your team page and copy the **verifier token** into `.env` as `AGENT_STUDIO_VERIFIER_TOKEN`, then restart `python -m agent serve`.
2. Write the `respond` handler (the first layer), for example with the Gemini module: return `{"text": await ask_gemini(str(task["input"]))}`.
3. Try it locally: `python -m agent race respond "What is 6 x 7?"` (with `python -m agent serve` running).
4. Deploy (for example to Railway or Render, adding the same env var) and paste the public URL on your team page. Then press **Run verification** on the race page.

**Memory on serverless hosting:** the Memory and Context layer checks that your agent remembers earlier turns of a conversation. On serverless hosts each request can reach a different instance, so history kept in a variable is lost between turns, even though it works locally. Use `memory` from `agent/race/memory.py` and set `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` from a free Upstash Redis database (upstash.com). Without them, `memory` keeps history in the process, which is fine locally and on an always-on server (Railway, Render, Fly).

**Tests:** `python -m unittest discover -s tests -v` includes `tests/test_race.py`, which checks the protocol handling offline. The race verifier checks your AI logic with fresh inputs on every attempt, so answers can't be hard-coded.

**If it fails:** 401 means the token in your environment doesn't match your team page (rotate it there and copy again). "doesn't handle … yet" means that handler is still the placeholder. The full protocol is in the [verification guide](https://github.com/Db0706/superteam-agent-studio/blob/main/docs/VERIFICATION_SDK.md).

---

## Pay.sh

**What it is:** [Pay.sh](https://pay.sh) is a free, open-source tool from the Solana Foundation. Its `pay` command pays HTTP 402 charges for you, using the same x402 protocol as this agent. It keeps its own wallet, separate from `AGENT_PRIVATE_KEY`, and it can let AI agents such as Claude Code pay for APIs while they work.

**Install it once:** `brew install pay` (macOS) or `npm install -g @solana/pay`. Check with `pay --version`.

**Try it** (with `python -m agent serve --reload` running in another terminal):

```bash
python -m agent paysh            # pays GET /premium-data once from your Pay.sh wallet
python -m agent paysh --wallet   # shows that wallet's devnet address and USDC balance
```

The first `python -m agent paysh` makes a Pay.sh devnet wallet and fails with "payment was refused", because the new wallet is empty. Run `python -m agent paysh --wallet`, send that address devnet USDC at https://faucet.circle.com (choose **Solana Devnet**), then run `python -m agent paysh` again. You can also call `pay` directly: `pay curl http://localhost:3000/premium-data`.

**Let Claude Code use your API:** run `pay claude` in this folder. Claude Code starts with Pay.sh's tools and pays for x402 APIs it calls, such as `http://localhost:3000/premium-data`, from the same wallet.

**Pay.sh's test network:** add `--sandbox` to pay public demo APIs on Pay.sh's hosted test network, which funds a throwaway wallet for you: `python -m agent paysh https://debugger.pay.sh/mpp/quote/AAPL --sandbox`.

**Get listed:** `python -m agent paysh --listing your-team` writes `pay-listing/providers/your-team/<this project>/PAY.md`, a listing for the Pay.sh catalog, and checks it with `pay catalog check`. Set `PAYSH_SERVICE_URL` to your deployed `https://` address first. The public catalog only lists APIs paid on Solana **mainnet**, so submit it to https://github.com/solana-foundation/pay-skills once your agent takes real money.

**If it fails:**

| Message | Fix |
| --- | --- |
| `The pay CLI is not installed` | `brew install pay` or `npm install -g @solana/pay` |
| `The payment was refused` | Your Pay.sh wallet has no devnet USDC. `python -m agent paysh --wallet`, then use the faucet. The seller address needs a USDC account too (see the payments setup) |
| `Could not connect to Surfpool` with `--sandbox` | Pay.sh's hosted test network is down. Leave out `--sandbox` and pay on devnet instead |
