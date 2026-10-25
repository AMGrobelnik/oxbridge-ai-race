"use strict";
/* The Crophecy, simple UI: one shared script for /app, /app/markets, /app/market/{qid}, /app/live, /app/theory, /app/leaderboard.
   Uses only the existing /harvest/api/* endpoints. */
(() => {
  const KEY = "harvest-player"; // shared with /harvest/play
  const API = "/harvest/api";
  const CROPPY_SCRIPTS = ["/harvest/static/croppy.js"];
  const GROUPS = {
    "Grains": ["wheat", "durum wheat", "maize", "feed maize", "rice"],
    "Oils": ["olive oil", "sunflower oil", "sunflower"],
    "Soft crops": ["coffee", "cocoa", "sugar beet"],
    "Land and water": ["farmland", "water", "all crops"],
  };
  const SAMPLE_THEORY = "Heat waves and droughts hit southern Europe harder every year, and markets are slow to price it in. "
    + "When stocks are thin, a single bad harvest pushes prices up fast, and prices that start rising keep rising for a while. "
    + "Rich importing countries buy farmland and stockpile food, which makes the squeeze worse.";

  // ------------------------------------------------------------ helpers
  function h(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  async function api(path, body) {
    const init = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(API + path, init);
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (!r.ok) { const e = new Error(data.error || `Something went wrong (HTTP ${r.status})`); e.status = r.status; throw e; }
    return data;
  }
  const pct = (p) => (p == null ? "–" : `${Math.round(p * 100)}%`);
  const b3 = (x) => (x == null ? "–" : Number(x).toFixed(3));
  const ordinal = (n) => { const s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); };
  function ago(iso) {
    const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
    if (!iso || Number.isNaN(s)) return "";
    if (s < 10) return "just now";
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} days ago`;
  }
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch (_) { return null; } };
  const save = (v) => { try { if (v) localStorage.setItem(KEY, JSON.stringify(v)); else localStorage.removeItem(KEY); } catch (_) { /* storage blocked */ } };
  const main = () => document.getElementById("main");
  const show = (...kids) => { const m = main(); m.replaceChildren(...kids.flat().filter(Boolean)); };
  const spinner = (text) => h("p", { class: "muted" }, h("span", { class: "spin" }), text);
  const failure = (e) => h("p", { class: "err" }, e.message || String(e));

  function cropGroup(crop) {
    for (const [g, crops] of Object.entries(GROUPS)) if (crops.includes(crop)) return g;
    return "Other";
  }
  // latest live forecast per agent for a market view
  function agentViews(m) {
    const by = new Map();
    for (const f of m.agent_forecasts || []) {
      const old = by.get(f.persona_id);
      if (!old || String(f.at || "") >= String(old.at || "")) by.set(f.persona_id, f);
    }
    return [...by.values()].sort((a, b) => b.p_yes - a.p_yes);
  }
  function who(trader, names) {
    if (trader.startsWith("agent:")) return names.get(trader.slice(6)) || trader.slice(6);
    if (trader.startsWith("ext:")) return "An outside agent";
    return "A player";
  }
  function sparkline(history, current) {
    const pts = (history || []).map((x) => x[1]);
    if (current != null) pts.push(current);
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "line");
    svg.setAttribute("viewBox", "0 0 200 48");
    svg.setAttribute("preserveAspectRatio", "none");
    if (pts.length < 2) return svg;
    const lo = Math.min(...pts), hi = Math.max(...pts), span = Math.max(hi - lo, 0.05), mid = (hi + lo) / 2;
    const line = document.createElementNS(ns, "polyline");
    line.setAttribute("points", pts.map((p, i) => `${(i / (pts.length - 1)) * 200},${46 - ((p - mid) / span + 0.5) * 44}`).join(" "));
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "#2b8a3e");
    line.setAttribute("stroke-width", "2.5");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(line);
    return svg;
  }

  // ------------------------------------------------------------ chrome: nav, credits badge, Croppy
  const NAV = [["/app", "Home", "home"], ["/app/markets", "Markets", "markets"], ["/app/live", "Live", "live"],
    ["/app/theory", "Theory", "theory"], ["/app/leaderboard", "Leaderboard", "board"]];
  function badgeText() {
    const me = load();
    return me && me.token ? `${me.nickname || "You"} · ${Math.round(me.credits ?? 1000)} play credits` : "Free play credits · no real money";
  }
  function renderChrome() {
    const page = document.body.dataset.page;
    const top = document.getElementById("top");
    top.replaceChildren(h("div", { class: "bar" },
      h("a", { class: "brand", href: "/app" }, "🌾 The Crophecy"),
      h("nav", {}, NAV.filter((n) => n[2] !== "home").map(([href, label, id]) => h("a", { href, class: (id === page || (id === "markets" && page === "market")) ? "on" : "" }, label))),
      h("span", { class: "badge", id: "badge", title: "Play credits only. They have no monetary value and cannot be bought or cashed out." }, badgeText())));
  }
  async function refreshBalance() {
    const me = load();
    if (!me || !me.token) return;
    try {
      const info = await api(`/me?token=${encodeURIComponent(me.token)}`);
      me.credits = info.balance;
      save(me);
    } catch (e) { if (e.status === 401) save(null); }
    document.getElementById("badge").textContent = badgeText();
  }
  async function loadCroppy() {
    for (const src of CROPPY_SCRIPTS) {
      try {
        const r = await fetch(src, { method: "HEAD" });
        if (r.ok) { document.body.append(h("script", { src, defer: true })); return; }
      } catch (_) { /* no Croppy here */ }
    }
  }

  // ------------------------------------------------------------ join (nickname dialog on first use)
  function ensurePlayer() {
    const me = load();
    if (me && me.token) return Promise.resolve(me);
    return new Promise((resolve, reject) => {
      const input = h("input", { type: "text", maxlength: "24", placeholder: "e.g. Maria", "aria-label": "Your nickname", autocomplete: "off" });
      const msg = h("p", { class: "err" });
      const dlg = h("dialog", {}, h("h2", { style: "margin-top:0" }, "Pick a nickname"),
        h("p", { class: "muted" }, "You get 1,000 free play credits. No sign-up, no real money."), input, msg,
        h("div", { class: "actions" },
          h("button", { class: "btn primary", onclick: async (ev) => {
            ev.target.disabled = true;
            try {
              const j = await api("/join", { nickname: input.value || "Guest" });
              const player = { ...j, credits: j.credits };
              save(player); dlg.close(); dlg.remove(); document.getElementById("badge").textContent = badgeText(); resolve(player);
            } catch (e) { msg.textContent = e.message; ev.target.disabled = false; }
          } }, "Start playing"),
          h("button", { class: "btn", onclick: () => { dlg.close(); dlg.remove(); reject(new Error("cancelled")); } }, "Cancel")));
      document.body.append(dlg);
      dlg.showModal();
      input.focus();
    });
  }

  // ------------------------------------------------------------ pages
  function home() {
    show(h("h1", {}, "Every harvest has its prophets. We score them."),
      h("p", { class: "lead" }, "The Crophecy is a prediction game about food prices and farming under heat and drought: AI agents with rival theories bet against you, and we keep score in the open."),
      h("div", { class: "big-btns" },
        h("a", { class: "btn primary", href: "/app/markets" }, "Markets"),
        h("a", { class: "btn primary", href: "/app/live" }, "Live"),
        h("a", { class: "btn primary", href: "/app/theory" }, "Write your theory"),
        h("a", { class: "btn primary", href: "/app/leaderboard" }, "Leaderboard")),
      h("p", { class: "muted small", style: "margin-top:24px" }, "Play credits only: no deposits, no cash-out, no real money."));
  }

  async function markets() {
    show(h("h1", {}, "Markets"), spinner("Loading markets…"));
    let list;
    try { list = (await api("/markets")).markets.filter((m) => m.status === "open"); } catch (e) { show(h("h1", {}, "Markets"), failure(e)); return; }
    let group = "All", query = "";
    const groups = ["All", ...Object.keys(GROUPS), "Other"].filter((g) => g === "All" || list.some((m) => cropGroup(m.crop) === g));
    const search = h("input", { type: "search", placeholder: "Search, e.g. wheat, Spain, drought", "aria-label": "Search markets",
      oninput: (e) => { query = e.target.value.toLowerCase(); draw(); } });
    const chips = h("div", { class: "chips" });
    const rows = h("div");
    function draw() {
      chips.replaceChildren(...groups.map((g) => h("button", { class: "chip" + (g === group ? " on" : ""), onclick: () => { group = g; draw(); } }, g)));
      const hits = list.filter((m) => (group === "All" || cropGroup(m.crop) === group)
        && (!query || `${m.title} ${m.region} ${m.crop}`.toLowerCase().includes(query)));
      rows.replaceChildren(...(hits.length ? hits.map((m) => {
        const views = agentViews(m), yes = views.filter((v) => v.p_yes > 0.5).length;
        return h("a", { class: "row", href: `/app/market/${encodeURIComponent(m.qid)}` },
          h("span", { class: "q" }, m.title, h("span", { class: "sub" }, `${m.region} · ${m.crop}` + (views.length ? ` · agents: ${yes} of ${views.length} say YES` : ""))),
          h("span", { class: "pct" }, pct(m.p_yes), h("small", {}, "YES")));
      }) : [h("p", { class: "muted" }, "No markets match.")]));
    }
    draw();
    show(h("h1", {}, "Markets"), search, chips, rows);
  }

  async function marketPage() {
    const qid = decodeURIComponent(location.pathname.replace(/^\/app\/market\//, ""));
    show(spinner("Loading…"));
    let m, state;
    try { m = await api(`/markets/${encodeURIComponent(qid)}`); state = await api("/state?since=999999999"); } catch (e) { show(h("h1", {}, "Market not found"), failure(e), h("a", { class: "btn", href: "/app/markets" }, "Back to markets")); return; }
    let spend = 25;
    const priceBox = h("div"), result = h("div"), position = h("div");
    const yesBtn = h("button", { class: "btn yes", onclick: () => bet("YES") }, "YES");
    const noBtn = h("button", { class: "btn no", onclick: () => bet("NO") }, "NO");
    const amounts = h("div", { class: "amounts" });
    const drawAmounts = () => amounts.replaceChildren(...[10, 25, 50, 100].map((a) => h("button", { class: "chip" + (a === spend ? " on" : ""), onclick: () => { spend = a; drawAmounts(); } }, `${a} credits`)));
    const drawPrice = () => priceBox.replaceChildren(
      h("div", { class: "hero-pct" }, pct(m.p_yes)),
      h("div", { class: "muted" }, "chance of YES, according to the market"),
      sparkline(m.history, m.p_yes));
    async function drawPosition() {
      const me = load();
      if (!me || !me.token) { position.replaceChildren(); return; }
      try {
        const info = await api(`/me?token=${encodeURIComponent(me.token)}`);
        me.credits = info.balance; save(me); document.getElementById("badge").textContent = badgeText();
        const pos = (info.positions || []).find((p) => p.qid === qid);
        position.replaceChildren(pos ? h("div", { class: "card" }, h("b", {}, "Your position: "),
          `${Number(pos.yes).toFixed(1)} YES shares, ${Number(pos.no).toFixed(1)} NO shares. Spent ${Number(pos.spent).toFixed(0)} credits, worth about ${Number(pos.value).toFixed(0)} now.`) : "");
      } catch (e) { if (e.status === 401) { save(null); position.replaceChildren(); } }
    }
    async function bet(side) {
      result.replaceChildren();
      let me;
      try { me = await ensurePlayer(); } catch (_) { return; }
      yesBtn.disabled = noBtn.disabled = true;
      const before = m.p_yes;
      try {
        const r = await api("/bet", { token: me.token, qid, side, spend });
        m = { ...m, ...r.market, agent_forecasts: m.agent_forecasts, history: r.market.history || m.history };
        drawPrice();
        result.replaceChildren(h("div", { class: "card good" }, h("b", {}, `You bet ${spend} credits on ${side}. `),
          `The YES price moved from ${pct(before)} to ${pct(r.market.p_yes)}. You have ${Math.round(r.balance)} credits left.`));
        me.credits = r.balance; save(me); document.getElementById("badge").textContent = badgeText();
        drawPosition();
      } catch (e) {
        if (e.status === 401) { save(null); result.replaceChildren(h("p", { class: "err" }, "Your session expired. Press the button again to pick a nickname.")); } else result.replaceChildren(failure(e));
      } finally { yesBtn.disabled = noBtn.disabled = m.status !== "open"; }
    }
    drawPrice(); drawAmounts();
    const views = agentViews(m);
    const names = new Map(state.personas.map((p) => [p.id, p.name]));
    show(h("a", { href: "/app/markets", class: "muted small" }, "← All markets"),
      h("h1", {}, m.title), h("p", { class: "muted" }, `${m.region} · ${m.crop} · closes ${m.resolve_month}`),
      priceBox,
      h("h2", {}, m.status === "open" ? "Place your bet" : "This market is closed"),
      m.status === "open" ? [h("p", { class: "muted" }, "How many play credits?"), amounts, h("div", { class: "yn" }, yesBtn, noBtn)] : null,
      result, position,
      h("h2", {}, "What the agents think"),
      views.length ? views.map((v) => h("div", { class: "agent" }, h("b", {}, names.get(v.persona_id) || v.name || v.persona_id), h("span", { class: "p" }, `${pct(v.p_yes)} YES`),
        h("span", { class: "why" }, v.rationale || ""))) : h("p", { class: "muted" }, "No agent has weighed in yet."));
    yesBtn.disabled = noBtn.disabled = m.status !== "open";
    drawPosition();
  }

  async function live() {
    const feed = h("div"), news = h("div");
    show(h("h1", {}, "Live"), h("p", { class: "muted small" }, "Updates every few seconds."), h("h2", {}, "Latest bets"), feed, h("h2", {}, "News"), news);
    async function tick() {
      try {
        const s = await api("/state");
        const names = new Map(s.personas.map((p) => [p.id, p.name]));
        const titles = new Map(s.markets.map((m) => [m.qid, m.title]));
        feed.replaceChildren(...[...s.recent_trades].reverse().slice(0, 40).map((t) => h("div", { class: "feed-item" },
          h("span", { class: "tag " + t.side.toLowerCase() }, t.side), h("b", {}, who(t.trader, names)), ` bet ${Math.round(t.spend)} credits `,
          h("a", { href: `/app/market/${encodeURIComponent(t.qid)}` }, titles.get(t.qid) || t.qid), h("span", { class: "muted small" }, ` · ${ago(t.at)}`),
          t.rationale ? h("div", { class: "muted", style: "font-size:15px;margin-top:4px" }, t.rationale) : null)));
      } catch (e) { feed.replaceChildren(failure(e)); }
      try { news.replaceChildren(...newsItems(await api("/news"))); } catch (e) { news.replaceChildren(h("p", { class: "muted" }, e.status === 404 ? "News is coming soon." : e.message)); }
    }
    tick();
    const timer = setInterval(() => { if (!document.hidden) tick(); }, 5000);
    window.addEventListener("pagehide", () => clearInterval(timer));
  }
  function newsItems(data) {
    const arr = Array.isArray(data) ? data : data.items || data.news || data.articles || data.headlines || [];
    if (!arr.length) return [h("p", { class: "muted" }, "No headlines yet.")];
    return arr.slice(0, 12).map((n) => {
      const title = typeof n === "string" ? n : n.title || n.headline || "";
      const url = typeof n === "string" ? null : n.url || n.link;
      const tags = typeof n === "string" ? [] : [].concat(n.tags || n.tag || n.topics || []).map((t) => (typeof t === "string" ? t : t.name || t.label || ""));
      return h("div", { class: "feed-item" }, tags.filter(Boolean).slice(0, 3).map((t) => h("span", { class: "tag" }, t)),
        url ? h("a", { href: url, target: "_blank", rel: "noopener" }, title) : title,
        n.published || n.at || n.date ? h("span", { class: "muted small" }, ` · ${ago(n.published || n.at || n.date)}`) : null);
    });
  }

  function theory() {
    const text = h("textarea", { maxlength: "1200", placeholder: "In a few sentences: how do climate, politics and money move food prices?", "aria-label": "Your theory" });
    const count = h("span", { class: "muted small" }, "0 / 1200");
    const out = h("div");
    const go = h("button", { class: "btn primary", style: "width:100%", onclick: run }, "Test my theory");
    text.addEventListener("input", () => { count.textContent = `${text.value.length} / 1200`; });
    async function run() {
      if (text.value.trim().length < 20) { out.replaceChildren(h("p", { class: "err" }, "Write at least a couple of sentences first.")); return; }
      go.disabled = true;
      out.replaceChildren(spinner("Testing your theory on 48 past questions with the answers hidden. This takes about 20 seconds…"));
      const me = load();
      try {
        const r = await api("/byo", { name: (me && me.nickname ? `${me.nickname}'s theory` : "My theory"), world_model: text.value, token: me && me.token ? me.token : undefined });
        const market = (r.field || []).find((f) => f.kind === "market");
        out.replaceChildren(h("div", { class: "card good" },
          h("h2", { style: "margin-top:0" }, `Your theory ranked ${ordinal(r.rank)} of ${r.of}`),
          h("p", {}, `Score (Brier) ${b3(r.brier_holdout)}` + (market ? ` vs the market's ${b3(market.brier)}` : "") + ". Lower is better; 0.25 is a coin flip."),
          r.cached ? h("p", { class: "muted small" }, "This exact theory was tested before, so this is the saved result.") : null,
          h("a", { class: "btn", href: "/app/leaderboard" }, "See the leaderboard")));
        if ((r.field || []).length) out.append(h("h2", {}, "How everyone scored"), ...r.field.map((f, i) => h("div", { class: "rank" },
          h("span", { class: "n" }, i + 1), h("span", { class: "nm" }, f.name), h("span", { class: "sc" }, b3(f.brier)))));
      } catch (e) { out.replaceChildren(failure(e)); }
      go.disabled = false;
    }
    show(h("h1", {}, "Write your theory"),
      h("p", { class: "lead" }, "Describe how you think the world moves food prices. We hand it to an AI, ask it 48 past questions with the answers hidden, and score it against the agents and the market."),
      text, h("div", { style: "display:flex;justify-content:space-between;margin:6px 0 14px" }, h("a", { href: "#", onclick: (e) => { e.preventDefault(); text.value = SAMPLE_THEORY; text.dispatchEvent(new Event("input")); } }, "Use an example"), count),
      go, out);
  }

  async function leaderboard() {
    show(h("h1", {}, "Leaderboard"), spinner("Loading…"));
    let s;
    try { s = await api("/state"); } catch (e) { show(h("h1", {}, "Leaderboard"), failure(e)); return; }
    const lb = s.leaderboard, models = [...lb.world_models].sort((a, b) => a.brier_holdout - b.brier_holdout);
    const verifyOut = h("div");
    const mk = (label, cls, tamper) => h("button", { class: "btn " + cls, onclick: async (ev) => {
      ev.target.disabled = true;
      verifyOut.replaceChildren(spinner("Checking…"));
      try { verifyOut.replaceChildren(explain(tamper ? await api("/verify", { tamper: true }) : await api("/verify"), tamper)); } catch (e) { verifyOut.replaceChildren(failure(e)); }
      ev.target.disabled = false;
    } }, label);
    show(h("h1", {}, "Leaderboard"),
      h("p", { class: "muted" }, `Scored on ${lb.interval.n_questions} past questions with the answers hidden. Lower score is better. The grey range shows how sure we are: when ranges overlap, it is too close to call.`),
      h("div", { class: "rank" }, h("span", { class: "n" }, "–"), h("span", { class: "nm" }, "The market"), h("span", { class: "sc" }, b3(lb.baselines.market))),
      models.map((m, i) => h("div", { class: "rank" }, h("span", { class: "n" }, i + 1),
        h("span", { class: "nm" }, m.name, m.origin && m.origin !== "house" ? h("span", { class: "tag", style: "margin-left:8px" }, m.origin.includes("human") ? "player" : m.origin) : null),
        h("span", { class: "sc" }, b3(m.brier_holdout), h("small", {}, `${b3(m.brier_holdout_lo)} to ${b3(m.brier_holdout_hi)}`)))),
      lb.insight ? h("p", { class: "card" }, lb.insight) : null,
      h("h2", {}, "Can we trust it?"),
      h("p", { class: "muted" }, "Every forecast was locked into a public chain of fingerprints before the answers were known."),
      h("div", { class: "actions" }, mk("Verify", "primary", false), mk("Try to tamper", "", true)),
      verifyOut);
  }
  function explain(v, tamper) {
    const n = v.ledger && v.ledger.count;
    if (!tamper) {
      const ok = v.chain_ok && v.commitments_ok !== false && v.windows_ok !== false;
      return h("div", { class: "card " + (ok ? "good" : "bad") }, ok
        ? `✅ All good. ${n} ledger entries are chained together and every sealed forecast still matches its fingerprint. Nothing was changed after the fact.`
        : "❌ Something does not match. At least one record changed after it was sealed.");
    }
    const caught = v.commitments_ok === false || v.windows_ok === false || v.chain_ok === false;
    const note = (v.details || []).find((d) => d.check === "tamper");
    return h("div", { class: "card " + (caught ? "good" : "bad") }, caught
      ? `✅ Caught. We secretly changed one forecast${note ? ` (${note.note})` : ""} and the fingerprint check failed straight away. That is how you know the real numbers are untouched.`
      : "❌ The change was not noticed. That should never happen; please tell the organisers.");
  }

  // ------------------------------------------------------------ boot
  const PAGES = { home, markets, market: marketPage, live, theory, board: leaderboard };
  renderChrome();
  (PAGES[document.body.dataset.page] || home)();
  refreshBalance();
  loadCroppy();
})();
