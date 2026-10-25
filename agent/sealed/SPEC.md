# Sealed: pre-registered, oracle-gated autoresearch

Status: architecture spec, 25 Oct 2026 (Oxford and Cambridge AI Race build track).
Every module named here exists as a typed skeleton; each workstream fills the files it owns
and must not change the interfaces below without the architect's say-so.

## 1. The concept

AI research agents grade their own homework: they iterate against a test they can see, and
the best-looking result is often the most overfit. Sealed is an autoresearch harness that
**cannot cheat its own test**:

1. **Seal.** Before iteration 1 the harness hashes the locked evaluation code, the dev seeds
   and a salted commitment to the held-out seeds and scenario shifts, and anchors that hash
   on Solana devnet as a memo. The block timestamp proves the test existed, unchanged, before
   the agent ran a single experiment.
2. **Search.** An LLM proposer (Gemini) proposes a candidate (a policy), the harness validates
   it, scores it on the **dev seeds only**, and keeps it if it beats the champion, else
   reverts. N iterations. The proposer never sees the holdout.
3. **Reveal.** The harness reveals the salt, re-scores every kept candidate on the sealed
   holdout (unseen seeds under the base scenario plus every shift), and applies a fixed gate
   that is itself part of the sealed code. The dev champion is either `GENERALISES` or
   `OVERFIT` (red "overfit rejected" moment); the best generalising candidate is selected.
   A second memo anchors the result hash and the verdict.
4. **Verify.** Anyone can re-hash the downloadable run bundle against the two on-chain memos.
   Change one number in one candidate and the match turns red.

The harness is the product; an environment is a plug-in of about three files. Plug-in #1 is
an age-structured SEIRV epidemic-policy simulator.

## 2. Package layout and ownership

| Path | Owner | Role |
|---|---|---|
| `agent/sealed/SPEC.md`, `records.py`, `hashing.py`, `gate.py`, `environment.py`, `__init__.py`, `envs/__init__.py`, `demo/sample_run.json` | architect (frozen) | Shared contract, implemented |
| `agent/sealed/harness.py`, `proposer.py`, `store.py`, `agent/commands/sealed.py`, `agent/sealed/demo/epidemic_demo.json`, `tests/test_sealed_harness.py` | W1 core loop | Loop, LLM proposer, run store, CLI |
| `agent/sealed/prereg.py`, `chain.py`, `tests/test_sealed_prereg.py` | W2 chain | Hashes, memos, devnet anchor, verify |
| `agent/sealed/envs/epidemic/*`, `tests/test_sealed_epidemic.py` | W3 env | Simulator, locked eval, holdout, demo trajectory |
| `agent/routes/sealed.py`, `agent/sealed/static/*`, `tests/test_sealed_routes.py`, one entry in `agent/modules.py` | W4 UI | HTTP API and the demo page |

Constraints for everyone: Python 3.12 venv at `.venv`, no new dependencies (no numpy: it is
not in `requirements.txt`; the simulator is pure stdlib), never print `.env` or keys, never
edit `agent/race/*` or existing tests, keep `tests/` green
(`PYTHONPATH=<repo> .venv/bin/python -m pytest -q tests`).

## 3. Shared types (`agent/sealed/records.py`)

All records are plain JSON-serialisable `TypedDict`s so they go straight to disk and over HTTP.

- `Candidate = dict[str, Any]`: the editable artifact. It is **data validated against a JSON
  schema**, not code, so the LLM cannot break the evaluator. The UI renders it like a
  `policy.py` block and shows field-level diffs.
- `Shift {id, label, params}`; `BASE_SHIFT` has `id="base"` and empty params.
- `GateSpec {max_relative_gap, no_shift_worse_than_baseline}`.
- `EvalSpec {version, metric, metric_label, higher_is_better (always True), dev_seeds, gate, notes}`.
- `Holdout {seeds, shifts}` (shifts exclude base; the harness always adds `BASE_SHIFT`).
- `ScoreReport {score, per_seed, breakdown, seeds, shift_id}`: `score` is the mean over seeds,
  higher is better; `breakdown` holds mean raw components for the UI.
- `Proposal {hypothesis, rationale, strategy, candidate}`; strategy is `continue|pivot|widen`.
- `ProposerContext`: what the proposer may see (goal, handbook, schema, champion, history).
  It has no holdout field, by construction.
