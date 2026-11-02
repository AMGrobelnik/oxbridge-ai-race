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

  var cid = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
  var sid = String(Math.floor(Date.now() / 1000)), hit = 0, last = "";

  function send() {
    var page = location.href;
    if (page === last) return;
    last = page;
    hit += 1;
    var q = new URLSearchParams({
      v: "2", tid: ID, cid: cid, sid: sid, sct: "1", seg: "0", _s: String(hit), _p: sid + hit,
      en: "page_view", dl: page, dt: document.title, ul: (navigator.language || "").toLowerCase(),
      sr: screen.width + "x" + screen.height, npa: "1", _ee: "1"
    });
    if (hit === 1) { q.set("_ss", "1"); q.set("_fv", "1"); q.set("_nsi", "1"); if (document.referrer) q.set("dr", document.referrer); }
    navigator.sendBeacon(URL + "?" + q.toString());
  }

  send();
  addEventListener("hashchange", send);  // the site routes by hash (#live, #markets, #m-<qid>)
})();
