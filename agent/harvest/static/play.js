"use strict";
(() => {
  const { el, api, pct, cr, signed, b3 } = H;
  H.initTheme(document.getElementById("theme"));
  const KEY = "harvest-player";
  let me = null, spend = 25, markets = [], personas = [], tmQuestions = [], tmDone = null, positions = new Map();

  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (_) { return null; } };
  const save = (v) => { try { if (v) localStorage.setItem(KEY, JSON.stringify(v)); else localStorage.removeItem(KEY); } catch (_) { /* storage blocked */ } };

  // ---------------------------------------------------------------- join
  async function showJoin() {
    document.getElementById("join").hidden = false;
    document.getElementById("app").hidden = true;
    document.getElementById("bal").hidden = true;
    try {
      const s = await api("/harvest/api/state?since=999999999");
      H.registerPersonas(s.personas);
      const box = document.getElementById("chips-av");
      box.innerHTML = "";
      for (const p of s.personas) box.append(H.avatar(`agent:${p.id}`, 34));
    } catch (_) { /* decorative */ }
  }
  document.getElementById("join-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("join-btn");
    btn.disabled = true;
    try {
      const r = await api("/harvest/api/join", { method: "POST", body: { nickname: document.getElementById("nick").value } });
      me = { token: r.token, user_id: r.user_id, nickname: r.nickname };
      save(me);
      H.toast(`Welcome ${r.nickname}: 1,000 credits. Player #${r.users}.`);
      start();
    } catch (err) {
      H.toast(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  // ---------------------------------------------------------------- app
  async function refreshMe() {
    try {
      const r = await api(`/harvest/api/me?token=${encodeURIComponent(me.token)}`);
      const b = document.getElementById("bal");
      b.hidden = false;
      b.textContent = `${cr(r.balance)} cr`;
      positions = new Map((r.positions || []).map((p) => [p.qid, p]));
      me.pnl = r.pnl;
      me.forecast = r.forecast;
      return true;
    } catch (err) {
      if (/unknown token/.test(err.message)) { save(null); me = null; showJoin(); }
      return false;
    }
  }
  document.getElementById("ptabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#ptabs button").forEach((x) => x.classList.toggle("on", x === b));
    for (const t of ["mk", "tm", "th", "rank"]) document.getElementById(`tab-${t}`).hidden = t !== b.dataset.tab;
    if (b.dataset.tab === "rank") renderRank();
    if (b.dataset.tab === "tm" && !tmQuestions.length) loadTm();
    if (b.dataset.tab === "th" && me.theory && !document.getElementById("th-result").childElementCount) {
      api(`/harvest/api/byo/${encodeURIComponent(me.theory)}`).then(renderTheory).catch(() => { /* not scored here */ });
    }
  });
  document.getElementById("spend").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    spend = Number(b.dataset.v);
    document.querySelectorAll("#spend button").forEach((x) => x.classList.toggle("on", x === b));
  });

  function consensus(m) {
    const fs = m.agent_forecasts || [];
    if (!fs.length) return null;
    return fs.reduce((a, f) => a + f.p_yes, 0) / fs.length;
  }
  function renderMarkets() {
    const host = document.getElementById("mk-list");
    const open = markets.filter((m) => m.status === "open");
    // soft (policy) first: the questions for governments, then the busiest commodity markets
    open.sort((a, b) => (a.kind === b.kind ? b.volume - a.volume : a.kind === "soft" ? -1 : 1));
    if (host.dataset.ids !== open.map((m) => m.qid).join(",")) {
      host.innerHTML = "";
      host.dataset.ids = open.map((m) => m.qid).join(",");
      for (const m of open) host.append(card(m));
    }
    for (const m of open) update(m);
  }
  const nodes = new Map();
  function card(m) {
    const c = el("div", "panel pm");
    const meta = el("div");
    meta.style.cssText = "display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted)";
    meta.append(el("span", `tag ${m.kind}`, m.kind === "hard" ? "commodity" : "policy"), el("span", null, `${m.region} · ${m.crop}`));
    c.append(meta, el("div", "ttl", m.title));
    const row = el("div", "row");
    const p = el("span", "p");
    const cons = el("div", "cons");
    row.append(p, el("span", "muted", "YES"), cons);
    cons.style.marginLeft = "auto";
    cons.style.textAlign = "right";
    c.append(row);
    const chart = H.svg("svg", { class: "chart" });
    c.append(chart);
    const btns = el("div", "btns");
    const y = el("button", "yesb"), n = el("button", "nob");
    y.addEventListener("click", () => bet(m.qid, "YES", y));
    n.addEventListener("click", () => bet(m.qid, "NO", n));
    btns.append(y, n);
    c.append(btns);
    const mine = el("div", "mine");
    c.append(mine);
    nodes.set(m.qid, { p, cons, chart, y, n, mine });
    return c;
  }
  function update(m) {
    const n = nodes.get(m.qid);
    if (!n) return;
    n.p.textContent = pct(m.p_yes);
    const c = consensus(m);
    n.cons.innerHTML = "";
    if (c != null) {
      n.cons.append(document.createTextNode(`agents say ${pct(c)}`));
      const dots = el("div");
      dots.style.cssText = "display:flex;gap:2px;justify-content:flex-end;margin-top:3px";
      for (const f of m.agent_forecasts) { const d = el("span"); d.style.cssText = `width:8px;height:8px;border-radius:50%;background:${f.color}`; d.title = `${f.name}: ${pct(f.p_yes)}`; dots.append(d); }
      n.cons.append(dots);
    }
    const vals = (m.history || []).map((h) => h[1]);
    H.lineChart(n.chart, [{ values: vals.length > 1 ? vals : [m.p_yes, m.p_yes], color: "var(--accent)", width: 2, fill: "color-mix(in srgb, var(--accent2) 14%, transparent)" }],
      { w: 300, h: 46, ymin: 0, ymax: 1, hline: 0.5, endDot: true });
    n.y.textContent = `YES ${Math.round(m.p_yes * 100)}%`;
    n.n.textContent = `NO ${Math.round((1 - m.p_yes) * 100)}%`;
    const pos = positions.get(m.qid);
    n.mine.textContent = pos ? `You hold ${pos.yes > 0.01 ? `${cr(pos.yes, 1)} YES` : ""}${pos.yes > 0.01 && pos.no > 0.01 ? " · " : ""}${pos.no > 0.01 ? `${cr(pos.no, 1)} NO` : ""}` : "";
  }
  async function bet(qid, side, btn) {
    btn.disabled = true;
    try {
      const r = await api("/harvest/api/bet", { method: "POST", body: { token: me.token, qid, side, spend } });
      const t = r.trade;
      H.toast(`${side} ${cr(t.shares, 1)} shares for ${cr(t.spend)} cr · price ${pct(t.price_before)} → ${pct(t.price_after)} · on the board now`);
      const i = markets.findIndex((m) => m.qid === qid);
      if (i >= 0) markets[i] = r.market;
      document.getElementById("bal").textContent = `${cr(r.balance)} cr`;
      await refreshMe();
      renderMarkets();
    } catch (err) {
      H.toast(err.message);
    } finally {
      btn.disabled = false;
    }
  }
  async function pollMarkets() {
    try {
      const r = await api("/harvest/api/markets");
      markets = r.markets;
      renderMarkets();
    } catch (_) { /* retry next tick */ }
  }

  // ---------------------------------------------------------------- time machine
  function hash(s) { let h = 2166136261; for (const ch of s) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); } return h >>> 0; }
  async function loadTm() {
    const r = await api("/harvest/api/holdout");
    const all = r.questions || [];
    const seed = hash(me.user_id);
    // six questions, one per commodity where possible, stable per player
    const bySeries = new Map();
    for (const q of [...all].sort((a, b) => hash(a.qid + seed) - hash(b.qid + seed))) if (!bySeries.has(q.series)) bySeries.set(q.series, q);
    tmQuestions = [...bySeries.values()].slice(0, 6);
    const host = document.getElementById("tm-list");
    host.innerHTML = "";
    for (const q of tmQuestions) {
      const c = el("div", "panel tq");
      c.append(el("div", "faint", `${q.region} · ${q.crop} · asked at ${q.cutoff}`));
      c.append(el("div", "ttl", q.title));
      const val = el("div", "val");
      const b = el("b", null, "50%");
      val.append(el("span", null, "NO"), b, el("span", null, "YES"));
      const inp = el("input");
      inp.type = "range"; inp.min = 1; inp.max = 99; inp.value = 50;
      inp.addEventListener("input", () => { b.textContent = `${inp.value}%`; });
      c.append(inp, val);
      const res = el("div", "res");
      c.append(res);
      q._inp = inp; q._res = res;
      host.append(c);
    }
  }
  document.getElementById("tm-submit").addEventListener("click", async () => {
    if (!tmQuestions.length) await loadTm();
    const btn = document.getElementById("tm-submit");
    btn.disabled = true;
    try {
      const r = await api("/harvest/api/forecast", { method: "POST", body: { token: me.token, answers: tmQuestions.map((q) => ({ qid: q.qid, p_yes: Number(q._inp.value) / 100 })) } });
      tmDone = r;
      const box = document.getElementById("tm-result");
      box.innerHTML = "";
      const c = el("div", "panel result");
      c.append(el("div", "kicker", "Your Brier score"), el("div", "big", b3(r.brier)));
      c.append(el("div", null, `Rank ${r.rank_vs_world_models} of ${r.of} against the world models on these ${r.n} questions`));
      const best = r.world_models[0];
      if (best) c.append(el("div", "muted", `Best world model here: ${best.name} (${b3(best.brier)})`));
      box.append(c);
      const out = new Map((r.reveal || []).map((x) => [x.qid, x]));
      for (const q of tmQuestions) {
        const o = out.get(q.qid);
        q._res.innerHTML = "";
        if (o) q._res.append(el("span", `side ${o.outcome ? "YES" : "NO"}`, o.outcome ? "YES" : "NO"), document.createTextNode(` actual ${cr(o.resolved_value, o.resolved_value < 100 ? 1 : 0)} in ${o.resolve_month}`));
        q._inp.disabled = true;
      }
      btn.textContent = "Locked in · see Rank";
      box.scrollIntoView({ behavior: "smooth" });
    } catch (err) {
      H.toast(err.message);
      btn.disabled = false;
    }
  });

  // ---------------------------------------------------------------- your theory (bring your own world model)
  const EXAMPLES = [
    ["Heat first", "Heat is the binding constraint on Europe's harvest. A hot, dry Mediterranean summer cuts yields faster than farmers can adapt, stocks are thin, and markets under-price that tail. Once prices start rising they keep rising for a season before they ease."],
    ["Gravity", "Food prices are pulled back to their long-run average. Every spike brings new planting, substitution and demand destruction; every slump cuts supply. Big moves usually reverse within six months, so bet against extremes and trust the calm."],
    ["Dollar rules", "Food prices are a macro story. A strong dollar and high interest rates make commodities cheaper and push speculators out; when rates fall the money floods back in. Weather is noise next to the dollar and the cost of credit."],
  ];
  const thText = document.getElementById("th-text"), thName = document.getElementById("th-name"), thCount = document.getElementById("th-count");
  const countTheory = () => { thCount.textContent = thText.value.length; };
  thText.addEventListener("input", countTheory);
  for (const [name, text] of EXAMPLES) {
    const b = el("button", null, name);
    b.type = "button";
    b.addEventListener("click", () => { thName.value = name; thText.value = text; countTheory(); thText.focus(); });
    document.getElementById("th-ex").append(b);
  }
  const STEPS = ["Committing your theory to the ledger…", "Forecasting sealed quarter 1 of 3…", "Forecasting sealed quarter 2 of 3…", "Forecasting sealed quarter 3 of 3…", "Scoring against the eight agents and the market…"];
  document.getElementById("th-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("th-btn"), busy = document.getElementById("th-busy"), step = document.getElementById("th-step");
    btn.disabled = true;
    busy.hidden = false;
    document.getElementById("th-result").innerHTML = "";
    let i = 0;
    step.textContent = STEPS[0];
    const timer = setInterval(() => { i = Math.min(i + 1, STEPS.length - 1); step.textContent = STEPS[i]; }, 4000);
    try {
      const r = await api("/harvest/api/byo", { method: "POST", body: { token: me.token, name: thName.value, world_model: thText.value } });
      me.theory = r.model_id;
      save(me);
      renderTheory(r);
    } catch (err) {
      H.toast(err.message, 4000);
    } finally {
      clearInterval(timer);
      busy.hidden = true;
      btn.disabled = false;
    }
  });
  function renderTheory(r) {
    const box = document.getElementById("th-result");
    box.innerHTML = "";
    const c = el("div", "panel result");
    c.append(el("div", "kicker", `${r.name} · Brier on the sealed holdout`), el("div", "big", b3(r.brier_holdout)));
    const house = (r.field || []).filter((f) => f.kind === "house");
    const beat = house.filter((f) => f.brier > r.brier_holdout).length;
    const market = (r.field || []).find((f) => f.kind === "market");
    c.append(el("div", null, `Rank ${r.rank} of ${r.of}: you beat ${beat} of ${house.length} AI world models${market ? (market.brier > r.brier_holdout ? " and the market" : `; the market scored ${b3(market.brier)}`) : ""}.`));
    c.append(el("div", "muted", `${r.n} questions over ${(r.cutoffs || []).length} sealed quarters · lower is better · coin flip 0.250${r.cached ? " · this exact theory was already scored" : ""}`));
    if (r.source === "fallback") c.append(el("div", "faint", "The forecaster was unavailable, so the house heuristic fallback scored it."));
    const tbl = el("div");
    tbl.style.cssText = "margin-top:12px;text-align:left";
    (r.field || []).forEach((f, idx) => tbl.append(rankRow(idx + 1, { id: f.kind === "house" ? `agent:${f.id}` : f.id, name: f.name, sub: f.kind === "house" ? "AI world model" : f.kind === "market" ? "baseline" : "your theory", me: f.kind === "human-model" }, b3(f.brier))));
    c.append(tbl);
    for (const x of (r.rationales || []).slice(0, 2)) {
      const w = el("div", "why");
      w.append(el("div", "q", x.title));
      const line = el("div");
      line.append(document.createTextNode(`You said ${pct(x.p_yes)} YES · it resolved `), el("span", `side ${x.outcome ? "YES" : "NO"}`, x.outcome ? "YES" : "NO"));
      w.append(line, el("div", "r", `“${x.rationale}”`));
      c.append(w);
    }
    const text = `My world model "${r.name}" scored ${b3(r.brier_holdout)} Brier on Harvest's sealed holdout: rank ${r.rank} of ${r.of} against 8 AI world models and the market. Beat it: ${location.origin}/harvest/play`;
    const share = el("div", "share");
    const code = el("code", null, text);
    const copy = el("button", "btn", "Copy");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(text); H.toast("Copied"); } catch (_) { H.toast("Select the text to copy it"); }
    });
    share.append(code, copy);
    c.append(share);
    box.append(c);
    box.scrollIntoView({ behavior: "smooth" });
  }

  // ---------------------------------------------------------------- rank
  async function renderRank() {
    await refreshMe();
    const s = await api("/harvest/api/leaderboard");
    const mc = document.getElementById("me-card");
    mc.innerHTML = "";
    mc.append(el("div", "kicker", me.nickname), el("div", "big", `${signed(me.pnl, 1)} cr`), el("div", "muted", "live credits PnL (marked at market prices)"));
    const rows = [];
    for (const m of s.world_models) if (m.origin !== "human-model" && (m.pnl_live != null || m.origin === "external")) rows.push({ id: m.origin === "house" ? `agent:${m.persona_id}` : m.persona_id, name: m.name, sub: m.origin === "house" ? "world model" : "outside agent", v: m.pnl_live || 0 });
    for (const h of s.humans) rows.push({ id: h.user_id, name: h.nickname, sub: "human", v: h.pnl || 0, me: h.user_id === me.user_id });
    rows.sort((a, b) => b.v - a.v);
    const live = document.getElementById("rank-live");
    live.innerHTML = "";
    rows.slice(0, 20).forEach((r, i) => live.append(rankRow(i + 1, r, `${signed(r.v, 1)}`)));
    const meIdx = rows.findIndex((r) => r.me);
    if (meIdx >= 20) live.append(rankRow(meIdx + 1, rows[meIdx], `${signed(rows[meIdx].v, 1)}`));
    const tm = document.getElementById("rank-tm");
    tm.innerHTML = "";
    const t2 = [];
    const sub = { house: "world model · full holdout", "human-model": "human-written theory · full holdout" };
    for (const m of s.world_models) if (m.brier_holdout != null) t2.push({ id: m.origin === "house" ? `agent:${m.persona_id}` : m.persona_id, name: m.name, sub: sub[m.origin] || "outside agent", v: m.brier_holdout, me: me.theory && m.persona_id === me.theory });
    for (const h of s.humans) if (h.brier != null) t2.push({ id: h.user_id, name: h.nickname, sub: `human · ${h.n} questions`, v: h.brier, me: h.user_id === me.user_id });
    t2.sort((a, b) => a.v - b.v);
    t2.slice(0, 20).forEach((r, i) => tm.append(rankRow(i + 1, r, b3(r.v))));
    if (!t2.some((r) => r.me)) tm.append(el("div", "empty", "Play the time machine to get a Brier score."));
  }
  function rankRow(n, r, v) {
    const row = el("div", `rk${r.me ? " me" : ""}`);
    row.append(el("span", "n", n), H.avatar(r.id, 30));
    const nm = el("div", "nm", r.me ? `${r.name} (you)` : r.name);
    nm.append(el("small", null, r.sub));
    row.append(nm, el("span", `v ${v.startsWith("−") ? "no" : v.startsWith("+") ? "yes" : ""}`, v));
    return row;
  }

  async function start() {
    document.getElementById("join").hidden = true;
    document.getElementById("app").hidden = false;
    if (!(await refreshMe())) return;
    try {
      const s = await api("/harvest/api/state?since=999999999");
      personas = s.personas;
      H.registerPersonas(personas);
      H.registerTraders(s.leaderboard);
    } catch (_) { /* avatars fall back */ }
    await pollMarkets();
    setInterval(pollMarkets, 5000);
  }

  me = load();
  if (me && me.token) start(); else showJoin();
})();
