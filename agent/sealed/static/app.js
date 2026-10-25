/* Sealed demo page: replays a RunRecord (GET /sealed/api/demo) or follows a live run. Plain JS, no build. */
"use strict";

(() => {
  const $ = (sel) => document.querySelector(sel);
  const SVG_NS = "http://www.w3.org/2000/svg";

  // ---------- helpers ----------
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  function s(tag, attrs, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, v);
    for (const c of children.flat()) if (c !== null && c !== undefined) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return el;
  }
  const fmt = (x, d = 1) => (typeof x === "number" && isFinite(x) ? x.toFixed(d) : "–");
  const short = (hex, a = 8, b = 6) => (hex && hex.length > a + b + 1 ? `${hex.slice(0, a)}…${hex.slice(-b)}` : hex || "–");
  const pct = (x) => (typeof x === "number" ? `${Math.round(x * 100)}%` : "–");
  function when(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "UTC" }) + " UTC";
  }
  function pyKey(path) {
    const parts = String(path).split(".");
    return parts[0] + parts.slice(1).map((p) => `["${p}"]`).join("");
  }
  function pyVal(v) {
    if (v === null || v === undefined) return "None";
    if (typeof v === "string") return JSON.stringify(v);
    if (typeof v === "boolean") return v ? "True" : "False";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }
  function copyButton(text) {
    return h("button", {
      type: "button", class: "copy", "aria-label": "Copy to clipboard",
      onclick: async (e) => {
        const b = e.currentTarget;
        try { await navigator.clipboard.writeText(text); b.textContent = "copied"; }
        catch { b.textContent = "select"; }
        setTimeout(() => { b.textContent = "copy"; }, 1400);
      },
    }, "copy");
  }
  function padlock(open) {
    return s("svg", { class: "padlock", viewBox: "0 0 48 48", "aria-hidden": "true" },
      s("path", { d: open ? "M15 22v-7a9 9 0 0 1 17.5-3" : "M15 22v-7a9 9 0 0 1 18 0v7", fill: "none", stroke: "currentColor", "stroke-width": "4", "stroke-linecap": "round" }),
      s("rect", { x: "9", y: "22", width: "30", height: "21", rx: "4", fill: "currentColor" }),
      s("circle", { cx: "24", cy: "31", r: "3", fill: "var(--surface)" }),
      s("rect", { x: "22.5", y: "32", width: "3", height: "6", rx: "1.5", fill: "var(--surface)" }));
  }
  const clone = (x) => JSON.parse(JSON.stringify(x));

  // ---------- state ----------
  const state = {
    record: null,        // the record being shown (original, never tampered)
    source: "demo",      // demo | run
    live: false,
    speed: 1,
    timer: null,
    steps: [],
    stepIdx: 0,
    shownIters: 0,
    revealShown: false,
    raw: null,           // the record's JSON text exactly as served (for verify)
    tampered: null,      // {record, iteration, path, before, after}
    pollTimer: null,
    follow: true,
  };

  // ---------- derived ----------
  const iters = (rec) => (rec && Array.isArray(rec.iterations) ? rec.iterations : []);
  const resultsByIter = (rec) => {
    const m = new Map();
    for (const r of (rec.reveal && rec.reveal.results) || []) m.set(r.iteration, r);
    return m;
  };
  function chainBadge(receipt) {
    if (!receipt) return h("span", { class: "badge pending" }, "not anchored");
    if (receipt.status === "confirmed") {
      return h("span", { class: "chain-line" },
        h("span", { class: "badge ok" }, "on-chain"),
        receipt.explorer_url ? h("a", { href: receipt.explorer_url, target: "_blank", rel: "noopener" }, "view on Solana Explorer ↗") : null);
    }
    return h("span", { class: "chain-line" },
      h("span", { class: "badge pending" }, "pending on-chain"),
      receipt.error ? h("span", { class: "chain-err" }, receipt.error.replace(/^pending on-chain:\s*/i, "")) : null);
  }

  // ---------- header ----------
  function renderHeader(rec) {
    $("#goal").hidden = !rec.goal;
    $("#goal-text").textContent = rec.goal || "";
    const n = iters(rec).filter((it) => it.decision !== "BASELINE").length;
    const bits = [rec.title, `${rec.mode === "cached" ? "Recorded" : rec.mode === "live" ? "Live" : ""} run ${rec.run_id}`.trim(), `proposer: ${rec.proposer}`, `${rec.iterations_planned || n} iterations`];
    $("#goal-meta").textContent = bits.filter(Boolean).join(" · ");
    $("#chart-metric").textContent = rec.metric_label ? `${rec.metric_label} (higher is better)` : "";
  }

  function setStep(active) {
    const order = ["seal", "search", "reveal", "verify"];
    const ai = order.indexOf(active);
    document.querySelectorAll(".stepper li").forEach((li) => {
      const i = order.indexOf(li.dataset.step);
      li.classList.toggle("active", i === ai);
      li.classList.toggle("done", i < ai);
      if (i === ai) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    });
  }

  // ---------- seal card ----------
  function renderSeal(rec) {
    const body = $("#seal-body");
    body.replaceChildren();
    const p = rec.preregistration;
    if (!p) {
      body.append(h("div", { class: "placeholder" }, h("span", { class: "spinner" }), " Sealing the test…"));
      return;
    }
    const first = iters(rec).find((it) => it.index === 1);
    const late = p.chain && p.chain.status === "confirmed" && first && p.chain.at > first.at;
    const opened = state.revealShown && rec.reveal;
    const holdout = opened ? rec.reveal.holdout : null;
    const vault = h("div", { class: "vault" + (opened ? " open" : ""), id: "vault" },
      padlock(!!opened),
      h("strong", {}, opened ? "holdout: revealed" : "holdout: sealed"),
      opened
        ? [h("small", {}, `${holdout.seeds.length} unseen seeds × ${holdout.shifts.length + 1} scenarios`),
           h("ul", {}, holdout.shifts.map((sh) => h("li", {}, sh.label)))]
        : h("small", {}, "unseen seeds and scenario shifts; the agent never sees them"));
    body.append(h("div", { class: "card seal-card" },
      h("dl", { class: "kv" },
        h("dt", {}, "Commitment"),
        h("dd", {}, h("span", { class: "hash hash-big", title: p.commitment }, short(p.commitment, 10, 8)), copyButton(p.commitment)),
        h("dt", {}, "On Solana"),
        h("dd", {}, chainBadge(p.chain), late ? h("span", { class: "badge pending plain" }, "anchored late") : null),
        h("dt", {}, "Sealed at"),
        h("dd", {}, when(p.sealed_at), h("span", { class: "muted" }, " · before iteration 1")),
        h("dt", {}, "Memo"),
        h("dd", {}, h("code", { class: "memo" }, p.memo)),
        h("dt", {}, "Locked code"),
        h("dd", {}, h("span", { class: "hash", title: p.code_sha256 }, short(p.code_sha256)), h("span", { class: "muted" }, " sim.py + eval.py")),
        h("dt", {}, "Dev seeds"),
        h("dd", {}, h("span", { class: "mono" }, (p.dev_seeds || []).join(", ")), h("span", { class: "muted" }, " visible to the agent")),
        h("dt", {}, "Holdout"),
        h("dd", {}, h("span", { class: "hash", title: p.holdout_commitment }, short(p.holdout_commitment)), h("span", { class: "muted" }, " salted commitment"))),
      vault));
  }

  // ---------- iteration cards ----------
  function diffBlock(diff, opts = {}) {
    const lines = [h("span", { class: "ln file" }, "# policy.py")];
    for (const ch of diff) {
      const key = pyKey(ch.path);
      if (ch.before !== null && ch.before !== undefined) lines.push(h("span", { class: "ln del" }, `- ${key} = ${pyVal(ch.before)}`));
      if (ch.after !== null && ch.after !== undefined) lines.push(h("span", { class: "ln " + (opts.tamper ? "tamper" : "add") }, `+ ${key} = ${pyVal(ch.after)}${opts.tamper ? "   # edited after sealing" : ""}`));
    }
    return h("pre", { class: "diff" }, lines);
  }
  function policyBlock(candidate) {
    const lines = [h("span", { class: "ln file" }, "# policy.py")];
    const walk = (obj, prefix) => {
      for (const [k, v] of Object.entries(obj)) {
        const path = prefix ? `${prefix}.${k}` : k;
        if (v && typeof v === "object" && !Array.isArray(v)) walk(v, path);
        else lines.push(h("span", { class: "ln" }, `${pyKey(path)} = ${pyVal(v)}`));
      }
    };
    walk(candidate || {}, "");
    return h("pre", { class: "diff" }, lines);
  }
  function iterationCard(it, prevChampion) {
    const score = it.dev ? it.dev.score : null;
    let delta = null;
    if (score !== null && it.decision !== "BASELINE" && typeof prevChampion === "number") {
      const d = score - prevChampion;
      delta = h("small", { class: d > 0 ? "delta-up" : "delta-down" }, `${d > 0 ? "+" : ""}${fmt(d, Math.abs(d) < 0.1 ? 2 : 1)} vs champion`);
    } else if (it.decision === "BASELINE") delta = h("small", {}, "starting point");
    else if (score === null) delta = h("small", {}, "not scored");
    const decLabel = { KEEP: "KEEP", REVERT: "REVERT", INVALID: "INVALID", BASELINE: "BASELINE" }[it.decision] || it.decision;
    return h("li", { class: `card it ${it.decision}`, id: `it-${it.index}` },
      h("div", { class: "it-head" },
        h("span", { class: "it-idx" }, `#${it.index}`),
        h("span", { class: `dec ${it.decision}` }, decLabel),
        it.decision !== "BASELINE" && it.strategy ? h("span", { class: "it-strat" }, it.strategy) : null,
        h("span", { class: "it-score" }, h("b", {}, score === null ? "–" : fmt(score)), delta)),
      h("p", { class: "hyp" }, it.hypothesis || ""),
      it.diff && it.diff.length ? diffBlock(it.diff) : null,
      it.errors && it.errors.length ? h("ul", { class: "errors" }, it.errors.map((e) => h("li", {}, e))) : null,
      h("details", { class: "policy" }, h("summary", {}, it.decision === "BASELINE" ? "Baseline policy.py" : "Full policy.py"), policyBlock(it.candidate)));
  }
  function renderStream(rec, n, scroll) {
    const stream = $("#stream");
    const list = iters(rec).slice(0, n);
    for (const ph of stream.querySelectorAll(":scope > .placeholder")) ph.remove();
    // Drop cards that no longer exist (replay restart), append the new ones.
    while (stream.children.length > list.length) stream.lastChild.remove();
    let last = null;
    for (let i = stream.children.length; i < list.length; i++) {
      const prev = i > 0 ? list[i - 1].champion_score : null;
      last = iterationCard(list[i], prev);
      stream.append(last);
    }
    if (!list.length) stream.replaceChildren(h("li", { class: "placeholder" }, rec.status === "sealing" ? "Waiting for the seal…" : "Iterations appear here."));
    if (last && scroll && state.follow) last.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "nearest" });
  }

  // ---------- chart ----------
  function niceTicks(lo, hi, count) {
    const span = hi - lo || 1;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => span / st <= count) || 10 * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
    return out;
  }
  function renderChart(rec, n, revealShown) {
    const box = $("#chart");
    const W = Math.max(280, Math.round(box.clientWidth || 480));
    const H = W < 480 ? 230 : 290;
    const m = { l: 44, r: 16, t: 18, b: 34 };
    const all = iters(rec);
    const shown = all.slice(0, n);
    const results = rec.reveal && revealShown ? rec.reveal.results : [];
    const maxX = Math.max(1, rec.iterations_planned || 0, all.length ? all[all.length - 1].index : 0);
    // A stable scale for the whole replay: every score the run will show.
    const vals = [];
    for (const it of (state.live ? shown : all)) { if (it.dev) vals.push(it.dev.score); vals.push(it.champion_score); }
    for (const r of (rec.reveal && (state.live ? results : rec.reveal.results)) || []) vals.push(r.holdout_mean);
    const finite = vals.filter((v) => typeof v === "number" && isFinite(v));
    let lo = finite.length ? Math.min(...finite) : 0, hi = finite.length ? Math.max(...finite) : 1;
    const pad = (hi - lo || 1) * 0.12;
    lo -= pad; hi += pad;
    const ticks = niceTicks(lo, hi, 5);
    lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
    const x = (i) => m.l + (i / maxX) * (W - m.l - m.r);
    const y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (H - m.t - m.b);

    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
    const grid = s("g", { class: "grid" });
    for (const t of ticks) {
      grid.append(s("line", { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t) }));
      grid.append(s("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end" }, String(+t.toFixed(1))));
    }
    const xStep = maxX > 10 && W < 480 ? 2 : 1;
    for (let i = 0; i <= maxX; i += xStep) grid.append(s("text", { x: x(i), y: H - m.b + 18, "text-anchor": "middle" }, i === 0 ? "base" : `#${i}`));
    svg.append(grid);

    if (shown.length) {
      // Champion step line (visible score).
      let d = "";
      shown.forEach((it, k) => {
        const px = x(it.index), py = y(it.champion_score);
        d += k === 0 ? `M${px},${py}` : `H${px}V${py}`;
      });
      svg.append(s("path", { class: "visible", d }));
      for (const it of shown) {
        const px = x(it.index);
        if (it.decision === "INVALID" || !it.dev) {
          const py = y(it.champion_score) + 16, r = 5;
          svg.append(s("path", { class: "dot-invalid", d: `M${px - r},${py - r}L${px + r},${py + r}M${px + r},${py - r}L${px - r},${py + r}` }));
        } else {
          svg.append(s("circle", { class: it.decision === "REVERT" ? "dot-revert" : "dot-keep", cx: px, cy: y(it.dev.score), r: it.decision === "REVERT" ? 5 : 6 }, s("title", {}, `#${it.index} ${it.decision}: ${fmt(it.dev.score)}`)));
        }
      }
    } else {
      svg.append(s("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, "The frontier draws as iterations arrive"));
    }

    if (results.length) {
      const g = s("g", { class: "sealed-in" });
      const sorted = [...results].sort((a, b) => a.iteration - b.iteration);
      for (const r of sorted) {
        g.append(s("line", { class: r.verdict === "OVERFIT" ? "drop-bad" : "drop-ok", x1: x(r.iteration), x2: x(r.iteration), y1: y(r.dev_score), y2: y(r.holdout_mean) }));
      }
      g.append(s("path", { class: "sealed", d: sorted.map((r, k) => `${k ? "L" : "M"}${x(r.iteration)},${y(r.holdout_mean)}`).join("") }));
      for (const r of sorted) {
        const bad = r.verdict === "OVERFIT";
        g.append(s("circle", { class: bad ? "pt-bad" : "pt-ok", cx: x(r.iteration), cy: y(r.holdout_mean), r: 6.5 }, s("title", {}, `#${r.iteration} sealed ${fmt(r.holdout_mean)} (${r.verdict})`)));
      }
      const champ = rec.reveal.dev_champion_iteration, sel = rec.reveal.selected_iteration;
      const tag = (iter, cls, label) => {
        const r = sorted.find((q) => q.iteration === iter);
        if (!r) return;
        const px = x(iter), py = y(r.holdout_mean);
        const anchor = px > W - 90 ? "end" : px < m.l + 50 ? "start" : "middle";
        g.append(s("text", { class: cls, x: anchor === "end" ? px + 4 : px, y: py + 22, "text-anchor": anchor }, label));
      };
      if (rec.reveal.verdict === "OVERFIT") tag(champ, "tag-bad", "OVERFIT");
      if (sel !== null && sel !== undefined) tag(sel, "tag-ok", "selected");
      svg.append(g);
    }
    box.replaceChildren(svg);
    $("#lg-sealed").hidden = !results.length;
    box.setAttribute("aria-label", results.length
      ? "Visible dev scores per iteration, with the sealed holdout means of the finalists"
      : "Champion dev score per iteration");
  }

  // ---------- reveal ----------
  function renderReveal(rec) {
    const body = $("#reveal-body");
    const rv = rec.reveal;
    if (!rv || !state.revealShown) {
      const msg = rec.status === "revealing" ? [h("span", { class: "spinner" }), " Re-scoring every finalist on the sealed holdout…"]
        : rec.status === "failed" ? ["The run failed before the reveal."]
        : [h("span", { class: "lock-glyph", "aria-hidden": "true" }), " The holdout stays sealed until the search ends."];
      body.replaceChildren(h("div", { class: "placeholder" }, msg));
      return;
    }
    const byIt = resultsByIter(rec);
    const base = byIt.get(0);
    const champ = byIt.get(rv.dev_champion_iteration);
    const sel = rv.selected_iteration !== null && rv.selected_iteration !== undefined ? byIt.get(rv.selected_iteration) : null;
    const hyp = (i) => { const it = iters(rec).find((q) => q.index === i); return it ? it.hypothesis : ""; };

    const cards = [];
    const rejected = rv.results.filter((r) => r.verdict === "OVERFIT");
    const kept = rv.results.filter((r) => r.iteration !== 0).length;
    // The red card: the dev champion when it overfits, else the best-looking candidate that did.
    const redCard = champ && champ.verdict === "OVERFIT" ? champ
      : rejected.slice().sort((a, b) => b.dev_score - a.dev_score)[0];
    if (redCard) {
      const who = redCard === champ ? `Dev champion #${redCard.iteration}` : `#${redCard.iteration}`;
      const others = rejected.length - 1;
      cards.push(h("div", { class: "verdict bad" },
        h("div", { class: "tag" }, "OVERFIT - rejected"),
        h("div", { class: "who" }, `${who}: “${hyp(redCard.iteration)}”`),
        h("div", { class: "nums" }, `Visible score ${fmt(redCard.dev_score)} → sealed score ${fmt(redCard.holdout_mean)} (${pct(redCard.relative_gap)} drop)`),
        redCard.reasons && redCard.reasons.length ? h("ul", {}, redCard.reasons.map((r) => h("li", {}, r))) : null,
        h("div", { class: "also" }, `${rejected.length} of ${kept} kept candidate${kept === 1 ? "" : "s"} rejected on the sealed holdout${others > 0 ? "; every rejection is listed in the table" : ""}.`)));
    }
    if (champ && champ.verdict !== "OVERFIT" && (!sel || sel.iteration !== champ.iteration)) {
      cards.push(h("div", { class: "verdict ok" },
        h("div", { class: "tag" }, "GENERALISES - kept"),
        h("div", { class: "who" }, `Dev champion #${champ.iteration}: “${hyp(champ.iteration)}”`),
        h("div", { class: "nums" }, `Visible score ${fmt(champ.dev_score)} → sealed score ${fmt(champ.holdout_mean)} (${pct(champ.relative_gap)} drop)`)));
    }
    if (sel) {
      cards.push(h("div", { class: "verdict ok" },
        h("div", { class: "tag" }, "GENERALISES - selected"),
        h("div", { class: "who" }, `${champ && sel.iteration === champ.iteration ? "Dev champion " : ""}#${sel.iteration}: “${hyp(sel.iteration)}”`),
        h("div", { class: "nums" }, `Visible score ${fmt(sel.dev_score)} → sealed score ${fmt(sel.holdout_mean)}; worst shift ${fmt(sel.worst_score)} (${sel.shift_labels[sel.worst_shift_id] || sel.worst_shift_id})`),
        h("ul", {}, h("li", {}, "Holds up on every sealed shift; no scenario falls below the naive baseline."))));
    } else if (!sel) {
      cards.push(h("div", { class: "verdict bad" }, h("div", { class: "tag" }, "No finalist generalises"), h("div", { class: "who" }, "Nothing is selected; the honest answer is the baseline.")));
    }

    const shiftIds = ["base", ...rv.holdout.shifts.map((x) => x.id)];
    const labels = (base && base.shift_labels) || {};
    const head = h("tr", {}, h("th", { scope: "col" }, "Candidate"), h("th", { scope: "col" }, "Visible (dev)"),
      shiftIds.map((id) => h("th", { scope: "col" }, labels[id] || id)),
      h("th", { scope: "col" }, "Sealed mean"), h("th", { scope: "col" }, "Gap"), h("th", { scope: "col" }, "Verdict"));
    const rows = [...rv.results].sort((a, b) => a.iteration - b.iteration).map((r, k) => {
      const isChamp = r.iteration === rv.dev_champion_iteration, isSel = r.iteration === rv.selected_iteration;
      const bad = r.verdict === "OVERFIT";
      const cls = isChamp && bad ? "row-bad" : isSel ? "row-sel" : "";
      return h("tr", { class: cls, style: `animation-delay:${0.25 + k * 0.18}s` },
        h("th", { scope: "row" }, r.iteration === 0 ? "#0 baseline" : `#${r.iteration}`, isChamp ? h("div", { class: "vtag muted" }, "dev champion") : null),
        h("td", {}, fmt(r.dev_score)),
        shiftIds.map((id) => {
          const v = r.per_shift[id];
          const below = r.iteration !== 0 && base && typeof base.per_shift[id] === "number" && v < base.per_shift[id];
          return h("td", { class: below ? "collapse" : "", title: below ? `below the naive baseline (${fmt(base.per_shift[id])})` : null }, fmt(v));
        }),
        h("td", { class: "mean" }, fmt(r.holdout_mean)),
        h("td", {}, pct(r.relative_gap)),
        h("td", { class: "vcell" }, h("span", { class: `vtag ${bad ? "bad" : "ok"}` }, bad ? "OVERFIT - rejected" : isSel ? "GENERALISES - selected" : "GENERALISES")));
    });

    body.replaceChildren(h("div", { class: "reveal-wrap" },
      h("div", { class: "verdicts" }, cards),
      h("div", { class: "card table-card" },
        h("div", { class: "table-scroll", tabindex: "0", "aria-label": "Finalists by holdout scenario" },
          h("table", { class: "shifts" }, h("thead", {}, head), h("tbody", {}, rows))),
        h("div", { class: "table-note" }, h("span", { class: "swipe" }, "Swipe the table for every scenario. "), `Scores on ${rv.holdout.seeds.length} unseen seeds per scenario. Red numbers fall below the naive baseline under that scenario. Gate: reject a gap over the sealed limit or any scenario worse than baseline.`)),
      h("div", { class: "card memo-row" },
        h("strong", {}, "Reveal memo"),
        chainBadge(rv.chain),
        h("code", { class: "memo" }, rv.memo))));
  }

  // ---------- verify ----------
  function verifyRecord() { return state.tampered ? state.tampered.record : state.record; }
  // The verify body is built from the exact JSON text the server sent: a plain JSON.stringify
  // would turn 33.0 into 33 and change the hash of an untouched bundle.
  function verifyPayload() {
    const raw = state.raw, t = state.tampered;
    if (!raw) return JSON.stringify({ record: verifyRecord() });
    if (!t) return `{"record":${raw}}`;
    if (typeof JSON.rawJSON === "function") {
      try {
        const keep = (k, v, ctx) => (typeof v === "number" && ctx && typeof ctx.source === "string" && ctx.source !== String(v) ? JSON.rawJSON(ctx.source) : v);
        const wire = JSON.parse(raw, keep);
        const it = wire.iterations.find((q) => q.index === t.iteration);
        setPath(it.candidate, t.path, t.after);
        return JSON.stringify({ record: wire });
      } catch { /* fall back to the parsed copy below */ }
    }
    return JSON.stringify({ record: t.record });
  }
  async function runVerify() {
    const rec = verifyRecord();
    if (!rec) return;
    setStep("verify");
    const out = $("#verify-result");
    out.replaceChildren(h("div", { class: "placeholder small" }, h("span", { class: "spinner" }), " Re-hashing the bundle and reading the on-chain memos…"));
    $("#btn-verify").disabled = true;
    try {
      const res = await fetch("/sealed/api/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: verifyPayload() });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      renderVerify(body);
    } catch (err) {
      out.replaceChildren(h("div", { class: "result pending" }, h("div", { class: "headline" }, h("span", { class: "icon" }, "!"), "Could not verify"), h("p", {}, String(err.message || err))));
    } finally {
      $("#btn-verify").disabled = false;
    }
  }
  function renderVerify(rep) {
    let cls, icon, headline;
    if (!rep.ok) { cls = "bad"; icon = "✕"; headline = "Does not match the sealed record"; }
    else if (rep.onchain_status === "confirmed") { cls = "ok"; icon = "✓"; headline = "Matches the on-chain record"; }
    else if (rep.onchain_status === "unreachable") { cls = "pending"; icon = "✓"; headline = "Matches the recorded commitment; devnet unreachable, on-chain memo not read"; }
    else { cls = "pending"; icon = "✓"; headline = "Matches the recorded commitment; on-chain record pending"; }
    const mark = (v) => (v === true ? h("span", { class: "mk y" }, "✓") : v === false ? h("span", { class: "mk n" }, "✕") : h("span", { class: "mk p" }, "…"));
    const onchainOk = rep.onchain_status === "confirmed" ? true : null;
    const checks = [
      [rep.code_match, "Sealed evaluation code (sim.py + eval.py) is unchanged", null],
      [rep.commit_match, "Commitment recomputed from the bundle", rep.commitment_recomputed ? `${short(rep.commitment_recomputed, 10, 8)}${rep.commitment_onchain ? `  on chain ${short(rep.commitment_onchain, 10, 8)}` : ""}` : null],
      [rep.result_match, "Result hash over every candidate, score and verdict", rep.result_recomputed ? `recomputed ${short(rep.result_recomputed, 10, 8)}  recorded ${short(rep.result_recorded, 10, 8)}` : null],
      [onchainOk, rep.onchain_status === "confirmed" ? "Both memos read back from Solana devnet" : rep.onchain_status === "unreachable" ? "On-chain memos could not be fetched" : "On-chain memos pending (wallet not funded yet)", null],
    ];
    const reasons = rep.reasons || [];
    $("#verify-result").replaceChildren(h("div", { class: `result ${cls}` },
      h("div", { class: "headline" }, h("span", { class: "icon", "aria-hidden": "true" }, icon), headline),
      h("ul", { class: "checks" }, checks.map(([v, label, hash]) => h("li", {}, mark(v), h("span", {}, label, hash ? h("div", { class: "h" }, hash) : null)))),
      reasons.length ? h("ul", { class: "reasons" }, reasons.map((r) => h("li", {}, r))) : null));
  }
  function firstNumericPath(obj, prefix = "") {
    for (const [k, v] of Object.entries(obj || {})) {
      const path = prefix ? `${prefix}.${k}` : k;
      if (typeof v === "number") return path;
      if (v && typeof v === "object" && !Array.isArray(v)) { const p = firstNumericPath(v, path); if (p) return p; }
    }
    return null;
  }
  function getPath(obj, path) { return path.split(".").reduce((o, k) => (o ? o[k] : undefined), obj); }
  function setPath(obj, path, val) { const ks = path.split("."); const last = ks.pop(); ks.reduce((o, k) => o[k], obj)[last] = val; }
  function tamper() {
    const rec = state.record;
    if (!rec) return;
    const target = rec.reveal && rec.reveal.selected_iteration !== null && rec.reveal.selected_iteration !== undefined
      ? rec.reveal.selected_iteration
      : (rec.reveal ? rec.reveal.dev_champion_iteration : iters(rec).length - 1);
    const copy = clone(rec);
    const it = iters(copy).find((q) => q.index === target) || iters(copy)[iters(copy).length - 1];
    if (!it) return;
    const numericDiff = (it.diff || []).find((c) => typeof getPath(it.candidate, c.path) === "number");
    const path = numericDiff ? numericDiff.path : firstNumericPath(it.candidate);
    if (!path) return;
    const before = getPath(it.candidate, path);
    const after = Number.isInteger(before) ? before + 1 : +(before + 0.01).toFixed(4);
    setPath(it.candidate, path, after);
    state.tampered = { record: copy, iteration: it.index, path, before, after };
    $("#btn-restore").hidden = false;
    $("#tamper-note").replaceChildren(h("div", {},
      h("p", { class: "sub", style: "margin:0 0 6px" }, `One number changed in candidate #${it.index}, after the seal (a reviewer would never spot it):`),
      diffBlock([{ path, before, after }], { tamper: true })));
    runVerify();
  }
  function restore() {
    state.tampered = null;
    $("#btn-restore").hidden = true;
    $("#tamper-note").replaceChildren();
    runVerify();
  }
  function resetVerify() {
    state.tampered = null;
    $("#btn-restore").hidden = true;
    $("#tamper-note").replaceChildren();
    $("#verify-result").replaceChildren(h("div", { class: "placeholder small" }, "Press ", h("strong", {}, "Verify against chain"), " to re-hash this run."));
  }
  function setVerifyEnabled(on) {
    $("#btn-verify").disabled = !on;
    $("#btn-tamper").disabled = !on;
  }
  function setDownload(rec) {
    const a = $("#dl-bundle");
    if (state.source === "run" && rec.run_id) {
      a.href = `/sealed/api/runs/${encodeURIComponent(rec.run_id)}/bundle`;
    } else {
      if (a.dataset.blob) URL.revokeObjectURL(a.dataset.blob);
      const url = URL.createObjectURL(new Blob([JSON.stringify(rec, null, 2)], { type: "application/json" }));
      a.dataset.blob = url;
      a.href = url;
    }
    a.setAttribute("download", `sealed-${rec.run_id}.json`);
  }

  // ---------- replay ----------
  function stopTimer() { if (state.timer) clearTimeout(state.timer); state.timer = null; }
  function progress() {
    const total = state.steps.length || 1;
    $("#progress-bar").style.width = `${Math.min(100, (state.stepIdx / total) * 100)}%`;
  }
  function buildSteps(rec) {
    const steps = [{ wait: 0, run: () => { renderSeal(rec); setStep("seal"); } }];
    iters(rec).forEach((_, i) => {
      steps.push({ wait: i === 0 ? 3800 : 3000, run: () => { state.shownIters = i + 1; setStep("search"); renderStream(rec, i + 1, true); renderChart(rec, i + 1, false); } });
    });
    if (rec.reveal) {
      steps.push({ wait: 3200, run: showReveal });
      steps.push({ wait: 5000, run: () => runVerify() });
    }
    return steps;
  }
  function showReveal() {
    const rec = state.record;
    state.shownIters = iters(rec).length;
    state.revealShown = true;
    setStep("reveal");
    renderStream(rec, state.shownIters, false);
    renderChart(rec, state.shownIters, true);
    renderSeal(rec);
    renderReveal(rec);
    setVerifyEnabled(true);
    if (state.follow) $("#reveal").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }
  function tick() {
    if (state.stepIdx >= state.steps.length) { stopTimer(); progress(); return; }
    const step = state.steps[state.stepIdx++];
    step.run();
    progress();
    if (state.stepIdx < state.steps.length) state.timer = setTimeout(tick, state.steps[state.stepIdx].wait / state.speed);
  }
  function replay() {
    stopTimer();
    const rec = state.record;
    if (!rec) return;
    state.shownIters = 0;
    state.revealShown = false;
    resetVerify();
    setVerifyEnabled(false);
    $("#stream").replaceChildren();
    renderHeader(rec);
    renderStream(rec, 0, false);
    renderChart(rec, 0, false);
    renderReveal(rec);
    setDownload(rec);
    state.steps = buildSteps(rec);
    state.stepIdx = 0;
    state.follow = true;
    window.scrollTo({ top: 0 });
    tick();
  }
  function skipToReveal() {
    const rec = state.record;
    if (!rec) return;
    stopTimer();
    renderSeal(rec);
    if (!rec.reveal) { state.shownIters = iters(rec).length; renderStream(rec, state.shownIters, false); renderChart(rec, state.shownIters, false); return; }
    showReveal();
    const vi = state.steps.length - 1;
    state.stepIdx = Math.max(0, vi);
    progress();
    state.timer = setTimeout(tick, 1600 / state.speed);
  }

  // ---------- live ----------
  function renderLive(rec) {
    state.record = rec;
    renderHeader(rec);
    state.revealShown = !!rec.reveal;
    renderSeal(rec);
    const n = iters(rec).length;
    const grew = n > state.shownIters;
    state.shownIters = n;
    renderStream(rec, n, grew);
    renderChart(rec, n, state.revealShown);
    renderReveal(rec);
    setDownload(rec);
    const stepFor = { sealing: "seal", running: "search", revealing: "reveal", done: "reveal", failed: "search" };
    setStep(stepFor[rec.status] || "search");
    const banner = $("#banner");
    banner.hidden = false;
    if (rec.status === "failed") { banner.className = "banner err"; banner.textContent = `Live run ${rec.run_id} failed: ${rec.error || "unknown error"}`; }
    else if (rec.status === "done") { banner.className = "banner live"; banner.textContent = `Live run ${rec.run_id} finished. Verify it below.`; }
    else { banner.className = "banner live"; banner.textContent = `Live run ${rec.run_id}: ${rec.status}… (${Math.max(0, n - 1)} of ${rec.iterations_planned} iterations)`; }
    setVerifyEnabled(rec.status === "done");
    $("#progress-bar").style.width = `${Math.min(100, (Math.max(0, n - 1) / Math.max(1, rec.iterations_planned)) * 90 + (rec.status === "done" ? 10 : 0))}%`;
  }
  async function poll(runId) {
    try {
      const res = await fetch(`/sealed/api/runs/${encodeURIComponent(runId)}`, { cache: "no-store" });
      const text = await res.text();
      const body = JSON.parse(text);
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      state.raw = text;
      renderLive(body);
      if (body.status === "done" || body.status === "failed") { state.pollTimer = null; if (body.status === "done") runVerify(); return; }
    } catch (err) {
      $("#live-msg").textContent = `Polling failed: ${err.message || err}`;
    }
    state.pollTimer = setTimeout(() => poll(runId), 1000);
  }
  function enterLive(runId) {
    stopTimer();
    if (state.pollTimer) clearTimeout(state.pollTimer);
    state.live = true;
    state.source = "run";
    state.shownIters = 0;
    $("#stream").replaceChildren();
    resetVerify();
    $("#btn-replay").textContent = "Back to demo";
    $("#btn-skip").disabled = true;
    document.querySelectorAll(".seg button").forEach((b) => { b.disabled = true; });
    poll(runId);
  }
  async function startLive(ev) {
    ev.preventDefault();
    const f = ev.currentTarget;
    const body = { env: (state.record && state.record.env) || "epidemic", proposer: f.proposer.value, iterations: Number(f.iterations.value), anchor: f.anchor.checked };
    $("#live-msg").textContent = "Starting…";
    try {
      const res = await fetch("/sealed/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const out = await res.json();
      if (res.status !== 202) throw new Error(out.error || `HTTP ${res.status}`);
      $("#live-msg").textContent = `Run ${out.run_id} started.`;
      history.replaceState(null, "", `?run=${encodeURIComponent(out.run_id)}&live=1`);
      $("#live-form").hidden = true;
      $("#btn-live").setAttribute("aria-expanded", "false");
      enterLive(out.run_id);
    } catch (err) {
      $("#live-msg").textContent = `Could not start: ${err.message || err}`;
    }
  }

  // ---------- boot ----------
  async function loadRecord() {
    const params = new URLSearchParams(location.search);
    const runId = params.get("run");
    if (runId && params.get("live")) { enterLive(runId); return; }
    const url = runId ? `/sealed/api/runs/${encodeURIComponent(runId)}` : "/sealed/api/demo";
    state.source = runId ? "run" : "demo";
    try {
      const res = await fetch(url, { cache: "no-store" });
      const text = await res.text();
      const body = JSON.parse(text);
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      state.record = body;
      state.raw = text;
      if (runId && body.status !== "done" && body.status !== "failed") { enterLive(runId); return; }
      replay();
    } catch (err) {
      const b = $("#banner");
      b.hidden = false;
      b.className = "banner err";
      b.textContent = `Could not load the run: ${err.message || err}`;
      $("#seal-body").replaceChildren(h("div", { class: "placeholder" }, "No run to show."));
    }
  }

  $("#btn-replay").addEventListener("click", () => {
    if (state.live) { location.href = "/sealed"; return; }
    replay();
  });
  $("#btn-skip").addEventListener("click", skipToReveal);
  document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
    state.speed = Number(b.dataset.speed);
    document.querySelectorAll(".seg button").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
    if (state.timer && state.stepIdx < state.steps.length) { stopTimer(); state.timer = setTimeout(tick, state.steps[state.stepIdx].wait / state.speed); }
  }));
  $("#btn-live").addEventListener("click", () => {
    const f = $("#live-form");
    f.hidden = !f.hidden;
    $("#btn-live").setAttribute("aria-expanded", String(!f.hidden));
  });
  $("#live-form").addEventListener("submit", startLive);
  $("#btn-verify").addEventListener("click", runVerify);
  $("#btn-tamper").addEventListener("click", tamper);
  $("#btn-restore").addEventListener("click", restore);
  // A manual scroll during the replay stops the auto-follow.
  for (const evt of ["wheel", "touchmove"]) window.addEventListener(evt, () => { state.follow = false; }, { passive: true });
  let lastW = 0;
  new ResizeObserver(() => {
    const w = $("#chart").clientWidth;
    if (state.record && Math.abs(w - lastW) > 4) { lastW = w; renderChart(state.record, state.shownIters, state.revealShown); }
  }).observe($("#chart"));

  setVerifyEnabled(false);
  loadRecord();
})();
