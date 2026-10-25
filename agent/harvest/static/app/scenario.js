"use strict";
/* /app/scenario: climate what-if sliders. POST /harvest/api/scenario on every change (debounced); the house world models
   re-forecast every open market and the page shows each market's probability moving from baseline to scenario. */
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
  const pts = (d) => `${d > 0 ? "+" : ""}${(d * 100).toFixed(1)} pts`;
  async function call(path, body) {
    const r = await fetch(API + path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `Something went wrong (HTTP ${r.status})`);
    return data;
  }

  async function boot() {
    const main = document.getElementById("main");
    let spec;
    try { spec = await call("/scenario"); } catch (e) { main.replaceChildren(h("p", { class: "err" }, e.message)); return; }
    const inputs = {};
    function slider(id, label, cfg, unit) {
      const out = h("output", {});
      const inp = h("input", { type: "range", id, min: cfg.min, max: cfg.max, step: cfg.step, value: 0, "aria-label": label });
      const draw = () => { const v = Number(inp.value); out.textContent = unit(v); };
      inp.addEventListener("input", () => { draw(); schedule(); });
      draw();
      inputs[id] = { inp, draw };
      return h("div", { class: "sl" }, h("label", { for: id }, label), inp, out);
    }
    const signed = (u) => (v) => `${v > 0 ? "+" : ""}${v}${u}`;
    const temps = spec.regions.map((r) => slider(`t-${r}`, r, spec.temp_anomaly, signed("°C")));
    const rain = slider("rain", "Rainfall", spec.rain_pct, signed("%"));
    const enso = slider("enso", "El Niño", spec.enso, (v) => (v === 0 ? "neutral" : `${v > 0 ? "+" : ""}${v}`));
    const status = h("p", { class: "muted small" });
    const results = h("div");
    const presets = h("div", { class: "chips" }, Object.entries(spec.presets || {}).map(([name, p]) => h("button", { class: "chip", onclick: () => apply(p) }, name)));
    main.replaceChildren(
      h("h1", {}, "What if?"),
      h("p", { class: "lead" }, "Move the sliders to imagine a hotter, drier or El Niño year. The AI agents re-forecast every live market under your scenario, each through its own world model."),
      presets,
      h("h2", {}, "Temperature anomaly this season"), ...temps,
      h("h2", {}, "Rain and ocean"), rain, enso,
      h("p", { class: "muted small" }, "Rainfall is the change vs a normal season. El Niño above zero means a warm Pacific (droughts in West Africa and Asia), below zero La Niña."),
      h("h2", {}, "How the markets would move"), status, results);

    function body() {
      const t = {};
      for (const r of spec.regions) t[r] = Number(inputs[`t-${r}`].inp.value);
      return { temp_anomaly: t, rain_pct: Number(inputs.rain.inp.value), enso: Number(inputs.enso.inp.value) };
    }
    function apply(p) {
      for (const r of spec.regions) inputs[`t-${r}`].inp.value = (p.temp_anomaly || {})[r] || 0;
      inputs.rain.inp.value = p.rain_pct || 0;
      inputs.enso.inp.value = p.enso || 0;
      Object.values(inputs).forEach((x) => x.draw());
      schedule(0);
    }
    let timer = null, seq = 0, retry = null;
    function schedule(ms = 450) { clearTimeout(timer); timer = setTimeout(run, ms); }
    async function run(isRetry) {
      const my = ++seq;
      clearTimeout(retry);
      status.replaceChildren(h("span", { class: "spin" }), "The agents are re-forecasting under your scenario…");
      try {
        const r = await call("/scenario", body());
        if (my !== seq) return;
        draw(r);
        if (r.llm_pending && !isRetry) retry = setTimeout(() => run(true), 4000);
      } catch (e) { if (my === seq) status.replaceChildren(h("span", { class: "err" }, e.message)); }
    }
    function draw(r) {
      const who = r.source === "llm" ? `AI agents (${r.llm_model || "LLM"}) on the most exposed markets, world-model rules on the rest`
        : "world-model rules (the AI agents are busy; try again in a moment for their own words)";
      status.textContent = `${r.markets.length} live markets re-forecast in ${(r.elapsed_ms / 1000).toFixed(1)} s by ${who}.`;
      results.replaceChildren(...r.markets.map((m) => {
        const lo = Math.min(m.baseline, m.scenario), hi = Math.max(m.baseline, m.scenario);
        const cls = Math.abs(m.delta) < 0.005 ? "" : m.delta > 0 ? "up" : "down";
        return h("div", { class: "move" },
          h("a", { class: "q", href: `/app/market/${encodeURIComponent(m.qid)}` }, m.title),
          h("div", { class: "muted small" }, `${m.region} · ${m.crop}${m.drivers.length ? " · " + m.drivers.join(", ") : ""}`),
          h("div", { class: "nums" }, `Now ${pct(m.baseline)} → in this scenario ${pct(m.scenario)} `, h("span", { class: cls }, `(${m.delta > 0 ? "▲" : m.delta < 0 ? "▼" : ""} ${pts(m.delta)})`)),
          h("div", { class: "track", "aria-hidden": "true" },
            h("div", { class: "span", style: `left:${lo * 100}%;width:${(hi - lo) * 100}%` }),
            h("div", { class: "dot base", style: `left:${m.baseline * 100}%` }),
            h("div", { class: "dot scen", style: `left:${m.scenario * 100}%` })),
          (m.rationales || []).filter((x) => Math.abs(x.delta) >= 0.005).map((x) => h("p", { class: "r" }, h("b", {}, `${x.name} (${pts(x.delta)}): `), x.text)));
      }));
    }
    apply(Object.values(spec.presets || {})[0] || {});
  }
  boot();
})();
