"use strict";
/* The Crophecy: wires the frontend/ design (index.html, copied verbatim from main) to the live market behind /harvest/api/*.

   agent/routes/site_pages.py serves index.html on clean routes (/home, /live, /markets, /market/<qid>, /leaderboard, /theory, /farmer),
   loads this file before the page's own script and adds three hooks (window.CROPHECY_HOOKS lists the ones in place):
     boot  the page's first go() waits for crophecyBoot below, which swaps the demo data for the live market first;
     sim   the page's random bet simulator does not start; real trades from GET /harvest/api/site?since=<seq> drive it;
     let   money / kfmt / MAXV are `let`, so prices read as play credits and the globe scales to real volumes.
   Everything else is done from here, by refilling the page's own data arrays (AGENTS, MARKETS, POS, EVENTS, ...) and
   replacing the few renderers whose numbers were baked into the demo. If the backend is unreachable or the boot hook is
   missing, the page keeps its demo data under a visible "Preview" banner, so nothing simulated is ever shown as real.

   Endpoints: GET /harvest/api/site (feed), /news, /site/anchors; POST /join, /bet, /croppy, /farmer/chat, /byo, /follow.
   Follow and copy: POST /harvest/api/follow {token, agent: persona id, follow, copy}. */
(() => {
  const API = "/harvest/api";
  const KEY = "harvest-player"; // the player saved by /harvest/play and /app, so one login works everywhere
  const POLL_MS = 5000;
  const HOOKS = window.CROPHECY_HOOKS || [];
  const PERSONA_CODES = { "agronomist": "AGRO", "adaptation-optimist": "ADPT", "macro-dollar": "MACR", "efficient-market-skeptic": "SKEP",
    "geopolitics-hawk": "HAWK", "climate-pessimist": "CLIM", "land-grab-realist": "LAND", "crowd-follower": "CRWD" };
  const CROP_CAT = { "wheat": "Grains", "durum wheat": "Grains", "maize": "Grains", "feed maize": "Grains", "rice": "Grains", "olive oil": "Oils",
    "sunflower oil": "Oils", "sunflower": "Oils", "coffee": "Softs", "cocoa": "Softs", "sugar beet": "Softs", "farmland": "Land",
    "water": "Weather", "all crops": "Policy" };
  const EXTRA_VIEWS = ["theory", "farmer"];
  window.CROPHECY_LIVE = HOOKS.includes("boot") && HOOKS.includes("sim");

  // ---------------------------------------------------------------- helpers
  const q1 = (s, r = document) => r.querySelector(s), qa = (s, r = document) => [...r.querySelectorAll(s)];
  const escH = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const num = (n) => Math.round(n).toLocaleString("en-US");
  const cr = (n) => `${n === 0 ? "0" : Math.abs(n) < 1 ? n.toFixed(2) : Math.abs(n) < 10 ? n.toFixed(1) : num(n)} cr`;
  const setText = (sel, text, root) => qa(sel, root).forEach((e) => { e.textContent = text; });
  async function call(path, body) {
    const init = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(API + path, init);
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (!r.ok) { const e = new Error(data.error || `Request failed (HTTP ${r.status})`); e.status = r.status; throw e; }
    return data;
  }
  const player = () => { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (_) { return null; } };
  const savePlayer = (v) => { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (_) { /* storage blocked */ } };
  const initials = (n) => { const w = String(n).replace(/[^A-Za-z /-]/g, "").split(/[\s/-]+/).filter(Boolean); return ((w[0] || "?")[0] + (w.length > 1 ? w[w.length - 1][0] : (w[0] || "?")[1] || "")).toUpperCase(); };
  const monthEnd = (ym) => { const [y, m] = ym.split("-").map(Number); return new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10); };

  // ---------------------------------------------------------------- state
  const S = { seq: 0, names: {}, house: {}, lb: null, anchors: [], fund: null, users: 0, agents: 0, head: null, personas: [], sessionFees: 0 };

  function preview(reason) {
    if (q1("[data-cr-preview]")) return;
    const bar = document.createElement("div");
    bar.dataset.crPreview = "";
    bar.className = "cr-preview";
    bar.innerHTML = `<b>Preview</b> ${escH(reason)} The numbers on this page are illustrative, not the live market.`;
    document.body.prepend(bar);
  }

  // ---------------------------------------------------------------- mapping: backend -> the page's data model
  function category(m) {
    const t = `${m.qid} ${m.title}`.toLowerCase();
    if (m.kind === "soft") {
      if (/drought|water|rationing|heat/.test(t) && !/budget|cap-/.test(t)) return "Weather";
      if (/farmland|land/.test(t)) return "Land";
      if (/ban|rule|law|budget|cap|sovereign|policy/.test(t)) return "Policy";
    }
    return CROP_CAT[m.crop] || "Policy";
  }
  function resolution(m) {
    const si = m.series_info || {};
    if (m.kind === "soft") {
      return { res: `YES if this happens by ${m.deadline || m.resolve_month}. A soft policy question: it is not auto-resolved and is never scored; its price is a live signal for governments. ${m.why || ""}`.trim(),
        src: "Public announcements (soft question, not auto-resolved)", resolver: "Not auto-resolved. Soft questions stay open as a policy signal." };
    }
    const r = m.rule || {}, up = /_gt$/.test(r.type || ""), unit = si.unit || "";
    return { res: `YES if the ${si.name || m.series} price for ${m.resolve_month} is ${up ? "above" : "below"} ${num(m.target)} ${unit} (it was ${num(m.base_value)} in ${m.base_month}).`,
      src: `${si.source || "IMF primary commodity prices"} · ${m.series}`, srcUrl: si.source_url,
      resolver: "Computed by the market engine from the published monthly series once it is out; payouts follow automatically." };
  }
  function mapMarkets(body) {
    const geo = {};
    for (const g of (body.geo && body.geo.regions) || []) geo[`${g.region}|${g.crop}`] = g;
    const alloc = {};
    for (const a of (body.fund && body.fund.allocations) || []) alloc[`${a.region}|${a.crop}`] = a;
    const out = body.markets.filter((m) => m.status === "open").map((v, i) => {
      const g = geo[`${v.region}|${v.crop}`] || { label: v.region, lat: 50.85, lng: 4.35 };
      const short = String(g.label).split(/ \(|:/)[0].split(" ").slice(0, 3).join(" ").replace(/ (and|the|of)$/, "");
      const hist = (v.history || []).length ? v.history : [[0, v.p_yes, null]];
      const h = hist.map((x) => x[1] * 100), ht = hist.map((x) => (x[2] ? Date.parse(x[2]) : null));
      if (h.length < 2) { h.unshift(h[0]); ht.unshift(ht[0]); }
      const sid = String(v.spec_id || v.qid.split("@")[0]);
      const rs = resolution(v);
      const end = v.kind === "soft" ? String(v.deadline || v.resolve_month).slice(0, 10) : monthEnd(v.resolve_month);
      const m = { id: v.qid, code: sid.toUpperCase().replace(/-\d{4}$/, "").slice(0, 12), q: v.title, cat: category(v),
        reg: `${short}, ${v.region}`, place: g.label, lat: g.lat, lon: g.lng, p: Math.round(v.p_yes * 100), vol: v.volume, fees: v.fees, b: v.b, end,
        sy: v.adverse_if === "yes" ? 1 : 0, tags: `${v.crop} ${v.region} ${sid.replace(/-/g, " ")}`, res: rs.res, src: rs.src, srcUrl: rs.srcUrl,
        resolver: rs.resolver, kind: v.kind, why: v.why || "", forecasts: v.agent_forecasts || [], gk: `${v.region}|${v.crop}`,
        s: { crop: v.why || "", adapt: v.why || "", macro: v.why || "", policy: v.why || "", climate: v.why || "" } };
      m.h = h; m.ht = ht; m.last = v.p_yes * 100; m.prev = h[h.length - 2];
      const chg = v.change || {}; // server-side, over the market's full history: change since 1, 7 and 30 days ago (or since it opened)
      m.d1 = +((chg["1d"] || 0) * 100).toFixed(1); m.d7 = +((chg["7d"] || 0) * 100).toFixed(1); m.d30 = +((chg["30d"] || 0) * 100).toFixed(1);
      m.tk = "M" + String(i + 1).padStart(2, "0"); m.agt = m.forecasts.length;
      return m;
    });
    return { out, alloc };
  }
  const FIELD = ["#5b7a3a", "#3d5a73", "#a4472c", "#2e6b3c", "#8a6517", "#8c2f39", "#2f6b66", "#6b5a3a"];
  function mapAgents(body) {
    const lb = body.leaderboard || {};
    const rows = (lb.by_persona || []).filter((r) => r.origin === "house" || r.origin === "narrative");
    const ids = Object.keys(body.house || {});
    const keys = rows.length ? rows.map((r) => r.key) : ids;
    const brier = Object.fromEntries(rows.map((r) => [r.key, r.brier_holdout]));
    const sorted = [...keys].sort((a, b) => (brier[a] ?? 9) - (brier[b] ?? 9));
    return sorted.map((key) => {
      const p = (body.personas || []).find((x) => String(x.id).split("--")[0] === key) || {};
      const row = rows.find((r) => r.key === key) || {};
      const name = String(row.label || p.name || key).split(" · ")[0];
      const wm = String(p.world_model || "");
      const fam = key.startsWith("narr-") || row.origin === "narrative" ? "trk" : "wm";
      return { id: key, n: name, ab: initials(name), c: FIELD[sorted.indexOf(key) % FIELD.length], fam, code: PERSONA_CODES[key] || key.replace(/^narr-/, "").replace(/[^a-z]/gi, "").slice(0, 4).toUpperCase(),
        watch: fam === "trk" ? (p.world_model || "").split(". ")[0] : undefined, b: brier[key] ?? 0.25, nModels: row.n_agents || 1,
        v: (p.beliefs && p.beliefs[0] ? p.beliefs[0] + "." : wm.split(". ")[0] + ".").replace(/\.\.$/, "."), style: wm.length > 260 ? wm.slice(0, 257) + "…" : wm };
    });
  }
  function actor(trader) {
    if (trader.startsWith("agent:")) {
      const ai = AGENTS.findIndex((a) => a.id === trader.slice(6).split("--")[0]);
      if (ai >= 0) return { kind: "ai", ai, a: AGENTS[ai] };
    }
    const name = S.names[trader] || trader.replace(/^(ext|user):/, "").replace(/-[0-9a-f]{6}$/, "");
    if (trader.startsWith("user:")) return { kind: "hum", ai: HUM_I, a: { n: name, role: "player", ab: name.slice(0, 2).toUpperCase(), c: "#8a9180", code: name.toUpperCase().slice(0, 10) } };
    return { kind: "ext", ai: EXT_I, a: { n: name, ab: "API", c: "#14190f", code: name.toUpperCase().slice(0, 10) } };
  }
  function eventOf(t) {
    const m = MK[t.qid];
    if (!m) return null;
    const who = actor(t.trader), yes = t.side === "YES", ts = Date.parse(t.at) || Date.now();
    const px = Math.round((yes ? t.price_before : 1 - t.price_before) * 100), to = Math.round((yes ? t.price_after : 1 - t.price_after) * 100);
    return { ...who, m, side: t.side, px, from: px, to, stake: t.spend, fee: t.fee || 0, time: hhmmss(new Date(ts)), ts, sig: `#${t.seq}`,
      code: who.kind === "ai" ? CODES[who.ai] : who.a.code, why: t.rationale || "", seq: t.seq, pa: t.price_after };
  }
  function buildPositions(m) {
    return AGENTS.map((a, ai) => {
      const fs = m.forecasts.filter((f) => String(f.persona_id).split("--")[0] === a.id);
      const est = fs.length ? Math.round(fs.reduce((s, f) => s + f.p_yes, 0) / fs.length * 100) : m.p;
      const why = (fs.find((f) => f.source === "llm") || fs[0] || {}).rationale || "No live forecast from this agent yet.";
      const hold = ((S.house[a.id] || {}).positions || {})[m.id] || { yes: 0, no: 0, spent: 0, pnl: 0 };
      const side = hold.yes > hold.no ? "YES" : hold.no > hold.yes ? "NO" : est >= m.p ? "YES" : "NO";
      const shares = side === "YES" ? hold.yes : hold.no;
      const entry = hold.spent > 0 && shares > 0 ? Math.round(hold.spent / shares * 100) : (side === "YES" ? m.p : 100 - m.p);
      const first = ((S.house[a.id] || {}).first || {})[m.id];
      const entryIdx = first == null ? -1 : m.hs.findIndex((s) => s >= first);
      return { a, ai, est, side, stake: Math.round(hold.spent), entry, entryIdx, pnl: Math.round(hold.pnl), why, nf: !fs.length, models: fs };
    });
  }

  function apply(body) {
    S.seq = body.seq; S.names = { ...S.names, ...(body.names || {}) }; S.house = body.house || {}; S.lb = body.leaderboard || null;
    S.anchors = body.anchors || []; S.fund = body.fund; S.users = body.users; S.agents = body.agents; S.head = body.ledger_head; S.personas = body.personas || [];
    for (const h of (S.lb && S.lb.humans) || []) S.names[h.user_id] = h.nickname;
    const agents = mapAgents(body);
    AGENTS.splice(0, AGENTS.length, ...agents);
    CODES.splice(0, CODES.length, ...agents.map((a) => a.code), "EXT", "HUM");
    try { NA = agents.length; EXT_I = NA; HUM_I = NA + 1; } catch (_) { /* let hook missing: indices stay demo-sized */ }
    if (typeof HEAD_ORDER !== "undefined") HEAD_ORDER.splice(0, HEAD_ORDER.length, ...agents.keys(), EXT_I, HUM_I);
    if (typeof SLOT_ORDER !== "undefined") SLOT_ORDER.splice(0, SLOT_ORDER.length, HUM_I, ...agents.keys(), EXT_I);
    if (typeof NOTE_GENS !== "undefined") NOTE_GENS.forEach((g, i) => { if (!g.safe) { const f = () => { try { return g(); } catch (_) { return null; } }; f.safe = true; NOTE_GENS[i] = f; } }); // notes about demo-only agents drop out
    const { out, alloc } = mapMarkets(body);
    const seqs = {};
    for (const v of body.markets) seqs[v.qid] = (v.history || []).map((x) => x[0]);
    for (const m of out) { m.hs = seqs[m.id] || []; if (m.hs.length < m.h.length) m.hs.unshift(m.hs[0]); }
    MARKETS.splice(0, MARKETS.length, ...out);
    for (const k of Object.keys(MK)) delete MK[k];
    for (const m of out) MK[m.id] = m;
    if (typeof CROPS !== "undefined") liveCrops();
    const cats = [...new Set(out.map((m) => m.cat))];
    CATS.splice(0, CATS.length, ...["Grains", "Oils", "Softs", "Weather", "Policy", "Land", "Logistics"].filter((c) => cats.includes(c)));
    REGIONS.length = 0;
    const by = {};
    for (const m of out) {
      if (!by[m.reg]) { by[m.reg] = { n: m.reg, lat: m.lat, lon: m.lon, ms: [], vol: 0, hub: false, gk: m.gk, risk: 0, flow: 0, share: 0 }; REGIONS.push(by[m.reg]); }
      by[m.reg].ms.push(m.id); by[m.reg].vol += m.vol; m.ri = REGIONS.indexOf(by[m.reg]);
    }
    fundRegions(alloc);
    try { MAXV = Math.max(1, ...REGIONS.map((r) => r.vol)); } catch (_) { /* let hook missing: globe sizes stay demo-scaled */ }
    for (const k of Object.keys(POS)) delete POS[k];
    for (const m of out) POS[m.id] = buildPositions(m);
    AGS.splice(0, AGS.length, ...AGENTS.map((a) => ({ bets: (S.house[a.id] || {}).bets || 0, pnl: Math.round((S.house[a.id] || {}).pnl || 0), vol: 0,
      cat: Object.fromEntries(CATS.map((c) => [c, a.b])) }))); // the demo's per-category Brier; its only reader (the record table) is replaced in fixDetail
    const orbit = [...out].sort((a, b) => b.vol - a.vol).filter((m, i, arr) => arr.findIndex((x) => x.ri === m.ri) === i).slice(0, 6);
    ORBIT.splice(0, ORBIT.length, ...orbit);
    EVENTS.length = 0;
    for (const t of [...(body.recent_trades || [])].reverse()) { const ev = eventOf(t); if (ev) EVENTS.push(ev); }
    ODDS = EVENTS.map((e) => e.m.id).filter((id, i, a) => a.indexOf(id) === i).concat(out.map((m) => m.id)).filter((id, i, a) => a.indexOf(id) === i);
    FUND = body.fund ? body.fund.treasury : 0; INFLOW = 0; bets = 0;
    vol = out.reduce((s, m) => s + m.vol, 0); v24 = vol;
  }
  function liveCrops() { // the farmer calculator prices each crop off its live 6-month "up N%" market; crops without one drop out
    const pre = { "Olive": "olive-up", "wheat": "wheat-up", "Maize": "maize-up", "Sunflower": "sunoil-up", "rice": "rice-above" };
    const keep = CROPS.map((c) => {
      const key = Object.keys(pre).find((k) => c.k.includes(k));
      const m = key && MARKETS.find((x) => x.id.startsWith(pre[key]));
      if (!m) return null;
      const up = /-up(\d+)-/.exec(m.id);
      return { ...c, id: m.id, rise: "yes", up: up ? +up[1] / 100 : c.up };
    }).filter(Boolean);
    CROPS.splice(0, CROPS.length, ...keep);
    if (typeof HEDGES === "undefined") return;
    const risks = [ // the hedge calculator's events, each a live market
      { k: "National drought emergency in Italy", id: "it-drought-2027", ev: "Italy declares a national drought emergency in 2027" },
      { k: "Short French wheat harvest", id: "fr-wheat-crop-2027", ev: "France's 2027 soft wheat harvest comes in below 30 Mt" },
      { k: "Short Spanish olive oil harvest", id: "es-olive-output-2027", ev: "Spain's 2027/28 olive oil output falls below 1.0 Mt" },
      { k: "Black Sea grain halt or export ban", id: "ru-grain-ban-2027", ev: "Russia or Ukraine halts grain exports for over 30 days by end of 2027" },
      { k: "Water rationing in Greece", id: "gr-water-rationing-2027", ev: "Athens or a major Greek island rations household water in 2027" },
      { k: "Olive oil price spike (for buyers and mills)", id: "olive-up15-6m@2026-09", ev: "olive oil rises 15% by March 2027" },
    ].filter((h) => MK[h.id]);
    HEDGES.splice(0, HEDGES.length, ...risks);
  }
  function fundRegions(alloc) {
    for (const r of REGIONS) {
      const a = alloc[r.gk];
      const ms = r.ms.map((id) => MK[id]);
      const adverse = ms.reduce((s, m) => s + (m.sy ? m.last : 100 - m.last), 0) / ms.length;
      r.risk = Math.round(a ? a.risk * 100 : adverse); r.flow = a ? a.amount : 0; r.share = a ? a.share : 0;
    }
  }

  // ---------------------------------------------------------------- live updates
  function pushTrade(t) {
    const ev = eventOf(t);
    if (!ev) return;
    const m = ev.m, prev = m.last;
    m.last = (t.price_after) * 100; m.prev = prev; m.dir = m.last >= prev ? 1 : -1; m.p = Math.round(m.last);
    m.h.push(m.last); m.ht.push(ev.ts); m.hs.push(t.seq);
    if (m.h.length > 240) { m.h.shift(); m.ht.shift(); m.hs.shift(); }
    m.vol += t.spend; m.fees = (m.fees || 0) + ev.fee;
    EVENTS.unshift(ev); EVENTS.length = Math.min(EVENTS.length, 150);
    bets++; vol += ev.stake; v24 += ev.stake; FUND += ev.fee; INFLOW += ev.fee; YN[ev.side === "YES" ? 0 : 1]++;
    if (ev.ai < AGENTS.length) AGS[ev.ai].bets++;
    bpmW.push([Date.now(), ev.stake]); while (bpmW.length && Date.now() - bpmW[0][0] > 60000) bpmW.shift();
    if (VIEW === "live" && gLive && !paused) liveOnBet(ev);
    if (VIEW === "markets" && tMap) termOnBet(ev);
    if (VIEW === "home") {
      if (typeof gHero !== "undefined" && gHero) gHero.comet(m.ri, "#8a6517", ev.ai);
      if (typeof gOrbit !== "undefined" && gOrbit && gOrbit.active < 0) gOrbit.comet(m.ri, "#8a6517", ev.ai);
      const ul = q1("[data-tx]");
      if (ul) { ul.insertAdjacentHTML("afterbegin", `<li><b>${escH(ev.sig)}</b><span>${escH(ev.a.n)} · ${escH(m.code)}</span><span>${ev.side}</span></li>`); while (ul.children.length > 3) ul.lastElementChild.remove(); }
    }
    setText("[data-vol]", money(vol)); setText("[data-bets]", String(bets)); setText("[data-fund]", money(FUND));
  }
  async function poll() {
    if (document.hidden) { setTimeout(poll, POLL_MS); return; }
    try {
      const body = await call(`/site?since=${S.seq}`);
      for (const t of body.recent_trades || []) pushTrade(t);
      S.seq = body.seq; S.names = { ...S.names, ...(body.names || {}) }; S.users = body.users; S.head = body.ledger_head;
      for (const v of body.markets || []) { const m = MK[v.qid]; if (m) { m.vol = v.volume; m.fees = v.fees; m.last = v.p_yes * 100; m.p = Math.round(m.last); } }
      if (body.fund) { FUND = body.fund.treasury; const alloc = {}; for (const a of body.fund.allocations || []) alloc[`${a.region}|${a.crop}`] = a; fundRegions(alloc); S.fund = { ...S.fund, ...body.fund }; }
      for (const [id, row] of Object.entries(body.house || {})) { const ai = AGENTS.findIndex((a) => a.id === id); if (ai >= 0) { AGS[ai].pnl = Math.round(row.pnl); AGS[ai].bets = row.bets; } }
      vol = MARKETS.reduce((s, m) => s + m.vol, 0); v24 = vol;
      setText("[data-vol]", money(vol)); setText("[data-v24]", money(vol)); setText("[data-v24n]", `+${bets} since you arrived`);
      renderFund(); paintLedger();
    } catch (e) { console.warn("crophecy poll", e); }
    setTimeout(poll, POLL_MS);
  }

  // ---------------------------------------------------------------- static copy that carried demo numbers
  function paintLedger() {
    const head = S.head ? num(S.head.seq) : "–";
    qa("[data-slot]").forEach((e) => { e.removeAttribute("data-slot"); e.dataset.crLedger = ""; });
    setText("[data-cr-ledger]", head);
    const last = S.anchors.find((a) => a.status === "confirmed");
    setText("[data-sealed]", last ? num(last.count) : "0");
  }
  function relabel(el, text) { if (el) el.textContent = text; }
  function paintChrome() {
    const chain = q1(".topbar .chain");
    qa("footer.foot .wrap span:last-child").forEach((e) => { e.textContent = "Live market data. Play credits only, no cash value."; });
    qa("footer.foot .wrap span:first-child").forEach((e) => { e.textContent = "Crophecy · Solana devnet"; });
    if (chain) { chain.title = "Hash-chained ledger entries, anchored to Solana devnet every 10 entries"; chain.childNodes.forEach((n) => { if (n.nodeType === 3 && n.textContent.includes("Devnet")) n.textContent = "Ledger "; }); }
    paintLedger();
  }
  function paintStatic() {
    const lb = S.lb || {}, mkt = (lb.baselines || {}).market, nOpen = MARKETS.length;
    const eb = q1(".h-eyebrow");
    if (eb) eb.innerHTML = `<i class="dot-live"></i><b>${AGENTS.length} AI world models</b> and ${num(S.agents)} outside agents are trading European harvests now`;
    const stats = qa(".h-stat");
    if (stats[2]) { stats[2].querySelector("b").setAttribute("data-fund", ""); }
    if (stats[3]) relabel(stats[3].querySelector("span"), "Ledger entries sealed");
    if (stats[0]) relabel(stats[0].querySelector("span"), "Volume traded (play credits)");
    const facts = qa("#h-sol .facts li span:last-child");
    relabel(facts[0], "Every bet, join and forecast is appended to a hash-chained ledger, and its head is anchored to Solana devnet every 10 entries. Nobody edits a forecast after the rain.");
    relabel(facts[1], "Price markets resolve against the IMF primary commodity series (via FRED). Soft policy questions stay open as live signals and are never scored.");
    relabel(facts[2], "2% of every trade flows to the Resilience Fund, which allocates itself to regions by market-implied climate risk.");
    qa(".block .mono").forEach((e) => { if (/slot/i.test(e.textContent)) e.textContent = "Ledger head"; else if (e.textContent === "sealed") e.textContent = "anchored"; });
    qa(".h-sec .btn.ghost").forEach((e) => { if (/All \d+ markets/.test(e.textContent)) e.firstChild.textContent = `All ${nOpen} markets `; });
    const kicker = q1("#h-agents .kicker");
    if (kicker && lb.n_holdout) kicker.textContent = `Sealed backtest · ${lb.n_holdout} holdout questions`;
    const lede = q1("#h-agents .sec-head p");
    if (lede) lede.textContent = `Lower Brier is better, scored on the sealed holdout${AGENTS[0] && AGENTS[0].nModels > 1 ? `, each persona averaged over its ${AGENTS[0].nModels} language models` : ""}. Agents left of the market line beat the crowd price. Click one to watch it bet live.`;
    if (mkt != null) {
      const key = qa(".lb-key span");
      if (key[0]) key[0].lastChild.textContent = `Market price ${mkt.toFixed(3)}`;
      qa(".lb-row .lb-bar u").forEach((u) => { u.style.left = `${Math.min(100, mkt / 0.25 * 100)}%`; });
    }
    const tx = q1("[data-tx]");
    if (tx) tx.innerHTML = S.anchors.slice(0, 3).map((a) => `<li><b><a href="${escH(a.explorer_url || "#")}" target="_blank" rel="noopener">${escH(String(a.signature || a.memo || "").slice(0, 4))}…${escH(String(a.signature || "").slice(-4))}</a></b><span>${escH(a.kind)} · ${num(a.count || 0)} entries</span><span>${escH(a.status)}</span></li>`).join("");
    setText("[data-vol]", money(vol)); setText("[data-bets]", "0");
    renderFund(); paintLedger();
  }
  function paintLive() {
    const kp = qa(".lv-kpis .kpi");
    if (kp[0]) { relabel(kp[0].querySelector("span"), "Volume"); }
    if (kp[1]) relabel(kp[1].querySelector("b"), String(MARKETS.length));
    if (kp[2]) { relabel(kp[2].querySelector("b"), `${AGENTS.length} + ${num(S.agents)}`); relabel(kp[2].querySelector("small"), "house models + outside agents"); }
    if (kp[3]) relabel(kp[3].querySelector("small"), "2% of volume");
    if (kp[4]) relabel(kp[4].querySelector("span"), "Ledger entries");
    setText("[data-v24]", money(vol)); setText("[data-v24n]", "live");
    const hard = MARKETS.filter((m) => m.kind === "hard").length; setText("[data-hard]", `${hard} price · ${MARKETS.length - hard} policy`);
    const lbl = q1(".lv-fund .lbl span");
    if (lbl) lbl.textContent = "Resilience Fund · 2% of every bet, allocated by market-implied climate risk";
    const ft = [...REGIONS].sort((a, b) => b.flow - a.flow).map((r) => `<span>${escH(r.n.split(",")[0])} <b>${money(r.flow)}</b> risk ${r.risk}</span>`).join("");
    const ftk = q1("[data-ftk]"); if (ftk) ftk.innerHTML = ft + ft;
    paintLedger();
  }
  async function paintMarkets() {
    const st = q1(".tm-st");
    if (st) st.childNodes.forEach((n) => { if (n.nodeType === 3 && /SOLANA/.test(n.textContent)) n.textContent = "LIVE · LEDGER #"; });
    qa(".tp-rg .tm-stats span").forEach((s) => { if (/CUT OF VOL/.test(s.textContent)) s.innerHTML = "CUT OF VOL <b>2.00%</b>"; if (/INFLOW/.test(s.textContent)) s.firstChild.textContent = "INFLOW SINCE OPEN "; });
    setText("[data-inflow]", money(INFLOW)); paintLedger();
    const box0 = q1("[data-news]"); if (box0) box0.innerHTML = "<span>■ LOADING NEWS…</span>";
    try {
      const n = await call("/news?limit=14");
      S.news = n.items || [];
      const h = S.news.map((x) => `<span>■ ${escH(String(x.title).toUpperCase())}</span>`).join("");
      const box = q1("[data-news]"); if (box && h) box.innerHTML = h + h;
    } catch (_) { const box = q1("[data-news]"); if (box) box.innerHTML = "<span>■ NEWS FEED UNAVAILABLE</span>"; }
  }

  // ---------------------------------------------------------------- overrides of the page's demo renderers
  function install() {
    try { money = (n) => cr(n); kfmt = (n) => (n >= 1e6 ? (n / 1e6).toFixed(2) + "M cr" : n >= 1e4 ? Math.round(n / 1e3) + "k cr" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k cr" : Math.round(n) + " cr"); } catch (_) { /* let hook missing */ }
    const orig = { go, initHome, initLive, initMarkets, renderDetail, pickAgent, desHelp };
    go = function (route, opt = {}) {
      route = route || "home";
      if (route === "leaderboard") { route = "home"; opt = { ...opt, anchor: "h-agents" }; }
      if (EXTRA_VIEWS.includes(route)) { showExtra(route); return; }
      orig.go(route, opt);
      try { history.replaceState(null, "", pathFor(route, opt)); } catch (_) { /* sandboxed */ }
    };
    initHome = function () { orig.initHome(); paintStatic(); };
    initLive = function () { orig.initLive(); paintLive(); };
    initMarkets = function () { orig.initMarkets(); paintMarkets(); };
    renderDetail = function (id) { orig.renderDetail(id); if (MK[id]) fixDetail(MK[id]); };
    pickAgent = function (ai) {
      try { orig.pickAgent(ai); } catch (e) { console.warn(e); }
      const box = q1("[data-insp]"); if (box && AGENTS[ai]) box.insertAdjacentHTML("beforeend", followHTML(ai));
    };
    desHelp = function () { orig.desHelp(); qa("[data-des-b] .tk").forEach((e) => { e.textContent = e.textContent.replace("M01–M16", `M01–M${String(MARKETS.length).padStart(2, "0")}`); }); };
    quoteFor = (ev) => (ev.why ? ev.why.replace(/^[\w.-]+: /, "") : ev.kind === "hum" ? "A player's bet." : "An outside agent's bet.");
    sealed = function () { const now = performance.now(); if (now - (sealed.t || 0) < 260) return; sealed.t = now; qa("[data-block]").forEach((b) => { b.classList.add("flash"); setTimeout(() => b.classList.remove("flash"), 220); }); };
    renderFund = function () {
      setText("[data-fund]", money(FUND));
      const r = q1("[data-ring]"); if (r) r.setAttribute("stroke-dashoffset", "0");
    };
    renderRegionBoard = function () {
      const host = q1("[data-rgb]"); if (!host) return;
      host.innerHTML = REGIONS.map((r, k) => ({ r, k })).sort((a, b) => b.r.risk - a.r.risk).map(({ r, k }) => `<tr data-rk="${k}"><td>${escH(r.n.split(",")[0].toUpperCase())}</td><td><span class="rbar"><i style="width:${r.risk}%;background:${RISKC[riskWord(r.risk)]}"></i></span><span style="color:${RISKC[riskWord(r.risk)]}">${r.risk}</span></td><td class="r">${(r.share * 100).toFixed(1)}%</td><td class="r" data-flow="${k}">${money(r.flow)}</td></tr>`).join("");
    };
    oddsChart = chart;
    desAgent = desAgentLive;
    desAgents = function () {
      const mkt = ((S.lb || {}).baselines || {}).market;
      des("DES · AGNT · SEALED BACKTEST", `<p>Brier on ${(S.lb || {}).n_holdout || "the"} sealed holdout questions, each persona averaged over its language models. Lower is better. Market ${mkt != null ? mkt.toFixed(3) : "–"}, coin flip 0.250. P&amp;L is live play credits.</p><div class="tt-wrap"><table class="tt"><thead><tr><th>#</th><th>CODE</th><th>AGENT</th><th class="r">BRIER</th><th class="r">P&amp;L LIVE</th><th class="r">BETS</th></tr></thead><tbody>${AGENTS.map((a, i) => `<tr data-act="agent:${i}"><td>${i + 1}</td><td style="color:var(--t-vi)">${CODES[i]}</td><td>${escH(a.n)}</td><td class="r ${mkt != null && a.b < mkt ? "t-up" : ""}">${a.b.toFixed(3)}</td><td class="r ${AGS[i].pnl >= 0 ? "t-up" : "t-dn"}">${AGS[i].pnl >= 0 ? "+" : "−"}${money(Math.abs(AGS[i].pnl))}</td><td class="r">${AGS[i].bets}</td></tr>`).join("")}</tbody></table></div>`);
    };
    desActors = function (kind) {
      const evs = EVENTS.filter((e) => e.kind === kind).slice(0, 14);
      des(`DES · ${kind === "hum" ? "HUM · HUMAN TRADERS" : "EXT · API TRADERS"}`, `<p>${kind === "hum" ? `Players trading alongside the agents with play credits (${num(S.users)} joined).` : `Outside agents trading through the public agent API (${num(S.agents)} registered). <a href="/harvest/api/agents" style="color:var(--t-vi)">API docs</a>`}</p><div class="tt-wrap"><table class="tt"><thead><tr><th>TIME</th><th>WHO</th><th>MKT</th><th>SIDE</th><th class="r">SIZE</th></tr></thead><tbody>${evs.map((e) => `<tr data-act="mkt:${escH(e.m.id)}"><td>${e.time}</td><td>${escH(e.code)}</td><td class="tk">${e.m.tk}</td><td class="${e.side === "YES" ? "t-up" : "t-dn"}">${e.side}</td><td class="r">${money(e.stake)}</td></tr>`).join("") || '<tr><td colspan="5">No recent trades.</td></tr>'}</tbody></table></div>`);
    };
    desRegion = function (k) {
      const r = REGIONS[k], ms = r.ms.map((id) => MK[id]);
      des(`DES · REGION · ${r.n.toUpperCase()}`, `<h3>${escH(r.n)}</h3><p>${escH(ms[0].why)}</p><div class="kv"><div><span>CLIMATE RISK</span><b style="color:${RISKC[riskWord(r.risk)]}">${r.risk} ${riskWord(r.risk)}</b></div><div><span>MARKETS</span><b>${ms.length}</b></div><div><span>VOLUME</span><b>${money(r.vol)}</b></div><div><span>FUND ALLOCATION</span><b>${money(r.flow)}</b></div></div><div class="tt-wrap"><table class="tt"><thead><tr><th>TKR</th><th>MARKET</th><th class="r">LAST</th><th class="r">VOL</th></tr></thead><tbody>${ms.map((m) => `<tr data-act="mkt:${escH(m.id)}"><td class="tk">${m.tk}</td><td class="q">${escH(m.q)}</td><td class="r">${m.last.toFixed(1)}</td><td class="r">${kfmt(m.vol)}</td></tr>`).join("")}</tbody></table></div><div class="des-act"><button type="button" class="p" data-act="liveregion:${k}">SHOW ON LIVE GLOBE &lt;GO&gt;</button></div>`);
    };
    desFund = function () {
      const f = S.fund || {};
      des("DES · FUND · RESILIENCE FUND", `<div class="kv"><div><span>BALANCE</span><b>${money(FUND)}</b></div><div><span>CUT OF VOLUME</span><b>${((f.fee_rate || 0.02) * 100).toFixed(2)}%</b></div><div><span>INFLOW SINCE OPEN</span><b>${money(INFLOW)}</b></div><div><span>EARMARK</span><b>${escH(String(f.earmark || "ADAPTATION").toUpperCase())}</b></div></div><p>Allocation follows market-implied climate risk: each region's share is its adverse-outcome probability over the sum. Memo <span style="color:var(--t-vi)">${escH(f.memo || "")}</span></p><div class="tt-wrap"><table class="tt"><thead><tr><th>REGION</th><th>CROP</th><th class="r">RISK</th><th class="r">SHARE</th><th class="r">AMOUNT</th></tr></thead><tbody>${(f.allocations || []).slice(0, 12).map((a) => `<tr><td>${escH(a.region.toUpperCase())}</td><td>${escH(a.crop.toUpperCase())}</td><td class="r">${Math.round(a.risk * 100)}</td><td class="r">${(a.share * 100).toFixed(1)}%</td><td class="r">${money(a.amount)}</td></tr>`).join("")}</tbody></table></div>`);
    };
    desSeal = async function () {
      des("DES · SEAL · LATEST ANCHORS", "<p>Loading anchors…</p>");
      let rows = S.anchors;
      try { rows = (await call("/site/anchors")).anchors; } catch (_) { /* keep the boot copy */ }
      q1("[data-des-b]").innerHTML = `<p>The ledger is hash-chained; every ${10} entries its head is written to Solana devnet as a memo. Anyone can recompute it at <a href="/harvest/api/verify" style="color:var(--t-vi)">/harvest/api/verify</a>.</p><div class="tt-wrap"><table class="tt"><thead><tr><th>KIND</th><th class="r">ENTRIES</th><th>TX</th><th>STATUS</th><th>TIME</th></tr></thead><tbody>${rows.map((a) => `<tr><td>${escH(String(a.kind).toUpperCase())}</td><td class="r">${num(a.count || 0)}</td><td>${a.explorer_url ? `<a href="${escH(a.explorer_url)}" target="_blank" rel="noopener" style="color:var(--t-vi)">${escH(String(a.signature).slice(0, 10))}…</a>` : "–"}</td><td>${escH(String(a.status).toUpperCase())}</td><td>${escH(String(a.at || "").slice(11, 19))}</td></tr>`).join("") || '<tr><td colspan="5">No anchors yet.</td></tr>'}</tbody></table></div>`;
    };
    desNews = function () {
      des("DES · NEWS", `<div class="tt-wrap"><table class="tt"><tbody>${(S.news || []).map((n) => `<tr><td style="white-space:normal"><a href="${escH(n.url)}" target="_blank" rel="noopener" style="color:var(--t-tx)">${escH(n.title)}</a><br><span style="color:var(--t-mu)">${escH(n.source)} · ${escH(String(n.published_at).slice(0, 16).replace("T", " "))}${n.direction ? ` · ${escH(n.direction.toUpperCase())}` : ""}</span></td></tr>`).join("") || "<tr><td>No headlines yet.</td></tr>"}</tbody></table></div>`);
    };
  }
  function pathFor(route, opt) {
    if (opt && opt.anchor === "h-agents") return "/leaderboard";
    if (route === "home") return "/";
    if (route.startsWith("m-")) return "/market/" + encodeURIComponent(route.slice(2));
    return "/" + route;
  }
  function routeFromPath() {
    const p = location.pathname.replace(/\/+$/, "");
    if (p.startsWith("/market/")) return "m-" + decodeURIComponent(p.slice(8));
    if (["/home", "/live", "/markets", "/leaderboard", "/theory", "/farmer"].includes(p)) return p.slice(1);
    return (location.hash || "#home").slice(1);
  }

  // ---------------------------------------------------------------- market page: real record, resolution and ticket
  function chart(host, m, range, pos) {
    const svg = q1("svg", host), tip = q1(".tip", host), W = host.clientWidth, H = 300, ml = 6, mr = 44, mt = 14, mb = 28, iw = W - ml - mr, ih = H - mt - mb;
    if (W < 10) return;
    const cut = Date.now() - range * 864e5;
    let s0 = m.ht.findIndex((t) => t != null && t >= cut);
    if (s0 < 0 || m.h.length - s0 < 2) s0 = Math.max(0, m.h.length - 2);
    const d = m.h.slice(s0), tt = m.ht.slice(s0), x = (i) => ml + i / (d.length - 1) * iw, y = (v) => mt + (1 - v / 100) * ih;
    const lab = (i) => { const t = tt[i]; if (t == null) return "open"; const dd = new Date(t); return Date.now() - t < 864e5 ? dd.toTimeString().slice(0, 5) : dd.toLocaleDateString("en-GB", { day: "numeric", month: "short" }); };
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    let g = "";
    for (const t of [0, 25, 50, 75, 100]) g += `<line x1="${ml}" x2="${ml + iw}" y1="${y(t)}" y2="${y(t)}" stroke="#dfe3d7" ${t === 50 ? "" : 'stroke-dasharray="2 4"'}/><text x="${W - 4}" y="${y(t) + 4}" text-anchor="end" font-family="Geist Mono, monospace" font-size="11" fill="#8a9180">${t}%</text>`;
    for (let k = 0; k <= 4; k++) { const i = Math.round(k / 4 * (d.length - 1)); g += `<text x="${x(i)}" y="${H - 8}" text-anchor="${k === 0 ? "start" : k === 4 ? "end" : "middle"}" font-family="Geist Mono, monospace" font-size="11" fill="#8a9180">${lab(i)}</text>`; }
    const path = d.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join("");
    g += `<defs><linearGradient id="oa" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#8a6517" stop-opacity=".18"/><stop offset="1" stop-color="#8a6517" stop-opacity="0"/></linearGradient></defs>`;
    g += `<path d="${path}L${x(d.length - 1)} ${y(0)}L${x(0)} ${y(0)}Z" fill="url(#oa)"/><path d="${path}" fill="none" stroke="#8a6517" stroke-width="2" stroke-linejoin="round"/>`;
    for (const p of pos) { const i = p.entryIdx - s0; if (p.entryIdx < 0 || i < 0 || i >= d.length) continue; g += `<circle cx="${x(i)}" cy="${y(d[i])}" r="4.5" fill="${p.a.c}" stroke="#fff" stroke-width="2"><title>${escH(p.a.n)} first traded here (${lab(i)})</title></circle>`; }
    g += `<circle cx="${x(d.length - 1)}" cy="${y(d[d.length - 1])}" r="5" fill="#8a6517" stroke="#fff" stroke-width="2"/>`;
    g += `<g data-x style="display:none"><line y1="${mt}" y2="${mt + ih}" stroke="#14190f" stroke-opacity=".35"/><circle r="5" fill="#fff" stroke="#8a6517" stroke-width="2"/></g><rect x="${ml}" y="${mt}" width="${iw}" height="${ih}" fill="transparent" data-hit/>`;
    svg.innerHTML = g;
    const cross = q1("[data-x]", svg), hit = q1("[data-hit]", svg);
    hit.addEventListener("pointermove", (e) => {
      const r = svg.getBoundingClientRect(), i = Math.max(0, Math.min(d.length - 1, Math.round((e.clientX - r.left - ml) / iw * (d.length - 1)))), X = x(i), Y = y(d[i]);
      cross.style.display = ""; q1("line", cross).setAttribute("x1", X); q1("line", cross).setAttribute("x2", X); q1("circle", cross).setAttribute("cx", X); q1("circle", cross).setAttribute("cy", Y);
      tip.innerHTML = `<b>${d[i].toFixed(1)}% YES</b>${lab(i)}`; tip.style.opacity = 1; const tw = tip.offsetWidth; tip.style.transform = `translate(${Math.min(W - tw, Math.max(0, X - tw / 2))}px,${Math.max(0, Y - 62)}px)`;
    });
    hit.addEventListener("pointerleave", () => { cross.style.display = "none"; tip.style.opacity = 0; });
  }
  function fixDetail(m) {
    const root = q1("[data-detail]");
    const pos = [...POS[m.id]].sort((a, b) => b.stake - a.stake);
    qa(".panel", root).forEach((panel) => {
      const h2 = (q1("h2", panel) || {}).textContent || "";
      if (q1(".rec-wrap", panel)) {
        const last = S.anchors.find((a) => a.status === "confirmed");
        panel.innerHTML = `<div class="panel-h"><h2>Live forecasts on the record</h2><span class="mono" style="color:var(--muted);display:inline-flex;gap:6px;align-items:center"><svg class="solmark"><use href="#sol"/></svg>Solana devnet</span></div>
          <div class="rec-wrap"><table class="rec"><thead><tr><th>Agent</th><th class="r">Forecast</th><th class="r">Models</th><th>Holds</th><th class="r">Staked</th><th class="r">P&amp;L</th><th class="r">Holdout Brier</th></tr></thead><tbody>
          ${pos.map((x) => `<tr><td><span style="display:inline-flex;gap:8px;align-items:center">${avatar(x.a, "sm")}${escH(x.a.n)}</span></td><td class="r mono">${x.nf ? "–" : x.est + "%"}</td><td class="r mono">${x.models.length}</td><td class="mono">${x.stake ? x.side : "–"}</td><td class="r mono">${money(x.stake)}</td><td class="r mono ${x.pnl >= 0 ? "up" : "dn"}">${x.pnl >= 0 ? "+" : "−"}${money(Math.abs(x.pnl))}</td><td class="r mono">${x.a.b.toFixed(3)}</td></tr>`).join("")}
          </tbody></table></div>
          <p class="mono" style="color:var(--muted);margin-top:12px">Every trade is appended to a hash-chained ledger and the head is anchored to Solana devnet every 10 entries${last ? `; latest anchor covers ${num(last.count)} entries: <a href="${escH(last.explorer_url)}" target="_blank" rel="noopener">view the transaction</a>` : ""}. Holdout Brier is the persona's sealed-backtest score.</p>`;
      } else if (/Resolution/.test(h2)) {
        panel.innerHTML = `<div class="panel-h"><h2>Resolution</h2></div><dl class="res"><dt>Rule</dt><dd>${escH(m.res)}</dd><dt>Source</dt><dd>${m.srcUrl ? `<a href="${escH(m.srcUrl)}" target="_blank" rel="noopener">${escH(m.src)}</a>` : escH(m.src)}</dd><dt>Resolves</dt><dd>${endLabel(m)}</dd><dt>Resolver</dt><dd>${escH(m.resolver)}</dd><dt>Fund share</dt><dd>2% of every trade, ${money(m.fees || 0)} so far, goes to the Resilience Fund.</dd>${m.why && m.kind !== "soft" ? `<dt>Why it matters</dt><dd>${escH(m.why)}</dd>` : ""}</dl>`;
      } else if (/Positions/.test(h2)) {
        qa(".pos-row .nums", panel).forEach((n, i) => { const x = pos[i]; if (x && !x.stake) n.innerHTML = `<b>${money(0)}</b><span>no position</span>`; });
      }
    });
    const hint = qa(".panel p.mono", root).find((p) => /Dots mark/.test(p.textContent));
    if (hint) hint.textContent = "Dots mark each agent's first trade here. Hover to read the price after any trade.";
    ticket(m);
  }
  function ticket(m) {
    const tk = q1("[data-ticket]");
    if (!tk) return;
    const inp = q1("#tk-amt", tk);
    const unit = q1(".amt i", tk); if (unit) { unit.textContent = "cr"; unit.style.fontSize = "11px"; }
    inp.max = 500; if (+inp.value > 500) inp.value = 50;
    qa('.quick button[data-add="max"]', tk).forEach((b) => { b.dataset.add = "500"; b.textContent = "Max"; });
    const note = q1(".note", tk); if (note) note.textContent = "Play credits only: every player starts with 1,000 and they have no cash value. 2% of each bet goes to the Resilience Fund.";
    const side = () => (q1('.yn button[aria-pressed="true"]', tk) || {}).dataset?.s || "YES";
    const calc = () => {
      const p = Math.min(0.99, Math.max(0.01, m.last / 100)), pp = side() === "YES" ? p : 1 - p, a = Math.max(0, Math.min(500, +inp.value || 0)), net = a * 0.98, b = m.b || 100;
      const sh = b * Math.log((Math.exp(net / b) - 1 + pp) / pp), avg = sh > 0 ? net / sh : pp;
      q1("[data-k=px]", tk).textContent = Math.round(avg * 100) + "¢"; q1("[data-k=sh]", tk).textContent = sh.toFixed(1);
      q1("[data-k=po]", tk).textContent = money(sh); q1("[data-k=pf]", tk).textContent = "+" + money(Math.max(0, sh - a));
    };
    inp.addEventListener("input", calc);
    qa(".yn button, .quick button", tk).forEach((b) => b.addEventListener("click", () => setTimeout(calc)));
    calc();
    const old = q1("[data-place]", tk), btn = old.cloneNode(true);
    old.replaceWith(btn);
    btn.textContent = "Place bet";
    const ok = q1("[data-ok]", tk);
    const say = (html, bad) => { ok.style.color = bad ? "var(--no)" : ""; ok.style.background = bad ? "color-mix(in srgb,var(--no) 8%,#fff)" : ""; ok.innerHTML = html; ok.classList.add("on"); };
    btn.addEventListener("click", async () => {
      const a = Math.min(500, +inp.value || 0);
      if (a < 1) { say("<b>Enter between 1 and 500 credits.</b>", true); return; }
      const me = await ensurePlayer(); if (!me) return;
      btn.disabled = true;
      try {
        const r = await call("/bet", { token: me.token, qid: m.id, side: side(), spend: a });
        say(`<b>${r.trade.side} ${money(r.trade.spend)} placed · ledger #${num(r.entry.seq)}</b><span class="mono">price now ${Math.round(r.market.p_yes * 100)}% YES · balance ${money(r.balance)}</span>`);
        balanceLine(tk, r.balance);
      } catch (e) {
        if (e.status === 401) { localStorage.removeItem(KEY); say("<b>Your session expired. Press the button again to rejoin.</b>", true); } else say(`<b>${escH(e.message)}</b>`, true);
      } finally { btn.disabled = false; }
    });
    const me = player();
    if (me) call(`/me?token=${encodeURIComponent(me.token)}`).then((r) => balanceLine(tk, r.balance)).catch(() => {});
  }
  function balanceLine(tk, bal) {
    let line = q1("[data-cr-bal]", tk);
    if (!line) { line = document.createElement("p"); line.dataset.crBal = ""; line.className = "mono cr-bal"; q1("h2", tk).after(line); }
    const me = player();
    line.textContent = `${me ? me.nickname + " · " : ""}balance ${money(bal)}`;
  }

  // ---------------------------------------------------------------- join, follow
  function modal(html) {
    const wrap = document.createElement("div");
    wrap.className = "cr-modal";
    wrap.innerHTML = `<div class="cr-card" role="dialog" aria-modal="true">${html}</div>`;
    document.body.append(wrap);
    wrap.addEventListener("click", (e) => { if (e.target === wrap) wrap.remove(); });
    return wrap;
  }
  function ensurePlayer() {
    const me = player();
    if (me && me.token) return Promise.resolve(me);
    return new Promise((resolve) => {
      const w = modal(`<h3>Pick a nickname</h3><p>You get 1,000 play credits to bet with. No sign-up, no real money.</p><form><input name="n" maxlength="24" placeholder="e.g. olive_ana" autocomplete="nickname" required><button class="btn dark" type="submit">Join</button></form><p class="cr-err" role="alert"></p>`);
      const f = q1("form", w), inp = q1("input", w);
      inp.focus();
      w.addEventListener("click", (e) => { if (e.target === w) resolve(null); });
      f.addEventListener("submit", async (e) => {
        e.preventDefault();
        try {
          const r = await call("/join", { nickname: inp.value.trim() });
          const v = { token: r.token, nickname: r.nickname, user_id: r.user_id };
          savePlayer(v); w.remove(); resolve(v);
        } catch (err) { q1(".cr-err", w).textContent = err.message; }
      });
    });
  }
  function followHTML(ai) {
    return `<h4>Follow this agent</h4><div class="cr-follow" data-cr-follow="${ai}"><button type="button" class="btn ghost" data-cr-f="follow">Follow</button><button type="button" class="btn dark" data-cr-f="copy">Copy its bets</button></div><p class="lede cr-fmsg" data-cr-fmsg></p>`;
  }
  async function follow(ai, copy, msgEl) {
    const me = await ensurePlayer(); if (!me) return;
    const a = AGENTS[ai];
    try {
      // the backend follows one house persona run (book.personas ids, e.g. crowd-follower--phi-4); the page shows the persona
      const pid = (S.personas.find((x) => x.id === a.id) || S.personas.find((x) => String(x.id).split("--")[0] === a.id) || { id: a.id }).id;
      const r = await call("/follow", { token: me.token, agent: pid, follow: true, copy });
      msgEl.textContent = copy ? `Copying ${a.n}: each of its bets places a small bet for you too.` : `Following ${a.n}.`;
      if (r && r.following && r.following.name) msgEl.textContent += ` Run: ${r.following.name}.`;
    } catch (e) {
      const notYet = (e.status === 404 && /^Not found/.test(e.message)) || e.status === 403 || /admin/i.test(e.message);
      msgEl.textContent = notYet ? "Follow and copy are being switched on; try again shortly." : e.message;
    }
  }

  // ---------------------------------------------------------------- extra views: theory (BYO), farmer chat; Croppy everywhere
  function showExtra(name) {
    VIEW = name;
    let view = q1(`[data-view="${name}"]`);
    if (!view) { view = document.createElement("main"); view.className = "view cr-x"; view.dataset.view = name; view.hidden = true; q1("main.view:last-of-type").after(view); (name === "theory" ? renderTheory : renderFarmer)(view); }
    qa(".view").forEach((v) => { v.hidden = v.dataset.view !== name; });
    qa("[data-nav]").forEach((a) => a.toggleAttribute("aria-current", a.dataset.nav === name));
    scrollTo(0, 0);
    try { history.replaceState(null, "", "/" + name); } catch (_) { /* sandboxed */ }
  }
  function renderTheory(view) {
    view.innerHTML = `<div class="wrap cr-page"><header class="sec-head"><span class="kicker">Write your theory</span><h2>Can your <em class="s">world model</em> beat the agents?</h2><p>Describe in a few sentences how climate, politics and money move food prices. We hash it, commit it to the ledger, then backtest it on the sealed holdout questions and rank it against the house world models.</p></header>
      <form class="cr-form" data-cr-theory><label>Name<input name="name" maxlength="40" required placeholder="Heat bites first"></label><label>Your world model<textarea name="wm" rows="6" maxlength="1200" required minlength="20">Heat waves and droughts hit southern Europe harder every year, and markets are slow to price it in. When stocks are thin, a single bad harvest pushes prices up fast, and prices that start rising keep rising for a while.</textarea></label><button class="btn dark lg" type="submit">Backtest my theory</button></form>
      <div data-cr-out class="cr-out"></div></div>`;
    q1("[data-cr-theory]", view).addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.currentTarget, out = q1("[data-cr-out]", view), btn = q1("button", f), me = player();
      btn.disabled = true; out.innerHTML = '<p class="mono">Backtesting on the sealed holdout. This can take a minute…</p>';
      try {
        const r = await call("/byo", { name: f.name.value, world_model: f.wm.value, ...(me ? { token: me.token } : {}) });
        const mkt = ((S.lb || {}).baselines || {}).market;
        out.innerHTML = `<div class="panel"><h3>${escH(r.name)}</h3><p class="mono">Brier on the sealed holdout <b>${r.brier_holdout != null ? r.brier_holdout.toFixed(3) : "–"}</b> over ${r.n || 0} questions · rank <b>${r.rank || "–"}</b>${r.of ? ` of ${r.of}` : ""}${mkt != null ? ` · market ${mkt.toFixed(3)}` : ""}</p><p class="mono" style="color:var(--muted)">World model hash ${escH(String(r.world_model_sha256 || "").slice(0, 16))}… committed before it saw a question.</p></div>`;
      } catch (err) { out.innerHTML = `<p class="cr-err">${escH(err.message)}</p>`; } finally { btn.disabled = false; }
    });
  }
  function chatUI(view, { title, kicker, lede, placeholder, send }) {
    view.innerHTML = `<div class="wrap cr-page"><header class="sec-head"><span class="kicker">${kicker}</span><h2>${title}</h2><p>${lede}</p></header><div class="cr-chat"><div class="cr-log" data-log aria-live="polite"></div><form data-ask><input name="m" maxlength="400" placeholder="${escH(placeholder)}" required><button class="btn dark" type="submit">Send</button></form></div></div>`;
    const log = q1("[data-log]", view);
    q1("[data-ask]", view).addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.currentTarget, text = f.m.value.trim(); if (!text) return;
      log.insertAdjacentHTML("beforeend", `<div class="cr-msg me">${escH(text)}</div>`); f.m.value = "";
      const wait = document.createElement("div"); wait.className = "cr-msg"; wait.textContent = "…"; log.append(wait); log.scrollTop = log.scrollHeight;
      try { wait.innerHTML = await send(text); } catch (err) { wait.textContent = err.message; }
      log.scrollTop = log.scrollHeight;
    });
  }
  function renderFarmer(view) {
    chatUI(view, { kicker: "For farmers", title: "Ask about <em class=\"s\">your crop</em>.", placeholder: "I grow olives near Jaén. Will prices fall?",
      lede: "Tell us what you grow and where. The farmer assistant answers with the live market's probability, the agents' reasoning and how confident they are. The same assistant answers on WhatsApp.",
      send: async (text) => {
        const r = await call("/farmer/chat", { message: text });
        return `${escH(r.reply).replace(/\n/g, "<br>")}${r.probability != null ? `<div class="mono cr-meta">${escH(r.crop || "")}${r.region ? " · " + escH(r.region) : ""} · ${Math.round(r.probability * 100)}% · confidence ${escH(r.confidence || "")}</div>` : ""}`;
      } });
  }
  const CROPPY_LINKS = { "/harvest": "/", "/harvest#models": "/leaderboard", "/harvest#markets": "/markets", "/harvest/play": "/markets", "/harvest/play#rank": "/leaderboard", "/harvest/board": "/live" };
  function croppy() {
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "cr-croppy-btn"; btn.textContent = "Ask Croppy";
    document.body.append(btn);
    const panel = document.createElement("div");
    panel.className = "cr-croppy"; panel.hidden = true;
    document.body.append(panel);
    const history = [];
    chatUI(panel, { kicker: "Croppy", title: "Your guide to The Crophecy", lede: "Ask what a market means, who is winning or how to bet.", placeholder: "Who is winning right now?",
      send: async (text) => {
        const r = await call("/croppy", { message: text, history: history.slice(-6), page: location.pathname });
        history.push({ role: "user", text }, { role: "croppy", text: r.reply });
        return `${escH(r.reply)}${(r.actions || []).length ? `<div class="cr-acts">${r.actions.map((a) => `<a href="${escH(CROPPY_LINKS[a.href] || a.href)}">${escH(a.label)}</a>`).join("")}</div>` : ""}`;
      } });
    q1(".sec-head", panel).insertAdjacentHTML("afterbegin", '<button type="button" class="cr-x-close" aria-label="Close">×</button>');
    btn.addEventListener("click", () => { panel.hidden = !panel.hidden; if (!panel.hidden) q1("input", panel).focus(); });
    q1(".cr-x-close", panel).addEventListener("click", () => { panel.hidden = true; });
  }
  function desAgentLive(ai) {
    const a = AGENTS[ai], s = AGS[ai], mkt = ((S.lb || {}).baselines || {}).market;
    const ps = MARKETS.map((m) => ({ m, p: POS[m.id][ai] })).sort((x, y) => y.p.stake - x.p.stake), tot = ps.reduce((t, x) => t + x.p.stake, 0), ys = ps.filter((x) => x.p.side === "YES").reduce((t, x) => t + x.p.stake, 0);
    des(`DES · ${CODES[ai]} · ${a.n}`, `<h3>${escH(a.n)}</h3><p>${escH(a.style)}</p>
      <div class="kv"><div><span>HOLDOUT BRIER</span><b>${a.b.toFixed(3)}</b></div><div><span>RANK</span><b>${ai + 1} / ${AGENTS.length}</b></div><div><span>P&amp;L LIVE</span><b class="${s.pnl >= 0 ? "t-up" : "t-dn"}">${s.pnl >= 0 ? "+" : "−"}${money(Math.abs(s.pnl))}</b></div><div><span>BETS</span><b>${s.bets}</b></div><div><span>YES SHARE</span><b>${tot ? Math.round(ys / tot * 100) : 0}%</b></div><div><span>STAKED</span><b>${money(tot)}</b></div><div><span>VS MARKET</span><b class="${mkt != null && a.b < mkt ? "t-up" : "t-dn"}">${mkt == null ? "–" : (a.b < mkt ? "BEATS " : "TRAILS ") + mkt.toFixed(3)}</b></div><div><span>MODELS</span><b>${a.nModels}</b></div></div>
      <div class="tt-wrap"><table class="tt"><thead><tr><th>TKR</th><th>MARKET</th><th>SIDE</th><th class="r">EST</th><th class="r">MKT</th><th class="r">STAKE</th></tr></thead><tbody>${ps.slice(0, 8).map((x) => `<tr data-act="mkt:${escH(x.m.id)}"><td class="tk">${x.m.tk}</td><td class="q" style="max-width:260px">${escH(x.m.q)}</td><td class="${x.p.side === "YES" ? "t-up" : "t-dn"}">${x.p.side}</td><td class="r">${x.p.est}</td><td class="r">${x.m.last.toFixed(1)}</td><td class="r">${money(x.p.stake)}</td></tr>`).join("")}</tbody></table></div>
      <div class="des-act"><button type="button" class="p" data-act="live:${ai}">WATCH ON LIVE GLOBE &lt;GO&gt;</button><button type="button" data-cr-desf="${ai}:0">FOLLOW</button><button type="button" data-cr-desf="${ai}:1">COPY BETS</button></div><p data-cr-fmsg style="color:var(--t-mu)"></p>`);
  }
  function wireGlobalClicks() {
    document.addEventListener("click", (e) => {
      const f = e.target.closest("[data-cr-f]");
      if (f) { const box = f.closest("[data-cr-follow]"); follow(+box.dataset.crFollow, f.dataset.crF === "copy", box.parentElement.querySelector("[data-cr-fmsg]")); return; }
      const d = e.target.closest("[data-cr-desf]");
      if (d) { e.stopPropagation(); const [ai, c] = d.dataset.crDesf.split(":"); follow(+ai, c === "1", q1("[data-des-b] [data-cr-fmsg]")); }
    }, true);
    const nav = q1(".topbar nav.links");
    if (nav) nav.insertAdjacentHTML("beforeend", '<a href="#theory" data-nav="theory">Theory</a><a href="#farmer" data-nav="farmer" class="opt">Farm chat</a>');
  }
  function styles() {
    const css = `.cr-preview{position:sticky;top:0;z-index:80;background:#fff4d6;color:#5c4400;font:500 13px var(--f-body);padding:8px 16px;text-align:center}
.cr-page{padding-top:40px;padding-bottom:80px;max-width:820px}.cr-page .sec-head{margin-bottom:24px}.cr-page .sec-head p{margin-top:10px;color:var(--muted)}
.cr-form{display:grid;gap:14px}.cr-form label{display:grid;gap:6px;font-weight:500}.cr-form input,.cr-form textarea,.cr-chat input,.cr-card input{font:inherit;padding:10px 12px;border:1px solid var(--line-strong);border-radius:10px;background:#fbfcf9;width:100%}
.cr-form .btn{justify-self:start}.cr-out{margin-top:24px}.cr-out .panel{padding:18px;border:1px solid var(--line);border-radius:14px}.cr-out h3{margin-bottom:6px}
.cr-err{color:var(--no);margin-top:8px}.cr-chat{border:1px solid var(--line);border-radius:16px;overflow:hidden;background:#fbfcf9}.cr-log{min-height:220px;max-height:52vh;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:10px;background:var(--soft)}
.cr-msg{align-self:flex-start;max-width:85%;background:#fbfcf9;border:1px solid var(--line);border-radius:14px;padding:10px 12px;white-space:normal;overflow-wrap:anywhere}.cr-msg.me{align-self:flex-end;background:var(--violet-soft);border-color:transparent}
.cr-meta{margin-top:6px;color:var(--muted)}.cr-acts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.cr-acts a{font:500 12px var(--f-mono);color:var(--violet-ink);background:var(--violet-soft);padding:4px 8px;border-radius:999px;text-decoration:none}
.cr-chat form{display:flex;gap:8px;padding:10px;border-top:1px solid var(--line)}.cr-chat form .btn{flex:none}
.cr-croppy-btn{position:fixed;right:16px;bottom:16px;z-index:70;border:0;border-radius:999px;padding:12px 18px;background:var(--ink);color:#fff;font:600 14px var(--f-body);box-shadow:0 10px 30px -10px rgba(30,40,20,.5);cursor:pointer}
.cr-croppy{position:fixed;right:16px;bottom:72px;z-index:71;width:min(380px,calc(100vw - 32px));background:#fbfcf9;border:1px solid var(--line);border-radius:18px;box-shadow:0 24px 60px -24px rgba(30,40,20,.45)}
.cr-croppy .cr-page{padding:16px;max-width:none}.cr-croppy .sec-head{margin-bottom:10px;position:relative}.cr-croppy h2{font-size:18px}.cr-croppy .sec-head p{font-size:13px;margin-top:4px}.cr-croppy .cr-log{max-height:40vh;min-height:140px}
.cr-x-close{position:absolute;right:-4px;top:-6px;border:0;background:none;font-size:22px;cursor:pointer;color:var(--muted)}
.cr-modal{position:fixed;inset:0;z-index:90;background:rgba(20,25,15,.45);display:grid;place-items:center;padding:16px}.cr-card{background:#fbfcf9;border-radius:16px;padding:22px;width:min(400px,100%)}.cr-card p{color:var(--muted);margin:6px 0 14px}.cr-card form{display:flex;gap:8px}
@media (max-width:760px){.cr-croppy-btn{bottom:84px;padding:9px 14px;font-size:13px}.cr-croppy{bottom:130px}}
.cr-follow{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}.cr-bal{color:var(--muted);margin:4px 0 10px}`;
    const st = document.createElement("style"); st.textContent = css; document.head.append(st);
  }

  // ---------------------------------------------------------------- boot
  async function boot(start) {
    styles();
    if (!window.CROPHECY_LIVE) { preview("This copy of the design changed on main and is not wired yet."); start(); return; }
    let body;
    try { body = await call("/site"); } catch (e) {
      preview("The live market is unreachable right now."); start(); setInterval(tick, 1000); return;
    }
    apply(body);
    install();
    wireGlobalClicks();
    paintChrome();
    if (!window.__croppyLoaded) croppy(); // the site-wide Croppy (/harvest/static/croppy.js) replaces this one
    go(routeFromPath(), { keep: true });
    setTimeout(poll, POLL_MS);
  }
  window.crophecyBoot = (start) => {
    const run = () => boot(start).catch((e) => {
      console.error("crophecy boot", e); preview("The live data could not be loaded.");
      try { start(); } catch (err) { console.error(err); }
    });
    if (document.body) run(); else document.addEventListener("DOMContentLoaded", run);
  };
})();
