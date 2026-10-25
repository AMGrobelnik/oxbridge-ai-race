"use strict";
/* Adds a "Share" button to the header of a page: copies the page's canonical link (the absolute PUBLIC_BASE_URL one that
   the Open Graph tags carry), or opens the phone's share sheet when there is one. */
(() => {
  const bar = document.querySelector("#top .bar");
  if (!bar) return;
  const canonical = (document.querySelector('link[rel="canonical"]') || {}).href || location.href;
  const btn = document.createElement("button");
  btn.className = "chip sm";
  btn.id = "share-btn";
  btn.type = "button";
  btn.textContent = "Share";
  btn.title = "Copy a link to this page";
  const flash = (text) => { btn.textContent = text; setTimeout(() => { btn.textContent = "Share"; }, 2000); };
  btn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(canonical);
      flash("Link copied");
    } catch (_) {
      if (navigator.share) { try { await navigator.share({ title: document.title, url: canonical }); } catch (e) { /* cancelled */ } }
      else window.prompt("Copy this link", canonical);
    }
  });
  bar.append(btn);
})();
