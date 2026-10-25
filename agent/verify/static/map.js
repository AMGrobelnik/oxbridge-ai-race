"use strict";
(() => {
  const COLORS = { green: "#18b26b", amber: "#f0a020", red: "#e5484d", grey: "#8a94a8" };
  const STATUS_TEXT = { green: "VERIFIED", amber: "STALE / PENDING", red: "FAILING", grey: "UNPROBED" };
  const PROBE_TEXT = { x402: "x402 paywall check", solana_pay: "Solana Pay payability check", micropay: "0.01 USDC test purchase" };
  const KINGS = [52.20431, 0.11653];
  const qs = new URLSearchParams(location.search);

  const map = L.map("map", { zoomControl: true, scrollWheelZoom: true }).setView(KINGS, 17);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  const markers = new Map();
  let state = null;
  let skew = 0; // server now - client now, ms
  let seenSeq = 0;
  let fitted = false;
  let pickMarker = null;
  let picked = null;
  let busy = 0; // re-verify requests in flight: keep the open popup as is

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  };
  const short = (h, n = 8) => (h ? `${h.slice(0, n)}…${h.slice(-4)}` : "");
  const serverNow = () => Date.now() + skew;
  function ago(iso) {
    if (!iso) return "never";
    const s = Math.max(0, Math.round((serverNow() - Date.parse(iso)) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }
  function until(iso) {
    if (!iso) return "";
    const s = Math.round((Date.parse(iso) - serverNow()) / 1000);
    return s <= 0 ? "running now" : s < 60 ? `in ${s}s` : `in ${Math.floor(s / 60)}m ${s % 60}s`;
  }

  function popupContent(p) {
    const root = el("div", "pop");
    root.append(el("h3", null, p.name));
    root.append(el("div", "demo", p.seed ? "demo merchant" : `onboarded · ${p.place || ""}`));
    const st = el("div", "st");
    st.append(el("span", `chip ${p.status}`, STATUS_TEXT[p.status]));
    const last = p.last;
    if (last) st.append(document.createTextNode(` verified by agent ${ago(last.at)}`));
    root.append(st);
    if (last) {
      root.append(el("div", "verdict", `“${last.verdict}”`));
      const dl = el("dl");
      const row = (k, v, node) => { dl.append(el("dt", null, k)); const dd = el("dd"); if (node) dd.append(node); else dd.textContent = v; dl.append(dd); };
      row("Probe", `${PROBE_TEXT[last.probe] || last.probe} · ${last.status}`);
      row("Result", last.reason);
      row("Ledger", null, el("span", "mono", `#${last.seq} ${short(last.hash, 12)}`));
      const r = last.receipt || {};
      if (r.status === "confirmed" && r.explorer_url) {
        const a = el("a", null, "anchored on devnet ↗"); a.href = r.explorer_url; a.target = "_blank"; a.rel = "noopener";
        row("Receipt", null, a);
      } else {
        row("Receipt", (r.error || "pending on-chain").replace(/^pending on-chain: /, "pending on-chain: "));
      }
      if (last.explorer_url) {
        const a = el("a", null, "test purchase tx ↗"); a.href = last.explorer_url; a.target = "_blank"; a.rel = "noopener";
        row("Payment", null, a);
      }
      root.append(dl);
    } else {
      root.append(el("div", "verdict", p.kind === "physical" ? "Physical listing only: no crypto checkout for an agent to test." : "Not probed yet."));
    }
    if (p.kind !== "physical") {
      const btn = el("button", "btn", "Re-verify now");
      const msg = el("div", "msg");
      btn.addEventListener("click", async () => {
        btn.disabled = true; btn.textContent = "Agent probing…"; msg.textContent = ""; busy += 1;
        let error = null;
        try {
          const res = await fetch("/map/api/reverify", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ merchant_id: p.id }) });
          const body = await res.json();
          if (!res.ok) error = body.error || `HTTP ${res.status}`;
        } catch (err) {
          error = err.message;
        } finally {
          busy--;
        }
        if (error) { msg.textContent = error; btn.disabled = false; btn.textContent = "Re-verify now"; return; }
        await refresh();
        const m = markers.get(p.id);
        if (m && m.isPopupOpen()) m.setPopupContent(popupContent(m._pin));
      });
      root.append(btn, msg);
    }
    return root;
  }

  function renderPins() {
    for (const p of state.merchants) {
      const style = { radius: 13, color: "#fff", weight: 3, fillColor: COLORS[p.status], fillOpacity: 0.95 };
      let m = markers.get(p.id);
      if (!m) {
        m = L.circleMarker([p.lat, p.lng], style).addTo(map);
        m.bindTooltip(p.name, { className: "pin-label", direction: "right", offset: [12, 0], permanent: !matchMedia("(max-width: 860px)").matches });
        m.bindPopup(() => popupContent(markers.get(p.id)._pin), { maxWidth: 320, autoPanPadding: [20, 20] });
        markers.set(p.id, m);
      } else {
        m.setStyle(style);
      }
      m._pin = p;
      if (m.isPopupOpen() && !busy) m.setPopupContent(popupContent(p));
    }
    if (!fitted && state.merchants.length) {
      fitted = true;
      const pts = state.merchants.filter((p) => Math.abs(p.lat - KINGS[0]) < 0.02 && Math.abs(p.lng - KINGS[1]) < 0.03).map((p) => [p.lat, p.lng]);
      if (pts.length) map.fitBounds(pts, { padding: [40, 40], maxZoom: 17 });
      const open = qs.get("open");
      if (open && markers.get(open)) setTimeout(() => markers.get(open).openPopup(), 300);
    }
  }

  function renderCounters() {
    const c = state.counters;
    document.getElementById("c-merchants").textContent = c.merchants;
    document.getElementById("c-probes").textContent = c.probes;
    document.getElementById("c-onboarded").textContent = c.onboarded_today;
    document.getElementById("c-passing").textContent = c.passing;
  }

  function renderRound() {
    const body = document.getElementById("round-body");
    body.replaceChildren();
    body.className = "";
    const r = state.round;
    const line = (...nodes) => { const d = el("div", "line"); d.append(...nodes); body.append(d); };
    if (!r) {
      line(el("span", "muted", "Waiting for the first round…"));
    } else {
      line(el("b", null, `Round ${r.round}`), document.createTextNode(` · ${r.probe_count} probes · planned by ${r.planner === "gemini" ? "Gemini agent" : "rules"}`));
      line(el("span", "muted", "commitment "), el("span", "mono", short(r.commitment, 16)));
      const rc = r.receipt || {};
      if (rc.status === "confirmed" && rc.explorer_url) {
        const a = el("a", null, "sealed on devnet ↗"); a.href = rc.explorer_url; a.target = "_blank"; a.rel = "noopener";
        line(el("span", "chip ok", "ON-CHAIN"), document.createTextNode(" "), a);
      } else {
        line(el("span", "chip wait", "PENDING"), document.createTextNode(` ${(rc.error || "pending on-chain").replace(/^pending on-chain: /, "")}`));
      }
      line(el("span", "muted", r.revealed_at ? `schedule revealed ${ago(r.revealed_at)}: anyone can recompute the hash` : "probe times sealed: hidden until the round ends"));
    }
    if (state.next_round_at) line(el("span", "muted", `next sealed round ${until(state.next_round_at)}`));
  }

  function renderFeed() {
    const list = document.getElementById("feed");
    list.replaceChildren();
    const colorOf = (s) => (s === "PASS" ? "green" : s === "PENDING" ? "amber" : "red");
    for (const e of state.feed) {
      const li = el("li", e.seq > seenSeq && seenSeq > 0 ? "fresh" : "");
      li.append(el("i", `dot ${colorOf(e.status)}`));
      const box = el("div");
      const head = el("div");
      head.append(el("span", "who", e.merchant_name), document.createTextNode(` · ${e.status}`));
      box.append(head, el("div", "v", e.verdict), el("div", "meta", `${PROBE_TEXT[e.probe] || e.probe} · ${ago(e.at)} · #${e.seq} ${short(e.hash, 6)}`));
      li.append(box);
      list.append(li);
    }
    if (!state.feed.length) list.append(el("li", "muted", "No probes yet: the first sealed round starts a few seconds after the server."));
    seenSeq = Math.max(seenSeq, ...state.feed.map((e) => e.seq), 0);
  }

  async function refresh() {
    try {
      const res = await fetch("/map/api/state", { cache: "no-store" });
      if (!res.ok) return;
      state = await res.json();
      skew = Date.parse(state.now) - Date.now();
      renderPins(); renderCounters(); renderRound(); renderFeed();
    } catch (_) { /* keep the last good view; the next poll retries */ }
  }

  // ---- onboarding ----
  const details = document.getElementById("onboard");
  const hint = document.getElementById("maphint");
  details.addEventListener("toggle", () => { hint.hidden = !details.open || !!picked; });
  map.on("click", (ev) => {
    if (!details.open) return;
    picked = ev.latlng;
    if (pickMarker) pickMarker.setLatLng(picked); else pickMarker = L.marker(picked).addTo(map);
    document.getElementById("coords").textContent = `Pin: ${picked.lat.toFixed(5)}, ${picked.lng.toFixed(5)}`;
    hint.hidden = true;
  });
  document.getElementById("onboard-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const msg = document.getElementById("onboard-msg");
    const btn = form.querySelector("button");
    const centre = map.getCenter();
    const where = picked || { lat: centre.lat, lng: centre.lng };
    const payload = { name: form.name.value, target: form.target.value, place: form.place.value || "Cambridge", lat: where.lat, lng: where.lng };
    btn.disabled = true; msg.className = "formmsg"; msg.textContent = "Agent is probing your checkout…";
    try {
      const res = await fetch("/map/api/onboard", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload) });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      const p = body.merchant;
      msg.textContent = `Added. Agent verdict: ${p.last ? p.last.status : "pending"}.`;
      showPayment(body.payment_link);
      if (pickMarker) { map.removeLayer(pickMarker); pickMarker = null; picked = null; }
      await refresh();
      const m = markers.get(p.id);
      if (m) { map.panTo(m.getLatLng()); m.openPopup(); }
      form.reset();
    } catch (err) {
      msg.className = "formmsg err"; msg.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });
  function showPayment(link) {
    const box = document.getElementById("onboard-result");
    const qrBox = document.getElementById("qr");
    qrBox.replaceChildren();
    if (!link) { box.hidden = true; return; }
    if (window.qrcode) {
      const qr = window.qrcode(0, "M");
      qr.addData(link); qr.make();
      qrBox.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: false });
    }
    const a = document.getElementById("paylink");
    a.textContent = link;
    a.href = /^(https?:|solana:)/i.test(link) ? link : "#";
    box.hidden = false;
  }

  refresh();
  setInterval(refresh, 4000);
  setInterval(() => { if (state) renderRound(); }, 1000);
})();
