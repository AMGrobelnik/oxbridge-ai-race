/* Google Analytics behind a consent banner (Consent Mode v2), for crophecy.bid only.
   Everything defaults to denied; gtag.js is not even loaded until the visitor clicks Accept,
   so no Google cookie is set and no request goes to Google before consent. The choice is kept
   in localStorage; clearing site data shows the banner again. */
(function () {
  var ID = "G-J6Q2K51RVP", KEY = "crophecy-analytics-consent";
  if (!/(^|\.)crophecy\.bid$/.test(location.hostname)) return;

  window.dataLayer = window.dataLayer || [];
  function gtag() { window.dataLayer.push(arguments); }
  window.gtag = window.gtag || gtag;
  gtag("consent", "default", { analytics_storage: "denied", ad_storage: "denied", ad_user_data: "denied", ad_personalization: "denied" });

  function get() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function set(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* private mode: ask again next visit */ } }

  var loaded = false;
  function load() {
    if (loaded) return;
    loaded = true;
    gtag("consent", "update", { analytics_storage: "granted" });
    var s = document.createElement("script");
    s.async = true;
    s.src = "https://www.googletagmanager.com/gtag/js?id=" + ID;
    document.head.appendChild(s);
    gtag("js", new Date());
    gtag("config", ID, { allow_google_signals: false, allow_ad_personalization_signals: false });
  }

  function banner() {
    var css = ".cx-consent{position:fixed;left:16px;bottom:16px;z-index:2147483000;max-width:340px;box-sizing:border-box;" +
      "padding:12px 14px;background:#fbfcf9;color:#14190f;border:1px solid #dfe3d7;border-radius:10px;" +
      "box-shadow:0 6px 24px rgba(20,25,15,.12);font:13px/1.45 'Geist',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif}" +
      ".cx-consent p{margin:0 0 10px}.cx-consent div{display:flex;gap:8px;justify-content:flex-end}" +
      ".cx-consent button{font:inherit;font-weight:500;cursor:pointer;padding:5px 12px;border-radius:7px;border:1px solid #dfe3d7;background:transparent;color:#14190f}" +
      ".cx-consent button.cx-yes{background:#14190f;border-color:#14190f;color:#fbfcf9}" +
      ".cx-consent button:focus-visible{outline:2px solid #c99a3b;outline-offset:2px}" +
      "@media (max-width:420px){.cx-consent{left:12px;right:12px;bottom:12px;max-width:none}}";
    var st = document.createElement("style");
    st.textContent = css;
    document.head.appendChild(st);
    var el = document.createElement("div");
    el.className = "cx-consent";
    el.setAttribute("role", "region");
    el.setAttribute("aria-label", "Cookie consent");
    el.innerHTML = '<p>We use analytics cookies to understand site usage.</p>' +
      '<div><button type="button" class="cx-no">Decline</button><button type="button" class="cx-yes">Accept</button></div>';
    el.querySelector(".cx-yes").addEventListener("click", function () { set("granted"); el.remove(); load(); });
    el.querySelector(".cx-no").addEventListener("click", function () { set("denied"); el.remove(); });
    document.body.appendChild(el);
  }

  var choice = get();
  if (choice === "granted") load();
  else if (choice !== "denied") {
    if (document.body) banner();
    else document.addEventListener("DOMContentLoaded", banner);
  }
})();
