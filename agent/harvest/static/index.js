"use strict";
(() => {
  const { el, api, pct, cr, signed, b3, short } = H;
  H.initTheme(document.getElementById("theme"));
  let state = null, story = null, geo = null, seq = null, filter = "hot";
  let trades = [];
  const marketsById = new Map();
  const flow = new H.Flow(document.getElementById("flow"), { w: 760, h: 450, maxMarkets: 7 });
  let mapApi = null, lastMapDraw = 0, revealed = false;

  // ---------------------------------------------------------------- stats
  const STATS = [
    ["markets", "Live markets"], ["volume", "Volume"], ["trades", "Bets placed"], ["models", "World models"],
    ["users", "Humans joined"], ["fund", "Resilience Fund"], ["ledger", "Ledger head"],
  ];
  const statNodes = {};
  for (const [k, label] of STATS) {
    const s = el("div", "stat");
    s.append(el("div", "k", label));
    const v = el("div", k === "ledger" ? "v small" : "v", "–");
    const d = el("div", "d", "");
    s.append(v, d);
    statNodes[k] = { v, d };
    document.getElementById("stats").append(s);
  }
  function renderStats() {
    const open = state.markets.filter((m) => m.status === "open");
    const vol = state.markets.reduce((a, m) => a + (m.volume || 0), 0);
    H.tween(statNodes.markets.v, open.length, (x) => cr(x));
    statNodes.markets.d.textContent = `${open.filter((m) => m.kind === "hard").length} commodity · ${open.filter((m) => m.kind === "soft").length} policy`;
    H.tween(statNodes.volume.v, vol, (x) => cr(x));
    statNodes.volume.d.textContent = "play credits staked";
    H.tween(statNodes.trades.v, state.seq || 0, (x) => cr(x));
    statNodes.trades.d.textContent = trades.length ? `last ${H.ago(trades[trades.length - 1].at)}` : "";
    H.tween(statNodes.models.v, state.personas.length + (state.agents || 0), (x) => cr(x));
    statNodes.models.d.textContent = `${state.personas.length} house · ${state.agents || 0} outside`;
    H.tween(statNodes.users.v, state.users || 0, (x) => cr(x));
    statNodes.users.d.textContent = "via QR, no signup";
    H.tween(statNodes.fund.v, state.fund.treasury || 0, (x) => cr(x, 1));
    statNodes.fund.d.textContent = `${pct(state.fund.fee_rate)} of every bet`;
    statNodes.ledger.v.textContent = `#${state.ledger_head.seq} ${state.ledger_head.hash.slice(0, 8)}`;
    statNodes.ledger.d.textContent = "hash-chained, memo-anchored";
  }

  // ---------------------------------------------------------------- feed + flow
  const feed = document.getElementById("feed");
  function pushTrades(newTrades, animate) {
    if (!newTrades.length) return;
    trades = trades.concat(newTrades).slice(-600);
    if (!animate) {
      feed.innerHTML = "";
      for (const t of newTrades.slice(-14).reverse()) feed.append(H.feedItem(t, marketsById, false));
      return;
    }
    newTrades.slice(-8).forEach((t, i) => setTimeout(() => {
      feed.prepend(H.feedItem(t, marketsById, true));
      while (feed.children.length > 14) feed.lastChild.remove();
      setTimeout(() => feed.querySelectorAll("li.fresh").forEach((n) => n.classList.remove("fresh")), 2500);
      flow.pulse(t);
      bumpCard(t.qid);
    }, i * 450));
  }

  // ---------------------------------------------------------------- market cards
  const cards = document.getElementById("cards");
  const cardNodes = new Map();
  document.getElementById("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    filter = b.dataset.f;
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
    renderCards(true);
  });
  function cardValues(m) {
    const replay = (story && story.live_paths && story.live_paths[m.qid]) || [];
    const live = (m.history || []).map((h) => h[1]);
    const vals = replay.concat(live);
    if (vals.length < 2) return { vals: [m.p_yes, m.p_yes], split: null };
    return { vals, split: replay.length ? replay.length - 1 : null };
  }
  function makeCard(m) {
    const c = el("div", "panel card");
    const meta = el("div", "meta");
    meta.append(el("span", `tag ${m.kind}`, m.kind === "hard" ? "commodity" : "policy"));
    meta.append(el("span", null, `${m.region} · ${m.crop}`));
    if (m.kind === "hard" && m.resolve_month) meta.append(el("span", "faint", `resolves ${m.resolve_month}`));
    c.append(meta, el("div", "ttl", m.title));
    const odds = el("div", "odds");
    const p = el("span", "p");
    const chg = el("span", "chg");
    const vol = el("span", "vol");
    odds.append(p, el("span", "muted", "YES"), chg, vol);
    c.append(odds);
    const chart = H.svg("svg", { class: "chart" });
    c.append(chart);
    const strip = el("div", "strip");
    strip.append(el("div", "rail"));
    const mk = el("div", "mkt");
    strip.append(mk);
    const dots = new Map();
    for (const f of m.agent_forecasts || []) {
      const d = el("div", "dot");
      d.style.background = f.color;
      d.title = `${f.name}: ${pct(f.p_yes)} — ${f.rationale || ""}`;
      strip.append(d);
      dots.set(f.persona_id, d);
    }
    c.append(strip);
    const leg = el("div", "strip-legend");
    leg.append(el("span", null, "NO"), el("span", null, "agents' forecasts · bar = market"), el("span", null, "YES"));
    c.append(leg);
    const node = { c, p, chg, vol, chart, mk, dots };
    cardNodes.set(m.qid, node);
    updateCard(m);
    return c;
  }
  function updateCard(m) {
    const n = cardNodes.get(m.qid);
    if (!n) return;
    n.p.textContent = pct(m.p_yes);
    const { vals, split } = cardValues(m);
    const open = split != null ? vals[split] : vals[0];
    const dlt = (m.p_yes - open) * 100;
    n.chg.textContent = Math.abs(dlt) < 0.05 ? "" : `${dlt > 0 ? "▲" : "▼"} ${Math.abs(dlt).toFixed(1)} pts`;
    n.chg.className = `chg ${dlt > 0 ? "yes" : "no"}`;
    n.vol.textContent = `${cr(m.volume)} cr`;
    const shades = split != null ? [{ x0: 0, x1: split, cls: "shade-replay", label: "REPLAY" }] : [];
    H.lineChart(n.chart, [{ values: vals, color: "var(--accent)", width: 2, fill: "color-mix(in srgb, var(--accent2) 14%, transparent)" }],
      { w: 300, h: 84, ymin: 0, ymax: 1, hline: 0.5, shades, endDot: true, pad: { l: 2, r: 5, t: 4, b: 4 } });
    n.mk.style.left = `${m.p_yes * 100}%`;
    for (const f of m.agent_forecasts || []) {
      const d = n.dots.get(f.persona_id);
      if (d) d.style.left = `${f.p_yes * 100}%`;
    }
  }
  function bumpCard(qid) {
    const n = cardNodes.get(qid);
    if (!n) return;
    n.c.classList.add("bump");
    setTimeout(() => n.c.classList.remove("bump"), 1200);
  }
  function renderCards(force) {
    let list = state.markets.filter((m) => m.status === "open");
    if (filter === "hard" || filter === "soft") list = list.filter((m) => m.kind === filter);
    else {
      const act = new Map();
      trades.slice(-80).forEach((t, i) => act.set(t.qid, (act.get(t.qid) || 0) + 1 + i / 40));
      list = [...list].sort((a, b) => (act.get(b.qid) || 0) - (act.get(a.qid) || 0) || b.volume - a.volume).slice(0, 9);
    }
    const ids = list.map((m) => m.qid).join(",");
    if (!force && cards.dataset.ids === ids) { list.forEach(updateCard); return; }
    if (!force && filter === "hot" && cards.dataset.ids) { list.forEach((m) => cardNodes.has(m.qid) && updateCard(m)); return; }
    cards.dataset.ids = ids;
    cards.innerHTML = "";
    cardNodes.clear();
    for (const m of list) cards.append(makeCard(m));
  }

  // ---------------------------------------------------------------- leaderboard
  const lbTable = document.getElementById("lb");
  function renderLeaderboard() {
    const lb = state.leaderboard;
    const pmap = new Map(state.personas.map((p) => [p.id, p]));
    const rows = [...lb.world_models];
    const bases = [["Market (final prices)", lb.baselines.market], ["Climatology (base rate)", lb.baselines.climatology], ["Coin flip", lb.baselines.coin]]
      .filter((b) => b[1] != null);
    const maxB = 0.3;
    lbTable.innerHTML = "";
    const head = el("tr");
    for (const h of ["#", "World model and conviction", "Brier holdout", "Brier dev", "Credits PnL holdout", "Credits PnL live"]) head.append(el("th", h.includes("PnL") || h.startsWith("Brier") ? "num" : "", h));
    lbTable.append(head);
    const baseQueue = [...bases].sort((a, b) => a[1] - b[1]);
    const mkBase = ([name, v]) => {
      const tr = el("tr", "base");
      tr.append(el("td", ""), el("td", "", `baseline · ${name}`));
      const td = el("td", "num", b3(v));
      tr.append(td, el("td", ""), el("td", ""), el("td", ""));
      return tr;
    };
    for (const m of rows) {
      while (baseQueue.length && m.brier_holdout != null && baseQueue[0][1] < m.brier_holdout) lbTable.append(mkBase(baseQueue.shift()));
      const tr = el("tr", "row");
      tr.append(el("td", "rank", m.rank));
      const td = el("td");
      const nm = el("div", "nm");
      nm.append(H.avatar(m.origin === "house" ? `agent:${m.persona_id}` : m.persona_id, 30));
      const txt = el("div");
      const title = el("b", null, m.name);
      txt.append(title);
      if (m.origin === "external") txt.append(document.createTextNode(" "), el("span", "pill", "outside agent"));
      if (m.origin === "human-model") txt.append(document.createTextNode(" "), el("span", "pill", "human-written theory"));
      const p = pmap.get(m.persona_id);
      txt.append(el("div", "conv", p ? p.world_model : (m.owner ? `by ${m.owner}` : "world model committed on the ledger")));
      nm.append(txt);
      td.append(nm);
      tr.append(td);
      const bh = el("td", "num", b3(m.brier_holdout));
      if (m.brier_holdout != null && m.brier_holdout_lo != null) {
        const half = (m.brier_holdout_hi - m.brier_holdout_lo) / 2;
        bh.append(el("span", "ci-pm", ` ± ${half.toFixed(2)}`));
        bh.title = `90% interval ${b3(m.brier_holdout_lo)} to ${b3(m.brier_holdout_hi)}` + (m.diff_vs_market_lo != null ? `; gap to market ${m.diff_vs_market >= 0 ? "+" : ""}${m.diff_vs_market.toFixed(3)} (${m.diff_vs_market_lo.toFixed(3)} to ${m.diff_vs_market_hi.toFixed(3)})${m.beats_market_ci ? ", beats the market" : ", within noise"}` : "");
      }
      if (m.brier_holdout != null) {
        const bar = el("div", "bbar");
        const i = el("i");
        i.style.width = `${Math.min(100, (100 * m.brier_holdout) / maxB)}%`;
        i.style.background = m.color || "var(--accent)";
        bar.append(i);
        if (m.brier_holdout_lo != null) {
          const w = el("span", "ci");
          w.style.left = `${Math.min(100, (100 * m.brier_holdout_lo) / maxB)}%`;
          w.style.width = `${Math.max(0, Math.min(100, (100 * m.brier_holdout_hi) / maxB) - Math.min(100, (100 * m.brier_holdout_lo) / maxB))}%`;
          bar.append(w);
        }
        if (lb.baselines.market != null) { const mk = el("span", "mk"); mk.style.left = `${(100 * lb.baselines.market) / maxB}%`; mk.title = "market baseline"; bar.append(mk); }
        bh.append(bar);
      }
      tr.append(bh, el("td", "num muted", b3(m.brier_dev)));
      const pho = m.origin === "human-model" ? null : m.pnl_holdout;
      const ph = el("td", `num ${pho == null ? "" : pho >= 0 ? "yes" : "no"}`, signed(pho));
      const pl = m.pnl_live != null && m.origin !== "human-model" ? m.pnl_live : null;
      tr.append(ph, el("td", `num ${pl == null ? "" : pl >= 0 ? "yes" : "no"}`, signed(pl)));
      tr.addEventListener("click", () => tr.classList.toggle("open"));
      lbTable.append(tr);
    }
    while (baseQueue.length) lbTable.append(mkBase(baseQueue.shift()));
    const note = document.getElementById("lb-note");
    if (note && lb.interval) note.textContent = `Whiskers and ± show the ${lb.interval.note}; if the whisker spans the market tick, the model is within noise of the market.`;
    const ins = lb.insight || state.insight;
    if (ins) { document.getElementById("insight").hidden = false; document.getElementById("insight-text").textContent = ins; }
  }

  // ---------------------------------------------------------------- PnL race
  let raceTimer = null;
  function raceData() {
    const cs = story.cutoffs.filter((c) => c.split !== "live");
    const series = story.personas.map((p) => {
      let acc = 0;
      return { p, values: [0].concat(cs.map((c) => (acc += c.pnl[p.id] || 0))) };
    });
    return { cs, series };
  }
  function renderRace(k) {
    if (!story || !story.has_replay) return;
    const { cs, series } = raceData();
    const n = cs.length;
    k = k == null ? n : k;
    const all = series.flatMap((s) => s.values);
    const ymin = Math.min(...all), ymax = Math.max(...all);
    const hoStart = cs.findIndex((c) => c.split === "holdout");
    const svgEl = document.getElementById("race");
    const shown = series.map((s) => ({ values: s.values.map((v, i) => (i <= k ? v : null)), color: s.p.color, width: 2.4 }));
    const xl = [];
    cs.forEach((c, i) => { if (c.cutoff.endsWith("-03") || i === n - 1) xl.push({ i: i + 1, text: c.cutoff.slice(0, 4) }); });
    const ch = H.lineChart(svgEl, shown, { w: 1160, h: 340, ymin: ymin * 1.08, ymax: ymax * 1.08, hline: 0, pad: { l: 6, r: 230, t: 10, b: 14 },
      shades: hoStart >= 0 ? [{ x0: hoStart + 0.5, x1: n, cls: "shade-holdout", label: "SEALED HOLDOUT" }] : [], xlabels: xl, endDot: false });
    svgEl.setAttribute("preserveAspectRatio", "xMidYMid meet");
    // end labels, de-overlapped
    const ends = series.map((s) => ({ s, v: s.values[Math.min(k, n)] })).sort((a, b) => b.v - a.v);
    let lastY = -1e9;
    for (const e of ends) {
      let y = ch.Y(e.v);
      if (y - lastY < 15) y = lastY + 15;
      lastY = y;
      H.svg("circle", { cx: ch.X(Math.min(k, n)), cy: ch.Y(e.v), r: 3.5, fill: e.s.p.color }, svgEl);
      const t = H.svg("text", { x: 944, y: y + 4, style: `font:600 12px var(--sans);fill:${e.s.p.color}` }, svgEl);
      t.textContent = `${e.s.p.name} ${signed(e.v)}`;
    }
    document.getElementById("race-q").textContent = k === 0 ? "start" : cs[Math.min(k, n) - 1].cutoff;
  }
  function playRace() {
    if (!story || !story.has_replay) return;
    clearInterval(raceTimer);
    const n = story.cutoffs.filter((c) => c.split !== "live").length;
    let k = 0;
    renderRace(0);
    raceTimer = setInterval(() => { k += 1; renderRace(k); if (k >= n) clearInterval(raceTimer); }, 420);
  }
  document.getElementById("race-play").addEventListener("click", playRace);

  // ---------------------------------------------------------------- sealed holdout
  function commitment(kind) { return (story.commitments || []).find((c) => c.kind === kind); }
  function renderSeal() {
    if (!story || !story.has_replay) return;
    const ho = commitment("holdout"), models = commitment("models");
    const n = story.holdout.length, quarters = new Set(story.holdout.map((q) => q.cutoff));
    document.getElementById("seal-sub").textContent = `${n} questions × ${story.personas.length} world models · ${[...quarters].join(", ")}`;
    const kv = document.getElementById("seal-kv");
    kv.innerHTML = "";
    const row = (k, v, cls) => { kv.append(el("dt", null, k)); const dd = el("dd", cls || ""); if (v instanceof Node) dd.append(v); else dd.textContent = v; kv.append(dd); };
    if (ho) {
      row("Committed at", ho.committed_at.replace("T", " ").replace("Z", " UTC"));
      row("Holdout sha256", el("div", "hashline", ho.sha256));
      row("Solana devnet memo", el("span", "mono", short(ho.memo, 30)));
      const r = ho.receipt || {};
      const st = el("span");
      st.append(el("span", `pill ${r.status === "confirmed" ? "ok" : "pending"}`, r.status === "confirmed" ? "confirmed on devnet" : "pending on-chain"));
      if (r.explorer_url) { st.append(document.createTextNode(" ")); const a = el("a", null, "explorer ↗"); a.href = r.explorer_url; a.target = "_blank"; st.append(a); }
      else if (r.error) st.append(el("div", "faint", r.error.replace(/^pending on-chain:\s*/, "")));
      row("Receipt", st);
    }
    if (models) row("World models sha", el("span", "mono", short(models.sha256, 16)));
    row("Forecaster", `${story.model} · masked prompts`);
    const grid = document.getElementById("reveal-grid");
    grid.innerHTML = "";
    for (const q of story.holdout) {
      const t = el("div", "rq sealed");
      t.title = q.title;
      t.append(el("div", "t", q.title.replace(/^Will /, "").replace(/ \(now [^)]*\)/, "").replace(/\?$/, "")));
      const mini = el("div", "mini");
      for (const p of story.personas) {
        const v = q.forecasts[p.id];
        if (v == null) continue;
        const s = el("span");
        s.style.left = `${v * 100}%`;
        s.style.background = p.color;
        mini.append(s);
      }
      t.append(mini, el("div", `o ${q.outcome ? "yes" : "no"}`, q.outcome == null ? "open" : q.outcome ? "YES" : "NO"));
      t.dataset.o = q.outcome;
      grid.append(t);
    }
    document.getElementById("ho-sub").textContent = "agents' forecasts are dots, outcome bottom right";
  }
  function reveal() {
    if (revealed || !story) return;
    revealed = true;
    document.getElementById("sealbox").classList.add("broken");
    document.getElementById("seal-state").textContent = "After resolution";
    const tiles = [...document.querySelectorAll(".rq")];
    tiles.forEach((t, i) => setTimeout(() => { t.classList.remove("sealed"); t.classList.add(t.dataset.o === "1" ? "r1" : "r0"); }, 30 * i));
    const best = ((story.scores && story.scores.world_models) || []).find((m) => m.brier_holdout != null);
    setTimeout(() => {
      document.getElementById("seal-title").textContent = best ? `${best.name} leads the holdout (Brier ${b3(best.brier_holdout)})` : "Resolved";
      const mr = document.getElementById("mask-row");
      mr.innerHTML = "";
      mr.append(el("span", "muted", "Mask revealed:"));
      const mask = (story.mask && story.mask.commodities) || {};
      Object.entries(mask).sort((a, b) => a[1].localeCompare(b[1])).forEach(([sid, label]) => mr.append(el("span", "pill", `${label} = ${H.COMMODITY[sid] || sid}`)));
    }, 30 * tiles.length);
    const b = document.getElementById("reveal");
    b.textContent = "Outcomes revealed";
    b.disabled = true;
  }
  document.getElementById("reveal").addEventListener("click", reveal);
  async function runVerify(tamper) {
    const box = document.getElementById("checks");
    box.innerHTML = "";
    box.append(el("span", "pill", tamper ? "tampering one holdout forecast and re-verifying…" : "recomputing every hash…"));
    try {
      const r = tamper ? await api("/harvest/api/verify", { method: "POST", body: { tamper: true } }) : await api("/harvest/api/verify");
      box.innerHTML = "";
      const pill = (label, v) => box.append(el("span", `pill ${v === true ? "ok" : v === false ? "bad" : ""}`, `${v === true ? "✓" : v === false ? "✗" : "·"} ${label}`));
      pill("commitments match", r.commitments_ok);
      pill("data windows ≤ cutoff", r.windows_ok);
      pill("bet ledger chain", r.chain_ok);
      const details = (r.details || []).filter((d) => d && typeof d === "object");
      const note = details.find((d) => d.check === "tamper");
      const ho = details.find((d) => d.check === "commitment" && d.kind === "holdout");
      const win = details.find((d) => d.check === "windows");
      const msg = el("div", "muted");
      msg.style.cssText = "flex-basis:100%;font-size:13px;margin-top:6px";
      if (tamper && note) msg.append(el("div", null, `Tamper test: ${note.note}.`));
      if (ho) {
        msg.append(el("div", "mono", `sealed     ${short(ho.stored, 16)}`));
        const rc = el("div", `mono ${ho.ok ? "yes" : "no"}`, `recomputed ${short(ho.recomputed, 16)} ${ho.ok ? "✓ match" : "✗ MISMATCH"}`);
        msg.append(rc);
      }
      msg.append(el("div", null, ho && !ho.ok ? "One changed forecast breaks the sealed hash: hindsight edits are detectable."
        : `Recomputed hashes equal the sealed memos${win ? `; ${win.n} data windows end at their cutoffs` : ""}. No forecast was edited after the seal.`));
      box.append(msg);
    } catch (e) {
      box.innerHTML = "";
      box.append(el("span", "pill bad", e.message));
    }
  }
  document.getElementById("verify").addEventListener("click", () => runVerify(false));
  document.getElementById("tamper").addEventListener("click", () => runVerify(true));

  // ---------------------------------------------------------------- price small multiples
  function renderPrices() {
    const host = document.getElementById("sm");
    host.innerHTML = "";
    const cuts = story.cutoffs || [];
    const ho = cuts.filter((c) => c.split === "holdout").map((c) => c.cutoff);
    const live = cuts.filter((c) => c.split === "live").map((c) => c.cutoff);
    for (const [sid, s] of Object.entries(story.prices || {})) {
      const months = s.points.map((p) => p[0]);
      const vals = s.points.map((p) => p[1]);
      const idx = (m) => months.indexOf(m);
      const shades = [];
      if (ho.length && idx(ho[0]) >= 0) shades.push({ x0: idx(ho[0]), x1: idx(ho[ho.length - 1]) + 3, cls: "shade-holdout" });
      if (live.length && idx(live[0]) >= 0) shades.push({ x0: idx(live[0]), x1: months.length - 1, cls: "shade-live" });
      const p = el("div", "panel");
      const h4 = el("h4", null, s.name);
      const last = vals[vals.length - 1], first = vals[months.indexOf("2022-03")] || vals[0];
      const ch = ((last / first - 1) * 100);
      h4.append(el("span", null, `${cr(last, last < 100 ? 1 : 0)} ${s.unit} · ${ch >= 0 ? "+" : ""}${ch.toFixed(0)}% since 2022Q1`));
      p.append(h4);
      const g = H.svg("svg", {}, p);
      const xl = months.map((m, i) => (m.endsWith("-01") && Number(m.slice(0, 4)) % 1 === 0 ? { i, text: `’${m.slice(2, 4)}` } : null)).filter(Boolean);
      const cutIdx = cuts.map((c) => idx(c.cutoff)).filter((i) => i >= 0);
      const r = H.lineChart(g, [{ values: vals, color: "var(--ink)", width: 1.6 }], { w: 280, h: 96, shades, xlabels: xl, pad: { l: 2, r: 2, t: 6, b: 12 } });
      for (const i of cutIdx) H.svg("line", { x1: r.X(i), x2: r.X(i), y1: 92 - 10, y2: 92 - 6, stroke: "var(--accent)", "stroke-width": 1 }, g);
      host.append(p);
    }
  }

  // ---------------------------------------------------------------- map + fund
  function renderFund() {
    const f = state.fund;
    H.tween(document.getElementById("fund-total"), f.treasury || 0, (x) => cr(x, 2));
    document.getElementById("fund-earmark").textContent = `Illustrative allocation for ${f.earmark || "European farmers' heat adaptation"}, set by market-implied risk. Fees are play credits.`;
    H.renderFund(document.getElementById("alloc"), f, { max: 11 });
    const kv = document.getElementById("fund-kv");
    kv.innerHTML = "";
    const row = (k, v) => { kv.append(el("dt", null, k)); const dd = el("dd"); if (v instanceof Node) dd.append(v); else dd.textContent = v; kv.append(dd); };
    row("Rule", `${pct(f.fee_rate)} of every bet, in play credits; share = mean adverse probability per region and crop`);
    row("Memo", el("span", "mono", short(f.memo, 28)));
    const r = f.receipt;
    row("Treasury", el("span", `pill ${r && r.status === "confirmed" ? "ok" : "pending"}`, r && r.status === "confirmed" ? "anchored on devnet" : "devnet test tokens: pending on-chain"));
  }
  function renderMap(force) {
    if (!window.L || !geo) return;
    if (!mapApi) mapApi = H.riskMap("map", { center: [43, 14], zoom: 4 });
    if (!force && Date.now() - lastMapDraw < 15000) return;
    lastMapDraw = Date.now();
    mapApi.draw(geo, state.fund, marketsById);
  }

  // ---------------------------------------------------------------- polling
  async function poll() {
    try {
      const s = await api(seq == null ? "/harvest/api/state" : `/harvest/api/state?since=${seq}`);
      const first = state == null;
      state = s;
      H.registerPersonas(s.personas);
      H.registerTraders(s.leaderboard);
      marketsById.clear();
      for (const m of s.markets) marketsById.set(m.qid, m);
      const fresh = s.recent_trades || [];
      seq = s.seq;
      flow.draw(s, trades.concat(fresh));
      pushTrades(fresh, !first);
      document.getElementById("feed-sub").textContent = `${cr(s.seq)} bets`;
      renderStats();
      renderCards(first);
      if (first || fresh.length) renderLeaderboard();
      renderFund();
      renderMap(first);
    } catch (e) {
      console.warn("state poll failed", e);
    }
    setTimeout(poll, 2500);
  }

  async function boot() {
    [story, geo] = await Promise.all([api("/harvest/api/story").catch(() => null), api("/harvest/api/geo").catch(() => null)]);
    if (story) {
      H.registerPersonas(story.personas);
      renderRace();
      renderSeal();
      renderPrices();
      const io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { playRace(); io.disconnect(); } }), { threshold: 0.5 });
      io.observe(document.getElementById("race"));
    }
    if (new URLSearchParams(location.search).get("reveal") === "1") setTimeout(reveal, 600);
    poll();
  }
  boot();
})();
