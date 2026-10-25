# Sealed: autoresearch that can't cheat its own test

Oxford and Cambridge AI Race, 25 Oct 2026. A Python agent built on the Superteam Agent Studio starter (FastAPI and Solana devnet). Sealed lives at `/sealed`. The starter's AI Race adapter and its six task handlers (`/agent-studio/v1/*`, `/race`) run unchanged in the same service.

## Problem

AI research agents grade their own homework. They iterate against a test they can see, and the result that scores best on that test is often the most overfit. Nobody can tell afterwards whether the test was moved, the seeds were cherry-picked or the metric was swapped mid-run. The agent's own logs can't settle it, because the agent wrote them.

## Description

Sealed is an autoresearch harness that commits to its own exam before it starts studying.

1. **Seal.** Before iteration 1 it hashes the locked evaluation code (`sim.py` and `eval.py`), the dev seeds, and a salted commitment to held-out seeds and scenario shifts. The hash goes on Solana devnet as a memo, and the block timestamp proves the test existed, unchanged, before the first experiment.
2. **Search.** An LLM proposer (Gemini) states one falsifiable hypothesis per iteration and submits a full candidate policy, which is JSON validated against a schema. The harness scores it on the dev seeds only and keeps it if it beats the champion; otherwise it reverts. The proposer's context has no holdout field.
3. **Reveal.** The salt is revealed and every kept candidate is re-scored on 20 unseen seeds under the base scenario and four shifts. A fixed gate, which is part of the sealed code, labels each candidate `GENERALISES` or `OVERFIT`. The best-scoring candidate that generalises is selected. A second memo anchors the result hash and the verdict.
4. **Verify.** Anyone can download the run bundle and re-hash it against the sealed record and the on-chain memos. Change one number in one candidate and the check turns red.

The harness is the product. An environment is a plug-in of about three files. Plug-in #1 is an age-structured SEIRV winter-epidemic simulator, where the goal is to minimise hospital-days for 10M people with GBP 40M, no school closures over 3 weeks and no widening of the age gap. Simulation evidence, not policy advice.

## Architecture

```
agent/sealed/
  harness.py      seal -> baseline -> N x (propose, validate, dev score, KEEP/REVERT/INVALID) -> reveal -> anchor
  proposer.py     GeminiProposer (one JSON-mode call per iteration, retries transient errors) | ScriptedProposer (replay)
  prereg.py       code hash, salted holdout commitment, commitment, result hash, memos, verify_bundle
  chain.py        devnet memo anchor via the starter's pay_usdc(memo=...); never raises, "pending on-chain" when unfunded
  gate.py         holdout gate: relative gap limit + "no shift worse than the naive baseline"
  hashing.py      canonical JSON (sorted keys, 6-dp floats, 33.0 == 33) + SHA-256
  store.py        run records as JSON, atomic writes; frozen demo in demo/epidemic_demo.json
  environment.py  plug-in loader: sim.py, eval.py, holdout.json, handbook.md, demo_trajectory.json
  envs/epidemic/  SEIRV simulator (pure stdlib), locked eval, sealed holdout
  static/         the demo page (plain HTML/CSS/JS, no build step)
agent/routes/sealed.py    HTTP API under /sealed/api
agent/commands/sealed.py  python -m agent sealed run|anchor|verify
```

| Endpoint | What it does |
| --- | --- |
| `GET /sealed` | Demo page: replays the frozen run (seal, search, reveal, verify) in about 45 s |
| `POST /sealed/api/runs` | Starts a live run, `{"env": "epidemic", "iterations": 8, "proposer": "gemini", "anchor": true}`, and returns 202 `{"run_id"}` |
| `GET /sealed/api/runs/{id}` | The run record as it grows; `/bundle` downloads it |
| `POST /sealed/api/runs/{id}/anchor` | Retries pending devnet memos once the wallet is funded |
| `POST /sealed/api/verify` | `{"record": <bundle>}` returns the re-hash report, fetching the on-chain memos when they are confirmed |
| `GET /sealed/api/demo`, `/sealed/api/envs` | The frozen demo run and the plug-ins |

