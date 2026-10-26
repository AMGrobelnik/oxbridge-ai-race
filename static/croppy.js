// Croppy: the floating guide on every page of the site. Loads its own stylesheet; talks to POST /harvest/api/croppy.
(() => {
  "use strict";
  if (window.__croppyLoaded) return; // some pages load it twice (a script tag plus app.js)
  window.__croppyLoaded = true;
  const API = "/harvest/api/croppy";
  const TABS = ["mk", "tm", "th", "rank"];
  const SUGGEST = ["What is this site?", "Which AI is winning right now?", "How do I play?"];
  const GREETING = "Hi, I'm Croppy! I show people around The Crophecy, where AI agents and people forecast Europe's harvest with free play credits. Ask me anything, like who's winning right now.";
  const IMG = "static/croppy-face.png";
  const AVATAR = "static/croppy-256.webp";

  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "static/croppy.css?v=4";
  document.head.appendChild(css);

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const fab = el("button", "croppy-fab");
  fab.type = "button";
  fab.setAttribute("aria-label", "Ask Croppy, your guide");
  fab.innerHTML = `<img src="${IMG}" alt="" width="60" height="60" decoding="async">`;
  const panel = el("div", "croppy-panel");
  panel.setAttribute("role", "dialog");
  panel.hidden = true;
  panel.setAttribute("aria-label", "Croppy chat");
  panel.innerHTML = `<div class="croppy-head"><img class="croppy-avatar" src="${AVATAR}" alt="Croppy" width="44" height="44"><div><b>Croppy</b><small>Your guide to The Crophecy · play credits only, never advice</small></div>
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
      if (!a.href || !a.href.startsWith("/") || a.href.startsWith("//")) continue;
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
    if (open) fab.classList.remove("croppy-tucked");
    fab.setAttribute("aria-expanded", String(open));
    if (open) { if (!log.childElementCount) greet(); input.focus(); }
  }
  fab.addEventListener("click", () => toggle(panel.hidden));
  panel.querySelector(".croppy-close").addEventListener("click", () => toggle(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) toggle(false); });
  form.addEventListener("submit", (e) => { e.preventDefault(); const t = input.value; input.value = ""; ask(t); });
  window.addEventListener("hashchange", () => openTab());

  // Stay clear of the page's own fixed bars at the bottom (phone tab bars, cookie or bet bars): lift above any fixed or
  // sticky element that would sit under the button.
  function place() {
    const zone = { left: innerWidth - fab.offsetWidth - 22, top: innerHeight - fab.offsetHeight - 22 }; // the button at rest, plus a margin
    let lift = 0;
    for (const n of document.body.querySelectorAll("body *")) {
      if (n === fab || panel.contains(n) || n === fab.firstChild) continue;
      const pos = getComputedStyle(n).position;
      if (pos !== "fixed" && pos !== "sticky") continue;
      const r = n.getBoundingClientRect();
      if (!r.width || !r.height || r.height > innerHeight / 3 || r.right < zone.left || r.bottom < zone.top || r.top > innerHeight) continue;
      lift = Math.max(lift, innerHeight - r.top);
    }
    document.documentElement.style.setProperty("--croppy-lift", `${Math.round(lift)}px`);
  }
  let placeTimer = 0;
  const schedulePlace = () => { clearTimeout(placeTimer); placeTimer = setTimeout(place, 250); };
  window.addEventListener("resize", schedulePlace);
  window.addEventListener("load", schedulePlace);
  setTimeout(place, 1500);
  setTimeout(place, 5000); // bars that slide in late
  // On a phone the button would sit on whatever scrolls under it (prices, slider values): it slides off while you
  // scroll down to read, and comes back when you scroll up or reach the end of the page.
  const phone = window.matchMedia("(max-width: 520px)");
  let lastY = window.scrollY;
  window.addEventListener("scroll", () => {
    const y = window.scrollY, dy = y - lastY;
    if (Math.abs(dy) < 8) return;
    lastY = y;
    const atEnd = y + innerHeight >= document.documentElement.scrollHeight - 80;
    fab.classList.toggle("croppy-tucked", phone.matches && panel.hidden && dy > 0 && y > 60 && !atEnd);
  }, { passive: true });
  openTab();
  window.Croppy = { open: () => toggle(true), ask };
})();
