"use strict";
/* "Why did this move?" on /app/market/{qid}: once app.js has drawn the price, adds a button that asks
   GET /harvest/api/markets/{qid}/why and shows Croppy's explanation with the bets and headlines it cites. */
(() => {
  const qid = decodeURIComponent(location.pathname.replace(/^\/app\/market\//, "").replace(/\/+$/, ""));
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
  const pts = (d) => `${d > 0 ? "+" : ""}${(d * 100).toFixed(1)} pts`;
  const betRow = (b) => h("div", { class: "feed-item" }, h("span", { class: "tag " + b.side.toLowerCase() }, b.side), h("b", {}, b.name),
    ` bet ${Math.round(b.spend)} credits, ${Math.round(b.price_before * 100)}% → ${Math.round(b.price_after * 100)}% (${pts(b.delta)})`,
    h("span", { class: "muted small" }, ` · ledger bet #${b.seq}`));
  function render(out, r) {
    const mv = r.move;
    const topSeqs = new Set(mv.top.map((b) => b.seq));
    const rest = (mv.bets || []).filter((b) => !topSeqs.has(b.seq));
    out.replaceChildren(h("div", { class: "card" },
      h("b", {}, "🌾 Croppy: "), r.explanation,
      mv.top.length ? [h("h2", { style: "font-size:18px;margin:14px 0 6px" }, "The bets that moved it most"),
        ...mv.top.map(betRow),
        rest.length ? h("details", { style: "margin-top:6px" }, h("summary", { class: "small", style: "cursor:pointer" }, `The other ${rest.length} bets Croppy read`), ...rest.map(betRow)) : null] : null,
      r.news.length ? [h("h2", { style: "font-size:18px;margin:14px 0 6px" }, r.news.some((n) => n.in_window) ? "Headlines in the window" : "Latest tagged headlines"),
        ...r.news.map((n) => h("div", { class: "feed-item" }, n.url ? h("a", { href: n.url, target: "_blank", rel: "noopener" }, n.title) : n.title,
          h("span", { class: "muted small" }, ` · ${n.source || ""} ${String(n.published_at || "").slice(0, 10)}`)))] : null));
  }
  function addButton(anchor) {
    const out = h("div");
    const btn = h("button", { class: "btn", id: "why-btn", style: "margin-top:12px", onclick: async () => {
      btn.disabled = true;
      out.replaceChildren(h("p", { class: "muted" }, h("span", { class: "spin" }), "Croppy is reading the ledger and the news…"));
      try {
        const r = await fetch(`/harvest/api/markets/${encodeURIComponent(qid)}/why`);
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
        render(out, data);
        // on a phone the answer lands below the fold: bring its start into view
        if (out.getBoundingClientRect().top > window.innerHeight * 0.6) out.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (e) { out.replaceChildren(h("p", { class: "err" }, e.message)); }
      btn.disabled = false;
    } }, "Why did this move?");
    anchor.after(btn, out);
  }
  const main = document.getElementById("main");
  const tryAdd = () => {
    if (document.getElementById("why-btn")) return true;
    const svg = main.querySelector("svg.line");
    if (!svg) return false;
    addButton(svg.parentElement);
    return true;
  };
  if (!tryAdd()) {
    const obs = new MutationObserver(() => { if (tryAdd()) obs.disconnect(); });
    obs.observe(main, { childList: true, subtree: true });
  }
})();
