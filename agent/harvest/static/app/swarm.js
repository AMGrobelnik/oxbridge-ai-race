// Agent swarm counter on the Live page: "N AI agents, M bets in the last hour" and the latest swarm bets.
(function () {
  const box = document.createElement("section");
  box.id = "swarm";
  box.style.cssText = "box-sizing:border-box;width:calc(100% - 32px);max-width:728px;margin:8px auto;padding:8px 12px;border:1px solid #ccc;border-radius:8px;font:14px system-ui,sans-serif";
  const main = document.getElementById("main");
  main.parentNode.insertBefore(box, main);
  const text = (tag, s, css) => { const e = document.createElement(tag); e.textContent = s; if (css) e.style.cssText = css; return e; };
  async function tick() {
    try {
      const r = await fetch("/harvest/api/swarm", { cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      const head = text("div", "");
      head.append(text("b", `\u{1F916} ${s.agents} AI agents, ${s.bets_last_hour} bets in the last hour`),
        text("span", ` (agent swarm; every bet is in the public ledger)`, "color:#666"));
      const list = document.createElement("div");
      list.style.cssText = "font-size:12px;color:#444;margin-top:4px";
      for (const b of s.recent.slice(0, 6)) {
        list.append(text("div", `${b.name} bet ${b.spend} ${b.side} on ${b.qid} → ${Math.round(b.price_after * 100)}%`));
      }
      box.replaceChildren(head, list);
    } catch (e) { /* the counter is optional */ }
  }
  tick();
  setInterval(() => { if (!document.hidden) tick(); }, 10000);
})();
