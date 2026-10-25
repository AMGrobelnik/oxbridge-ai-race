/* Beat the Seal phone page: build a strategy, see the visible score live, seal and reveal. */
"use strict";

const pct = (v) => `${Math.round(v * 100)}%`;
const SLIDERS = [
  { key: "risk_on_sol_weight", label: "SOL when risk-on", step: 0.05, show: pct, doc: "Share of your $100 in SOL when the signal says go." },
  { key: "risk_off_sol_weight", label: "SOL when risk-off", step: 0.05, show: pct, doc: "Share in SOL when the signal says get out, or the stop-loss fires." },
  { key: "lookback_days", label: "Lookback", step: 1, show: (v) => `${v} days`, doc: "Days of price history the rule looks at." },
  { key: "entry_threshold_pct", label: "Signal threshold", step: 0.5, show: (v) => `${v}%`, doc: "How big a move flips the signal." },
  { key: "stop_loss_pct", label: "Stop-loss", step: 1, show: (v) => (v ? `${v}%` : "off"), doc: "Go risk-off while SOL is this far below its lookback high." },
  { key: "staked_share", label: "Staked SOL", step: 0.05, show: pct, doc: "Part of the SOL leg staked at 7% a year." },
  { key: "rebalance_band", label: "Rebalance band", step: 0.01, show: (v) => `±${pct(v)}`, doc: "Trade only when your weight drifts this far (each swap costs 0.2%)." },
];

const state = { config: null, params: null, tab: "lb", stats: null, myNick: null, scoreTimer: 0, scoreSeq: 0, busy: false };

