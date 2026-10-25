"use strict";
(() => {
  const { el, api, pct, cr, signed, b3 } = H;
  H.initTheme(document.getElementById("theme"));
  const flowHost = document.getElementById("flow");
  const flow = new H.Flow(flowHost, { w: 620, h: 330, maxMarkets: 6 });
  flow.svg.style.height = "100%";
  flow.svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  const stream = document.getElementById("stream");
  const marketsById = new Map();
  let seq = null, trades = [], geo = null, mapApi = null, lastMap = 0;

  function counters(s) {
    const vol = s.markets.reduce((a, m) => a + (m.volume || 0), 0);
    H.tween(document.getElementById("c-bets"), s.seq || 0, (x) => cr(x));
    H.tween(document.getElementById("c-vol"), vol, (x) => cr(x));
    H.tween(document.getElementById("c-users"), s.users || 0, (x) => cr(x));
    H.tween(document.getElementById("c-agents"), s.agents || 0, (x) => cr(x));
    H.tween(document.getElementById("c-fund"), s.fund.treasury || 0, (x) => cr(x, 1));
    document.getElementById("feed-sub").textContent = `ledger #${s.ledger_head.seq} ${s.ledger_head.hash.slice(0, 10)}`;
  }
  function leaderboard(s) {
    const lb = s.leaderboard;
    const ul = document.getElementById("blb");
    ul.innerHTML = "";
    const bases = [["market", lb.baselines.market], ["coin flip", lb.baselines.coin]].filter((b) => b[1] != null).sort((a, b) => a[1] - b[1]);
    let n = 0;
    for (const m of lb.world_models.slice(0, 9)) {
      while (bases.length && m.brier_holdout != null && bases[0][1] < m.brier_holdout) {
        const [nm, v] = bases.shift();
        const li = el("li", "base");
        li.append(el("span"), el("span"), el("span", null, `baseline · ${nm}`), el("span", "v", b3(v)));
        ul.append(li);
      }
      n += 1;
      const li = el("li");
      li.append(el("span", "n", n), H.avatar(m.origin === "house" ? `agent:${m.persona_id}` : m.persona_id, 22), el("span", "nm", m.name), el("span", "v", b3(m.brier_holdout)));
      ul.append(li);
    }
    document.getElementById("ins").textContent = lb.insight || "";
    const hum = document.getElementById("hum");
    hum.innerHTML = "";
    const rows = lb.world_models.filter((m) => m.pnl_live != null).map((m) => ({ id: m.origin === "house" ? `agent:${m.persona_id}` : m.persona_id, name: m.name, v: m.pnl_live }))
      .concat(lb.humans.map((h) => ({ id: h.user_id, name: h.nickname, v: h.pnl || 0, human: true })));
    rows.sort((a, b) => b.v - a.v);
    const humans = rows.filter((r) => r.human);
    const show = rows.slice(0, 2).concat(humans.filter((h) => rows.indexOf(h) >= 2).slice(0, 2));
    if (!humans.length) show.push({ id: "user:none", name: "Scan the QR to join", v: null, human: true });
    for (const r of show) {
      const li = el("li");
      li.append(el("span", "n", r.v == null ? "" : rows.indexOf(r) + 1), H.avatar(r.id, 22), el("span", "nm", r.name), el("span", `v ${r.v == null ? "" : r.v >= 0 ? "yes" : "no"}`, r.v == null ? "" : signed(r.v, 1)));
      hum.append(li);
    }
  }
  function push(fresh, animate) {
    trades = trades.concat(fresh).slice(-600);
    if (!animate) {
      stream.innerHTML = "";
      for (const t of fresh.slice(-16).reverse()) stream.append(H.feedItem(t, marketsById, false));
      return;
    }
    fresh.slice(-8).forEach((t, i) => setTimeout(() => {
      stream.prepend(H.feedItem(t, marketsById, true));
      while (stream.children.length > 16) stream.lastChild.remove();
      setTimeout(() => stream.querySelectorAll("li.fresh").forEach((n) => n.classList.remove("fresh")), 2500);
      flow.pulse(t);
      if (t.trader.startsWith("user:")) H.toast(`${H.trader(t.trader).name} bet ${cr(t.spend)} on ${t.side}: ${H.shortTitle(marketsById.get(t.qid))}`, 3500);
    }, i * 380));
  }
  async function poll() {
    try {
      const s = await api(seq == null ? "/harvest/api/state" : `/harvest/api/state?since=${seq}`);
      const first = seq == null;
      H.registerPersonas(s.personas);
      H.registerTraders(s.leaderboard);
      marketsById.clear();
      for (const m of s.markets) marketsById.set(m.qid, m);
      const fresh = s.recent_trades || [];
      seq = s.seq;
      flow.draw(s, trades.concat(fresh));
      push(fresh, !first);
      counters(s);
      if (first || fresh.length) leaderboard(s);
      if (window.L && geo) {
        if (!mapApi) mapApi = H.riskMap("bmap", { center: [41, 22], zoom: 3, static: true, scale: 0.65 });
        if (first || Date.now() - lastMap > 15000) { lastMap = Date.now(); mapApi.draw(geo, s.fund, marketsById); }
      }
    } catch (e) {
      console.warn("poll failed", e);
    }
    setTimeout(poll, 2000);
  }
  (async () => {
    geo = await api("/harvest/api/geo").catch(() => null);
    api("/harvest/api/playurl").then((r) => { document.getElementById("url").textContent = r.url.replace(/^https?:\/\//, ""); }).catch(() => {});
    poll();
  })();
})();
