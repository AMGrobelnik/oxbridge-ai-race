"""Exploratory (not pre-registered): Phi-4 vs market pooled over the original 48 and the new 64 questions,
plus each model's discrimination gap on the new questions."""
import json
import random
from collections import defaultdict
from statistics import mean

rng = random.Random(11)
FILES = ["/home/adrian/projects/oxbridge-ai-race/agent/harvest/data/demo_replay.json",
         "/home/adrian/projects/oxbridge-ai-race-wt-phi4/analysis/phi4_replication.json"]


def load(path):
    rows, gaps = [], defaultdict(lambda: ([], []))
    for c in json.load(open(path))["cutoffs"]:
        if c["split"] != "holdout":
            continue
        out = {r["qid"]: (1.0 if r.get("outcome") in (True, 1, "yes", "YES") else 0.0) for r in c["resolutions"]}
        series = {q["qid"]: q["series"] for q in c["questions"]}
        per = defaultdict(lambda: defaultdict(list))
        for f in c["forecasts"]:
            m = f["persona_id"].split("--")[1]
            per[f["qid"]][m].append(f["p_yes"])
            gaps[m][int(out[f["qid"]])].append(f["p_yes"])
        for qid, y in out.items():
            mk = (c["final_prices"][qid] - y) ** 2
            rows.append((series[qid], mean((p - y) ** 2 for p in per[qid]["phi-4"]) - mk))
    return rows, gaps


old, _ = load(FILES[0])
new, gaps = load(FILES[1])
print("discrimination gap on new questions (avg p when YES minus when NO):")
for m, (no, yes) in sorted(gaps.items()):
    print(f"  {m:22} {mean(yes) - mean(no):+.2f}")
rows = old + new
diffs = [d for _, d in rows]
by = defaultdict(list)
for s, d in rows:
    by[s].append(d)
keys = list(by)
stats = sorted(mean([d for k in (rng.choice(keys) for _ in keys) for d in by[k]]) for _ in range(20000))
flip = sum(1 for _ in range(20000) if mean(d * rng.choice((-1, 1)) for d in diffs) <= mean(diffs)) / 20000
print(f"pooled n={len(rows)}: Phi-4 minus market {mean(diffs):+.3f}, 90% CI by series [{stats[1000]:+.3f},{stats[19000]:+.3f}], sign-flip p {flip:.3f}")
