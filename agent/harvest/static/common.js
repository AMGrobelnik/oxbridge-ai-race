"use strict";
/* Harvest shared front-end helpers: API, formatting, SVG charts, bets flow, feed, map and fund. */
window.H = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const MONO = { "climate-pessimist": "CP", "efficient-market-skeptic": "EM", "geopolitics-hawk": "GH", "adaptation-optimist": "AO",
    "macro-dollar": "MD", "land-grab-realist": "LG", "agronomist": "AG", "crowd-follower": "CF" };
  const personas = new Map();
  const names = new Map(); // trader id -> {name, color, kind}

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function svg(tag, attrs = {}, parent) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      if ((k === "fill" || k === "stroke") && String(v).includes("var(")) n.style.setProperty(k, v);
      else n.setAttribute(k, v);
    }
    if (parent) parent.append(n);
    return n;
  }
  async function api(path, opts = {}) {
    const init = { headers: { "Content-Type": "application/json" }, ...opts };
    if (opts.body && typeof opts.body !== "string") init.body = JSON.stringify(opts.body);
    const r = await fetch(path, init);
    let data = {};
    try { data = await r.json(); } catch (_) { /* empty body */ }
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }
  const pct = (p, d = 0) => (p == null ? "–" : `${(p * 100).toFixed(d)}%`);
  const cr = (x, d = 0) => (x == null ? "–" : Number(x).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const signed = (x, d = 0) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}${cr(Math.abs(x), d)}`);
  const b3 = (x) => (x == null ? "–" : Number(x).toFixed(3));
  const short = (h, n = 10) => (h ? `${h.slice(0, n)}…${h.slice(-6)}` : "");
  function ago(iso) {
    if (!iso) return "";
    const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
    if (s < 5) return "just now";
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ago`;
  }
  function riskColor(r) {
    const stops = [[0, [47, 158, 68]], [0.35, [242, 179, 61]], [0.6, [232, 89, 12]], [1, [201, 42, 42]]];
    r = Math.max(0, Math.min(1, r || 0));
    for (let i = 1; i < stops.length; i++) {
      if (r <= stops[i][0]) {
        const [a, ca] = stops[i - 1], [b, cb] = stops[i];
        const t = (r - a) / (b - a);
        return `rgb(${ca.map((c, j) => Math.round(c + (cb[j] - c) * t)).join(",")})`;
      }
    }
    return "rgb(201,42,42)";
  }

  // ---- theme
  function initTheme(btn) {
    let saved = null;
    try { saved = localStorage.getItem("harvest-theme"); } catch (_) { /* storage blocked */ }
    const q = new URLSearchParams(location.search).get("theme");
    const pick = q || saved;
    if (pick === "light" || pick === "dark") document.documentElement.dataset.theme = pick;
    if (!btn) return;
    const label = () => { btn.textContent = isDark() ? "☀" : "☾"; };
    label();
    btn.addEventListener("click", () => {
      const next = isDark() ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("harvest-theme", next); } catch (_) { /* storage blocked */ }
      label();
      document.dispatchEvent(new CustomEvent("harvest-theme"));
    });
  }
  function isDark() {
    const t = document.documentElement.dataset.theme;
    if (t) return t === "dark";
    return matchMedia("(prefers-color-scheme: dark)").matches;
  }

  // ---- traders
  function registerPersonas(list) {
    for (const p of list || []) {
      personas.set(p.id, p);
      names.set(`agent:${p.id}`, { name: p.name, color: p.color, kind: "agent", mono: MONO[p.id] || p.name.slice(0, 2).toUpperCase(), id: p.id });
    }
  }
  function registerTraders(lb) {
    for (const m of (lb && lb.world_models) || []) {
      if (m.origin === "external") names.set(m.persona_id, { name: m.name, color: "#495057", kind: "external", mono: "EX" });
      if (m.origin === "human-model") names.set(m.persona_id, { name: m.name, color: m.color || "#f59f00", kind: "human-model", mono: "TH" });
    }
    for (const h of (lb && lb.humans) || []) names.set(h.user_id, { name: h.nickname, color: "#212529", kind: "human", mono: (h.nickname || "?").slice(0, 2).toUpperCase() });
  }
  function trader(id) {
    return names.get(id) || (id.startsWith("user:") ? { name: "player", color: "#212529", kind: "human", mono: "🙂" }
      : id.startsWith("ext:") ? { name: id.slice(4), color: "#495057", kind: "external", mono: "EX" } : { name: id, color: "#868e96", kind: "agent", mono: "?" });
  }
  function avatar(id, size) {
    const t = trader(id);
    const a = el("div", `av${t.kind === "human" ? " human" : ""}`, t.mono);
    if (t.kind !== "human") a.style.background = t.color;
    if (size) { a.style.width = a.style.height = `${size}px`; a.style.fontSize = `${Math.round(size * 0.38)}px`; }
    a.title = t.name;
    return a;
  }

  // ---- markets
  const COMMODITY = { PWHEAMTUSDM: "Wheat", PMAIZMTUSDM: "Maize", POLVOILUSDM: "Olive oil", PCOFFOTMUSDM: "Coffee", PCOCOUSDM: "Cocoa",
    PSUGAISAUSDM: "Sugar", PSUNOUSDM: "Sunflower oil", PRICENPQUSDM: "Rice" };
  function shortTitle(m) {
    if (!m) return "";
    const t = m.title || m.qid;
    return t.replace(/^Will /, "").replace(/\?$/, "");
  }
  function rationaleFor(trade, marketsById) {
    const m = marketsById.get(trade.qid);
    if (!m || !trade.trader.startsWith("agent:")) return "";
    const f = (m.agent_forecasts || []).find((x) => `agent:${x.persona_id}` === trade.trader);
    if (!f) return "";
    return f.rationale && f.rationale !== "world-model lean on the prior" ? f.rationale : `World-model lean: ${pct(f.p_yes)} YES`;
  }

  // ---- line chart: series [{values, color, width, dash, fill}], opts {w,h,ymin,ymax,shades:[{x0,x1,cls,label}],xlabels:[{i,text}],yfmt,pad}
  function lineChart(target, series, opts = {}) {
    const w = opts.w || 300, h = opts.h || 90;
    const pad = Object.assign({ l: 4, r: 4, t: 6, b: 4 }, opts.pad || {});
    const n = Math.max(2, ...series.map((s) => s.values.length));
    const all = series.flatMap((s) => s.values.filter((v) => v != null));
    let ymin = opts.ymin ?? Math.min(...all), ymax = opts.ymax ?? Math.max(...all);
    if (!isFinite(ymin) || !isFinite(ymax)) { ymin = 0; ymax = 1; }
    if (ymax - ymin < 1e-9) { ymax += 1; ymin -= 1; }
    const X = (i) => pad.l + (i / (n - 1)) * (w - pad.l - pad.r);
    const Y = (v) => pad.t + (1 - (v - ymin) / (ymax - ymin)) * (h - pad.t - pad.b);
    const root = target.tagName && target.tagName.toLowerCase() === "svg" ? target : svg("svg", {}, target);
    root.innerHTML = "";
    root.setAttribute("viewBox", `0 0 ${w} ${h}`);
    root.setAttribute("preserveAspectRatio", "none");
    for (const s of opts.shades || []) {
      const x0 = X(Math.max(0, s.x0)), x1 = X(Math.min(n - 1, s.x1));
      svg("rect", { x: x0, y: pad.t, width: Math.max(0, x1 - x0), height: h - pad.t - pad.b, class: s.cls || "shade-holdout" }, root);
      if (s.label) svg("text", { x: x0 + 4, y: pad.t + 11, class: "shade-label" }, root).textContent = s.label;
    }
    if (opts.grid) {
      const g = svg("g", { class: "grid" }, root);
      for (const v of opts.grid) svg("line", { x1: pad.l, x2: w - pad.r, y1: Y(v), y2: Y(v), "stroke-dasharray": "2 4", "vector-effect": "non-scaling-stroke" }, g);
    }
    if (opts.hline != null) svg("line", { x1: pad.l, x2: w - pad.r, y1: Y(opts.hline), y2: Y(opts.hline), stroke: "var(--line2)", "stroke-dasharray": "3 3", "vector-effect": "non-scaling-stroke" }, root);
    if (opts.vline != null) svg("line", { x1: X(opts.vline), x2: X(opts.vline), y1: pad.t, y2: h - pad.b, stroke: "var(--faint)", "stroke-dasharray": "2 3", "vector-effect": "non-scaling-stroke" }, root);
    const paths = [];
    for (const s of series) {
      let d = "", started = false;
      s.values.forEach((v, i) => {
        if (v == null) { started = false; return; }
        d += `${started ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`;
        started = true;
      });
      if (!d) continue;
      if (s.fill) {
        const last = s.values.length - 1;
        svg("path", { d: `${d}L${X(last)},${h - pad.b}L${X(0)},${h - pad.b}Z`, fill: s.fill, stroke: "none" }, root);
      }
      const p = svg("path", { d, fill: "none", stroke: s.color, "stroke-width": s.width || 1.6, "stroke-linejoin": "round", "stroke-linecap": "round",
        "vector-effect": "non-scaling-stroke", ...(s.dash ? { "stroke-dasharray": s.dash } : {}), ...(s.opacity ? { opacity: s.opacity } : {}) }, root);
      paths.push(p);
    }
    for (const lab of opts.xlabels || []) {
      svg("text", { x: X(lab.i), y: h - 1, "text-anchor": lab.anchor || "middle", class: "axis-t", style: "font:500 9px var(--mono);fill:var(--faint)" }, root).textContent = lab.text;
    }
    if (opts.endDot) {
      for (const s of series) {
        const i = s.values.length - 1;
        if (s.values[i] != null) svg("circle", { cx: X(i), cy: Y(s.values[i]), r: 3, fill: s.color }, root);
      }
    }
    return { root, X, Y, paths };
  }

  // ---- animated number
  function tween(node, to, fmt, ms = 700) {
    const from = Number(node.dataset.v || 0);
    node.dataset.v = to;
    if (from === to || !isFinite(from)) { node.textContent = fmt(to); return; }
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 3);
      node.textContent = fmt(from + (to - from) * e);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    if (Math.abs(to - from) > 1e-6) { node.classList.remove("flash"); void node.offsetWidth; node.classList.add("flash"); }
  }

  // ---- feed
  function feedItem(t, marketsById, fresh) {
    const li = el("li", fresh ? "fresh" : "");
    li.append(avatar(t.trader));
    const mid = el("div");
    const who = el("div", "who");
    who.append(document.createTextNode(`${trader(t.trader).name} `));
    who.append(el("span", `side ${t.side}`, t.side));
    mid.append(who);
    const m = marketsById.get(t.qid);
    mid.append(el("div", "what", shortTitle(m) || t.qid));
    const why = rationaleFor(t, marketsById);
    if (why) mid.append(el("div", "why", `“${why}”`));
    li.append(mid);
    const right = el("div");
    right.append(el("div", "amt", `${cr(t.spend)} cr`));
    right.append(el("div", "mv", `${pct(t.price_before)}→${pct(t.price_after)}`));
    right.append(el("div", "mv", ago(t.at)));
    li.append(right);
    return li;
  }

  // ---- bets flow: traders (left) -> markets (right, YES/NO pools), bands by stake, particles per trade
  class Flow {
    constructor(host, opts = {}) {
      this.host = host;
      this.w = opts.w || 760;
      this.h = opts.h || 440;
      this.maxMarkets = opts.maxMarkets || 7;
      this.svg = svg("svg", { viewBox: `0 0 ${this.w} ${this.h}` }, host);
      this.gBands = svg("g", {}, this.svg);
      this.gNodes = svg("g", {}, this.svg);
      this.gParticles = svg("g", {}, this.svg);
      this.stakes = new Map(); // `${trader}|${qid}` -> credits
      this.left = [];
      this.right = [];
      this.pos = new Map();
    }
    layout(state) {
      const trades = this._all || [];
      const vol = new Map();
      for (const m of state.markets) vol.set(m.qid, m.volume || 0);
      const recent = new Map();
      trades.slice(-60).forEach((t, i) => recent.set(t.qid, (recent.get(t.qid) || 0) + t.spend * (1 + i / 30)));
      const mk = [...state.markets].filter((m) => m.status === "open")
        .sort((a, b) => (recent.get(b.qid) || 0) - (recent.get(a.qid) || 0) || (b.volume || 0) - (a.volume || 0)).slice(0, this.maxMarkets);
      const keep = new Set(this.right.map((m) => m.qid));
      const same = mk.length === this.right.length && mk.every((m) => keep.has(m.qid));
      if (same && this.left.length) { this.right = this.right.map((r) => mk.find((m) => m.qid === r.qid)); return false; }
      this.right = mk;
      const left = (state.personas || []).map((p) => `agent:${p.id}`);
      left.push("ext:*", "user:*");
      this.left = left;
      return true;
    }
    draw(state, trades) {
      this._all = trades;
      this.state = state;
      const changed = this.layout(state);
      const W = this.w, Hh = this.h;
      const lx = 178, rx = W - 250;
      if (changed) {
        this.gNodes.innerHTML = "";
        this.pos.clear();
        const ls = (Hh - 20) / this.left.length;
        this.left.forEach((id, i) => {
          const y = 14 + ls * (i + 0.5);
          this.pos.set(id, { x: lx, y });
          const t = id === "ext:*" ? { name: "Outside agents", color: "#495057", mono: "EX" } : id === "user:*" ? { name: "Humans (QR)", color: "var(--ink)", mono: "YOU" } : trader(id);
          svg("rect", { x: lx - 4, y: y - 9, width: 8, height: 18, rx: 3, fill: t.color }, this.gNodes);
          const tx = svg("text", { x: lx - 12, y: y + 4, "text-anchor": "end", class: "node-label" }, this.gNodes);
          tx.textContent = t.name;
        });
        const rs = (Hh - 20) / Math.max(1, this.right.length);
        this.right.forEach((m, i) => {
          const y = 14 + rs * (i + 0.5);
          const hh = Math.min(46, rs - 10);
          this.pos.set(m.qid, { x: rx, y, h: hh });
          const g = svg("g", { "data-q": m.qid }, this.gNodes);
          svg("rect", { x: rx, y: y - hh / 2, width: 10, height: hh, rx: 3, fill: "var(--bg2)" }, g);
          svg("rect", { x: rx, y: y - hh / 2, width: 10, height: hh * m.p_yes, rx: 3, fill: "var(--yes)", class: "pool-yes" }, g);
          svg("rect", { x: rx, y: y - hh / 2 + hh * m.p_yes, width: 10, height: hh * (1 - m.p_yes), rx: 3, fill: "var(--no)", opacity: 0.85, class: "pool-no" }, g);
          const t1 = svg("text", { x: rx + 18, y: y - 3, class: "node-label" }, g);
          t1.textContent = this._clip(shortTitle(m).replace(/ \(now [^)]*\)/, ""), 36);
          const t2 = svg("text", { x: rx + 18, y: y + 11, class: "node-sub" }, g);
          t2.textContent = `${pct(m.p_yes)} YES · ${cr(m.volume)} cr`;
          t2.setAttribute("class", "node-sub pct");
        });
      } else {
        for (const m of this.right) {
          const g = this.gNodes.querySelector(`[data-q="${CSS.escape(m.qid)}"]`);
          const p = this.pos.get(m.qid);
          if (!g || !p) continue;
          const hh = p.h;
          g.querySelector(".pool-yes").setAttribute("height", hh * m.p_yes);
          const no = g.querySelector(".pool-no");
          no.setAttribute("y", p.y - hh / 2 + hh * m.p_yes);
          no.setAttribute("height", hh * (1 - m.p_yes));
          g.querySelector(".pct").textContent = `${pct(m.p_yes)} YES · ${cr(m.volume)} cr`;
        }
      }
      this._bands(trades);
    }
    _clip(s, n) { return s.length > n ? `${s.slice(0, n - 1)}…` : s; }
    _group(id) { return id.startsWith("user:") ? "user:*" : id.startsWith("ext:") ? "ext:*" : id; }
    _path(from, to, side) {
      const a = this.pos.get(from), b = this.pos.get(to);
      if (!a || !b) return null;
      const by = side === "YES" ? b.y - b.h / 4 : b.y + b.h / 4;
      const mx = (a.x + b.x) / 2;
      return `M${a.x + 4},${a.y} C${mx},${a.y} ${mx},${by} ${b.x},${by}`;
    }
    _bands(trades) {
      const stake = new Map();
      for (const t of trades) {
        if (!this.pos.has(t.qid)) continue;
        const k = `${this._group(t.trader)}|${t.qid}|${t.side}`;
        stake.set(k, (stake.get(k) || 0) + t.spend);
      }
      const max = Math.max(1, ...stake.values());
      this.gBands.innerHTML = "";
      for (const [k, v] of stake) {
        const [from, qid, side] = k.split("|");
        const d = this._path(from, qid, side);
        if (!d) continue;
        const color = from === "user:*" ? "var(--ink)" : from === "ext:*" ? "#495057" : trader(from).color;
        svg("path", { d, fill: "none", stroke: color, "stroke-width": Math.max(1, 14 * Math.sqrt(v / max)), opacity: 0.16, "stroke-linecap": "round" }, this.gBands);
      }
    }
    pulse(t) {
      const from = this._group(t.trader);
      const d = this._path(from, t.qid, t.side);
      if (!d) return;
      const color = from === "user:*" ? "var(--heat)" : from === "ext:*" ? "#495057" : trader(t.trader).color;
      const guide = svg("path", { d, fill: "none", stroke: color, "stroke-width": 2, opacity: 0.55 }, this.gParticles);
      const len = guide.getTotalLength();
      guide.setAttribute("stroke-dasharray", `${len}`);
      guide.setAttribute("stroke-dashoffset", `${len}`);
      const r = Math.max(4, Math.min(11, 3 + Math.sqrt(t.spend)));
      const dot = svg("circle", { r, fill: color, stroke: "var(--panel)", "stroke-width": 2 }, this.gParticles);
      const t0 = performance.now(), dur = 1300;
      const step = (now) => {
        const k = Math.min(1, (now - t0) / dur), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
        const p = guide.getPointAtLength(e * len);
        dot.setAttribute("cx", p.x); dot.setAttribute("cy", p.y);
        guide.setAttribute("stroke-dashoffset", `${len * (1 - e)}`);
        if (k < 1) requestAnimationFrame(step);
        else {
          dot.animate([{ r, opacity: 1 }, { r: r * 2.4, opacity: 0 }], { duration: 500 }).onfinish = () => dot.remove();
          guide.animate([{ opacity: 0.55 }, { opacity: 0 }], { duration: 900 }).onfinish = () => guide.remove();
        }
      };
      requestAnimationFrame(step);
    }
  }

  // ---- map
  function riskMap(id, opts = {}) {
    const map = L.map(id, { zoomSnap: 0.25, zoomControl: !opts.static, scrollWheelZoom: false, attributionControl: true, worldCopyJump: true })
      .setView(opts.center || [44, 18], opts.zoom || 4);
    let tiles = null;
    const setTiles = () => {
      if (tiles) map.removeLayer(tiles);
      tiles = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 18, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      }).addTo(map);
    };
    setTiles();
    document.addEventListener("harvest-theme", setTiles);
    const layer = L.layerGroup().addTo(map);
    const arcLayer = L.layerGroup().addTo(map);
    let geo = null;
    const arcPts = (a, b) => {
      const pts = [], n = 40;
      const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
      const dx = b[1] - a[1], dy = b[0] - a[0];
      const c = [mx + dx * 0.25, my - dy * 0.25];
      for (let i = 0; i <= n; i++) {
        const t = i / n;
        pts.push([(1 - t) ** 2 * a[0] + 2 * (1 - t) * t * c[0] + t * t * b[0], (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * c[1] + t * t * b[1]]);
      }
      return pts;
    };
    let fitted = false;
    function draw(g, fund, markets) {
      geo = g || geo;
      if (!geo) return;
      if (!fitted && opts.fit !== false) {
        fitted = true;
        const pts = (geo.regions || []).concat(geo.buyers || []).map((p) => [p.lat, p.lng]);
        if (pts.length) map.fitBounds(pts, { padding: [18, 18] });
      }
      layer.clearLayers();
      arcLayer.clearLayers();
      const alloc = new Map(((fund && fund.allocations) || []).map((a) => [`${a.region}|${a.crop}`, a]));
      const maxAmt = Math.max(1e-9, ...[...alloc.values()].map((a) => a.amount));
      // risk for regions with no open market: none; draw small grey
      for (const o of geo.origins || []) {
        L.circleMarker([o.lat, o.lng], { radius: 5, color: "#868e96", weight: 1, fillColor: "#adb5bd", fillOpacity: 0.6 })
          .bindTooltip(`Grows here: ${o.name}`).addTo(layer);
      }
      const byId = new Map();
      for (const r of geo.regions || []) {
        byId.set(r.id, r);
        const a = alloc.get(`${r.region}|${r.crop}`);
        const risk = a ? a.risk : null;
        const color = risk == null ? "#868e96" : riskColor(risk);
        const radius = (a ? 7 + 17 * Math.sqrt(a.amount / maxAmt) : 6) * (opts.scale || 1);
        const qs = a ? a.questions.map((q) => (markets && markets.get(q)) || { title: q, p_yes: null }) : [];
        const html = `<b>${r.label}</b><br><span style="opacity:.75">${r.region} · ${r.crop}</span><br>` +
          (a ? `Market-implied risk <b>${pct(risk)}</b> · fund share <b>${pct(a.share, 1)}</b> (${cr(a.amount, 2)} cr)<br>` +
            qs.slice(0, 4).map((m) => `<span style="font-size:12px">• ${m.title} <b>${pct(m.p_yes)}</b></span>`).join("<br>") : "no open market");
        L.circleMarker([r.lat, r.lng], { radius: radius + 6 * (opts.scale || 1), stroke: false, fillColor: color, fillOpacity: 0.18 }).addTo(layer);
        L.circleMarker([r.lat, r.lng], { radius, color: "#fff", weight: 1.5, fillColor: color, fillOpacity: 0.88 }).bindPopup(html).bindTooltip(`${r.region} · ${r.crop}: ${risk == null ? "–" : pct(risk)} risk`).addTo(layer);
      }
      const buyers = new Map((geo.buyers || []).map((b) => [b.id, b]));
      for (const b of buyers.values()) {
        L.circleMarker([b.lat, b.lng], { radius: 7, color: "#fff", weight: 1.5, fillColor: "#7048e8", fillOpacity: 0.95 }).bindTooltip(`Sovereign buyer: ${b.name}`).addTo(arcLayer);
      }
      for (const arc of geo.arcs || []) {
        const b = buyers.get(arc.from);
        const to = arc.to_point || byId.get(arc.to);
        if (!b || !to) continue;
        const pts = arcPts([b.lat, b.lng], [to.lat, to.lng]);
        L.polyline(pts, { color: "#7048e8", weight: arc.kind === "holding" ? 2.6 : 1.8, opacity: 0.85, className: "arc-path", dashArray: arc.kind === "holding" ? null : "3 7" })
          .bindTooltip(arc.label).addTo(arcLayer);
        if (arc.to_point) L.circleMarker([to.lat, to.lng], { radius: 4, color: "#7048e8", weight: 1, fillColor: "#b197fc", fillOpacity: 0.9 }).bindTooltip(arc.to_point.name).addTo(arcLayer);
      }
    }
    return { map, draw };
  }

  // ---- fund
  function renderFund(host, fund, opts = {}) {
    host.innerHTML = "";
    const allocs = [...(fund.allocations || [])].sort((a, b) => b.amount - a.amount).slice(0, opts.max || 12);
    const max = Math.max(1e-9, ...allocs.map((a) => a.share));
    for (const a of allocs) {
      const row = el("div", "row");
      row.append(el("div", "lbl", `${a.region} · ${a.crop}`));
      const bar = el("div", "bar");
      const i = el("i");
      i.style.width = `${(100 * a.share) / max}%`;
      i.style.background = riskColor(a.risk);
      i.title = `risk ${pct(a.risk)}`;
      bar.append(i);
      row.append(bar);
      row.append(el("div", "amt", `${cr(a.amount, 1)}`));
      host.append(row);
    }
    if (!allocs.length) host.append(el("div", "empty", "No open markets yet."));
  }

  function toast(msg, ms = 2600) {
    let t = document.querySelector(".toast");
    if (!t) { t = el("div", "toast"); document.body.append(t); }
    t.textContent = msg;
    t.classList.add("on");
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove("on"), ms);
  }

  return { el, svg, api, pct, cr, signed, b3, short, ago, riskColor, initTheme, isDark, registerPersonas, registerTraders, trader, avatar,
    personas, COMMODITY, shortTitle, rationaleFor, lineChart, tween, feedItem, Flow, riskMap, renderFund, toast, MONO };
})();