- `FieldChange {path, before, after}`: one changed leaf, dotted path.
- `ChainReceipt {status: confirmed|pending, memo, signature, explorer_url, cluster, at, error}`.
- `Preregistration`, `IterationRecord`, `HoldoutResult`, `Reveal`, `RunRecord`, `RunSummary`,
  `VerifyReport`: see section 8 and the docstrings in `records.py`.

## 4. Environment plug-in interface

A plug-in is a package `agent/sealed/envs/<name>/`:

| File | Locked? | Contents |
|---|---|---|
| `sim.py` | sealed (hashed) | `NAME`, `TITLE`, `GOAL` (one sentence), `CANDIDATE_SCHEMA` (JSON Schema), `baseline() -> Candidate`, `validate(candidate) -> list[str]`, `simulate(candidate, *, seed, shift) -> dict[str, float]` |
| `eval.py` | sealed (hashed) | `SPEC: EvalSpec` and `evaluate(candidate, *, seeds, shift) -> ScoreReport` |
| `holdout.json` | sealed (salted commitment) | `{"seeds": [...], "shifts": [Shift, ...]}` |
| `handbook.md` | no | Domain notes for the proposer prompt |
| `demo_trajectory.json` | no | `list[Proposal]`, a scripted proposer run for the cached demo |
| `__init__.py` | no | empty |

`agent/sealed/environment.py` wraps a plug-in as `Environment` (implemented):
`load_environment(name)`, `list_environments()`, and on the object `spec`, `candidate_schema`,
`baseline()`, `validate()`, `evaluate()`, `dev_report(candidate)`, `handbook()`,
`demo_trajectory()`, `load_holdout()`, `code_files()`, `public_view()`.
Only the reveal step and `prereg.py` may call `load_holdout()`.

Rules for a plug-in: deterministic for a given `(candidate, seed, shift)`; scoring 20 seeds
under one shift takes under 2 s; `validate` catches anything `simulate` would choke on
(missing keys, wrong types, out-of-range values, budget overrun).

## 5. The autoresearch loop (`harness.py`, `proposer.py`)

```
run_sealed(env_name, *, iterations=8, proposer="gemini"|"scripted", anchor=True, store, run_id=None, on_update=None) -> RunRecord
```

1. `status="sealing"`: `salt = prereg.new_salt()`;
   `prereg.build_preregistration(env, run_id, salt)`; `chain.anchor(memo)` (never raises;
   returns a `pending` receipt when the wallet is unfunded or devnet is unreachable). Save.
2. `status="running"`: iteration 0 is the baseline, `decision="BASELINE"`, scored on dev.
3. For i in 1..N: build `ProposerContext` (champion, last 6 iterations, never holdout) ->
   `await proposer.propose(ctx)` -> `env.validate` -> `env.dev_report` in a worker thread
   with a 10 s timeout. `KEEP` iff valid and `score > champion score`; else `REVERT`; any
   exception, invalid JSON or schema error gives `INVALID` with `errors`. Save after each.
4. `status="revealing"`: load holdout; finalists are the baseline and every `KEEP`; each is
   scored on holdout seeds under `BASE_SHIFT` plus every shift; `gate.judge(...)` (section 6)
   gives each a verdict. `dev_champion_iteration` = last KEEP (or 0);
   `selected_iteration` = highest-dev-score finalist with `GENERALISES` (or `None`).
   `result_sha256 = prereg.result_digest(record)`; `chain.anchor(reveal memo)`.
5. `status="done"`; save. Any unexpected exception: `status="failed"`, `error` set, saved.

Proposers (`Proposer` protocol: `name`, `async propose(ctx) -> Proposal`):
- `GeminiProposer`: one `ask_gemini(prompt, json_output=True)` call per iteration. Prompt =
  goal, handbook, schema, champion candidate with its dev breakdown, recent history; asks for
  a one-line falsifiable hypothesis, a strategy (`continue|pivot|widen`) and the full
  candidate. Paraphrase AI Inventor prompt ideas; never import AI Inventor code.
- `ScriptedProposer`: replays `env.demo_trajectory()` in order (deterministic, no network).

`start_background_run(...) -> run_id` schedules `run_sealed` as an asyncio task (kept in a
module-level set) for the HTTP API.

## 6. Holdout gate (`gate.py`, implemented and sealed by reference)

