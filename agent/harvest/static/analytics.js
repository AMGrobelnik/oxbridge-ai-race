/* Cookieless Google Analytics (GA4) for crophecy.bid only. Nothing is stored on the visitor's
   device: no cookies, localStorage or sessionStorage, and gtag.js is never loaded (GA4's gtag
   ignores client_storage 'none' and writes _ga cookies whenever analytics storage is granted, and
   with storage denied its cookieless pings never reach the reports). Instead each page load sends
   GA4 page_view hits straight to the collection endpoint with a random client id that lives only
   in this page's memory, so visits are counted but never linked across page loads or days. No ads
   signals: no Google signals, no ad personalisation, no user id. */
(function () {
  var ID = "G-J6Q2K51RVP", URL = "https://www.google-analytics.com/g/collect";
  if (!/(^|\.)crophecy\.bid$/.test(location.hostname) || !navigator.sendBeacon) return;

  function rand() { return String(1 + Math.floor(Math.random() * 2147483646)); }
  // client id, session id and page id: random per page load, kept only in this closure
  var cid = rand() + "." + Math.floor(Date.now() / 1000), sid = rand(), pid = rand(), hit = 0, last = "";

  function send() {
    var page = location.href;  // the full URL, utm_* parameters included
    var view = page.replace(/#home$/, "");  // "/" and "/#home" are the same view
    if (view === last) return;
    last = view;
    hit += 1;
    var q = new URLSearchParams({
      v: "2", tid: ID, cid: cid, sid: sid, sct: "1", seg: "1", _s: String(hit), _p: pid,
      en: "page_view", dl: page, dt: document.title, ul: (navigator.language || "").toLowerCase(),
      sr: screen.width + "x" + screen.height, npa: "1", _ee: "1"
    });
    if (hit === 1) {  // a new user and a new session on every page load, since nothing is remembered
      q.set("_fv", "1"); q.set("_ss", "1"); q.set("_nsi", "1");
      if (document.referrer) q.set("dr", document.referrer);
    }
    navigator.sendBeacon(URL + "?" + q.toString());
  }

  send();
  // The site switches views with history.replaceState('#<route>') (also once right after load, which
  // rewrites "/" to "/#home"), back/forward fire popstate, and some views route by hash (#live,
  // #markets, #m-<qid>). Each sends a page_view for the new URL; send() drops a repeat of the view
  // just sent ("/" and "/#home" count as one), so the load-time rewrite and hashchange firing
  // alongside popstate each count once.
  // The hit is deferred a tick so document.title already reflects the new view.
  function later() { setTimeout(send, 0); }
  ["pushState", "replaceState"].forEach(function (m) {
    var orig = history[m];
    history[m] = function () { var r = orig.apply(this, arguments); later(); return r; };
  });
  addEventListener("popstate", later);
  addEventListener("hashchange", later);
})();
