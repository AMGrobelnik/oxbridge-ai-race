/* Shared helpers for the Beat the Seal page and board. Every user string goes through textContent. */
"use strict";

const RULE_INFO = {
  hold: { name: "Hold", blurb: "Fixed SOL weight" },
  momentum: { name: "Momentum", blurb: "Ride the trend" },
  mean_reversion: { name: "Buy dips", blurb: "Mean reversion" },
};

function $(id) { return document.getElementById(id); }

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function fmt(n, digits = 1) {
  return Number.isFinite(n) ? n.toFixed(digits) : "--";
}

function signed(n, digits = 1) {
  return (n > 0 ? "+" : n < 0 ? "−" : "±") + Math.abs(n).toFixed(digits);
}

async function api(path, body) {
  const init = body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (_) { data = null; }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || "null");
    localStorage.setItem(key, JSON.stringify(value));
  } catch (_) { return null; }
  return null;
}

const SHORT_SHIFT = { base: "plain 2026 data", crash: "SOL crash", usdc_depeg: "USDC depeg", fee_spike: "5x fees", vol_spike: "vol spike" };

/* Why an entry was rejected, rebuilt from its per-shift scores and the sealed gate (one short line). */
function failReason(entry, baseline, gate) {
  if (gate && entry.relative_gap > gate.max_relative_gap) {
    return `${Math.round(entry.relative_gap * 100)}% below visible`;
  }
  const per = entry.per_shift || {};
  let worst = null;
  for (const id of Object.keys(per)) {
    const gap = per[id] - (baseline[id] ?? -Infinity);
    if (gap < -1e-9 && (worst === null || gap < worst.gap)) worst = { id, gap };
  }
  if (worst) return `fails ${SHORT_SHIFT[worst.id] || worst.id} (${signed(worst.gap)} vs naive)`;
  return "failed the sealed gate";
}

function bumpCounter(id, value) {
  const box = $(id);
  if (!box) return;
  const b = box.querySelector("b");
  const next = String(value);
  if (b.textContent !== next) {
    b.textContent = next;
    box.classList.remove("bump");
    void box.offsetWidth;
    box.classList.add("bump");
  }
}
