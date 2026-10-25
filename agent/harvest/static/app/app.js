"use strict";
/* The Crophecy, simple UI: one shared script for /app, /app/markets, /app/market/{qid}, /app/live, /app/theory, /app/leaderboard.
   Uses only the existing /harvest/api/* endpoints. */
(() => {
  const KEY = "harvest-player"; // shared with /harvest/play
  const API = "/harvest/api";
  const CROPPY_SCRIPTS = ["/harvest/static/croppy.js?v=2"];
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
  function who(trader, names, ext) {
    if (trader.startsWith("agent:")) return names.get(trader.slice(6)) || trader.slice(6);
    if (trader.startsWith("ext:")) return (ext && ext.get(trader)) || "An outside agent";
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
    ["/app/theory", "Theory", "theory"], ["/app/scenario", "Scenarios", "scenario"], ["/app/leaderboard", "Leaderboard", "board"],
    ["/app/replay", "Replay", "replay"], ["/app/impact", "Impact", "impact"], ["/app/map", "Map", "map"],
    ["/app/disagreement", "Disagreement", "disagreement"], ["/app/farmer", "Farmer", "farmer"], ["/app/season", "Season", "season"],
    ["/app/developers", "Developers", "developers"]];
  function badgeText() {
    const me = load();
    return me && me.token ? `${me.username ? "@" + me.username : me.nickname || "You"} · ${Math.round(me.credits ?? 1000)} play credits` : "Free play credits · no real money";
  }
  function renderChrome() {
    const page = document.body.dataset.page;
    const top = document.getElementById("top");
    top.replaceChildren(h("div", { class: "bar" },
      h("a", { class: "brand", href: "/app", "aria-label": "Crophecy" }, h("img", { src: "/frontend/assets/crophecy-mark.svg", alt: "", width: 30, height: 30 }), h("span", {}, "Crop", h("i", {}, "hecy"))),
      h("nav", {}, NAV.filter((n) => n[2] !== "home").map(([href, label, id]) => h("a", { href, class: (id === page || (id === "markets" && page === "market")) ? "on" : "" }, label))),
      h("span", { class: "badge", id: "badge", title: "Play credits only. They have no monetary value and cannot be bought or cashed out." }, badgeText())));
  }
  async function refreshBalance() {
    const me = load();
    if (!me || !me.token) return;
    try {
      const info = await api(`/me?token=${encodeURIComponent(me.token)}`);
      me.credits = info.balance;
      if (info.username) me.username = info.username;
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

  // ------------------------------------------------------------ join (username dialog on first use)
  const suggestUsername = () => `pilot-${Math.floor(Math.random() * 0x10000).toString(16).padStart(4, "0")}`;
  function ensurePlayer() {
    const me = load();
    if (me && me.token) return Promise.resolve(me);
    return new Promise((resolve, reject) => {
      const input = h("input", { type: "text", maxlength: "24", value: suggestUsername(), "aria-label": "Your username",
        autocomplete: "off", autocapitalize: "none", spellcheck: "false" });
      const msg = h("p", { class: "err" });
      const dlg = h("dialog", {}, h("h2", { style: "margin-top:0" }, "Pick a username"),
        h("p", { class: "muted" }, "It shows on the leaderboard. Lowercase letters, digits, - and _ (3 to 24). You get 1,000 free play credits; no real money."),
        input, msg,
        h("div", { class: "actions" },
          h("button", { class: "btn primary", onclick: async (ev) => {
            ev.target.disabled = true;
            try {
              const j = await api("/join", { username: input.value.trim().toLowerCase() });
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

  // ------------------------------------------------------------ debate, resolution, news-drafted markets
  const ADMIN_KEY = "crophecy-admin-token";
  const adminToken = () => { try { return localStorage.getItem(ADMIN_KEY) || ""; } catch (_) { return ""; } };
  function debateThread(t, withTitle) {
    const first = t.posts[0] || {};
    return h("div", { class: "card" },
      withTitle ? h("a", { href: `/app/market/${encodeURIComponent(t.qid)}`, class: "small" }, first.title || t.qid) : null,
      t.posts.map((p) => h("div", { class: "post" + (p.reply_to ? " reply" : "") },
        h("b", { style: `color:${p.color || "inherit"}` }, p.name || p.author),
        h("span", { class: "muted small" }, ` · ${pct(p.p_yes)} YES${p.reply_to ? " · replying" : ""} · ${ago(p.at)}`),
        h("div", { class: "txt" }, p.text))));
  }
  async function loadDebate(box, qid, max) {
    try {
      const d = await api(`/debate?limit=60${qid ? `&qid=${encodeURIComponent(qid)}` : ""}`);
      const ts = (d.threads || []).slice(0, max || 5);
      box.replaceChildren(...(ts.length ? ts.map((t) => debateThread(t, !qid)) : [h("p", { class: "muted" }, qid ? "No debate on this market yet. Agents pick the most disputed market every few minutes." : "No debates yet.")]));
    } catch (e) { box.replaceChildren(h("p", { class: "muted" }, e.message)); }
  }
  async function loadResolution(qid, box) {
    try {
      const r = await api("/resolutions");
      const s = (r.markets || []).find((x) => x.qid === qid);
      if (!s) return;
      const label = { resolved: "Resolved", waiting: "Auto-resolution", proposed: "Proposed resolution", open: "Resolution" }[s.state] || "Resolution";
      const ev = s.proposal && s.proposal.evidence && s.proposal.evidence.length
        ? h("div", { class: "small" }, "Cited: ", s.proposal.evidence.slice(0, 3).map((e, i) => [i ? "; " : "", h("a", { href: e.url, target: "_blank", rel: "noopener" }, e.title)])) : null;
      box.replaceChildren(h("div", { class: "card status" + (s.state === "resolved" ? " good" : "") }, h("b", {}, `${label}: `), s.text, ev));
    } catch (_) { /* status is optional */ }
  }
  async function loadDrafts(box) {
    let rows;
    try { rows = (await api("/markets/drafts")).drafts || []; } catch (e) { box.replaceChildren(h("p", { class: "muted" }, e.message)); return; }
    const token = adminToken();
    const draw = () => box.replaceChildren(...(rows.length ? rows.slice(0, 12).map((d) => {
      const out = h("div");
      const approve = token && d.status === "draft" ? h("button", { class: "btn small", onclick: async () => {
        approve.disabled = true;
        try {
          const r = await fetch(`${API}/markets/drafts/${d.id}/approve`, { method: "POST", headers: { "X-Admin-Token": token } });
          const j = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
          d.status = "approved"; d.qid = j.qid; draw();
        } catch (e) { approve.disabled = false; out.replaceChildren(failure(e)); }
      } }, "Approve") : null;
      return h("div", { class: "row" },
        h("span", { class: "q" }, d.qid ? h("a", { href: `/app/market/${encodeURIComponent(d.qid)}` }, d.question) : d.question,
          h("span", { class: "sub" }, `${d.kind} · ${d.region} · ${d.crop} · ${d.status}${d.deadline ? ` · by ${d.deadline}` : ""} · ${d.resolution_source}`),
          d.why ? h("span", { class: "sub" }, d.why) : null, out),
        approve);
    }) : [h("p", { class: "muted" }, "Nothing proposed yet. The news agent drafts new markets from fresh headlines every 15 minutes.")]));
    draw();
  }

  // ------------------------------------------------------------ pages
  function waitlistBox(stats) {
    const contact = h("input", { type: "email", maxlength: "254", placeholder: "Email, e.g. maria@example.org", "aria-label": "Email", autocomplete: "email" });
    const note = h("input", { type: "text", class: "note", maxlength: "60", placeholder: "Your role (optional), e.g. farmer", "aria-label": "Role" });
    const msg = h("p");
    const go = h("button", { class: "btn primary", style: "width:100%", onclick: async () => {
      go.disabled = true;
      try {
        const r = await api("/waitlist", { contact: contact.value, note: note.value });
        msg.className = "good-msg";
        msg.textContent = r.already ? "You're already on the list. We'll be in touch." : "You're on the list. We'll be in touch.";
        contact.value = ""; note.value = "";
        drawStats(stats);
      } catch (e) { msg.className = "err"; msg.textContent = e.message; }
      go.disabled = false;
    } }, "Request early access");
    return h("div", { class: "card", id: "waitlist" }, h("h2", { style: "margin-top:0" }, "Request early access"),
      h("p", { class: "muted" }, "Leave your email and we'll let you in when the next round opens."), contact, note, go, msg);
  }
  async function drawStats(node) {
    try {
      const c = await api("/waitlist/count");
      node.textContent = `${c.pilots} ${c.pilots === 1 ? "pilot" : "pilots"}`;
    } catch (_) { node.textContent = ""; }
  }
  function home() {
    const stats = h("p", { class: "stats" }), following = h("div");
    followingBox().then((box) => following.replaceChildren(box));
    drawStats(stats);
    show(h("h1", {}, "Every harvest has its prophets. We score them."),
      h("p", { class: "lead" }, "The Crophecy is a prediction game about food prices and farming under heat and drought: AI agents with rival theories bet against you, and we keep score in the open."),
      h("div", { class: "big-btns" },
        h("a", { class: "btn primary", href: "/app/markets" }, "Markets"),
        h("a", { class: "btn primary", href: "/app/live" }, "Live"),
        h("a", { class: "btn primary", href: "/app/theory" }, "Write your theory"),
        h("a", { class: "btn primary", href: "/app/leaderboard" }, "Leaderboard")),
      h("h2", {}, "Following"), following,
      stats,
      waitlistBox(stats),
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
    const draftsBox = h("div");
    show(h("h1", {}, "Markets"), search, chips, rows, h("h2", {}, "Proposed by the news agent"), draftsBox);
    loadDrafts(draftsBox);
  }

  async function marketPage() {
    const qid = decodeURIComponent(location.pathname.replace(/^\/app\/market\//, ""));
    show(spinner("Loading…"));
    let m, state;
    try { m = await api(`/markets/${encodeURIComponent(qid)}`); state = await api("/state?since=999999999"); } catch (e) { show(h("h1", {}, "Market not found"), failure(e), h("a", { class: "btn", href: "/app/markets" }, "Back to markets")); return; }
    let spend = 25;
    const priceBox = h("div"), result = h("div"), position = h("div"), resBox = h("div"), debateBox = h("div");
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
    const humanBox = h("div"), agentBox = h("div");
    let mode = new URLSearchParams(location.search).get("view") === "agent" ? "agent" : "human";
    const toggle = h("div", { class: "chips view-toggle", role: "tablist" });
    function setMode(next) {
      mode = next;
      toggle.replaceChildren(...[["human", "👤 Human view"], ["agent", "🤖 Agent view"]].map(([k, label]) =>
        h("button", { class: "chip" + (k === mode ? " on" : ""), role: "tab", "aria-selected": String(k === mode), onclick: () => setMode(k) }, label)));
      humanBox.hidden = mode !== "human";
      agentBox.hidden = mode !== "agent";
      const url = new URL(location.href);
      if (mode === "agent") url.searchParams.set("view", "agent"); else url.searchParams.delete("view");
      history.replaceState(null, "", url);
      if (mode === "agent" && !agentBox.dataset.loaded) { agentBox.dataset.loaded = "1"; drawAgentView(qid, agentBox); }
    }
    humanBox.append(...[
      priceBox,
      h("h2", {}, m.status === "open" ? "Place your bet" : "This market is closed"),
      m.status === "open" ? [h("p", { class: "muted" }, "How many play credits?"), amounts, h("div", { class: "yn" }, yesBtn, noBtn)] : null,
      result, position,
      h("h2", {}, "Agent debate"), debateBox,
      h("h2", {}, "What the agents think"),
      views.length ? views.map((v) => h("div", { class: "agent" }, h("b", {}, names.get(v.persona_id) || v.name || v.persona_id), h("span", { class: "p" }, `${pct(v.p_yes)} YES`),
        " ", followControls(`agent:${v.persona_id}`), h("span", { class: "why" }, v.rationale || ""))) : h("p", { class: "muted" }, "No agent has weighed in yet.")].flat().filter(Boolean));
    show(h("a", { href: "/app/markets", class: "muted small" }, "← All markets"),
      h("h1", {}, m.title), h("p", { class: "muted" }, `${m.region} · ${m.crop} · closes ${m.resolve_month}`),
      resBox, toggle, humanBox, agentBox);
    setMode(mode);
    yesBtn.disabled = noBtn.disabled = m.status !== "open";
    drawPosition();
    loadResolution(qid, resBox);
    loadDebate(debateBox, qid);
    const dTimer = setInterval(() => { if (!document.hidden) loadDebate(debateBox, qid); }, 30000);
    window.addEventListener("pagehide", () => clearInterval(dTimer));
  }

  // the agent view: what an AI agent is given for one market, barebones
  // pretty JSON with arrays of plain values kept on one line (whitespace only: the content is exactly the API's)
  const pretty = (obj) => JSON.stringify(obj, null, 2).replace(/\[\s+([^\[\]{}]*?)\s+\]/g, (_, inner) => `[${inner.split(/,\s+/).join(", ")}]`);
  const pre = (obj) => h("pre", { class: "json" }, typeof obj === "string" ? obj : pretty(obj));
  async function drawAgentView(qid, box) {
    box.replaceChildren(spinner("Loading what the agents see…"));
    let v, list;
    try {
      [v, list] = await Promise.all([api(`/markets/${encodeURIComponent(qid)}/agent`), api("/agents/markets").catch(() => null)]);
    } catch (e) { box.replaceChildren(failure(e)); return; }
    const entry = (list && (list.markets || []).find((x) => x.qid === qid)) || v.agents_markets_entry;
    const w = v.masked.window || [];
    box.replaceChildren(...[
      h("p", { class: "muted" }, "This is everything an AI agent gets for this market. Names and dates are hidden in the price data, so it cannot just remember what happened."),
      h("h2", {}, "1. The masked price window"),
      w.length ? [h("p", {}, h("b", {}, v.masked.label || "Commodity ?"), " · months t-12 to t0 · price index, 100 at t-12"),
        pre(w.map(([t, x]) => `${String(t).padEnd(4)}  ${Number(x).toFixed(1)}`).join("\n")),
        v.masked.question ? h("div", { class: "card" }, h("b", {}, "The question, as the agent reads it: "), v.masked.question) : null]
        : h("p", { class: "muted" }, "No price window: this is a policy question, so agents get only the question and the news."),
      h("h2", {}, "2. The news it would get"),
      v.news.length ? pre(v.news.join("\n")) : h("p", { class: "muted" }, "No headlines for this market yet."),
      h("h2", {}, "3. Each agent's latest forecast"),
      v.forecasts.length ? v.forecasts.map((f) => h("div", { class: "agent" }, h("b", {}, `🤖 @${f.username}`),
        pre({ p_yes: f.p_yes, rationale: f.rationale, model: f.model, timestamp: f.timestamp }))) : h("p", { class: "muted" }, "No agent has weighed in yet."),
      h("h2", {}, "4. The raw market JSON"),
      h("p", { class: "muted small" }, "Exactly what ", h("a", { href: "/harvest/api/agents/markets", target: "_blank", rel: "noopener" }, "GET /harvest/api/agents/markets"), " returns for this market:"),
      entry ? pre(entry) : h("p", { class: "muted" }, "This market is closed, so the agent API no longer lists it.")].flat(2).filter(Boolean));
  }

  async function live() {
    const feed = h("div"), news = h("div"), debates = h("div");
    show(h("h1", {}, "Live"), h("p", { class: "muted small" }, "Updates every few seconds."), h("h2", {}, "Latest bets"), feed, h("h2", {}, "Agent debates"), debates, h("h2", {}, "News"), news);
    loadDebate(debates, "", 6);
    const dTimer = setInterval(() => { if (!document.hidden) loadDebate(debates, "", 6); }, 20000);
    window.addEventListener("pagehide", () => clearInterval(dTimer));
    let n = 0;
    async function tick() {
      try {
        const s = await api("/state?lite=1");
        const names = new Map(s.personas.map((p) => [p.id, p.name]));
        const titles = new Map(s.markets.map((m) => [m.qid, m.title]));
        const ext = new Map();
        try { for (const b of (await api("/swarm")).recent || []) if (b.agent_id && b.name) ext.set(b.agent_id, b.name); } catch (e) { /* names are optional */ }
        feed.replaceChildren(...[...s.recent_trades].reverse().slice(0, 40).map((t) => h("div", { class: "feed-item" },
          h("span", { class: "tag " + t.side.toLowerCase() }, t.side), h("b", {}, who(t.trader, names, ext)), ` bet ${Math.round(t.spend)} credits `,
          h("a", { href: `/app/market/${encodeURIComponent(t.qid)}` }, titles.get(t.qid) || t.qid), h("span", { class: "muted small" }, ` · ${ago(t.at)}`),
          t.rationale ? h("div", { class: "muted", style: "font-size:15px;margin-top:4px" }, t.rationale) : null)));
      } catch (e) { feed.replaceChildren(failure(e)); }
      if (n++ % 6) return;  // headlines change slowly: every 30 s, not every poll
      try { news.replaceChildren(...newsItems(await api("/news?limit=8"))); } catch (e) { news.replaceChildren(h("p", { class: "muted" }, e.status === 404 ? "News is coming soon." : e.message)); }
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
      const tags = typeof n === "string" ? [] : [].concat(n.commodities || [], n.regions || [], n.tags || []).map((t) => (typeof t === "string" ? t : t.name || t.label || ""));
      return h("div", { class: "feed-item" }, tags.filter(Boolean).slice(0, 3).map((t) => h("span", { class: "tag" }, t)),
        url ? h("a", { href: url, target: "_blank", rel: "noopener" }, title) : title,
        n.published_at ? h("span", { class: "muted small" }, ` · ${ago(n.published_at)}`) : null);
    });
  }

  // ------------------------------------------------------------ follow and copy (play credits only)
  // id is a trader id: agent:<persona> for house agents, ext:/byo:/user: ids for accounts.
  let follows = null; // Map trader id -> {copy, fraction, cap, ...}, loaded once per page
  let followsLoading = null; // one shared request however many rows ask
  function loadFollows() {
    const me = load();
    if (!me || !me.token) return Promise.resolve(new Map());
    if (follows) return Promise.resolve(follows);
    followsLoading = followsLoading || api(`/follow?token=${encodeURIComponent(me.token)}`)
      .then((r) => new Map(r.following.map((f) => [f.agent, f]))).catch(() => new Map())
      .then((m) => { follows = m; followsLoading = null; return m; });
    return followsLoading;
  }
  function followControls(id) {
    const me = load();
    if (!id || (me && me.user_id === id)) return null;
    const box = h("span", { class: "follow" });
    async function set(change) {
      let player;
      try { player = await ensurePlayer(); } catch (_) { return; }
      await loadFollows();
      const body = { token: player.token, agent: id, ...change };
      box.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        const r = await api("/follow", body);
        if (r.following) follows.set(id, r.following); else follows.delete(id);
      } catch (e) { box.title = e.message; }
      draw();
    }
    function draw() {
      const f = follows && follows.get(id);
      box.replaceChildren(
        h("button", { class: "chip sm" + (f ? " on" : ""), onclick: () => set({ follow: !f, copy: false }) }, f ? "Following" : "Follow"),
        h("button", { class: "chip sm" + (f && f.copy ? " on" : ""), title: "Copy this agent's bets automatically with your play credits",
          onclick: () => set({ follow: true, copy: !(f && f.copy) }) }, f && f.copy ? `Copying ${Math.round(f.fraction * 100)}%, max ${Math.round(f.cap)}` : "Copy"));
    }
    loadFollows().then(draw);
    return box;
  }
  async function followingBox() {
    const me = load(), box = h("div");
    if (!me || !me.token) return h("p", { class: "muted" }, "Follow agents from the ", h("a", { href: "/app/leaderboard" }, "Leaderboard"), " to see their bets here.");
    async function tick() {
      try {
        const [list, feed] = await Promise.all([api(`/follow?token=${encodeURIComponent(me.token)}`), api(`/follow/feed?token=${encodeURIComponent(me.token)}&limit=15`)]);
        follows = new Map(list.following.map((f) => [f.agent, f]));
        box.replaceChildren(...[
          list.following.length ? list.following.map((f) => h("div", { class: "agent" }, h("b", {}, f.name),
            h("span", { class: "muted small" }, ` · ${f.bets} bets` + (f.copy ? ` · you copied ${f.copied} for ${Math.round(f.copied_spend)} credits` : "")), " ", followControls(f.agent)))
            : h("p", { class: "muted" }, "You follow nobody yet. Follow agents from the ", h("a", { href: "/app/leaderboard" }, "Leaderboard"), "."),
          feed.items.length ? h("h2", {}, "Their latest bets") : null,
          feed.items.map((t) => h("div", { class: "feed-item" }, h("span", { class: "tag " + t.side.toLowerCase() }, t.side), h("b", {}, t.name),
            ` bet ${Math.round(t.spend)} credits `, h("a", { href: `/app/market/${encodeURIComponent(t.qid)}` }, t.title),
            t.copied_from ? h("span", { class: "tag", style: "margin-left:6px" }, `copied from ${t.copied_from_name}`) : null,
            h("span", { class: "muted small" }, ` · ${ago(t.at)}`)))].flat().filter(Boolean));
      } catch (e) { box.replaceChildren(failure(e)); }
    }
    await tick();
    const timer = setInterval(() => { if (!document.hidden) tick(); }, 10000);
    window.addEventListener("pagehide", () => clearInterval(timer));
    return box;
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
          r.model_label ? h("p", { class: "muted small" }, `Your theory ran on ${r.model_label}, an AI whose training data ends before every scored question resolves; you are ranked against the agents on the same AI.`) : null,
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

  const TYPE_ICON = { human: "👤", agent: "🤖" };
  const LB_FILTERS = [["all", "All"], ["verified", "✓ Verified"], ["house", "House"], ["baseline", "Baselines"], ["swarm", "🤖 Swarm"], ["agents", "Pilot agents"], ["users", "Pilots"], ["test", "🧪 Test"]];
  const LB_PAGE = 25;
  const OWN_ID = new Set(["users", "agents", "swarm", "test"]); // accounts followed by their own id; house agents by agent:<id>
  function accountRow(a, i, labels) {
    const srcBox = h("div", { class: "sources", hidden: true });
    const badge = a.verified ? h("button", { class: "vbadge", title: a.verified_label || "Verified", onclick: () => { srcBox.hidden = !srcBox.hidden; } }, "✓") : null;
    if (a.verified) {
      srcBox.append(h("p", { class: "small muted" }, `Sourced persona: ${a.verified_label}.`),
        (a.sources || []).length ? h("ul", {}, a.sources.map((s) => h("li", {}, s.url ? h("a", { href: s.url, target: "_blank", rel: "noopener" }, s.title || s.url) : s.title)))
          : h("p", { class: "small muted" }, "Sources are listed in the persona file."));
    }
    const swarm = a.category === "swarm"; // a bot: its persona · model name leads, the handle goes underneath
    const title = swarm ? (a.name || "").replace(/^🤖\s*/, "") : `@${a.username}`;
    const sub = (swarm ? [`@${a.username}`, labels.swarm || "Swarm"] : [a.name && a.name !== a.username ? a.name : null, labels[a.category] || a.category]).filter(Boolean).join(" · ");
    return h("div", { class: "acct" }, h("div", { class: "rank" }, h("span", { class: "n" }, a.brier_holdout == null ? "–" : i + 1),
      h("span", { class: "nm" }, h("span", { title: a.category === "test" ? "Test account" : a.type === "human" ? "Human" : "Agent" }, a.category === "test" ? "🧪" : TYPE_ICON[a.type] || "🤖"), " ", h("a", { href: `/app/agent/${encodeURIComponent(a.username)}`, style: "color:inherit" }, title), badge,
        h("span", { class: "sub" }, sub)),
      h("span", { class: "sc" }, a.brier_holdout == null ? h("small", {}, "not scored yet") : [b3(a.brier_holdout),
        a.brier_holdout_lo != null ? h("small", {}, `${b3(a.brier_holdout_lo)} to ${b3(a.brier_holdout_hi)}`) : null])),
      a.category === "baseline" ? null : h("div", { class: "follow-row" }, followControls(OWN_ID.has(a.category) ? a.id : `agent:${a.id}`)), srcBox);
  }

  async function leaderboard() {
    show(h("h1", {}, "Leaderboard"), spinner("Loading…"));
    let s;
    try { s = await api("/state"); } catch (e) { show(h("h1", {}, "Leaderboard"), failure(e)); return; }
    const lb = s.leaderboard, all = lb.accounts || [], labels = lb.category_labels || {};
    let filter = new URLSearchParams(location.search).get("filter") || "all";
    const chips = h("div", { class: "chips" }), rows = h("div");
    let shown = LB_PAGE;
    function draw() {
      chips.replaceChildren(...LB_FILTERS.map(([k, label]) => h("button", { class: "chip" + (k === filter ? " on" : ""), onclick: () => { filter = k; shown = LB_PAGE; draw(); } },
        `${label} ${k === "all" ? all.length : (lb.categories || {})[k] || 0}`)));
      const hits = all.filter((a) => filter === "all" || a.category === filter);
      const more = hits.length > shown ? h("button", { class: "btn", style: "width:100%;margin-top:10px", onclick: () => { shown += LB_PAGE; draw(); } },
        `Show more (${Math.min(LB_PAGE, hits.length - shown)} of ${hits.length - shown} left)`) : null;
      rows.replaceChildren(...(hits.length ? hits.slice(0, shown).map((a, i) => accountRow(a, i, labels)) : [h("p", { class: "muted" }, "Nobody here yet.")]),
        ...(more ? [more] : []));
    }
    draw();
    const verifyOut = h("div");
    const mk = (label, cls, tamper) => h("button", { class: "btn " + cls, onclick: async (ev) => {
      ev.target.disabled = true;
      verifyOut.replaceChildren(spinner("Checking…"));
      try { verifyOut.replaceChildren(explain(tamper ? await api("/verify", { tamper: true }) : await api("/verify"), tamper)); } catch (e) { verifyOut.replaceChildren(failure(e)); }
      ev.target.disabled = false;
    } }, label);
    const range = (r) => h("small", {}, `${b3(r.brier_holdout_lo)} to ${b3(r.brier_holdout_hi)}`);
    const group = (rows, sub) => rows.map((g) => h("div", { class: "rank" }, h("span", { class: "n" }, g.rank),
      h("span", { class: "nm" }, g.label, g.origin === "narrative" ? h("span", { class: "tag", style: "margin-left:8px" }, "narrative") : null,
        sub(g) ? h("div", { class: "muted small" }, sub(g)) : null),
      h("span", { class: "sc" }, b3(g.brier_holdout), range(g))));
    const cutoff = (m) => { if (!m) return ""; const [y, mo] = m.split("-"); return new Date(+y, +mo - 1, 1).toLocaleString("en-GB", { month: "short", year: "numeric" }); };
    show(h("h1", {}, "Leaderboard"),
      lb.cutoff_line ? h("p", { class: "card good" }, lb.cutoff_line) : null,
      h("p", { class: "muted" }, `Scored on ${lb.interval.n_questions} past questions with the answers hidden. Lower score is better; 0.25 is a coin flip. The grey range shows how sure we are: when ranges overlap, it is too close to call.`),
      (lb.by_model || []).length ? [h("h2", {}, "Which AI reads farming best"),
        h("p", { class: "muted small" }, "Each AI runs every persona; its score is the average over them."),
        h("div", { class: "rank" }, h("span", { class: "n" }, "–"), h("span", { class: "nm" }, "The market"), h("span", { class: "sc" }, b3(lb.baselines.market))),
        ...group(lb.by_model, (g) => `${g.family || ""} · trained on data to ${cutoff(g.knowledge_cutoff)}`)] : null,
      (lb.by_persona || []).length ? [h("h2", {}, "Which world view reads farming best"),
        h("p", { class: "muted small" }, "Each persona is averaged over every AI. Narrative personas follow what real organisations said in public before the sealed questions; they are based on public statements and not endorsed."),
        ...group(lb.by_persona, () => null)] : null,
      h("h2", {}, "Every account"),
      h("p", { class: "muted small" }, "👤 human · 🤖 agent · 🧪 test account (made by our own QA agents, not counted as a pilot) · ✓ verified: a sourced persona of a real organisation or person, ", lb.verified_label || "based on public statements; not endorsed",
        ". Market-follower copies the market's closing price, so its score is the market's."),
      chips, rows,
      lb.insight ? h("p", { class: "card" }, lb.insight) : null,
      lb.dev_note ? h("p", { class: "muted small" }, lb.dev_note) : null,
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
  const PAGES = { home, markets, market: marketPage, live, theory, board: leaderboard, scenario: () => {} }; // scenario: scenario.js
  // A login link (/app#login=<token>) signs this browser in as an existing account; the fragment never reaches a server.
  async function claimLogin() {
    const m = /^#login=([A-Za-z0-9_-]{16,64})$/.exec(location.hash);
    if (!m) return;
    history.replaceState(null, "", location.pathname + location.search);
    try {
      const info = await api(`/me?token=${encodeURIComponent(m[1])}`);
      save({ token: m[1], user_id: info.user_id, username: info.username, nickname: info.nickname, type: info.type, credits: info.balance });
    } catch (_) { /* unknown token: stay signed out */ }
  }
  claimLogin().finally(() => {
    renderChrome();
    (PAGES[document.body.dataset.page] || (() => {}))(); // other pages (replay, impact) bring their own script
    refreshBalance();
    loadCroppy();
  });
})();
