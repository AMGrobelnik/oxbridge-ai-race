// /pitch projector page: polls /pitch/api/state and draws the featured market, human vs bot tallies and the bet feed.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const MINUTES = 20;
  const NS = "http://www.w3.org/2000/svg";
  let seen = null;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const pct = (p) => (p * 100).toFixed(1) + "%";
  const fmt = (n) => Number(n || 0).toLocaleString("en-GB");

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  function drawChart(path, pNow, now) {
    const box = $("chart");
    const w = box.clientWidth, h = box.clientHeight;
    if (!w || !h) return;
    const padL = 56, padR = 16, padT = 10, padB = 30;
    const t0 = now - MINUTES * 60000;
    const x = (t) => padL + ((t - t0) / (now - t0)) * (w - padL - padR);
    const ps = path.filter((r) => r[0] >= t0).map((r) => r[1]).concat([pNow]);
    const lo = Math.max(0, Math.floor((Math.min(...ps) - 0.05) * 10) / 10), hi = Math.min(1, Math.ceil((Math.max(...ps) + 0.05) * 10) / 10);
    const y = (p) => padT + (hi - p) / (hi - lo) * (h - padT - padB);
    const svg = el("svg", { viewBox: `0 0 ${w} ${h}`, role: "img", "aria-label": "Probability of YES over the last " + MINUTES + " minutes" });
    const fs = Math.max(12, Math.round(h * 0.045));
    const step = hi - lo > 0.5 ? 0.2 : 0.1;
    for (let g = lo; g <= hi + 1e-9; g = Math.round((g + step) * 10) / 10) {
      el("line", { x1: padL, x2: w - padR, y1: y(g), y2: y(g), stroke: Math.abs(g - 0.5) < 1e-9 ? "#5a5240" : "#2b271d", "stroke-dasharray": Math.abs(g - 0.5) < 1e-9 ? "6 6" : "", "stroke-width": 1 }, svg);
      const t = el("text", { x: padL - 10, y: y(g) + fs * 0.35, "text-anchor": "end", fill: "#776f5d", "font-size": fs, "font-family": "JetBrains Mono, monospace" }, svg);
      t.textContent = Math.round(g * 100) + "%";
    }
    for (let m = MINUTES; m >= 0; m -= 5) {
      const t = el("text", { x: x(now - m * 60000), y: h - 6, "text-anchor": "middle", fill: "#776f5d", "font-size": fs, "font-family": "JetBrains Mono, monospace" }, svg);
      t.textContent = m === 0 ? "now" : `-${m} min`;
    }
    const pts = path.filter((r) => r[0] >= t0);
    const start = path.length && path[0][0] < t0 ? path[0][1] : (pts.length ? pts[0][1] : pNow);
    let d = `M${x(t0).toFixed(1)},${y(start).toFixed(1)}`;
    for (const [t, p] of pts) { d += `H${x(t).toFixed(1)}V${y(p).toFixed(1)}` }
    d += `H${x(now).toFixed(1)}V${y(pNow).toFixed(1)}`;
    el("path", { d: d + `V${y(lo)}H${x(t0)}Z`, fill: "rgba(81,207,102,.10)", stroke: "none" }, svg);
    el("path", { d, fill: "none", stroke: "#51cf66", "stroke-width": Math.max(3, h * 0.008), "stroke-linejoin": "round" }, svg);
    for (const [t, p, who] of pts) {
      if (who !== "h") continue;
      el("circle", { cx: x(t), cy: y(p), r: Math.max(7, h * 0.02), fill: "#f7d070", stroke: "#0d0c09", "stroke-width": 3 }, svg);
    }
    el("circle", { cx: x(now), cy: y(pNow), r: Math.max(6, h * 0.016), fill: "#51cf66" }, svg);
    box.replaceChildren(svg);
  }

  function renderFeed(feed) {
    const ul = $("feed");
    if (!feed.length) return;
    const prev = seen;
    seen = new Set(feed.map((b) => b.seq));
    ul.innerHTML = feed.slice(0, 14).map((b) => {
      const isNew = prev && !prev.has(b.seq);
      const human = b.who === "human";
      return `<li class="${human ? "human" : "bot"}${isNew ? " new" : ""}"><span class="ic">${human ? "👤" : "🤖"}</span>` +
        `<span class="nm">${esc(human ? "@" + b.username : b.name)}<small>${esc(b.icon || "")} ${esc(b.tag || "")} · ${Math.round(b.spend)} cr</small></span>` +
        `<span class="pill ${b.side}">${b.side}</span></li>`;
    }).join("");
  }

  function side(prefix, s) {
    $(prefix + "n").textContent = fmt(s.bets);
    const share = s.yes_share == null ? null : s.yes_share;
    $(prefix + "s").textContent = s.bets ? `${fmt(s.traders)} ${prefix === "h" ? "people" : "agents"} · ${share == null ? "-" : Math.round(share * 100) + "% of credits on YES"}` : (prefix === "h" ? "Be the first: scan the code" : "bets so far");
    $(prefix + "b").style.width = (share == null ? 50 : share * 100) + "%";
  }

  let evSeen = {};
  function renderEvents(events) {
    const box = $("events");
    box.style.setProperty("--n", events.length);
    const row = (cls, ic, s) => `<div class="w ${cls}"><span>${ic}</span><div class="bar"><i style="width:${s.yes_share == null ? 0 : s.yes_share * 100}%"></i></div>` +
      `<span>${s.bets ? `${fmt(s.bets)} · ${Math.round(s.yes_share * 100)}% Y` : "-"}</span></div>`;
    box.innerHTML = events.map((e) => {
      const hot = evSeen[e.qid] != null && e.human.bets > evSeen[e.qid];
      return `<div class="ev${e.featured ? " featured" : ""}${hot ? " hot" : ""}"><div class="tag"><span>${esc(e.icon)} ${esc(e.tag)}</span>${e.featured ? "<span>★</span>" : ""}</div>` +
        `<div class="q">${esc(e.plain)}</div>` +
        `<div class="pp">${pct(e.p_yes)}<small>YES${e.ai_p == null ? "" : ` · AI ${Math.round(e.ai_p * 100)}%`}</small></div>` +
        row("h", "👤", e.human) + row("b", "🤖", e.bot) + `</div>`;
    }).join("");
    evSeen = Object.fromEntries(events.map((e) => [e.qid, e.human.bets]));
  }

  async function tick() {
    try {
      const r = await fetch(`/pitch/api/state?minutes=${MINUTES}`, { cache: "no-store" });
      if (!r.ok) throw new Error(r.status);
      const s = await r.json();
      if (!s.featured) { $("qtitle").textContent = "No open market to feature."; return; }
      const f = s.featured;
      $("url").textContent = s.vote_url.replace(/^https?:\/\//, "");
      $("qtitle").textContent = f.title;
      $("qmeta").textContent = `Featured market · ${f.kind === "soft" ? "policy" : "price"} · resolves ${f.resolve_month || ""} · ${fmt(Math.round(f.volume))} credits traded`;
      $("p").textContent = pct(f.p_yes);
      const dp = (f.p_yes - (s.path.length ? s.path[0][1] : f.p_yes)) * 100;
      $("delta").textContent = `${dp >= 0 ? "▲" : "▼"} ${Math.abs(dp).toFixed(1)} pts in ${MINUTES} min`;
      $("delta").style.color = dp >= 0 ? "var(--yes)" : "var(--no)";
      drawChart(s.path, f.p_yes, s.now);
      side("h", s.tally.human);
      side("b", s.tally.bot);
      const lastMin = s.path.filter((r) => r[0] >= s.now - 60000).length;
      $("rate").textContent = `${lastMin} in the last min`;
      $("guests").innerHTML = s.tally.human.traders ? `<b>${fmt(s.tally.human.traders)}</b> humans have bet so far.` : "";
      renderFeed(s.feed);
      if (s.events) renderEvents(s.events);
    } catch (e) {
      $("rate").textContent = "reconnecting…";
    }
  }
  tick();
  setInterval(() => { if (!document.hidden) tick(); }, 5000);
  window.addEventListener("resize", tick);
})();
