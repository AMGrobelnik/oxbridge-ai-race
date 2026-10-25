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

/* Name plus one short line for a leaderboard row or an Overfit wall row (edge over the baseline). */
function entryWho(e, wall) {
  const who = el("div", "who");
  const info = RULE_INFO[e.params.rule] || { name: e.params.rule };
  const line = wall ? `breaks under ${SHORT_SHIFT[e.worst_shift] || e.worst_shift}` : `${info.name} · visible edge ${signed(e.visible_edge)}`;
  who.append(el("b", "", e.nickname), el("span", "", line));
  return who;
}

function entryVal(e, wall) {
  const val = el("div", `val num${wall ? " red" : ""}`);
  if (wall) val.append(document.createTextNode(`${signed(e.visible_edge)}→${signed(e.sealed_edge)}`), el("small", "", `edge lost ${fmt(e.edge_drop)}`));
  else val.append(document.createTextNode(signed(e.mean_edge)), el("small", "", `sealed edge · ${fmt(e.sealed_score)}`));
  return val;
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
