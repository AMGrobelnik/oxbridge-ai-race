/* Cookieless Google Analytics for crophecy.bid only: nothing is stored on the visitor's device
   (no cookies, localStorage or sessionStorage). client_storage 'none' stops gtag writing the _ga
   cookies, and a fresh random client_id per page load means visits are counted but never linked
   across pages or days. Ads signals are off and every ad consent type is denied. */
(function () {
  var ID = "G-J6Q2K51RVP";
  if (!/(^|\.)crophecy\.bid$/.test(location.hostname)) return;

  window.dataLayer = window.dataLayer || [];
  function gtag() { window.dataLayer.push(arguments); }
  window.gtag = window.gtag || gtag;
  gtag("consent", "default", { analytics_storage: "granted", ad_storage: "denied", ad_user_data: "denied", ad_personalization: "denied" });
  gtag("js", new Date());
  var cid = window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + "." + Date.now();
  gtag("config", ID, { client_storage: "none", client_id: cid, allow_google_signals: false, allow_ad_personalization_signals: false });

  var s = document.createElement("script");
  s.async = true;
  s.src = "https://www.googletagmanager.com/gtag/js?id=" + ID;
  document.head.appendChild(s);
})();
