// /vote phone page: a guest account (token kept in localStorage, so one guest per device) betting on the featured market
// and the headline events listed by /pitch/api/state. After a bet a card shows "You vs the bots" and links the full market.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const KEY = "crophecy-vote-guest";
  let token = null, me = null, spend = 10, busy = false, cooldownUntil = 0, shown = "";
  const vs = {};  // qid -> the latest "vs" from a bet response

  const store = {
    get() { try { return localStorage.getItem(KEY); } catch (e) { return null; } },
    set(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* private mode: the guest lasts for this tab */ } },
  };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const pct = (p) => (p * 100).toFixed(0) + "%";
  const card = (qid) => document.querySelector(`.card[data-qid="${CSS.escape(qid)}"]`);

  function msg(text, cls, qid) {
    const m = (qid && card(qid) && card(qid).querySelector(".msg")) || $("msg");
    for (const x of document.querySelectorAll(".msg")) if (x !== m) x.textContent = "";
    m.textContent = text; m.className = "msg" + (cls ? " " + cls : "");
  }

  function showMe(d) {
    me = d;
    $("credits").textContent = `${Math.floor(d.credits_left)} credits`;
    $("me").innerHTML = `You are <b>👤 @${esc(d.username)}</b>. ${d.n_bets ? `${d.n_bets} bet${d.n_bets > 1 ? "s" : ""} placed.` : "Pick YES or NO on any event: are the bots right?"}`;
    setButtons();
  }

  function setButtons() {
    const can = me && me.credits_left >= 1 && !busy && Date.now() >= cooldownUntil;
    for (const b of document.querySelectorAll(".card .bet")) b.disabled = !can;
    for (const b of document.querySelectorAll(".card .bet small")) b.textContent = `${Math.min(spend, Math.floor(me ? me.credits_left : spend))} cr`;
    if (me && me.credits_left < 1) msg("You have used all 100 credits. Watch the big screen!", "ok");
  }

  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(d.error || `HTTP ${r.status}`), { status: r.status, retry: Number(r.headers.get("Retry-After") || 0) });
    return d;
  }

  async function guest() {
    try {
      const d = await post("/vote/api/guest", { token: store.get() || "" });
      token = d.token;
      store.set(token);
      showMe(d);
    } catch (e) {
      $("me").textContent = `Could not get a guest handle (${e.message}). Retrying…`;
      setTimeout(guest, Math.max(3000, (e.retry || 0) * 1000));
    }
  }

  function drawVs(qid) {
    const v = vs[qid], c = card(qid);
    if (!v || !c) return;
    const box = c.querySelector(".vs");
    const you = v.you.yes >= v.you.no ? `YES ${Math.round(v.you.yes)}${v.you.no ? ` · NO ${Math.round(v.you.no)}` : ""}` : `NO ${Math.round(v.you.no)}${v.you.yes ? ` · YES ${Math.round(v.you.yes)}` : ""}`;
    const b = v.bots;
    const ai = b.ai_p == null ? "-" : `${pct(b.ai_p)} YES`;
    const split = b.yes_share == null ? "no bets yet" : `${pct(b.yes_share)} of credits on YES`;
    box.innerHTML = `<h4>You vs the bots</h4>` +
      `<div class="row"><span>👤 You</span><b>${esc(you)} cr</b></div>` +
      `<div class="row"><span>🤖 Swarm's average forecast</span><b>${ai}</b></div>` +
      `<div class="row"><span>🤖 ${b.agents} bot${b.agents === 1 ? "" : "s"} betting</span><b>${split}</b></div>` +
      `<div class="row"><span>Market now</span><b>${pct(v.p_yes)} YES</b></div>` +
      `<a href="${esc(v.url)}">See the full market →</a>`;
    box.hidden = false;
  }

  async function bet(qid, side) {
    if (!token || busy) return;
    busy = true; setButtons(); msg(`Placing ${side}…`, "", qid);
    try {
      const d = await post("/vote/api/bet", { token, side, spend, qid });
      showMe(d);
      vs[qid] = d.vs;
      drawVs(qid);
      msg(`You bet ${d.trade.spend} on ${side}: ${pct(d.trade.price_before)} → ${pct(d.trade.price_after)}. Look at the big screen!`, "ok", qid);
      cooldownUntil = Date.now() + 2500;
      setTimeout(setButtons, 2600);
    } catch (e) {
      if (e.status === 401) { store.set(""); token = null; guest(); }
      if (e.retry) { cooldownUntil = Date.now() + e.retry * 1000; setTimeout(setButtons, e.retry * 1000 + 100); }
      msg(e.message, "err", qid);
    } finally {
      busy = false; setButtons();
      tick();
    }
  }

  function spark(el, path, pNow, now) {
    const w = 300, h = 48, t0 = now - 20 * 60000;
    const pts = path.filter((r) => r[0] >= t0);
    const ps = pts.map((r) => r[1]).concat([pNow]);
    const lo = Math.max(0, Math.min(...ps) - 0.05), hi = Math.min(1, Math.max(...ps) + 0.05);
    const x = (t) => ((t - t0) / (now - t0)) * w, y = (p) => 4 + (hi - p) / (hi - lo) * (h - 8);
    let d = `M0,${y(pts.length ? pts[0][1] : pNow).toFixed(1)}`;
    for (const [t, p] of pts) d += `H${x(t).toFixed(1)}V${y(p).toFixed(1)}`;
    d += `H${w}V${y(pNow).toFixed(1)}`;
    const dots = pts.filter((r) => r[2] === "h").map(([t, p]) => `<circle cx="${x(t).toFixed(1)}" cy="${y(p).toFixed(1)}" r="3.5" fill="var(--accent2)" stroke="var(--panel)" stroke-width="1.5"/>`).join("");
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Price over the last 20 minutes">` +
      `<path d="${d}" fill="none" stroke="var(--yes)" stroke-width="2.5" vector-effect="non-scaling-stroke"/>${dots}</svg>`;
  }

  function buildCards(events) {
    $("events").innerHTML = events.map((e) => `<article class="card${e.featured ? " featured" : ""}" data-qid="${esc(e.qid)}">` +
      `${e.featured ? `<span class="badge">On the big screen</span>` : ""}` +
      `<div class="qmeta">${esc(e.icon)} ${esc(e.tag)}</div>` +
      `<div class="qtitle">${esc(e.plain)}</div>` +
      `<div class="odds"><span class="crowd" data-f="p">--<small>chance, crowd + bots</small></span><span class="ai" data-f="ai">--<small>AI swarm says</small></span></div>` +
      `${e.featured ? `<div class="spark"></div>` : ""}` +
      `<div class="bets"><button class="bet YES" type="button" data-side="YES" disabled>YES<small></small></button>` +
      `<button class="bet NO" type="button" data-side="NO" disabled>NO<small></small></button></div>` +
      `<p class="msg" role="status"></p><div class="vs" hidden></div></article>`).join("");
    for (const qid in vs) drawVs(qid);
    setButtons();
  }

  async function tick() {
    try {
      const r = await fetch("/pitch/api/state?minutes=20", { cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      if (!s.events || !s.events.length) return;
      const ids = s.events.map((e) => e.qid).join("|");
      if (ids !== shown) { shown = ids; buildCards(s.events); }
      for (const e of s.events) {
        const c = card(e.qid);
        if (!c) continue;
        c.querySelector('[data-f="p"]').firstChild.textContent = pct(e.p_yes);
        c.querySelector('[data-f="ai"]').firstChild.textContent = e.ai_p == null ? "-" : pct(e.ai_p);
        if (e.featured && s.featured) spark(c.querySelector(".spark"), s.path, s.featured.p_yes, s.now);
      }
      $("feed").innerHTML = s.feed.slice(0, 8).map((b) => {
        const human = b.who === "human", mine = me && b.username === me.username;
        return `<li class="${mine ? "mine" : ""}"><span>${human ? "👤" : "🤖"}</span><span class="who">${esc(human ? "@" + b.username : b.name)}${mine ? " (you)" : ""}</span>` +
          `<span title="${esc(b.tag)}">${esc(b.icon || "")}</span><span class="pill ${b.side}">${b.side}</span><span>${Math.round(b.spend)}</span></li>`;
      }).join("");
    } catch (e) { /* the next tick retries */ }
  }

  $("events").addEventListener("click", (ev) => {
    const b = ev.target.closest(".bet");
    if (!b || b.disabled) return;
    bet(b.closest(".card").dataset.qid, b.dataset.side);
  });
  $("amounts").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    spend = Number(b.dataset.v);
    for (const x of $("amounts").querySelectorAll("button")) x.setAttribute("aria-pressed", String(x === b));
    setButtons();
  });
  guest();
  tick();
  setInterval(() => { if (!document.hidden) tick(); }, 5000);
})();