function toast(text, isErr) {
  const t = $("toast");
  t.textContent = text;
  t.className = `toast${isErr ? " err" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 3200);
}

function renderRules() {
  const box = $("rules");
  box.replaceChildren();
  for (const rule of state.config.rules) {
    const info = RULE_INFO[rule] || { name: rule, blurb: "" };
    const b = el("button", "rule");
    b.type = "button";
    b.dataset.rule = rule;
    b.append(el("b", "", info.name), el("span", "", info.blurb));
    b.addEventListener("click", () => { state.params.rule = rule; syncControls(); scheduleScore(); });
    box.append(b);
  }
}

function renderSliders() {
  const box = $("sliders");
  box.replaceChildren();
  for (const s of SLIDERS) {
    const [lo, hi] = state.config.limits[s.key];
    const wrap = el("div", "slider");
    wrap.id = `s-${s.key}`;
    const top = el("div", "top");
    const label = el("label", "", s.label);
    label.htmlFor = `r-${s.key}`;
    const out = el("output", "num");
    top.append(label, out);
    const input = el("input");
    Object.assign(input, { type: "range", id: `r-${s.key}`, min: lo, max: hi, step: s.step });
    input.addEventListener("input", () => {
      state.params[s.key] = state.config.integer_keys.includes(s.key) ? parseInt(input.value, 10) : parseFloat(input.value);
      syncControls();
      scheduleScore();
    });
    wrap.append(top, input, el("div", "doc", s.doc));
    box.append(wrap);
  }
}

function syncControls() {
  const p = state.params;
  for (const b of $("rules").children) b.setAttribute("aria-pressed", String(b.dataset.rule === p.rule));
  for (const s of SLIDERS) {
    const input = $(`r-${s.key}`);
    const [lo, hi] = state.config.limits[s.key];
    input.value = p[s.key];
    input.style.setProperty("--p", `${((p[s.key] - lo) / (hi - lo)) * 100}%`);
    $(`s-${s.key}`).querySelector("output").textContent = s.show(p[s.key]);
  }
  const hold = p.rule === "hold";
  const noStop = !p.stop_loss_pct;
  $("s-entry_threshold_pct").classList.toggle("dim", hold);
  $("s-lookback_days").classList.toggle("dim", hold && noStop);
  $("s-risk_off_sol_weight").classList.toggle("dim", hold && noStop);
  store("bts-params", p);
}

function scheduleScore() {
  clearTimeout(state.scoreTimer);
  state.scoreTimer = setTimeout(scoreNow, 80);
}

async function scoreNow() {
  const seq = ++state.scoreSeq;
  try {
    const data = await api("/challenge/api/score", { params: state.params });
    if (seq === state.scoreSeq) showDev(data);
  } catch (err) {
    toast(err.message, true);
  }
}

function showDev(data) {
  const base = state.config.baseline_scores.dev_score;
  const edge = data.dev_score - base;
  const big = $("dev");
  big.textContent = signed(edge);
  big.className = `big num ${edge > 0.05 ? "pos" : edge < -0.05 ? "neg" : ""}`;
  $("dev-delta").textContent = `score ${fmt(data.dev_score)} vs ${fmt(base)}`;
  $("dev-delta").className = "chip flat";
  const b = data.breakdown;
  const cells = [
    [`${signed(b.return_pct)}%`, "return"],
    [`${fmt(b.max_drawdown_pct)}%`, "max drop"],
    [fmt(b.trades, 0), "trades"],
    [`$${fmt(b.fees_usd, 2)}`, "fees"],
  ];
  $("breakdown").replaceChildren(...cells.map(([v, k]) => { const d = el("div"); d.append(el("b", "num", v), el("span", "", k)); return d; }));
}

async function translate() {
  const sentence = $("sentence").value.trim();
  if (!sentence) { $("sentence").focus(); return; }
  const btn = $("parse");
  btn.disabled = true;
  btn.textContent = "Thinking…";
  try {
    const data = await api("/challenge/api/parse", { sentence, params: state.params });
    state.params = data.params;
    syncControls();
    showDev(data);
    const note = $("parse-note");
    note.replaceChildren(el("b", "", data.source === "gemini" ? "Gemini: " : "Keywords: "), document.createTextNode(data.summary || "sliders set"));
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Ask AI";
  }
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function seal() {
  if (state.busy) return;
  const nick = $("nick").value.trim();
  if (!nick) {
    $("nick").focus();
    $("nick").scrollIntoView({ behavior: "smooth", block: "center" });
    toast("Pick a nickname first");
    return;
  }
  state.busy = true;
  $("seal").disabled = true;
  $("overlay").classList.remove("hidden");
  try {
    const [data] = await Promise.all([api("/challenge/api/submit", { nickname: nick, params: state.params }), sleep(1100)]);
    state.myNick = data.entry.nick_sha256;
    $("nick").value = data.nickname;
    store("bts-nick", data.nickname);
    renderReveal(data);
    applyStats(data.stats);
    loadStats();
  } catch (err) {
    toast(err.message, true);
  } finally {
    $("overlay").classList.add("hidden");
    $("seal").disabled = false;
    state.busy = false;
  }
}

const OUTCOME = {
  GENERALISES: { stamp: "GENERALISES", cls: "gen" },
  OVERFIT: { stamp: "OVERFIT", cls: "over" },
  FAILS: { stamp: "FAILS THE SEAL", cls: "over" },
};

function edgeClass(v) { return v > 0.05 ? "pos" : v < -0.05 ? "neg" : ""; }

function renderReveal(data) {
  const r = data.result;
  const gen = r.outcome === "GENERALISES";
  const look = OUTCOME[r.outcome] || OUTCOME.FAILS;
  const labels = r.shift_labels;
  const box = $("reveal");
  box.replaceChildren();
  box.classList.remove("hidden", "reveal");
  void box.offsetWidth;
  box.classList.add("reveal");

  const head = el("div", "verdict-head");
  head.append(el("span", `stamp ${look.cls}`, look.stamp));
  const sub = gen ? (data.rank ? `#${data.rank} on the leaderboard` : "Beats the baseline everywhere")
    : r.outcome === "OVERFIT" ? "Added to the Overfit wall" : "Never had an edge to lose";
  head.append(el("span", `rankbadge ${gen ? "" : "red"}`, sub));
  box.append(head);

  // Edge over the naive 50/50 baseline: visible (2025) vs sealed (weakest 2026 scenario).
  const flow = el("div", "flow");
  const left = el("div", "side");
  left.append(el("small", "", "Visible edge 2025"), el("b", edgeClass(r.visible_edge), signed(r.visible_edge)), el("em", "", `score ${fmt(r.dev_score)}`));
  const right = el("div", "side");
  right.append(el("small", "", "Sealed edge 2026 + stress"), el("b", edgeClass(r.sealed_edge), signed(r.sealed_edge)), el("em", "", `weakest: ${labels[r.worst_shift_id] || r.worst_shift_id}`));
  const change = r.sealed_edge - r.visible_edge;
  // The arrow takes the verdict's colour or stays grey: green only on GENERALISES, red only on a
  // failing verdict, so a red verdict never sits next to a green up-arrow (or the reverse).
  const tone = gen ? (change > 0.05 ? "up" : "flat") : (change < -0.05 ? "down" : "flat");
  flow.append(left, el("span", `arrow chip ${tone}`, `→ ${signed(change)}`), right);
  box.append(flow);
  box.append(el("p", "rawline", `Edge = score minus the naive 50/50 buy-and-hold baseline. Raw scores: visible ${fmt(r.dev_score)} vs ${fmt(r.baseline_dev_score)}, sealed mean ${fmt(r.sealed_score)} (mean edge ${signed(r.mean_edge)}).`));

  const reasons = el("ul", "reasons");
  if (gen) {
    reasons.append(el("li", "ok", "Held up on unseen 2026 windows and matched or beat the baseline in every stress test."));
  } else if (r.outcome === "OVERFIT") {
    reasons.append(el("li", "", `Your ${signed(r.visible_edge)} visible edge collapsed to ${signed(r.sealed_edge)} under ${labels[r.worst_shift_id] || r.worst_shift_id}: it was fitted to 2025, not a real edge.`));
  } else {
    const names = r.losing_shifts.map((id) => labels[id] || id).join(", ");
    reasons.append(el("li", "", `Doesn't beat buy-and-hold under: ${names}.`));
  }
  box.append(reasons);

  const shifts = el("div", "shifts");
  const span = Math.max(1, ...Object.values(r.edges).map(Math.abs), Math.abs(r.visible_edge));
  for (const id of Object.keys(r.edges)) {
    const edge = r.edges[id];
    const row = el("div", "shift");
    const lbl = el("div", "lbl");
    lbl.append(el("span", "", labels[id] || id), el("b", edgeClass(edge), signed(edge)));
    const bar = el("div", "ebar");
    const fill = el("i", edge >= -1e-9 ? "good" : "bad");
    const w = `${(Math.min(Math.abs(edge), span) / span) * 50}%`;
    if (edge >= 0) fill.style.left = "50%"; else fill.style.right = "50%";
    fill.style.width = "0%";
    bar.append(fill, el("u"));
    row.append(lbl, bar);
    shifts.append(row);
    requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = w; }));
  }
  shifts.append(el("div", "legend", "Edge over the baseline in each sealed scenario. Right of the line: you beat it."));
  box.append(shifts);

  const c = el("div", "chainline mono");
  c.append(el("b", "", `Entry #${data.entry.seq}`), document.createTextNode(` · ${data.entry.hash.slice(0, 16)}… chained to ${data.entry.prev.slice(0, 10)}… · anchored on Solana devnet in batches`));
  box.append(c);

  const actions = el("div", "actions");
  const again = el("button", "btn", "Tweak & retry");
  again.addEventListener("click", () => $("sentence").scrollIntoView({ behavior: "smooth", block: "center" }));
  const share = el("button", "btn primary", "Share");
  share.addEventListener("click", async () => {
    const url = state.config.public_url;
    const text = gen ? `My strategy beat the seal: ${signed(r.sealed_edge)} over buy-and-hold on unseen 2026 data. Can yours?`
      : r.outcome === "OVERFIT" ? `The seal caught me overfitting: ${signed(r.visible_edge)} edge in 2025, ${signed(r.sealed_edge)} on sealed 2026. Can you beat it?`
      : "My strategy failed the seal. Can you beat it?";
    try {
      if (navigator.share) await navigator.share({ title: "Beat the Seal", text, url });
      else { await navigator.clipboard.writeText(`${text} ${url}`); toast("Link copied"); }
    } catch (_) { /* share sheet dismissed */ }
  });
  actions.append(again, share);
  box.append(actions);
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

