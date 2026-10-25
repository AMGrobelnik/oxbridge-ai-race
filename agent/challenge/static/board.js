/* Projector board: auto-refreshes the counters, leaderboard, overfit wall, chain head and feed. */
"use strict";

const board = { config: null, seen: new Set(), first: true };
const REFRESH_MS = 4000;

function rowsInto(list, entries, wall) {
  list.replaceChildren();
  if (!entries.length) {
    const li = el("li", "empty");
    li.append(el("b", "", wall ? "No collapses yet" : "Waiting for the first strategy to survive the seal"), document.createTextNode(wall ? "Overfit strategies land here." : "Scan the QR code and play."));
    list.append(li);
    return;
  }
  const cfg = board.config;
  entries.slice(0, 8).forEach((e, i) => {
    const fresh = !board.first && !board.seen.has(`${wall ? "w" : "l"}${e.seq}`);
    board.seen.add(`${wall ? "w" : "l"}${e.seq}`);
    const li = el("li", `row${fresh ? " fresh" : ""}`);
    const who = el("div", "who");
    const info = RULE_INFO[e.params.rule] || { name: e.params.rule };
    who.append(el("b", "", e.nickname), el("span", "", wall ? failReason(e, cfg.baseline_scores.per_shift, cfg.gate) : `${info.name} · visible ${fmt(e.dev_score)}`));
    const val = el("div", `val num${wall ? " red" : ""}`);
    if (wall) val.append(document.createTextNode(`${fmt(e.dev_score)}→${fmt(e.sealed_score)}`), el("small", e.drop < 0 ? "up" : "", `${signed(-e.drop)} on the seal`));
    else val.append(document.createTextNode(fmt(e.sealed_score)), el("small", "", "sealed"));
    li.append(el("span", "pos", i + 1), who, val);
    list.append(li);
  });
}

function renderChain(data) {
  const s = data.stats;
  const box = $("chain");
  box.replaceChildren();
  const head = el("div");
  head.append(el("b", "", "Chain head "), document.createTextNode(`${s.chain_head.slice(0, 20)}… (${s.sealed} entries)`));
  box.append(head);
  const a = s.last_confirmed_anchor || s.last_anchor;
  const line = el("div");
  line.style.marginTop = "6px";
  if (!a) {
    line.append(el("span", "badge pending", data.anchoring ? "next anchor soon" : "anchoring off"), document.createTextNode(` every ${data.anchor_every} entries or ${Math.round(data.anchor_interval_s / 60)} min`));
  } else if (a.status === "confirmed") {
    line.append(el("span", "badge ok", "on-chain"), document.createTextNode(` devnet memo at #${a.count}`));
  } else {
    line.append(el("span", "badge pending", "pending on-chain"), document.createTextNode(` batch #${a.count}`));
  }
  box.append(line);
}

function renderTicker(recent) {
  const t = $("ticker");
  t.replaceChildren();
  for (const e of recent.slice(0, 3)) {
    const li = el("li");
    const gen = e.verdict === "GENERALISES";
    li.append(el("span", "", e.nickname), el("span", gen ? "v-gen" : "v-over", `${gen ? "GENERALISES" : "OVERFIT"} ${fmt(e.sealed_score)}`));
    t.append(li);
  }
}

async function refresh() {
  try {
    const data = await api("/challenge/api/stats");
    bumpCounter("c-players", data.stats.players);
    bumpCounter("c-sealed", data.stats.sealed);
    bumpCounter("c-gen", data.stats.generalises);
    rowsInto($("lb"), data.leaderboard, false);
    rowsInto($("wall"), data.overfit_wall, true);
    renderChain(data);
    renderTicker(data.recent);
    board.first = false;
  } catch (_) { /* projector keeps the last state */ }
}

async function init() {
  board.config = await api("/challenge/api/config");
  $("url").textContent = board.config.public_url.replace(/^https?:\/\//, "");
  await refresh();
  setInterval(refresh, REFRESH_MS);
  setInterval(() => { $("qr").src = `/challenge/qr.svg?t=${Date.now()}`; }, 300000);
}

init();
