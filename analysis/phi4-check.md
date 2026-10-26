# Is Phi-4's edge over the market real? (26 Oct 2026, 01:45)

Data: `agent/harvest/data/demo_replay.json`, sealed holdout split: 48 questions on 3 cutoffs
(2025-09, 2025-12, 2026-03), 16 personas per model. Script: `phi4.py` in the session tmp dir (copied below).

| model | Brier | vs market | 90% CI (cluster by series) | p (sign-flip) |
|---|---|---|---|---|
| phi-4 | 0.185 | −0.046 | [−0.076, −0.006] | 0.021 |
| gemini-2.5-flash-lite | 0.251 | +0.020 | [−0.019, +0.059] | 0.840 |
| llama-3.3-70b | 0.252 | +0.021 | [−0.011, +0.053] | 0.888 |
| gemini-2.5-flash | 0.260 | +0.029 | [−0.017, +0.070] | 0.895 |
| gpt-4o-mini | 0.268 | +0.037 | [−0.041, +0.117] | 0.877 |
| gemma-3-27b | 0.273 | +0.042 | [−0.006, +0.088] | 0.949 |

Market 0.231; coin flip 0.250; YES base rate 0.31.

Findings:
- On its own, Phi-4's lead is significant (p = 0.02; CI excludes zero even when questions on the same
  commodity are resampled together).
- But it was picked as the best of 6. If no model had skill, the best of 6 would look this good
  15% of the time (p ≈ 0.15 after selection). Not proven.
- The edge sits almost entirely in one cutoff: 2025-09 −0.018, 2025-12 0.000, 2026-03 −0.120.
- It is real discrimination, not a "say NO" trick: Phi-4's average p is 0.53 on questions that resolved
  YES and 0.30 on NO (gap 0.23; other models 0.10–0.16), and its average p (0.37) is middling.
- Side note: the market itself (0.231) is worse than a constant at the base rate (0.215 in-sample),
  so the market maker is poorly calibrated on these questions; "beats the market" is a low bar.

## Pre-registration (written 26 Oct 2026 ~01:55, before any new forecast exists)

- Claim: Phi-4 agents beat the market on NEW holdout questions; the control (llama-3.3-70b) does not.
- Data: the same replay pipeline (agent/harvest/replay.py, same 16 personas x 6 models, same masking,
  same question bank, same market book), on monthly cutoffs never used before: 2025-10, 2025-11,
  2026-01, 2026-02 (all after every model's training cutoff; 3- and 6-month horizons resolved).
- Only forecasts with source "llm" count; fallback heuristics are reported separately.
- Metric: mean over questions of (model's mean Brier over its 16 personas − market Brier).
- Test: one-sided, alpha 0.05, cluster bootstrap by commodity series (90% CI upper bound < 0) and
  sign-flip permutation p < 0.05. Supported if both hold for Phi-4; also report the control and all models.
- Caveat stated in advance: the new cutoffs overlap in time with the old ones (2025-09, 2025-12,
  2026-03), so outcomes are correlated with the original 48; this is a robustness check, not a fully
  independent sample.

Verdict before replication: promising, not established.

## Replication (run 26 Oct 2026 01:30–01:41; $0.23 of OpenRouter spend)

64 new questions on cutoffs 2025-10, 2025-11, 2026-01, 2026-02; 6,144 forecasts, every one from the LLM
(no heuristic fallbacks). YES base rate 0.27. Market Brier 0.206 (better than on the original 48).

| model | Brier | vs market | 90% CI (by series) | p (sign-flip) | YES−NO gap |
|---|---|---|---|---|---|
| phi-4 | 0.185 | −0.021 | [−0.041, +0.001] | 0.132 | +0.23 |
| gemini-2.5-flash-lite | 0.231 | +0.025 | [−0.019, +0.068] | 0.899 | +0.14 |
| gpt-4o-mini | 0.235 | +0.029 | [−0.068, +0.125] | 0.831 | +0.20 |
| llama-3.3-70b (control) | 0.255 | +0.049 | [+0.018, +0.080] | 0.999 | +0.07 |
| gemini-2.5-flash | 0.262 | +0.056 | [+0.004, +0.095] | 0.998 | +0.15 |
| gemma-3-27b | 0.284 | +0.078 | [+0.011, +0.141] | 0.998 | +0.12 |

Pre-registered test: NOT supported. Phi-4's CI upper bound is +0.001 (needed < 0) and p = 0.13
(needed < 0.05). The control behaves as predicted (clearly worse than the market).

What did replicate:
- Phi-4 scored exactly 0.185 again and was again the best of the six, the only model at or below the
  market. Its edge is negative in all four new cutoffs (−0.030, −0.001, −0.011, −0.041).
- It again separates YES from NO best (gap +0.23; others +0.07 to +0.20).
- Every other model is worse than the market, three of them clearly so.

Exploratory, not pre-registered (both sets pooled, n = 112): Phi-4 − market = −0.032, 90% CI by series
[−0.052, −0.004], sign-flip p = 0.011. The two sets overlap in time, so this overstates the evidence.

Bottom line: Phi-4 is consistently the best model here and probably has a small real edge, but it is
smaller than the 48-question result suggested (about −0.02 to −0.03, not −0.046), and the pre-registered
replication did not clear the bar. Honest pitch line: "Phi-4 was the best model on both tests and the only
one that kept up with the market; the margin is small and not yet statistically established."

Artifacts: branch `phi4-replication` of AMGrobelnik/oxbridge-ai-race (`analysis/`), log
`logs/phi4-replication.log`. Next step: more out-of-sample questions (monthly cutoffs from
2025-09 and more thresholds per series), re-run Phi-4 and one control model, pre-register the claim
"Phi-4 beats the market" before looking.