For a finalist with dev score `d` and holdout scores `h[s]` per shift (base included):
`holdout_mean = mean(h)`, `relative_gap = (d - holdout_mean) / max(|d|, 1e-9)`.
Verdict is `OVERFIT` if `relative_gap > gate.max_relative_gap`, or, when
`gate.no_shift_worse_than_baseline`, if any `h[s]` is below the baseline's `h[s]`
("collapses under 1.4x transmissibility: 12.3 vs naive baseline 20.1"). Otherwise
`GENERALISES`. The gate parameters live in `eval.SPEC`, so they are part of the seal.

## 7. Pre-registration and reveal (`prereg.py`, `chain.py`)

Hashes (SHA-256 hex over `hashing.canonical_json`, which sorts keys, uses compact separators,
UTF-8, rejects NaN; floats in hashed payloads are rounded to 6 decimals first):

- `code_sha256 = sha256(b"".join(name + b"\0" + bytes + b"\0" for sim.py, eval.py))`
- `holdout_commitment = sha256(canonical_json({"salt": salt, "holdout": holdout}))`
- `commitment = sha256(canonical_json({"v": 1, "run_id", "env", "code_sha256", "dev_seeds", "holdout_commitment"}))`
- `result_sha256 = sha256(canonical_json({"v": 1, "run_id", "commitment",
  "iterations": [{"index", "candidate", "dev_score", "decision"}],
  "results": [{"iteration", "per_shift", "verdict"}], "selected_iteration"}))`

Memos (ASCII, under 300 bytes, the run id matches `^[A-Za-z0-9_-]{1,64}$`):

- commit: `sealed:v1:commit:<run_id>:<commitment>`
- reveal: `sealed:v1:reveal:<run_id>:<salt>:<result_sha256>:<GENERALISES|OVERFIT>:<selected_iteration or none>`

`chain.anchor(memo, *, to=None, amount_usdc=None) -> ChainReceipt` is a thin wrapper over the
starter's `pay_usdc(load_agent_keypair(), to, amount_usdc, memo=memo, guard=get_spending_tracker())`,
the same memo path the race `run_workflow` handler uses for its 400-point payment (the race
handlers are not changed). Defaults: `to = SEALED_ANCHOR_TO` or the agent's own address
(a self-transfer), `amount_usdc = SEALED_ANCHOR_USDC` or 0.01. It never raises:
`PaymentsConfigError`, `TransferError`, timeouts and RPC errors give
`status="pending"` with a short human `error` ("pending on-chain: agent wallet not funded").
`chain.anchor_pending(record) -> RunRecord` retries every pending receipt (commit, then
reveal); a commit anchored after iteration 1 started keeps its original `sealed_at` and the
UI labels it "anchored late". `chain.fetch_memo(signature) -> str | None` reads a devnet
transaction and returns its memo text. `chain.explorer_url(signature)` gives
`https://explorer.solana.com/tx/<sig>?cluster=devnet`.

`prereg.verify_bundle(record, env, *, commit_memo_onchain, reveal_memo_onchain) -> VerifyReport`
recomputes `code_sha256` from the installed plug-in files, the holdout commitment from the
revealed salt and holdout, the commitment and the result hash from the bundle, and compares
each with the recorded values and the on-chain memos (when the receipts are confirmed).
`ok` is true only when every available comparison matches. With pending receipts it checks
against the recorded values and says "on-chain record pending".

## 8. Run record format (JSON on disk)

`RunStore` (`store.py`) writes one file per run, `<root>/<run_id>.json`, atomically
(temp file plus `os.replace`) after every step. `root` = `SEALED_RUNS_DIR` or
`<repo>/.sealed_runs/` (gitignored). The frozen demo lives in the repo at
`agent/sealed/demo/<env>_demo.json`; until W1 freezes one, `demo/sample_run.json` (a
hand-made fixture with the exact shape) stands in.

```jsonc
{
  "schema": "sealed.run/v1",
  "run_id": "20261025T133000Z-epidemic-a1b2",
  "env": "epidemic", "title": "...", "goal": "...", "metric_label": "...",
  "mode": "live",                 // or "cached" for a frozen demo
  "proposer": "gemini",           // or "scripted"
  "iterations_planned": 8,
  "status": "done",               // sealing | running | revealing | done | failed
  "created_at": "ISO-8601 UTC", "updated_at": "ISO-8601 UTC",
  "preregistration": {"env", "run_id", "code_sha256", "dev_seeds", "holdout_commitment",
                      "commitment", "memo", "sealed_at", "chain": ChainReceipt},
  "iterations": [{"index", "proposer", "hypothesis", "rationale", "strategy", "candidate",
                  "diff": [FieldChange], "dev": ScoreReport | null,
                  "decision": "BASELINE|KEEP|REVERT|INVALID", "errors": [],
                  "champion_score": 0.0, "duration_s": 0.0, "at": "ISO"}],
  "reveal": {"salt", "holdout": Holdout, "revealed_at",
             "results": [{"iteration", "dev_score", "per_shift": {"base": 0.0, ...},
                          "shift_labels": {...}, "holdout_mean", "worst_shift_id",
                          "worst_score", "relative_gap", "verdict", "reasons": []}],
             "dev_champion_iteration", "selected_iteration", "verdict",
             "result_sha256", "memo", "chain": ChainReceipt} | null,
  "error": null
}
```

