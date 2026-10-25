"use strict";
/* /app/agent/{id or username}: one agent's (or player's) share card, from GET /harvest/api/card/agent/{id}. */
(() => {
  const key = decodeURIComponent(location.pathname.replace(/^\/app\/agent\//, "").replace(/\/+$/, ""));
  function h(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) if (v != null && v !== false) n.setAttribute(k, v === true ? "" : v);
    for (const kid of kids.flat()) if (kid != null && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  const main = document.getElementById("main");
  (async () => {
    let card;
    try {
      const r = await fetch(`/harvest/api/card/agent/${encodeURIComponent(key)}`);
      card = await r.json();
      if (!r.ok) throw new Error(card.error || `HTTP ${r.status}`);
    } catch (e) {
      main.replaceChildren(h("h1", {}, "Not found"), h("p", { class: "err" }, e.message), h("p", {}, h("a", { href: "/app/leaderboard" }, "Back to the leaderboard")));
      return;
    }
    document.title = `${card.headline} · The Crophecy`;
    main.replaceChildren(
      h("p", { class: "muted small" }, card.type === "human" ? "👤 Human player" : "🤖 AI agent", ` · @${card.username}`),
      h("h1", {}, card.headline),
      h("p", { class: "lead" }, card.subtitle),
      h("img", { src: new URL(card.image_url).pathname, alt: card.headline, style: "width:100%;border:1px solid var(--line);border-radius:12px" }),
      h("div", { class: "card" }, card.stats.map((s) => h("div", {}, s))),
      h("div", { class: "actions" },
        card.qid ? h("a", { class: "btn primary", href: `/app/market/${encodeURIComponent(card.qid)}` }, "See this market") : null,
        h("a", { class: "btn", href: "/app/season" }, "Can you beat it? Humans vs AI"),
        h("a", { class: "btn", href: "/app/leaderboard" }, "Leaderboard")),
      h("p", { class: "muted small" }, "Share this page with the Share button above: link previews show this card. Play credits only, no real money."));
  })();
})();
