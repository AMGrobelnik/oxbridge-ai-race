"""Does Phi-4 really beat the market on the sealed holdout? Paired, question-level statistics."""
import json
import random
from collections import defaultdict
from statistics import mean

import sys
D = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "/home/adrian/projects/oxbridge-ai-race/agent/harvest/data/demo_replay.json"))
rng = random.Random(7)

rows = []  # (cutoff, qid, series, outcome, market_p, {model: mean Brier over its personas})
for c in D["cutoffs"]:
    if c["split"] != "holdout":
        continue
    out = {r["qid"]: r for r in c["resolutions"]}
    series = {q["qid"]: q.get("series") or q["spec_id"] for q in c["questions"]}
    per = defaultdict(lambda: defaultdict(list))
    for f in c["forecasts"]:
        model = f["persona_id"].split("--")[1]
        per[f["qid"]][model].append(f["p_yes"])
    for qid, r in out.items():
        y = r.get("outcome", r.get("yes"))
        y = 1.0 if y in (True, 1, "yes", "YES") else 0.0
        mk = c["final_prices"][qid]
        briers = {m: mean((p - y) ** 2 for p in ps) for m, ps in per[qid].items()}
        rows.append((c["cutoff"], qid, series[qid], y, mk, briers))

models = sorted(rows[0][5])
print(f"holdout questions: {len(rows)} over cutoffs {sorted({r[0] for r in rows})}; base rate YES {mean(r[3] for r in rows):.2f}")
mkt = [(r[4] - r[3]) ** 2 for r in rows]
print(f"market Brier {mean(mkt):.3f}; coin flip 0.250; always-base-rate {mean((mean(x[3] for x in rows) - r[3]) ** 2 for r in rows):.3f}")


def boot(diffs, groups, n=20000):
    """Cluster bootstrap over groups (resample whole clusters), returns 90% CI of the mean diff."""
    by = defaultdict(list)
    for d, g in zip(diffs, groups):
        by[g].append(d)
    keys = list(by)
    stats = []
    for _ in range(n):
        s = [d for k in (rng.choice(keys) for _ in keys) for d in by[k]]
        stats.append(mean(s))
    stats.sort()
    return stats[int(0.05 * n)], stats[int(0.95 * n)]


def signflip(diffs, n=20000):
    obs = mean(diffs)
    hits = sum(1 for _ in range(n) if mean(d * rng.choice((-1, 1)) for d in diffs) <= obs)
    return hits / n


print(f"\n{'model':16} Brier  diff-vs-mkt  90%CI(question)   90%CI(series-cluster)  p(sign-flip)")
best = {}
for m in models:
    b = [r[5][m] for r in rows]
    diffs = [bi - mi for bi, mi in zip(b, mkt)]
    ci_q = boot(diffs, list(range(len(diffs))))
    ci_s = boot(diffs, [r[2] for r in rows])
    p = signflip(diffs)
    best[m] = mean(diffs)
    print(f"{m:16} {mean(b):.3f}  {mean(diffs):+.3f}      [{ci_q[0]:+.3f},{ci_q[1]:+.3f}]   [{ci_s[0]:+.3f},{ci_s[1]:+.3f}]       {p:.3f}")

# Selection effect: how often does the BEST of 6 models beat the market by this much if no model has skill?
# Null: for each question, randomly swap each model's Brier with the market's (sign-flip per model).
obs_best = min(best.values())
n, hits = 5000, 0
for _ in range(n):
    bests = []
    for m in models:
        diffs = [(r[5][m] - mk) * rng.choice((-1, 1)) for r, mk in zip(rows, mkt)]
        bests.append(mean(diffs))
    hits += min(bests) <= obs_best
print(f"\nbest model's edge {obs_best:+.3f}; chance the best of {len(models)} no-skill models looks this good: {hits / n:.3f}")
print("per-cutoff edge of the best model:", {
    co: round(mean(r[5][min(best, key=best.get)] - (r[4] - r[3]) ** 2 for r in rows if r[0] == co), 3)
    for co in sorted({r[0] for r in rows})})