## 9. HTTP API (`agent/routes/sealed.py`, registered by `register(app)` like every route plugin)

| Method and path | Body | Returns |
|---|---|---|
| `GET /sealed` | | The demo page (`agent/sealed/static/index.html`) |
| `GET /sealed/static/*` | | Static assets (Starlette `StaticFiles` mount) |
| `GET /sealed/api/envs` | | `[Environment.public_view()]` |
| `GET /sealed/api/demo` | | The frozen demo `RunRecord` (falls back to `sample_run.json`) |
| `GET /sealed/api/runs` | | `[RunSummary]`, newest first |
| `GET /sealed/api/runs/{run_id}` | | `RunRecord` (404 if unknown; 400 if the id fails the regex) |
| `GET /sealed/api/runs/{run_id}/bundle` | | `RunRecord` as a download (`Content-Disposition`) |
| `POST /sealed/api/runs` | `{"env": "epidemic", "iterations": 8, "proposer": "gemini", "anchor": true}` | 202 `{"run_id"}`; iterations capped at 12 |
| `POST /sealed/api/runs/{run_id}/anchor` | | Updated `RunRecord` after `chain.anchor_pending` |
| `POST /sealed/api/verify` | `{"record": RunRecord}` | `VerifyReport` (fetches on-chain memos for confirmed receipts) |

Errors are JSON `{"error": "..."}` like the rest of the app. No route ever returns secrets.

## 10. Demo UI (`agent/sealed/static/`)

One page, plain HTML, CSS and JS, no build step, no external scripts; works at phone and
laptop widths. A judge follows it in 60 seconds:

1. **Header:** "Sealed: autoresearch that can't cheat its own test", the goal sentence.
2. **Sealed test card:** commitment hash, memo, chain badge (green "on-chain" with explorer
   link, or amber "pending on-chain"), sealed time, a padlocked "holdout: sealed" tile.
3. **Iteration stream:** one card per iteration: hypothesis, field diff, dev score, KEEP
   (green) / REVERT (grey) / INVALID (outlined).
4. **Frontier chart (inline SVG):** champion dev score over iterations; after the reveal,
   holdout means for the finalists as a second series.
5. **Reveal:** finalists x shifts table; the dev champion in red "OVERFIT - rejected" with the
   reason, the selected candidate in green "GENERALISES"; reveal memo badge.
6. **Verify:** "Verify against chain" posts the bundle to `/sealed/api/verify` (green match);
   "Tamper one number" edits one candidate field client-side and re-verifies (red
   "does not match the sealed record").
7. Footer disclaimer: "Simulation evidence, not policy advice."

## 11. Cached-demo mode

The judges' demo never waits on Gemini or devnet:
- The page loads `GET /sealed/api/demo` and **replays** it client-side: the seal card first,
  then one iteration card about every 3 s, then the reveal, then verify (about 45 s; controls
  for replay, skip to reveal, and speed).
- The frozen run is a real run made earlier with `python -m agent sealed run --env epidemic
  --proposer gemini|scripted --iterations 8 --freeze`, which writes
  `agent/sealed/demo/epidemic_demo.json` with `mode="cached"`. Its chain receipts are real
  if the wallet was funded at freeze time, otherwise pending, and
  `python -m agent sealed anchor <run_id> --freeze` upgrades them later.
- "Run live" (POST `/sealed/api/runs`, then poll `GET /sealed/api/runs/{id}` every second) is
  an optional extra.

## 12. CLI (`agent/commands/sealed.py`)

`python -m agent sealed run --env epidemic [--iterations 8] [--proposer gemini|scripted] [--no-anchor] [--freeze]`,
`python -m agent sealed anchor <run_id> [--freeze]`, `python -m agent sealed verify <run_id>`.