function applyStats(s) {
  bumpCounter("c-players", s.players);
  bumpCounter("c-sealed", s.sealed);
  bumpCounter("c-gen", s.generalises);
}

function renderList() {
  const list = $("list");
  list.replaceChildren();
  const st = state.stats;
  if (!st) return;
  const wall = state.tab === "wall";
  const rows = wall ? st.overfit_wall : st.leaderboard;
  if (!rows.length) {
    const li = el("li", "empty");
    li.append(el("b", "", wall ? "Nobody has overfit yet" : "No sealed winners yet"), document.createTextNode(wall ? "Push your visible edge as high as it goes and see what happens." : "Be the first strategy to survive the seal."));
    list.append(li);
    return;
  }
  rows.forEach((e, i) => {
    const li = el("li", `row${e.nick_sha256 === state.myNick ? " me" : ""}`);
    li.append(el("span", "pos", i + 1), entryWho(e, wall), entryVal(e, wall));
    list.append(li);
  });
}

async function loadStats() {
  try {
    state.stats = await api("/challenge/api/stats");
    applyStats(state.stats.stats);
    renderList();
  } catch (_) { /* keep the last board */ }
}

function setTab(tab) {
  state.tab = tab;
  $("tab-lb").setAttribute("aria-selected", String(tab === "lb"));
  $("tab-wall").setAttribute("aria-selected", String(tab === "wall"));
  renderList();
}

async function init() {
  state.config = await api("/challenge/api/config");
  const saved = store("bts-params");
  state.params = saved && typeof saved === "object" && saved.rule ? { ...state.config.baseline, ...saved } : { ...state.config.baseline };
  const nick = store("bts-nick");
  if (typeof nick === "string") $("nick").value = nick;
  renderRules();
  renderSliders();
  syncControls();
  scoreNow();
  loadStats();
  setInterval(() => { if (!document.hidden) loadStats(); }, 6000);
  $("parse").addEventListener("click", translate);
  $("sentence").addEventListener("keydown", (e) => { if (e.key === "Enter") translate(); });
  $("seal").addEventListener("click", seal);
  $("reset").addEventListener("click", () => { state.params = { ...state.config.baseline }; syncControls(); scheduleScore(); });
  $("tab-lb").addEventListener("click", () => setTab("lb"));
  $("tab-wall").addEventListener("click", () => setTab("wall"));
}

init().catch((err) => toast(`Could not load the challenge: ${err.message}`, true));
