// Croppy: a floating guide on the /harvest pages. Loads its own stylesheet; talks to POST /harvest/api/croppy.
(() => {
  "use strict";
  const API = "/harvest/api/croppy";
  const TABS = ["mk", "tm", "th", "rank"];
  const SUGGEST = ["How do I bet?", "What's a world model?", "Write my theory"];
  const GREETING = "Hi, I'm Croppy! I can show you around The Crophecy, where AI world models and people forecast Europe's harvest with free play credits. What would you like to know?";
  const MASCOT = `<svg viewBox="0 0 48 48" aria-hidden="true"><g class="croppy-mascot">
    <path d="M24 46V26" stroke="#5c940d" stroke-width="3" stroke-linecap="round"/>
    <path d="M24 36c-6-1-10-5-11-10 6 0 10 4 11 10zM24 32c5-1 9-4 10-9-5 0-9 3-10 9z" fill="#82c91e"/>
    <ellipse cx="24" cy="17" rx="9" ry="13" fill="#f2c94c" stroke="#c58b11" stroke-width="1.5"/>
    <path d="M18 9l-3-6M30 9l3-6M24 4V0" stroke="#c58b11" stroke-width="1.5" stroke-linecap="round"/>
    <circle cx="20.5" cy="16" r="2.2" fill="#1c1a14"/><circle cx="27.5" cy="16" r="2.2" fill="#1c1a14"/>
    <circle cx="21.2" cy="15.3" r=".7" fill="#fff"/><circle cx="28.2" cy="15.3" r=".7" fill="#fff"/>
    <path d="M21 21.5q3 2.5 6 0" stroke="#1c1a14" stroke-width="1.4" fill="none" stroke-linecap="round"/>
    <circle cx="17.5" cy="20" r="1.6" fill="#f08c8c" opacity=".7"/><circle cx="30.5" cy="20" r="1.6" fill="#f08c8c" opacity=".7"/>
  </g></svg>`;

  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/harvest/static/croppy.css";
  document.head.appendChild(css);

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const fab = el("button", "croppy-fab");
  fab.type = "button";
  fab.setAttribute("aria-label", "Ask Croppy, your guide");
  fab.innerHTML = MASCOT;
  const panel = el("div", "croppy-panel");
  panel.setAttribute("role", "dialog");
  panel.hidden = true;
  panel.setAttribute("aria-label", "Croppy chat");
  panel.innerHTML = `<div class="croppy-head">${MASCOT}<div><b>Croppy</b><small>Your guide · play credits only, never advice</small></div>
    <button class="croppy-close" type="button" aria-label="Close">×</button></div>
    <div class="croppy-log" aria-live="polite"></div>
    <form class="croppy-form"><input maxlength="500" placeholder="Ask Croppy anything…" aria-label="Your question" autocomplete="off"><button type="submit">Ask</button></form>`;
  document.body.append(panel, fab);

  const log = panel.querySelector(".croppy-log");
  const form = panel.querySelector(".croppy-form");
  const input = form.querySelector("input");
  const send = form.querySelector("button");
  const history = [];
  let busy = false;

  const scroll = () => { log.scrollTop = log.scrollHeight; };
  function say(role, text) { const m = el("div", `croppy-msg ${role === "user" ? "user" : "bot"}`, text); log.appendChild(m); scroll(); return m; }
  function actions(list) {
    if (!list || !list.length) return;
    const row = el("div", "croppy-acts");
    for (const a of list) {
      if (!a.href || !a.href.startsWith("/harvest")) continue;
      const link = el("a", null, a.label);
      link.href = a.href;
      link.addEventListener("click", () => setTimeout(() => openTab(), 0));
      row.appendChild(link);
    }
    log.appendChild(row);
    scroll();
  }

  // /harvest/play#th etc. opens that tab (the play page itself has no hash routing).
  function openTab(tries = 0) {
    if (location.pathname !== "/harvest/play") return;
    const tab = location.hash.slice(1);
    if (!TABS.includes(tab)) return;
    const app = document.getElementById("app");
    if (!app || app.hidden) { if (tries < 10) setTimeout(() => openTab(tries + 1), 500); return; } // tabs exist once joined
    const b = document.querySelector(`#ptabs button[data-tab="${tab}"]`);
    if (b) b.click();
  }

  async function ask(text) {
    text = text.trim().slice(0, 500);
    if (!text || busy) return;
    busy = true;
    send.disabled = true;
    const sugg = log.querySelector(".croppy-sugg");
    if (sugg) sugg.remove();
    say("user", text);
    const dots = say("bot", "…");
    dots.classList.add("dots");
    fab.classList.add("croppy-thinking");
    panel.classList.add("croppy-thinking");
    let body;
    try {
      const r = await fetch(API, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: history.slice(-8), page: location.pathname + location.hash }) });
      body = await r.json();
      if (!r.ok) body = { reply: body.error || "Oops, I tripped over a furrow. Try again in a moment!" };
    } catch (_) {
      body = { reply: "I can't reach the field right now. Try again in a moment!" };
    }
    dots.remove();
    fab.classList.remove("croppy-thinking");
    panel.classList.remove("croppy-thinking");
    say("bot", body.reply);
    actions(body.actions);
    history.push({ role: "user", text }, { role: "croppy", text: body.reply });
    busy = false;
    send.disabled = false;
    input.focus();
  }

  function greet() {
    say("bot", GREETING);
    const row = el("div", "croppy-sugg");
    for (const q of SUGGEST) { const b = el("button", null, q); b.type = "button"; b.addEventListener("click", () => ask(q)); row.appendChild(b); }
    log.appendChild(row);
  }

  function toggle(open) {
    panel.hidden = !open;
    fab.setAttribute("aria-expanded", String(open));
    if (open) { if (!log.childElementCount) greet(); input.focus(); }
  }
  fab.addEventListener("click", () => toggle(panel.hidden));
  panel.querySelector(".croppy-close").addEventListener("click", () => toggle(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) toggle(false); });
  form.addEventListener("submit", (e) => { e.preventDefault(); const t = input.value; input.value = ""; ask(t); });
  window.addEventListener("hashchange", () => openTab());
  openTab();
  window.Croppy = { open: () => toggle(true), ask };
})();
