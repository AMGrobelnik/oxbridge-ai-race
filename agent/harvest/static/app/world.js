"use strict";
/* /app/farmer, /app/map and /app/disagreement: farmer mode, the climate-stress map and the disagreement index.
   Reads GET /harvest/api/farmer, /harvest/api/map and /harvest/api/disagreement (SPEC.md section 16). */
(() => {
  const API = "/harvest/api";
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
  const pct = (p) => `${Math.round(p * 100)}%`;
  const main = () => document.getElementById("main");
  const put = (...kids) => main().replaceChildren(...kids.flat().filter(Boolean));
  async function call(path) {
    const r = await fetch(API + path);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Something went wrong (HTTP ${r.status})`);
    return data;
  }
  const fail = (e) => main().replaceChildren(h("p", { class: "err" }, e.message || String(e)));
  const marketLink = (m) => h("a", { class: "row", href: `/app/market/${encodeURIComponent(m.qid)}` },
    h("span", { class: "q" }, m.title, m.crop ? h("span", { class: "sub" }, m.crop) : null), h("span", { class: "pct" }, pct(m.p_yes)));

  // ------------------------------------------------------------ farmer mode
  const CROPS = ["wheat", "maize", "barley", "rapeseed", "sunflower"];
  const REGIONS = ["France", "Spain", "Italy", "Germany", "Poland", "Romania", "Greece"];
  function farmer() {
    const q = new URLSearchParams(location.search);
    const state = { crop: q.get("crop") || "wheat", region: q.get("region") || "France" };
    const out = h("div", { id: "answer" });
    const pick = (list, key) => h("div", { class: "chips", role: "group" }, list.map((v) =>
      h("button", { class: "chip" + (state[key] === v ? " on" : ""), "data-v": v, onclick: (ev) => {
        state[key] = v;
        for (const b of ev.target.parentNode.children) b.classList.toggle("on", b.dataset.v === v);
        ask();
      } }, v)));
    put(
      h("h1", {}, "Will my price rise by harvest?"),
      h("p", { class: "lead" }, "Pick your crop and region. You get one probability, the reason in plain words and how sure the AIs are. No betting."),
      h("h2", {}, "My crop"), pick(CROPS, "crop"),
      h("h2", {}, "My region"), pick(REGIONS, "region"), out);
    let seq = 0;
    async function ask() {
      const mine = ++seq;
      history.replaceState(null, "", `?crop=${state.crop}&region=${state.region}`);
      out.replaceChildren(h("p", { class: "muted" }, h("span", { class: "spin" }), "Asking Croppy…"));
      try {
        const a = await call(`/farmer?crop=${encodeURIComponent(state.crop)}&region=${encodeURIComponent(state.region)}`);
        if (mine !== seq) return;
        const cls = a.confidence.level === "high" ? "good" : a.confidence.level === "low" ? "bad" : "";
        out.replaceChildren(h("div", { class: "card" },
          h("div", { class: "muted small" }, a.question, ` (${a.region})`),
          h("div", { class: "hero-pct" }, pct(a.probability)),
          h("div", { class: "muted small" }, "chance, from: ", h("a", { href: `/app/market/${encodeURIComponent(a.market.qid)}` }, a.market.title)),
          h("p", {}, a.reason),
          h("div", { class: `card ${cls}` }, h("b", {}, "Confidence: "), a.confidence.text),
          a.stress ? h("p", { class: "small" }, `Heat stress in ${a.region}: ${a.stress.level} (${a.stress.heat_anomaly_c > 0 ? "+" : ""}${a.stress.heat_anomaly_c} °C). `,
            h("a", { href: "/app/map" }, "See the map")) : null,
          h("p", { class: "muted small" }, a.source === "fallback" ? "Reason written from the agents' own words." : "Reason by Croppy (AI).")));
      } catch (e) { if (mine === seq) out.replaceChildren(h("p", { class: "err" }, e.message)); }
    }
    ask();
  }

  // ------------------------------------------------------------ map
  function stressColor(s) { // green (low) to dark red (severe)
    const stops = [[0, [43, 138, 62]], [0.35, [240, 200, 60]], [0.65, [240, 120, 40]], [1, [170, 20, 20]]];
    for (let i = 1; i < stops.length; i++) {
      if (s <= stops[i][0]) {
        const [a, ca] = stops[i - 1], [b, cb] = stops[i], t = (s - a) / (b - a);
        return `rgb(${ca.map((c, j) => Math.round(c + (cb[j] - c) * t)).join(",")})`;
      }
    }
    return "rgb(170,20,20)";
  }
  async function mapPage() {
    let d;
    try { d = await call("/map"); } catch (e) { fail(e); return; }
    const panel = h("div", { id: "region" });
    const box = h("div", { id: "map" });
    const a = d.anomaly;
    put(
      h("h1", {}, "Where is the heat?"),
      h("p", { class: "lead" }, "Europe's farm regions coloured by heat stress. Tap a region to see the markets it moves."),
      a ? h("p", { class: "small muted" }, `Europe land is ${a.value_c > 0 ? "+" : ""}${a.value_c.toFixed(1)} °C above the 1901-2000 normal (${a.from} to ${a.to}, NOAA).`) : null,
      box, h("div", { class: "legend small" }, ["low", "moderate", "high", "severe"].map((l, i) =>
        h("span", {}, h("i", { style: `background:${stressColor(i / 3)}` }), l))),
      panel,
      h("h2", {}, "Europe-wide markets"), d.europe_wide.map(marketLink),
      h("p", { class: "muted small" }, d.method));
    function select(r) {
      panel.replaceChildren(h("div", { class: "card" },
        h("h2", { style: "margin-top:0" }, r.region),
        h("p", {}, `Heat stress ${r.level}: about ${r.heat_anomaly_c > 0 ? "+" : ""}${r.heat_anomaly_c} °C above normal (stress ${pct(r.stress)}).`),
        r.markets.length ? r.markets.map(marketLink) : h("p", { class: "muted" }, "No market is about this region yet.")));
    }
    const map = L.map(box, { scrollWheelZoom: false }).setView([46, 12], 4);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 10, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    for (const r of d.regions) {
      L.circleMarker([r.lat, r.lng], { radius: 14 + 18 * r.stress, color: "#222", weight: 1, fillColor: stressColor(r.stress), fillOpacity: 0.8 })
        .addTo(map).bindTooltip(`${r.region}: ${r.level} (${r.markets.length} markets)`).on("click", () => select(r));
    }
    map.fitBounds(d.regions.map((r) => [r.lat, r.lng]), { padding: [30, 30] });
    select(d.regions[0]);
  }

  // ------------------------------------------------------------ disagreement index
  async function disagreementPage() {
    let d;
    try { d = await call("/disagreement?limit=15"); } catch (e) { fail(e); return; }
    const agent = (a, label) => h("div", { class: "agent" }, h("span", { class: "tag" }, label), h("b", {}, a.name), h("span", { class: "p" }, pct(a.p_yes)),
      h("span", { class: "why" }, a.reason || "No reason given."));
    put(
      h("h1", {}, "Where the AIs disagree most"),
      h("p", { class: "lead" }, "Where the AI agents disagree most is where the future is really uncertain. Ranked by how far their forecasts spread."),
      d.markets.map((m) => h("div", { class: "card" },
        h("div", { class: "muted small" }, `#${m.rank} · ${m.region} · ${m.crop}`),
        h("a", { href: `/app/market/${encodeURIComponent(m.qid)}`, style: "font-weight:700;color:inherit" }, m.title),
        h("div", { class: "range" }, h("i", { style: `left:${m.bear.p_yes * 100}%;width:${(m.bull.p_yes - m.bear.p_yes) * 100}%` }),
          h("b", { style: `left:${m.price * 100}%`, title: "market price" })),
        h("p", { class: "small" }, `Spread ${Math.round(m.std * 100)} points across ${m.n_agents} agents (from ${pct(m.bear.p_yes)} to ${pct(m.bull.p_yes)}); market price ${pct(m.price)}.`),
        agent(m.bull, "Highest"), agent(m.bear, "Lowest"))));
  }

  const PAGES = { farmer, map: mapPage, disagreement: disagreementPage };
  (PAGES[document.body.dataset.page] || (() => {}))();
})();
