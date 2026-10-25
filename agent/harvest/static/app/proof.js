"use strict";
/* Proof the scoring is real: /app/replay (tournament replay), /app/impact (real-world payoff) and the calibration
   section under /app/leaderboard. Reads /harvest/api/replay/timeline, /harvest/api/impact and /harvest/api/calibration. */
(() => {
  const API = "/harvest/api";
  const NS = "http://www.w3.org/2000/svg";
  const page = document.body.dataset.page;

  function h(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "style") n.style.cssText = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  function s(tag, attrs, ...kids) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) n.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid != null) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  async function get(path) {
    const r = await fetch(API + path, { cache: "no-store" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }
  const b3 = (x) => (x == null ? "–" : Number(x).toFixed(3));
  const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
  const credits = (x) => Math.round(x).toLocaleString("en-GB");
  const main = () => document.getElementById("main");
  const fail = (e) => h("p", { class: "err" }, e.message || String(e));
  const css = `
    .race { position:relative; margin:12px 0 4px; }
    .race .r { position:absolute; top:0; left:0; right:0; height:40px; display:flex; align-items:center; gap:10px; transition:transform .6s ease, opacity .4s; }
    .race .nm { width:220px; flex:none; font-weight:600; font-size:15px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .race .track { flex:1 1 auto; height:26px; background:var(--soft); border-radius:6px; overflow:hidden; }
    .race .fill { height:100%; border-radius:6px; }
    .race .v { width:118px; flex:none; text-align:right; font-weight:800; font-variant-numeric:tabular-nums; font-size:15px; }
    .race .v small { display:block; font-weight:400; font-size:12px; color:var(--muted); }
    .quarter { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
    .quarter b { font-size:40px; font-weight:800; font-variant-numeric:tabular-nums; }
    .prog { height:6px; background:var(--line); border-radius:3px; margin:8px 0 4px; }
    .prog div { height:100%; background:var(--green); border-radius:3px; width:0; }
    svg.chart { width:100%; height:auto; display:block; font:12px system-ui,sans-serif; }
    table.t { width:100%; border-collapse:collapse; font-size:16px; }
    table.t th, table.t td { padding:8px 6px; border-bottom:1px solid var(--line); text-align:right; }
    table.t th:first-child, table.t td:first-child { text-align:left; }
    .dot { display:inline-block; width:12px; height:12px; border-radius:50%; margin-right:6px; vertical-align:-1px; }
    .stat { font-size:44px; font-weight:800; line-height:1.05; }
    @media (max-width:520px) { table.t { font-size:14px; } table.t th, table.t td { padding:6px 3px; } .race .nm { width:120px; font-size:12px; } .race .v { width:92px; font-size:13px; } }`;
  document.head.append(h("style", {}, css));

  // ------------------------------------------------------------ s. tournament replay
  async function replayPage() {
    let t;
    try { t = await get("/replay/timeline"); } catch (e) { main().replaceChildren(fail(e)); return; }
    if (!t.has_replay || !t.steps.length) { main().replaceChildren(h("p", { class: "muted" }, "The backtest has not been run yet.")); return; }
    const agents = t.agents, steps = t.steps, n = steps.length, ROW = 44, TOP = Math.min(agents.length, 12);
    const star = new Set(t.final.slice(0, TOP).map((r) => r.id));
    const maxBank = Math.max(...steps.flatMap((st) => agents.map((a) => st.agents[a.id].bankroll)));
    const STEP_MS = 30000 / n;
    let pos = 0, speed = 1, playing = false, last = 0;

    const quarter = h("b", {}, steps[0].cutoff), phase = h("span", { class: "tag" }, "dev");
    const progress = h("div");
    const rows = new Map(agents.map((a) => {
      const fill = h("div", { class: "fill", style: `background:${a.color || "#2b8a3e"}` });
      const v = h("div", { class: "v" });
      const r = h("div", { class: "r" }, h("div", { class: "nm", title: a.name }, a.name), h("div", { class: "track" }, fill), v);
      return [a.id, { r, fill, v }];
    }));
    const race = h("div", { class: "race", style: `height:${TOP * ROW}px` }, [...rows.values()].map((x) => x.r));
    const chart = s("svg", { class: "chart", viewBox: "0 0 640 240", role: "img", "aria-label": "Cumulative Brier score per agent" });
    const finalBox = h("div");
    const playBtn = h("button", { class: "btn small primary", onclick: () => toggle() }, "▶ Play");
    const speeds = [0.5, 1, 2, 4].map((x) => h("button", { class: "chip" + (x === 1 ? " on" : ""), onclick: (ev) => {
      speed = x; for (const b of ev.target.parentNode.children) b.classList.toggle("on", b === ev.target);
    } }, `${x}×`));

    main().replaceChildren(
      h("h1", {}, "Tournament replay"),
      h("p", { class: "lead" }, `${agents.length} AI agents trade ${n} quarters of real European crop prices. Each one only sees data up to the quarter it bets in; the last quarters are a sealed holdout.`),
      h("div", { class: "actions", style: "align-items:center" }, playBtn,
        h("button", { class: "btn small", onclick: () => { pos = 0; finalBox.replaceChildren(); draw(); } }, "↺ Restart"),
        h("div", { class: "chips", style: "margin:0" }, speeds)),
      h("div", { class: "quarter" }, quarter, phase),
      h("div", { class: "prog" }, progress),
      h("h2", { style: "margin-top:14px" }, "Bankroll (play credits)"),
      h("p", { class: "muted small", style: "margin-top:-6px" }, `1,000 to start, plus every quarter's profit or loss carried forward, so a bad trader can end below zero.${agents.length > TOP ? ` The top ${TOP} of ${agents.length} are shown at each moment.` : ""}`),
      race,
      h("h2", {}, "Brier score so far (lower is better)"),
      chart,
      h("p", { class: "muted small" }, t.note),
      finalBox);

    // chart scaffolding
    const all = steps.flatMap((st) => [...agents.map((a) => st.agents[a.id].brier_cum), st.market_brier_cum]).filter((x) => x != null);
    const lo = Math.max(0, Math.floor((Math.min(...all) - 0.01) * 50) / 50), hi = Math.max(0.26, Math.ceil((Math.max(...all) + 0.01) * 50) / 50);
    const X = (i) => 46 + (i / Math.max(1, n - 1)) * 580, Y = (b) => 12 + (1 - (b - lo) / (hi - lo)) * 200;
    const holdStart = steps.findIndex((st) => st.split === "holdout");
    if (holdStart >= 0) chart.append(s("rect", { x: X(holdStart) - 8, y: 8, width: X(n - 1) - X(holdStart) + 14, height: 208, fill: "#fff4e6" }),
      s("text", { x: X(holdStart) - 4, y: 208, fill: "#d9480f" }, "sealed holdout"));
    for (let v = lo; v <= hi + 1e-9; v += 0.04) {
      chart.append(s("line", { x1: 46, x2: 626, y1: Y(v), y2: Y(v), stroke: "#e3e3e3" }), s("text", { x: 40, y: Y(v) + 4, "text-anchor": "end", fill: "#6b6b6b" }, v.toFixed(2)));
    }
    chart.append(s("line", { x1: 46, x2: 626, y1: Y(0.25), y2: Y(0.25), stroke: "#c92a2a", "stroke-dasharray": "4 4" }),
      s("text", { x: 624, y: Y(0.25) - 4, "text-anchor": "end", fill: "#c92a2a" }, "coin flip 0.25"));
    steps.forEach((st, i) => { if (i % 2 === 0 || i === n - 1) chart.append(s("text", { x: X(i), y: 234, "text-anchor": i === n - 1 ? "end" : "middle", fill: "#6b6b6b" }, st.cutoff)); });
    const lines = new Map(agents.map((a) => [a.id, star.has(a.id) ? s("polyline", { fill: "none", stroke: a.color || "#2b8a3e", "stroke-width": 2.5 })
      : s("polyline", { fill: "none", stroke: "#ced4da", "stroke-width": 1, opacity: 0.6 })]));
    const marketLine = s("polyline", { fill: "none", stroke: "#1a1a1a", "stroke-width": 2, "stroke-dasharray": "6 3" });
    chart.append(...agents.filter((a) => !star.has(a.id)).map((a) => lines.get(a.id)), ...agents.filter((a) => star.has(a.id)).map((a) => lines.get(a.id)), marketLine);
    const marketLabel = s("text", { fill: "#1a1a1a", "font-weight": 700 }, "market");
    chart.append(marketLabel);

    function path(get, upto) {
      const pts = [];
      const whole = Math.floor(upto);
      for (let i = 0; i <= whole; i++) { const b = get(steps[i]); if (b != null) pts.push(`${X(i)},${Y(b)}`); }
      const frac = upto - whole;
      if (frac > 0 && whole + 1 < n) {
        const a = get(steps[whole]), b = get(steps[whole + 1]);
        if (a != null && b != null) pts.push(`${X(whole + frac)},${Y(a + (b - a) * frac)}`);
      }
      return pts.join(" ");
    }
    function lerp(id, key, p) {
      const i = Math.min(n - 1, Math.floor(p)), j = Math.min(n - 1, i + 1), f = p - i;
      const a = steps[i].agents[id][key], b = steps[j].agents[id][key];
      return a == null || b == null ? a : a + (b - a) * f;
    }
    function draw() {
      const i = Math.min(n - 1, Math.floor(pos));
      quarter.textContent = steps[i].cutoff;
      phase.textContent = steps[i].split === "holdout" ? "sealed holdout" : "dev quarter";
      phase.className = "tag " + (steps[i].split === "holdout" ? "no" : "");
      progress.style.width = `${(pos / (n - 1)) * 100}%`;
      const at = steps[Math.min(n - 1, Math.round(pos))].agents; // re-rank once per quarter, so rows glide instead of jitter
      const order = agents.map((a) => [a.id, lerp(a.id, "bankroll", pos), at[a.id].bankroll]).sort((x, y) => y[2] - x[2]);
      order.forEach(([id, bank], rank) => {
        const r = rows.get(id);
        r.r.style.transform = `translateY(${Math.min(rank, TOP) * ROW}px)`;
        r.r.style.opacity = rank < TOP ? "1" : "0";
        r.fill.style.width = `${Math.max(1, (bank / maxBank) * 100)}%`;
        r.v.replaceChildren(credits(bank), h("small", {}, `Brier ${b3(lerp(id, "brier_cum", pos))}`));
      });
      for (const a of agents) lines.get(a.id).setAttribute("points", path((st) => st.agents[a.id].brier_cum, pos));
      marketLine.setAttribute("points", path((st) => st.market_brier_cum, pos));
      const mb = steps[i].market_brier_cum;
      if (mb != null) { marketLabel.setAttribute("x", Math.min(X(pos) + 4, 590)); marketLabel.setAttribute("y", Y(mb) - 6); }
    }
    const table = (rowsF) => h("table", { class: "t" }, h("tr", {}, h("th", {}, "World model"), h("th", {}, "Bankroll"), h("th", {}, "Brier"), h("th", {}, "Brier rank")), rowsF);
    function finish() {
      const row = (r) => h("tr", {},
        h("td", {}, `${r.rank_bankroll}. `, h("span", { class: "dot", style: `background:${r.color}` }), r.name),
        h("td", {}, credits(r.bankroll)), h("td", {}, b3(r.brier_cum)), h("td", {}, `#${r.rank_brier}`));
      const last = steps[n - 1];
      finalBox.replaceChildren(h("h2", {}, "Final ranking"),
        table(t.final.slice(0, TOP).map(row)),
        t.final.length > TOP && h("details", {}, h("summary", {}, `All ${t.final.length} agents`), table(t.final.slice(TOP).map(row))),
        h("p", { class: "muted small" }, `The market's own Brier over the same questions: ${b3(last.market_brier_cum)}. A coin flip scores 0.250. `,
          h("a", { href: "/app/impact" }, "What would this have meant in the real world? →")));
    }
    function frame(ts) {
      if (!playing) return;
      const dt = last ? ts - last : 0;
      last = ts;
      pos = Math.min(n - 1, pos + (dt / STEP_MS) * speed);
      draw();
      if (pos >= n - 1) { playing = false; playBtn.textContent = "▶ Play"; finish(); return; }
      requestAnimationFrame(frame);
    }
    function toggle() {
      if (!playing && pos >= n - 1) { pos = 0; finalBox.replaceChildren(); }
      playing = !playing;
      playBtn.textContent = playing ? "❚❚ Pause" : "▶ Play";
      last = 0;
      if (playing) requestAnimationFrame(frame);
    }
    draw();
    const q = new URLSearchParams(location.search);
    if (q.has("at")) { pos = Math.max(0, Math.min(n - 1, Number(q.get("at")) || 0)); draw(); if (pos >= n - 1) finish(); }
    else if (q.get("autoplay") !== "0") toggle();
  }

  // ------------------------------------------------------------ o. real-world payoff
  function intervalBars(block) {
    const items = [["market", "The Crophecy market", "#2b8a3e"], ["consensus", "Agent consensus", "#7048e8"],
      ["base_rate", "Historical base rate", "#868e96"], ["coin", "Coin flip", "#c92a2a"]];
    const max = 0.4, W = 640, L = 170, R = 600;
    const X = (b) => L + (Math.min(b, max) / max) * (R - L);
    const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${items.length * 46 + 30}`, role: "img", "aria-label": "Brier score with 90% intervals" });
    for (let v = 0; v <= max + 1e-9; v += 0.1) svg.append(s("line", { x1: X(v), x2: X(v), y1: 6, y2: items.length * 46 + 6, stroke: "#e3e3e3" }),
      s("text", { x: X(v), y: items.length * 46 + 24, "text-anchor": "middle", fill: "#6b6b6b" }, v.toFixed(1)));
    items.forEach(([k, label, color], i) => {
      const b = block[k] || {}, y = 14 + i * 46;
      if (b.brier == null) return;
      svg.append(s("text", { x: L - 10, y: y + 16, "text-anchor": "end", "font-size": 15, fill: "#1a1a1a" }, label),
        s("rect", { x: L, y, width: X(b.brier) - L, height: 24, fill: color, rx: 4, opacity: 0.85 }),
        s("text", { x: X(Math.max(b.brier, b.hi ?? 0)) + 8, y: y + 17, "font-weight": 700, "font-size": 15, fill: "#1a1a1a" }, b3(b.brier)));
      if (b.lo != null) svg.append(s("line", { x1: X(b.lo), x2: X(b.hi), y1: y + 12, y2: y + 12, stroke: "#1a1a1a", "stroke-width": 2 }),
        s("line", { x1: X(b.lo), x2: X(b.lo), y1: y + 5, y2: y + 19, stroke: "#1a1a1a", "stroke-width": 2 }),
        s("line", { x1: X(b.hi), x2: X(b.hi), y1: y + 5, y2: y + 19, stroke: "#1a1a1a", "stroke-width": 2 }));
    });
    return svg;
  }
  async function impactPage() {
    let d;
    try { d = await get("/impact"); } catch (e) { main().replaceChildren(fail(e)); return; }
    if (!d.has_replay) { main().replaceChildren(h("p", { class: "muted" }, "The backtest has not been run yet.")); return; }
    const ho = d.holdout, w = d.early_warning.holdout, lvl = Math.round(d.interval_level * 100);
    const m = ho.market;
    main().replaceChildren(
      h("h1", {}, "What it would have meant"),
      h("p", { class: "lead" }, "We replayed the agents' market over real European crop prices, sealed its forecasts, and only then looked at what happened. Here is the honest scorecard."),
      h("div", { class: "card " + (m.beats_base_ci ? "good" : "") }, d.summary),
      h("h2", {}, `Accuracy on ${ho.n} sealed questions`),
      h("p", { class: "muted small" }, `Brier score, lower is better. Black whiskers are ${lvl}% bootstrap intervals.`),
      intervalBars(ho),
      h("p", { class: "small" }, `Market minus base rate: ${b3(m.diff_vs_base)} Brier (${lvl}% interval ${b3(m.diff_lo)} to ${b3(m.diff_hi)}). `,
        m.beats_base_ci ? "The whole interval is below zero, so this is unlikely to be luck." : "The interval crosses zero, so this could be luck."),
      h("h2", {}, "Early warning for buyers"),
      h("div", { class: "big-btns" },
        h("div", { class: "card" }, h("div", { class: "stat" }, `${w.market.flagged} of ${w.market.events}`),
          `adverse price moves the market gave 50%+ odds when the question opened, on average ${w.market.lead_months_avg ?? "–"} months before they resolved.`),
        h("div", { class: "card" }, h("div", { class: "stat" }, `${w.base_rate.flagged} of ${w.base_rate.events}`),
          "flagged by the historical base rate over the same questions.")),
      h("p", { class: "small" }, `False alarms: the market ${w.market.false_alarms}, the base rate ${w.base_rate.false_alarms} (out of ${ho.n - w.market.events} questions where nothing bad happened). `,
        h("span", { class: "muted" }, d.early_warning.note)),
      h("h2", {}, "By commodity (sealed holdout)"),
      h("table", { class: "t" },
        h("tr", {}, h("th", {}, "Commodity"), h("th", {}, "n"), h("th", {}, "Market"), h("th", {}, "Base rate"), h("th", {}, "Better")),
        d.by_commodity.map((c) => h("tr", {}, h("td", {}, c.commodity), h("td", {}, c.n), h("td", {}, b3(c.market.brier)), h("td", {}, b3(c.base_rate.brier)),
          h("td", {}, c.market.brier < c.base_rate.brier ? h("span", { class: "tag yes" }, "market") : h("span", { class: "tag" }, "base"))))),
      h("p", { class: "muted small" }, "Each commodity has only 3 to 9 sealed questions, so single rows are noisy; read the pattern, not one cell."),
      h("h2", {}, "Who would pay for this"),
      h("div", {}, d.payers.map((p) => h("div", { class: "agent" }, h("b", {}, p.who), h("span", { class: "why" }, p.why)))),
      h("h2", {}, "What this does not show"),
      h("ul", { class: "small" }, d.caveats.map((c) => h("li", {}, c))),
      h("p", { class: "small" }, h("a", { href: "/app/replay" }, "Watch the tournament replay →"), " · ",
        h("a", { href: "/app/leaderboard#calibration" }, "Calibration per agent →")));
  }

  // ------------------------------------------------------------ n. calibration (under the leaderboard)
  function reliability(c) {
    const W = 360, P = 40, S = W - P - 12;
    const X = (p) => P + p * S, Y = (p) => 10 + (1 - p) * S;
    const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${W + 12}`, style: "max-width:420px", role: "img", "aria-label": "Reliability diagram" });
    for (let v = 0; v <= 1.0001; v += 0.2) {
      svg.append(s("line", { x1: X(v), x2: X(v), y1: Y(0), y2: Y(1), stroke: "#eee" }), s("line", { x1: X(0), x2: X(1), y1: Y(v), y2: Y(v), stroke: "#eee" }),
        s("text", { x: X(v), y: Y(0) + 16, "text-anchor": "middle", fill: "#6b6b6b" }, pct(v)), s("text", { x: P - 6, y: Y(v) + 4, "text-anchor": "end", fill: "#6b6b6b" }, pct(v)));
    }
    svg.append(s("line", { x1: X(0), y1: Y(0), x2: X(1), y2: Y(1), stroke: "#adb5bd", "stroke-dasharray": "5 4" }),
      s("text", { x: X(0.5), y: W + 10, "text-anchor": "middle", fill: "#1a1a1a" }, "forecast probability"),
      s("text", { x: 12, y: Y(0.5), transform: `rotate(-90 12 ${Y(0.5)})`, "text-anchor": "middle", fill: "#1a1a1a" }, "how often it happened"));
    const color = c.agent.color || "#2b8a3e", maxN = Math.max(...c.bins.map((b) => b.n), 1);
    const pts = c.bins.filter((b) => b.n);
    svg.append(s("polyline", { points: pts.map((b) => `${X(b.mean_p)},${Y(b.freq)}`).join(" "), fill: "none", stroke: color, "stroke-width": 2 }));
    for (const b of pts) {
      svg.append(s("line", { x1: X(b.mean_p), x2: X(b.mean_p), y1: Y(b.freq_lo), y2: Y(b.freq_hi), stroke: color, "stroke-width": 2, opacity: 0.6 }),
        s("circle", { cx: X(b.mean_p), cy: Y(b.freq), r: 3 + 7 * Math.sqrt(b.n / maxN), fill: color, stroke: "#fff", "stroke-width": 1.5 },
          s("title", {}, `${b.n} forecasts in ${pct(b.lo)}–${pct(b.hi)}: said ${pct(b.mean_p)}, happened ${pct(b.freq)} (${pct(b.freq_lo)}–${pct(b.freq_hi)})`)));
    }
    return svg;
  }
  async function calibrationSection() {
    const box = h("section", { id: "calibration", style: "max-width:760px;margin:0 auto;padding:0 16px 90px" }, h("p", { class: "muted" }, "Loading calibration…"));
    main().after(box);
    let sum;
    try { sum = await get("/calibration"); } catch (e) { box.replaceChildren(fail(e)); return; }
    if (!sum.agents.length) { box.remove(); return; }
    const detail = h("div");
    const chips = h("select", { "aria-label": "Agent", style: "font:inherit;padding:10px;border:2px solid var(--line);border-radius:12px;max-width:100%;margin:8px 0", onchange: (ev) => pick(ev.target.value) });
    async function pick(id) {
      chips.value = id;
      try {
        const c = await get(`/calibration?agent=${encodeURIComponent(id)}`);
        detail.replaceChildren(reliability(c),
          h("p", {}, h("b", {}, c.agent.name), `: ECE ${b3(c.ece)} (${Math.round(c.interval_level * 100)}% interval ${b3(c.ece_lo)}–${b3(c.ece_hi)}), Brier ${b3(c.brier)}, ${c.n} resolved forecasts.`),
          h("p", { class: "muted small" }, c.note, " Dot size is the number of forecasts in the bin; the line through each dot is its interval."));
      } catch (e) { detail.replaceChildren(fail(e)); }
    }
    for (const a of sum.agents) chips.append(h("option", { value: a.id }, `${a.name} (ECE ${b3(a.ece)})`));
    const calRow = (a) => h("tr", {}, h("td", {}, h("span", { class: "dot", style: `background:${a.color}` }), a.name), h("td", {}, b3(a.ece)),
      h("td", {}, `${b3(a.ece_lo)}–${b3(a.ece_hi)}`), h("td", {}, b3(a.brier)));
    const calTable = (list) => h("table", { class: "t" }, h("tr", {}, h("th", {}, "Forecaster"), h("th", {}, "ECE"), h("th", {}, `${Math.round(sum.interval_level * 100)}% interval`), h("th", {}, "Brier")), list.map(calRow));
    const featured = sum.agents.filter((a, i) => i < 10 || a.id === "market" || a.id === "consensus");
    const rest = sum.agents.filter((a) => !featured.includes(a));
    box.replaceChildren(h("h2", {}, "Calibration: when they say 70%, does it happen 70% of the time?"),
      h("p", { class: "muted small" }, `Every resolved backtest forecast (${sum.agents[0].n} per agent), grouped into ${sum.bins} probability bins. Pick a forecaster (sorted by ECE, best first):`),
      chips, detail,
      h("h3", {}, "Best calibrated, plus the market and the consensus"), calTable(featured),
      rest.length > 0 && h("details", {}, h("summary", {}, `All ${sum.agents.length} forecasters`), calTable(rest)));
    pick(sum.agents[0].id);
    if (location.hash === "#calibration") box.scrollIntoView();
  }

  if (page === "replay") replayPage();
  else if (page === "impact") impactPage();
  else if (page === "board") calibrationSection();
})();
