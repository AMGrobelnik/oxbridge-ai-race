/* Google Analytics (GA4) for crophecy.bid, banner-free under the UK statistical-purposes exception
   (PECR Schedule A1 para 5, in force since 5 Feb 2026): site statistics only, no ads, visitors are
   told what is collected on /privacy/ and can object there with one click.

   gtag.js is not loaded. Each event is sent straight to GA's collection endpoint, with the same
   parameters gtag and GA4 enhanced measurement would send:
   - page_view: page address (GA reads utm_* campaign tags from it), title, referrer, language,
     screen size; one per view of the app (#home, #live, #markets, #m-<qid> ...);
   - user_engagement: foreground time on the page (_et), sent when the page is hidden or left, so GA
     reports engaged sessions and average engagement time (a view change carries the time so far);
   - scroll: once per view, when 90% of it has been seen;
   - click: links to other sites (link_url, link_domain, outbound);
   - file_download: links to documents (file_extension, file_name, link_url).
   A random client id and the current session are kept in one first-party localStorage item
   (crophecy_ga), the job gtag's _ga/_ga_<id> cookies do, so a visitor is one user across pages and
   visits and a session ends after 30 minutes without activity, as in GA. No cookies, no Google
   signals, no ad personalisation (npa=1), no user id, no fingerprinting. Nothing is sent or stored
   once the visitor objects (crophecy_ga_optout) or their browser sends Global Privacy Control. */
