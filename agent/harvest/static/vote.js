// /vote phone page: a guest account (token kept in localStorage, so one guest per device) betting on the featured market.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const KEY = "crophecy-vote-guest";
  let token = null, me = null, spend = 10, busy = false, cooldownUntil = 0;

  const store = {
    get() { try { return localStorage.getItem(KEY); } catch (e) { return null; } },
    set(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* private mode: the guest lasts for this tab */ } },
  };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const pct = (p) => (p * 100).toFixed(1) + "%";

  function msg(text, cls) { const m = $("msg"); m.textContent = text; m.className = "msg" + (cls ? " " + cls : ""); }

  function showMe(d) {
    me = d;
    $("credits").textContent = `${Math.floor(d.credits_left)} credits`;
    $("me").innerHTML = `You are <b>👤 @${esc(d.username)}</b>. ${d.n_bets ? `${d.n_bets} bet${d.n_bets > 1 ? "s" : ""} placed.` : "Pick a side: are the bots right?"}`;
    setButtons();
  }

  function setButtons() {
    const can = me && me.credits_left >= 1 && !busy && Date.now() >= cooldownUntil;
    $("yes").disabled = $("no").disabled = !can;
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

  async function bet(side) {
    if (!token || busy) return;
    busy = true; setButtons(); msg(`Placing ${side}…`);
    try {
      const d = await post("/vote/api/bet", { token, side, spend });
      showMe(d);
      msg(`You bet ${d.trade.spend} on ${side}. The price moved ${pct(d.trade.price_before)} → ${pct(d.trade.price_after)}. Look at the big screen!`, "ok");
      $("p").textContent = pct(d.market.p_yes);
      cooldownUntil = Date.now() + 2500;
      setTimeout(setButtons, 2600);
    } catch (e) {
      if (e.status === 401) { store.set(""); token = null; guest(); }
      if (e.retry) { cooldownUntil = Date.now() + e.retry * 1000; setTimeout(setButtons, e.retry * 1000 + 100); }
      msg(e.message, "err");
    } finally {
      busy = false; setButtons();
      tick();
    }
  }

  function spark(path, pNow, now) {
    const w = 300, h = 64, t0 = now - 20 * 60000;
    const pts = path.filter((r) => r[0] >= t0);
    const ps = pts.map((r) => r[1]).concat([pNow]);
    const lo = Math.max(0, Math.min(...ps) - 0.05), hi = Math.min(1, Math.max(...ps) + 0.05);
    const x = (t) => ((t - t0) / (now - t0)) * w, y = (p) => 4 + (hi - p) / (hi - lo) * (h - 8);
    let d = `M0,${y(pts.length ? pts[0][1] : pNow).toFixed(1)}`;
    for (const [t, p] of pts) d += `H${x(t).toFixed(1)}V${y(p).toFixed(1)}`;
    d += `H${w}V${y(pNow).toFixed(1)}`;
    const dots = pts.filter((r) => r[2] === "h").map(([t, p]) => `<circle cx="${x(t).toFixed(1)}" cy="${y(p).toFixed(1)}" r="3.5" fill="var(--accent2)" stroke="var(--panel)" stroke-width="1.5"/>`).join("");
    $("spark").innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Price over the last 20 minutes">` +
      `${lo < 0.5 && hi > 0.5 ? `<line x1="0" x2="${w}" y1="${y(0.5)}" y2="${y(0.5)}" stroke="var(--line2)" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>` : ""}` +
      `<path d="${d}" fill="none" stroke="var(--yes)" stroke-width="2.5" vector-effect="non-scaling-stroke"/>${dots}</svg>`;
  }

  async function tick() {
    try {
      const r = await fetch("/pitch/api/state?minutes=20", { cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      if (!s.featured) return;
      const f = s.featured;
      $("qtitle").textContent = f.title;
      $("qmeta").textContent = `Live market · resolves ${f.resolve_month || ""}`;
      $("p").textContent = pct(f.p_yes);
      $("yesq").textContent = `≈ pays ${(1 / Math.max(f.p_yes, 0.01)).toFixed(2)}×`;
      $("noq").textContent = `≈ pays ${(1 / Math.max(1 - f.p_yes, 0.01)).toFixed(2)}×`;
      spark(s.path, f.p_yes, s.now);
      $("feed").innerHTML = s.feed.slice(0, 8).map((b) => {
        const human = b.who === "human", mine = me && b.username === me.username;
        return `<li class="${mine ? "mine" : ""}"><span>${human ? "👤" : "🤖"}</span><span class="who">${esc(human ? "@" + b.username : b.name)}${mine ? " (you)" : ""}</span>` +
          `<span class="pill ${b.side}">${b.side}</span><span>${Math.round(b.spend)}</span></li>`;
      }).join("");
    } catch (e) { /* the next tick retries */ }
  }

  $("amounts").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    spend = Number(b.dataset.v);
    for (const x of $("amounts").querySelectorAll("button")) x.setAttribute("aria-pressed", String(x === b));
  });
  $("yes").addEventListener("click", () => bet("YES"));
  $("no").addEventListener("click", () => bet("NO"));
  guest();
  tick();
  setInterval(() => { if (!document.hidden) tick(); }, 5000);
})();