Hashes and memos (SPEC: `agent/sealed/SPEC.md`):
- `commitment = sha256({v, run_id, env, code_sha256, dev_seeds, holdout_commitment})`, where `holdout_commitment = sha256({salt, holdout})`.
- The commit memo is `sealed:v1:commit:<run_id>:<commitment>`.
- The reveal memo is `sealed:v1:reveal:<run_id>:<salt>:<result_sha256>:<verdict>:<selected>`.

## AI capabilities

- **An LLM as the research proposer.** Gemini (`agent/ai/gemini.py`, JSON mode) reads the goal, the domain handbook, the candidate's JSON schema, the champion with its dev score breakdown and the last six experiments. It returns a one-line falsifiable hypothesis, a strategy (`continue`, `pivot` or `widen`) and a full candidate. It is told to change one idea at a time.
- **An external oracle, not an LLM judge.** Keep and revert decisions use the locked simulator score on dev seeds. The final verdict uses the sealed holdout and a fixed gate. No model grades its own work.
- **Robustness.** Proposals are data validated against a JSON schema, never code. Each evaluation has a 10 s timeout. A bad, over-budget or failed proposal becomes an `INVALID` iteration and the loop carries on.
- **The same service also answers the AI Race tasks:** respond, extract, use_tools, answer_from_source, converse and run_workflow (`agent/race/`).

## How it was verified

**End to end through the HTTP API on the live service** (`POST /sealed/api/runs`, proposer `gemini`, 8 iterations, anchoring on). The run is `20261025T125119Z-epidemic-d428`, and it is now the cached demo in `agent/sealed/demo/epidemic_demo.json`.

- **Sealed first.** The commitment `26cea50b…7f530a` was sealed at 12:51:19 UTC. The baseline was scored at 12:51:20 and iteration 1 at 12:51:35. The devnet memo is honestly marked "pending on-chain: agent wallet not funded".
- **The loop improves on the dev seeds.** The score is thousand hospital-days averted per GBP 1m, net of penalties. It rose from 17.59 (naive baseline) to 25.68 (+46%), with 8 of 8 proposals kept.
- **Overfitting is caught.** The holdout rejected 6 of the 8 kept candidates as `OVERFIT`, including the dev champion #8: it scored 25.68 on dev but 17.73 on the holdout, a 31% drop against a 30% limit. Under 1.4x transmissibility it collapses to 10.89, below the naive baseline's 14.70, because Gemini had cut the surge ICU beds to zero and raised the closure triggers.
- **A final candidate passes.** #3 (school closures may run 21 days) scored 22.79 on dev and 17.93 on the holdout, a 21% gap. It holds above the naive baseline under every shift and is `GENERALISES - selected`. Its holdout mean beats the dev champion's, although its dev score is lower.
- **Verify.** `POST /sealed/api/verify` on the downloaded bundle gives code, commitment and result hash all matching (`67e94902…1cc438f`). On the page, "Tamper one number" changes `school_close_max_days` from 21 to 22 on #3, and only the result hash fails.
- **Other runs give the same pattern.**
  - Two more Gemini runs each had at least one candidate rejected on the holdout.
  - The scripted replay (`--proposer scripted`) goes 17.59 to 28.03 on dev. Its dev champion collapses to -8.75 under 1.4x transmissibility and is rejected, and #4 is selected.
- **Tests.** `PYTHONPATH=. .venv/bin/python -m pytest -q tests` runs offline with no keys. It covers the harness, prereg and verify, the epidemic env, the routes and the integration checks, and the starter and race tests still pass.
- **The page in a real browser.** Chromium via Playwright, at 1280 and 390 px, light and dark, with no console errors and no horizontal scroll.

**Known limits:**
- Until the wallet is funded the memos are pending and verification compares against the recorded commitment only. `python -m agent sealed anchor <run_id> --freeze` upgrades a run once it is funded.
- The simulator is a calibrated stand-in, not a forecast.
- The holdout file ships in the repo, so the seal proves the test did not change during the run, not that the author never looked at it.

## Run Sealed

```bash
python -m agent serve                     # then open http://localhost:3000/sealed (PORT in .env)
python -m agent sealed run --env epidemic --proposer gemini --iterations 8          # live run in the terminal
python -m agent sealed run --env epidemic --proposer scripted --iterations 8 --no-anchor   # offline replay
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