(function () {
  var ID = "G-J6Q2K51RVP", URL = "https://www.google-analytics.com/g/collect";
  var KEY = "crophecy_ga", OPT_OUT = "crophecy_ga_optout", DEBUG = "crophecy_ga_debug";
  var SESSION_MS = 30 * 60 * 1000, ENGAGED_MS = 10 * 1000;
  var OWN = /(^|\.)crophecy\.bid$/;
  // GA4 enhanced measurement's default file-download extensions
  var FILES = /\.(pdf|xlsx?|docx?|txt|rtf|csv|exe|key|pp[st]x?|7z|pkg|rar|gz|zip|avi|mov|mp4|mpe?g|wmv|midi?|mp3|wav|wma)$/i;

  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } }
  function del(k) { try { localStorage.removeItem(k); } catch (e) { /* storage blocked */ } }

  // Used by the opt-out button on /privacy/.
  window.crophecyAnalytics = {
    optedOut: function () { return get(OPT_OUT) === "1" || navigator.globalPrivacyControl === true; },
    optOut: function () { set(OPT_OUT, "1"); del(KEY); },
    optIn: function () { del(OPT_OUT); }
  };

  if (!OWN.test(location.hostname) || !navigator.sendBeacon) return;

  function rand() { return String(1 + Math.floor(Math.random() * 2147483646)); }
  var memory = null;  // fallback when localStorage is blocked: one user per page load
  var pid = rand();   // page id, per page load like gtag's _p

  // Foreground time: counted while the page is visible and focused, as gtag does.
  var activeSince = null, pendingMs = 0, left = false;  // left: after pagehide, until pageshow
  function isActive() { return !left && document.visibilityState === "visible" && document.hasFocus(); }
  function tick() {
    var now = Date.now();
    if (activeSince !== null) pendingMs += now - activeSince;
    activeSince = isActive() ? now : null;
  }
  function takeEngagement() { tick(); var ms = pendingMs; pendingMs = 0; return ms; }

  // Sends one event; params are extra GA parameters (ep.* text, epn.* numbers).
  function send(name, params) {
    if (window.crophecyAnalytics.optedOut()) return;
    var now = Date.now(), st = memory, et = takeEngagement();
    try { st = JSON.parse(get(KEY)) || memory; } catch (e) { st = memory; }
    st = st || {};
    var newUser = !st.cid, newSession = newUser || !st.sid || now - (st.last || 0) > SESSION_MS;
    if (newUser) { st.cid = rand() + "." + Math.floor(now / 1000); st.sct = 0; }
    if (newSession) {
      st.sid = String(Math.floor(now / 1000)); st.sct = (st.sct || 0) + 1;
      st.n = 0; st.pv = 0; st.et = 0;
    }
    st.n = (st.n || 0) + 1;  // hit number within the session (_s)
    if (name === "page_view") st.pv = (st.pv || 0) + 1;
    st.et = (st.et || 0) + et;
    st.last = now;
    memory = st;
    set(KEY, JSON.stringify(st));

    var q = new URLSearchParams({
      v: "2", tid: ID, cid: st.cid, sid: st.sid, sct: String(st.sct), _s: String(st.n), _p: pid,
      // GA counts a session as engaged after 10 s in the foreground or two page views
      seg: st.pv > 1 || st.et >= ENGAGED_MS ? "1" : "0",
      en: name, dl: location.href, dt: document.title,
      ul: (navigator.language || "").toLowerCase(), sr: screen.width + "x" + screen.height,
      npa: "1", _ee: "1"
    });
    if (et > 0) q.set("_et", String(et));
    if (newUser) q.set("_fv", "1");
    if (newSession) { q.set("_ss", "1"); q.set("_nsi", "1"); }
    if (get(DEBUG) === "1") q.set("_dbg", "1");  // shows the hits in GA DebugView
    for (var k in params) if (params[k] !== "" && params[k] != null) q.set(k, String(params[k]));
    navigator.sendBeacon(URL + "?" + q.toString());
  }

  // Page views. The root page is one app whose views are hash routes (#live, #markets, #m-<qid>):
  // it switches them with history.pushState/replaceState (also once right after load, which
  // rewrites "/" to "/#home"), back/forward fire popstate, and plain #route links fire hashchange.
  // Each new view is a page_view; a repeat of the view just sent is dropped ("/" and "/#home" are
  // one view), so the load-time rewrite and hashchange firing alongside popstate count once. On
  // other pages (the waitlist, this notice) a #fragment only scrolls, so it is not a new view.
  // A back/forward restore from the bfcache is a new view too.
  var app = location.pathname === "/" || location.pathname === "/index.html";
  var last = "", firstView = true, scrolled = false;
  function viewKey() {
    var u = location.href.replace(/#home$/, "");
    return app ? u : u.split("#")[0];
  }
  function pageView(force) {
    var view = viewKey();
    if (view === last && !force) return;
    last = view;
    scrolled = false;
    var extra = {};
    if (firstView && document.referrer) extra.dr = document.referrer;  // later views come from this site
    firstView = false;
    send("page_view", extra);
  }
  pageView();
  addEventListener("pageshow", function (e) { left = false; if (e.persisted) pageView(true); });
  // deferred a tick so document.title already reflects the new view
  function later() { setTimeout(pageView, 0); }
  ["pushState", "replaceState"].forEach(function (m) {
    var orig = history[m];
    history[m] = function () { var r = orig.apply(this, arguments); later(); return r; };
  });
  addEventListener("popstate", later);
  addEventListener("hashchange", later);

  // Engagement time, flushed whenever the page goes to the background or is left.
  function flush() { tick(); if (pendingMs > 0) send("user_engagement"); }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flush(); else tick();
  });
  addEventListener("pagehide", function () { tick(); left = true; flush(); });
  addEventListener("focus", tick);
  addEventListener("blur", tick);

  // Scroll depth: once per view, at 90% of the page.
  addEventListener("scroll", function () {
    if (scrolled) return;
    var doc = document.documentElement, height = Math.max(doc.scrollHeight, document.body.scrollHeight);
    if (height > innerHeight && scrollY + innerHeight >= 0.9 * height) {
      scrolled = true;
      send("scroll", { "epn.percent_scrolled": 90 });
    }
  }, { passive: true });

  // Outbound links and file downloads, like enhanced measurement; mouse and keyboard clicks,
  // middle clicks included (auxclick).
  function onLink(e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a || !/^https?:$/.test(a.protocol)) return;
    var common = {
      "ep.link_url": a.href, "ep.link_id": a.id,
      "ep.link_classes": typeof a.className === "string" ? a.className : ""
    };
    var file = FILES.exec(a.pathname);
    if (file) {
      common["ep.file_extension"] = file[1].toLowerCase();
      var path = a.pathname;
      try { path = decodeURIComponent(path); } catch (err) { /* keep it encoded */ }
      common["ep.file_name"] = path;
      common["ep.link_text"] = (a.innerText || a.textContent || "").trim().slice(0, 100);
      send("file_download", common);
    } else if (a.hostname !== location.hostname && !OWN.test(a.hostname)) {
      common["ep.link_domain"] = a.hostname;
      common["ep.outbound"] = "true";
      send("click", common);
    }
  }
  document.addEventListener("click", onLink, true);
  document.addEventListener("auxclick", function (e) { if (e.button === 1) onLink(e); }, true);
})();
