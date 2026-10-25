# Submission answers (ready to paste)

## Title

Sealed Backtest

## Problem solved

AI research agents grade their own homework. A quant agent that iterates against a backtest it can see will find the parameters that fit that history, and the best-looking result is usually the most overfit: the bug that looks like a discovery. Afterwards nobody can tell whether the test was moved, the data cherry-picked or the metric swapped mid-run, and the agent's own logs can't settle it because the agent wrote them.

This matters most in onchain finance. Funds and DAOs will only let AI agents run strategies when there are guardrails: limited autonomy, human sign-off on big actions and an onchain audit trail. Today an agent can show a beautiful backtest and there is no way to check that its success criteria were agreed in advance. Sealed Backtest gives research agents that missing property: success criteria agreed in advance, sealed on Solana so any later edit is detectable, and a holdout that rejects the overfit winner.

## Project description

Sealed Backtest is a research agent with an audit trail. Before the agent runs a single experiment, the harness hashes the locked evaluation code, the dev data and a salted commitment to a hidden holdout, and posts that hash on Solana devnet as a memo. The block timestamp proves the test existed, unchanged, before the agent started. Success criteria are agreed in advance and sealed so any later edit is detectable.

Then an LLM (Gemini) proposes strategies one falsifiable hypothesis at a time, and the harness scores them on dev data only. At the end the holdout is revealed, every kept candidate is re-scored on it, and a fixed gate, itself part of the sealed code, labels each GENERALISES or OVERFIT. A second memo anchors the result hash and verdict. Anyone can download the run bundle and re-hash it against the chain; change one number in one candidate and the match turns red.

The headline demo is SOL/USDC strategy research on real Kraken data. The agent finds a grid-tuned dip-buyer that scores 103.3 on dev but only 91.0 on the sealed 2026 holdout, and Sealed rejects it as OVERFIT. A slow trend filter with staking passes (91.0 on dev, 95.6 on the holdout) and is selected. That is the point: it catches the bug that looks like a discovery.

Guardrails are built in. Every round of the search is one link in a hash chain bound to the sealed commitment and posted as its own on-chain memo, so no round can be edited, dropped or reordered. Agent payments above a cap (0.50 USDC by default) are not sent: they queue for human co-sign, with an append-only audit log. Spending caps from the starter stay in force.

Same harness, any scored environment: a plug-in is about three files. Besides strategy research, a tokenomics stress test (vesting, emissions, pool depth, buybacks over 18 months) and an epidemic-policy simulator ship today.

Wallet note: the devnet payer is not funded yet, so on-chain calls show a clearly labelled "pending on-chain" state and go on chain with one command once it is funded.

Team: Adrian Mladenic Grobelnik, Antonio Aciob and Eashan Rautaray (Cambridge).

## Technical architecture

Python 3.12 service on the Superteam Agent Studio starter (FastAPI, Solana devnet). The sealed package is pure stdlib with no new dependencies.

Harness (agent/sealed/harness.py): seal, baseline, then N iterations of propose, validate, dev-score, KEEP or REVERT or INVALID, then reveal and anchor. State is saved atomically to a JSON run record after every step.

Plug-ins (agent/sealed/envs/<name>/): sim.py and eval.py are locked and hashed; holdout.json is covered by a salted commitment; handbook.md and a demo trajectory are not sealed. Strategy uses real Kraken SOL/USD daily data (its SHA-256 is pinned in the locked code) with fees, slippage and staking yield. Candidates are JSON validated against a schema, never code, so the LLM cannot break the evaluator.

Sealing (prereg.py, hashing.py): canonical JSON (sorted keys, 6-decimal floats) hashed with SHA-256. code_sha256 covers sim.py and eval.py; holdout_commitment = sha256(salt, holdout); commitment covers run id, env, code hash, dev seeds and holdout commitment. Memos: sealed:v1:commit:<run>:<commitment> and sealed:v1:reveal:<run>:<salt>:<result_sha256>:<verdict>:<selected>.

Audit chain (audit.py): each round's hash covers the previous hash (the commitment for round 0), the hypothesis, candidate, dev score and decision, and is posted as sealed:v1:round:<run>:<i>:<hash>. verify_bundle recomputes the chain and reports the first broken round.

Chain (chain.py): memos go through the starter's pay_usdc with a memo on Solana devnet. It never raises: an unfunded wallet or unreachable RPC gives a pending receipt, and sealed anchor retries later. Late anchors are labelled "anchored late".

Gate (gate.py): OVERFIT if the relative dev-to-holdout gap exceeds the limit, or if any scenario is worse than the naive baseline.

Co-sign (agent/payments/cosign.py, routes/cosign.py): payments over the cap become approvals (pending, approved_pending_onchain, sent, rejected) with approve and reject endpoints, an optional bearer token and an audit.jsonl.

Interfaces: FastAPI routes under /sealed/api and /api/cosign, a CLI (python -m agent sealed run|anchor|verify), and a no-build HTML demo page at /sealed that replays a frozen run in about 45 s (seal, search, reveal, verify, "tamper one number"). Tests run offline with no keys.

## AI capabilities

What the AI does: Gemini (JSON mode) is the research proposer. Each iteration it reads the goal, a domain handbook, the candidate schema, the current champion with its dev score breakdown and the last six experiments, and returns a one-line falsifiable hypothesis, a strategy (continue, pivot or widen) and a full candidate. It is told to change one idea at a time. It is never shown the holdout; the context type has no holdout field.

Which models: Gemini via the starter's ask_gemini helper. A scripted proposer replays a fixed trajectory for deterministic offline tests and the cached demo. The service also answers the AI Race tasks (respond, extract, use_tools, answer_from_source, converse, run_workflow).

No LLM judge: keep and revert decisions come from the locked simulator score on dev data, and the final verdict comes from the sealed holdout and a fixed gate. No model grades its own work.

How we checked it works: (1) A live Gemini run through the HTTP API with anchoring on improved the dev score by 46% on the epidemic plug-in while the holdout rejected 6 of 8 kept candidates as OVERFIT, including the dev champion, which collapsed below the naive baseline under a stressed scenario. (2) On strategy, the dev-tuned dip-buyer (103.3 dev, 91.0 holdout) is rejected and the trend filter with staking (91.0 to 95.6) passes. (3) Tampering with one number in a bundle makes the verification fail. (4) Offline pytest suite covering harness, hashing, audit chain, co-sign, all plug-ins and routes. (5) The demo page tested in a real browser at phone and laptop widths. Limits: the devnet wallet is unfunded so memos are pending, and backtests are not forecasts.
