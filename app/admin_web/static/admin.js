// The admin page. Everything shown is built from DOM nodes with textContent,
// never parsed as HTML: job errors, links, and emails come from users. Charts
// are drawn here from what the server already reports; nothing is collected
// for them and nothing is loaded from elsewhere.
"use strict";

const main = document.getElementById("main");
const modalRoot = document.getElementById("modal-root");
const SVG_NS = document.querySelector(".adm-nav svg").namespaceURI;
const REFRESH_SECONDS = 30;
// Changes that need a reason; the server checks this too.
const REASON_REQUIRED = new Set(["job.cancel", "user.credit", "limits.block"]);
const STATUS_KIND = {
  queued: "info", processing: "busy", completed: "good", failed: "bad",
  cancelled: "quiet", preparing: "quiet", uploading: "quiet",
};
const DURATIONS = [["", "until removed"], ["30m", "30 minutes"], ["1h", "1 hour"], ["6h", "6 hours"], ["1d", "1 day"], ["7d", "7 days"]];

let me = null;
let currentView = null;
let refreshTimer = null;

// --- Building blocks -----------------------------------------------------------------

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

// Replaces a node's content; empty slots (null, false) are skipped, not shown.
function fill(node, ...children) {
  node.textContent = "";
  return append(node, children);
}

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "text") node.textContent = value;
    else if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  return append(node, children);
}

function svg(tag, attributes = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(name, value);
  }
  return append(node, children);
}

const numbers = new Intl.NumberFormat("en");

function fmt(value) {
  return value === null || value === undefined ? "–" : numbers.format(value);
}

function compact(value) {
  const size = Math.abs(value);
  if (size >= 1e6) return `${(value / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
  if (size >= 1e3) return `${(value / 1e3).toFixed(1).replace(/\.0$/, "")}K`;
  return String(Math.round(value));
}

function when(timestamp) {
  if (!timestamp) return "–";
  return new Date(timestamp * 1000).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

function span(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 2 * 86400) return `${(seconds / 3600).toFixed(1)} h`;
  return `${Math.round(seconds / 86400)} d`;
}

function ago(timestamp) {
  return timestamp ? `${span(Math.max(0, Date.now() / 1000 - timestamp))} ago` : "–";
}

function bytes(count) {
  if (count === null || count === undefined) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (count >= 1024 && index < units.length - 1) { count /= 1024; index += 1; }
  return `${count.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function shortId(id) {
  return id ? String(id).slice(0, 8) : "–";
}

// --- Talking to the server ---------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    const error = new Error(data && typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function post(path, body) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

function query(parameters) {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(parameters)) if (value !== "" && value !== null && value !== undefined) search.set(name, value);
  return search.toString() ? `?${search}` : "";
}

function toast(text, kind = "success") {
  const node = el("div", { class: `toast ${kind === "error" ? "error" : ""}`, role: kind === "error" ? "alert" : "status", text });
  document.getElementById("toasts").append(node);
  setTimeout(() => node.remove(), 5000);
}

// --- Page pieces -----------------------------------------------------------------------

function head(title, subtitle, ...right) {
  return el("div", { class: "adm-head" },
    el("div", {}, el("h1", { text: title }), subtitle ? el("p", { text: subtitle }) : null),
    el("div", { class: "adm-head-right" }, right));
}

function card(title, subtitle, ...content) {
  return el("section", { class: "card" }, title ? el("h2", { text: title }) : null,
    subtitle ? el("p", { class: "card-sub", text: subtitle }) : null, content);
}

function stat(label, value, note) {
  return el("div", { class: "stat" }, el("div", { class: "label", text: label }),
    el("div", { class: "value", text: value }), note ? el("div", { class: "note" }, note) : null);
}

function badge(text, kind = "quiet") {
  return el("span", { class: `badge ${kind}`, text });
}

function statusBadge(status) {
  return badge(status || "unknown", STATUS_KIND[status] || "quiet");
}

function facts(pairs) {
  const list = el("dl", { class: "facts" });
  for (const [label, value] of pairs) {
    if (value === undefined) continue;
    list.append(el("dt", { text: label }), el("dd", {}, value === null || value === "" ? "–" : value));
  }
  return list;
}

function emptyState(text) {
  return el("div", { class: "empty", text });
}

function loading() {
  return el("div", { class: "loading", text: "Loading…" });
}

function errorBox(error) {
  return el("div", { class: "alert error", role: "alert", text: error.message || String(error) });
}

function table(columns, rows, { onRow, empty = "Nothing to show" } = {}) {
  if (!rows || !rows.length) return emptyState(empty);
  const headRow = el("tr", {}, columns.map((column) => el("th", { scope: "col", class: column.num ? "num" : null, text: column.label })));
  const body = rows.map((row) => {
    const line = el("tr", { class: onRow ? "clickable" : null },
      columns.map((column) => el("td", { class: column.num ? "num" : null },
        column.render ? column.render(row) : (row[column.key] ?? "–"))));
    if (onRow) {
      line.tabIndex = 0;
      line.addEventListener("click", () => onRow(row));
      line.addEventListener("keydown", (event) => { if (event.key === "Enter") onRow(row); });
    }
    return line;
  });
  return el("div", { class: "table-wrap" }, el("table", { class: "admin-table" }, el("thead", {}, headRow), el("tbody", {}, body)));
}

function meter(value, limit, label) {
  const share = limit ? Math.min(1, value / limit) : 0;
  const fill = el("div", { class: `meter-fill ${share >= 0.9 ? "bad" : share >= 0.7 ? "warn" : ""}` });
  fill.style.width = `${Math.round(share * 100)}%`;
  return el("div", { class: "meter", role: "img", "aria-label": `${label}: ${value} of ${limit}` },
    el("div", { class: "meter-track" }, fill),
    el("div", { class: "meter-text" }, el("span", { text: label }), el("span", { text: `${Math.round(share * 100)}%` })));
}

function segmented(options, selected, onChange) {
  const group = el("div", { class: "seg", role: "group" });
  for (const [value, label] of options) {
    const button = el("button", { type: "button", "aria-pressed": String(value === selected), text: label });
    button.addEventListener("click", () => {
      for (const other of group.children) other.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-pressed", "true");
      onChange(value);
    });
    group.append(button);
  }
  return group;
}

function select(options, selected) {
  return el("select", { class: "pw-select" }, options.map(([value, label]) =>
    el("option", { value, text: label, selected: value === selected })));
}

function field(label, control, hint) {
  return el("label", { class: "pw-field" }, el("span", { class: "pw-label" }, label), control, hint ? el("span", { class: "pw-hint", text: hint }) : null);
}

function input(attributes = {}) {
  return el("input", { class: "pw-input", type: "text", autocomplete: "off", spellcheck: "false", ...attributes });
}

// --- Charts ----------------------------------------------------------------------------
// Columns grow from one baseline, at most 24px wide, with a rounded data end
// and a 2px gap between stacked segments. Every chart has a hover tooltip and
// a table view; two or more series get a legend.

function niceMax(value) {
  if (value <= 0) return 4;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) if (step * power >= value) return step * power;
  return 10 * power;
}

function columnPath(x, y, width, height, round) {
  const r = Math.min(round, width / 2, height);
  if (r <= 0) return `M${x},${y + height}V${y}H${x + width}V${y + height}Z`;
  return `M${x},${y + height}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${y + height}Z`;
}

function columnChart({ labels, series, format = fmt, unit = "", shortLabel = (label) => label }) {
  const width = 520, height = 220, left = 40, right = 6, top = 12, bottom = 26;
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const totals = labels.map((_, index) => series.reduce((sum, one) => sum + (one.values[index] || 0), 0));
  const max = niceMax(Math.max(0, ...totals));
  const y = (value) => top + plotHeight - (value / max) * plotHeight;
  const band = plotWidth / Math.max(1, labels.length);
  const barWidth = Math.max(2, Math.min(24, band * 0.62));
  const chart = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": series.map((one) => one.name).join(", ") });

  for (let step = 0; step <= 4; step += 1) {
    const value = (max / 4) * step;
    chart.append(svg("line", { class: step ? "gridline" : "baseline", x1: left, x2: width - right, y1: y(value), y2: y(value) }),
      svg("text", { class: "tick", x: left - 8, y: y(value) + 3, "text-anchor": "end" }, compact(value)));
  }
  const every = Math.max(1, Math.ceil(labels.length / 8));
  const container = el("div", { class: "chart" });
  const tip = el("div", { class: "tooltip", hidden: true });

  labels.forEach((label, index) => {
    const x = left + band * index + (band - barWidth) / 2;
    const highlight = svg("rect", { class: "hover-band", x: left + band * index, y: top, width: band, height: plotHeight });
    chart.append(highlight);
    let base = 0;
    const drawn = series.map((one) => ({ one, value: one.values[index] || 0 })).filter((part) => part.value > 0);
    drawn.forEach((part, position) => {
      const isTop = position === drawn.length - 1;
      const y0 = y(base), y1 = y(base + part.value);
      // The 2px gap in the surface colour separates touching segments.
      const segmentHeight = Math.max(1, y0 - y1 - (position > 0 ? 2 : 0));
      chart.append(svg("path", { class: part.one.cls, d: columnPath(x, y1, barWidth, segmentHeight, isTop ? 4 : 0) }));
      base += part.value;
    });
    if (index % every === 0) {
      chart.append(svg("text", { class: "tick", x: left + band * index + band / 2, y: height - 8, "text-anchor": "middle" }, shortLabel(label)));
    }
    const show = () => {
      highlight.classList.add("on");
      fill(tip, el("strong", { text: label }),
        series.map((one) => el("div", {}, el("span", {}, series.length > 1 ? el("i", { class: one.cls }) : null, one.name),
          el("b", { text: `${format(one.values[index] || 0)}${unit}` }))),
        series.length > 1 ? el("div", {}, el("span", { text: "Total" }), el("b", { text: `${format(totals[index])}${unit}` })) : null);
      const box = chart.getBoundingClientRect();
      tip.style.left = `${((left + band * index + band / 2) / width) * box.width}px`;
      tip.style.top = `${(y(totals[index]) / height) * box.height}px`;
      tip.hidden = false;
    };
    const hide = () => { highlight.classList.remove("on"); tip.hidden = true; };
    chart.append(svg("rect", {
      class: "hit", x: left + band * index, y: top, width: band, height: plotHeight + bottom, tabindex: "0",
      "aria-label": `${label}: ${series.map((one) => `${one.name} ${format(one.values[index] || 0)}`).join(", ")}`,
      onmouseenter: show, onmouseleave: hide, onfocus: show, onblur: hide,
    }));
  });
  container.append(chart, tip);
  return container;
}

function chartCard(title, subtitle, { labels, series, ...options }) {
  if (!labels.length) return card(title, subtitle, emptyState("No data in this period"));
  const legend = series.length > 1
    ? el("div", { class: "legend" }, series.map((one) => el("span", {}, el("i", { class: one.cls }), one.name)))
    : null;
  const body = el("div");
  const showChart = () => fill(body, columnChart({ labels, series, ...options }));
  const showTable = () => fill(body, table(
    [{ label: "Period", key: "period" }, ...series.map((one, index) => ({ label: one.name, num: true, render: (row) => fmt(row.values[index]) }))],
    labels.map((label, index) => ({ period: label, values: series.map((one) => one.values[index] || 0) })),
  ));
  const toggle = segmented([["chart", "Chart"], ["table", "Table"]], "chart", (view) => (view === "chart" ? showChart() : showTable()));
  showChart();
  return el("section", { class: "card" },
    el("div", { class: "chart-head" }, el("div", {}, el("h2", { text: title }), el("p", { class: "card-sub", text: subtitle })), toggle),
    legend, body);
}

function horizontalBars(items) {
  const max = Math.max(1, ...items.map((item) => item.value));
  return el("div", { class: "hbars" }, items.map((item) => {
    const fill = el("div", { class: "fill" });
    fill.style.width = `${(item.value / max) * 100}%`;
    return el("div", { class: "hbar", role: "img", "aria-label": `${item.name}: ${item.value}` },
      el("span", { class: "name", text: item.name }), el("div", { class: "track" }, fill), el("span", { class: "count", text: fmt(item.value) }));
  }));
}

// --- Modal and the two-step changes ------------------------------------------------------

function openModal({ title, subtitle, wide = false }) {
  const close = el("button", { type: "button", class: "modal-close", "aria-label": "Close", text: "×" });
  const body = el("div", { class: "modal-body" });
  const foot = el("div", { class: "modal-foot" });
  const dialog = el("div", { class: `modal ${wide ? "wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": title },
    el("div", { class: "modal-head" }, el("div", {}, el("h2", { text: title }), subtitle ? el("p", { text: subtitle }) : null), close),
    body, foot);
  const backdrop = el("div", { class: "modal-backdrop" }, dialog);
  let timer = null;
  const done = () => {
    clearInterval(timer);
    document.removeEventListener("keydown", onKey);
    backdrop.remove();
  };
  const onKey = (event) => { if (event.key === "Escape") done(); };
  close.addEventListener("click", done);
  backdrop.addEventListener("mousedown", (event) => { if (event.target === backdrop) done(); });
  document.addEventListener("keydown", onKey);
  fill(modalRoot, backdrop);
  // Focus starts on Close, never on a button that changes something.
  close.focus();
  return { body, foot, close: done, every: (seconds, tick) => { timer = setInterval(tick, seconds * 1000); } };
}

function planFacts(action, plan) {
  if (action === "user.credit") {
    return facts([["Account", plan.email || plan.user_id], ["Change", `${plan.minutes > 0 ? "+" : ""}${fmt(plan.minutes)} minutes`],
      ["Balance", `${fmt(plan.balance)} → ${fmt(plan.balance_after)} minutes`]]);
  }
  if (action === "job.cancel") {
    const charge = !plan.billed ? "nothing (billing is off)" : plan.refund ? "nothing (refund)" : `${fmt(plan.charge)} of ${fmt(plan.full_cost)} minutes`;
    return facts([["Job", el("span", { class: "mono", text: plan.job_id })], ["Now", statusBadge(plan.status)],
      ["Progress", `${Math.round((plan.progress || 0) * 100)}%`], ["Backend", plan.backend], ["Charged", charge]]);
  }
  return facts(Object.entries(plan).map(([key, value]) => [key.replaceAll("_", " "),
    typeof value === "boolean" ? (value ? "yes" : "no") : key.startsWith("expires") ? (value ? `in ${span(value)}` : "until removed") : value]));
}

// Every change: what it is and why, then the plan from the server, then one
// confirmation within two minutes. The server keeps what was reviewed.
function change(action, args, { title, describe, after }) {
  const modal = openModal({ title });
  const reason = input({ maxlength: "300", placeholder: REASON_REQUIRED.has(action) ? "Required; no email addresses" : "Optional; no email addresses" });
  const review = el("button", { type: "button", class: "btn gold", text: "Review" });
  const message = el("div");
  modal.body.append(el("div", { class: "steps" }, el("span", { class: "on", text: "1 · Reason" }), el("span", { text: "2 · Confirm" })),
    describe ? el("div", { class: "pw-field" }, describe) : null, field("Reason, kept in the audit log", reason), message);
  modal.foot.append(el("button", { type: "button", class: "btn", text: "Cancel", onclick: modal.close }), review);
  review.addEventListener("click", async () => {
    review.disabled = true;
    try {
      const planned = await post("/api/actions/plan", { action, args, reason: reason.value });
      confirmStep(modal, action, planned, after);
    } catch (error) {
      fill(message, errorBox(error));
      review.disabled = false;
    }
  });
}

function confirmStep(modal, action, planned, after) {
  const confirm = el("button", { type: "button", class: `btn ${planned.serious ? "danger solid" : "gold"}`, text: "Confirm" });
  const countdown = el("span", { class: "countdown" });
  const message = el("div");
  let left = planned.expires_in;
  const tick = () => {
    left -= 1;
    countdown.textContent = left > 0 ? `Expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "Expired: start again";
    if (left <= 0) confirm.disabled = true;
  };
  tick();
  modal.every(1, tick);
  fill(modal.body, 
    el("div", { class: "steps" }, el("span", { text: "1 · Reason" }), el("span", { class: "on", text: "2 · Confirm" })),
    planned.serious ? el("div", { class: "alert warn", text: "This affects users at once." }) : null,
    planFacts(action, planned.plan),
    el("p", { class: "pw-hint" }, "Reason: ", planned.reason || "none given"),
    message);
  fill(modal.foot, countdown, el("button", { type: "button", class: "btn", text: "Back out", onclick: modal.close }), confirm);
  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try {
      const done = await post("/api/actions/apply", { token: planned.token });
      modal.close();
      toast(`${done.title}: done`);
      (after || rerender)();
    } catch (error) {
      fill(message, errorBox(error));
    }
  });
}

function canChange(action) {
  return me && me.actions.includes(action);
}

// --- Overview ----------------------------------------------------------------------------

async function overview() {
  fill(main, head("Overview", "What the server is doing now"), loading());
  const refresh = el("button", { type: "button", class: "btn", text: "Refresh", onclick: () => overview() });
  let state;
  try {
    state = await api("/api/status");
  } catch (error) {
    fill(main, head("Overview", "What the server is doing now", refresh), errorBox(error));
    return;
  }
  const jobs = state.jobs;
  const worker = state.local_worker;
  const remote = state.remote;
  const notice = state.maintenance;
  const controlsActive = [
    state.controls.paused !== null ? badge("submissions paused", "warn") : null,
    state.controls.links_off !== null ? badge("links off", "warn") : null,
    state.controls.per_user_limit !== null ? badge(`per-user limit ${state.controls.per_user_limit}`, "info") : null,
    state.controls.blocked_users ? badge(`${state.controls.blocked_users} blocked`, "bad") : null,
  ].filter(Boolean);
  const refused = (state.video_sites || []).filter((site) => site.refused || site.recovering);

  fill(main, 
    head("Overview", `Updated ${when(Date.now() / 1000)} · refreshes every ${REFRESH_SECONDS} s`, refresh),
    notice ? el("div", { class: "alert warn", text: `Planned ${notice.action || "maintenance"}: ${notice.message || ""}` }) : null,
    el("div", { class: "stats" },
      el("div", { class: "stat" }, el("div", { class: "label", text: "Places used" }),
        el("div", { class: "value", text: `${fmt(state.places.used)} / ${fmt(state.places.limit)}` }),
        meter(state.places.used, state.places.limit || 1, "MAX_ACTIVE_JOBS")),
      stat("Queued", fmt(jobs.queued), `${fmt(jobs.preparing + jobs.uploading)} being checked or uploaded`),
      stat("Processing", fmt(jobs.processing), `${fmt(state.backends.local)} local · ${fmt(state.backends.remote)} remote`),
      stat("Local worker", worker ? "Online" : "No heartbeat",
        worker ? `${worker.capacity} at once · avg ${span(worker.average_job_seconds)} · seen ${ago(worker.updated_at)}` : badge("check the worker", "bad")),
      stat("Remote", remote.enabled ? `${fmt(remote.in_use)} / ${fmt(remote.limit)}` : "Off", remote.enabled ? "jobs on RunPod" : "OUTSOURCE_PROVIDER=none")),
    el("div", { class: "grid two" },
      card("Jobs by status", "Live, in Redis", horizontalBars(Object.entries(jobs).map(([name, value]) => ({ name, value })))),
      card("Disk", state.disk ? state.disk.path : "Data folder not mounted",
        state.disk ? [
          facts([["Job files", bytes(state.disk.jobs_bytes)], ["Free", `${bytes(state.disk.free_bytes)} of ${bytes(state.disk.total_bytes)}`]]),
          meter(state.disk.total_bytes - state.disk.free_bytes, state.disk.total_bytes, "Used"),
        ] : emptyState("No data folder")),
      card("Queues", "Messages not finished yet, including taken ones",
        table([{ label: "Queue", key: "queue" }, { label: "Messages", num: true, key: "messages" }, { label: "Dead", num: true, key: "dead" }],
          Object.entries(state.queues).map(([queue, counts]) => ({ queue, ...counts })))),
      card("Billing backups", null, facts([
        ["On this disk", fmt(state.backups.count)],
        ["Newest", state.backups.newest_at ? ago(state.backups.newest_at) : badge("none", "bad")],
        ["Off-site", !state.backups.offsite ? "off" : state.backups.offsite.error
          ? el("span", {}, badge("failing", "bad"), " ", state.backups.offsite.error)
          : `last copy ${ago(state.backups.offsite.succeeded_at)}`],
      ])),
      card("Controls", "Temporary limits from the command line or this page",
        controlsActive.length ? el("div", { class: "btn-row" }, controlsActive) : el("p", { class: "secondary", text: "None active" })),
      card("Video sites", "Sites refusing this server, or recovering",
        refused.length ? table([{ label: "Site", key: "site" }, { label: "State", render: (row) => badge(row.refused ? "refused" : "recovering", row.refused ? "bad" : "warn") }], refused)
          : el("p", { class: "secondary", text: "All sites accepted" })),
    ),
  );
}

// --- Jobs ------------------------------------------------------------------------------------

function jobs() {
  const filters = { status: "", backend: "", user: "", since: "1d" };
  const results = el("div");
  const load = async () => {
    fill(results, loading());
    try {
      const rows = await api("/api/jobs" + query({ ...filters, limit: 100 }));
      fill(results, card(null, null, table([
        { label: "Created", render: (row) => when(row.created_at) },
        { label: "Job", render: (row) => el("span", { class: "mono", text: shortId(row.job_id) }) },
        { label: "User", render: (row) => row.email || row.user_id || "–" },
        { label: "Status", render: (row) => statusBadge(row.status) },
        { label: "Backend", key: "backend" },
        { label: "Length", num: true, render: (row) => span(row.duration_seconds) },
        { label: "Error", render: (row) => (row.error ? String(row.error).slice(0, 80) : "–") },
      ], rows, { onRow: (row) => openJob(row.job_id), empty: "No jobs match" })));
    } catch (error) {
      fill(results, errorBox(error));
    }
  };
  const user = input({ placeholder: "email or user ID" });
  user.addEventListener("change", () => { filters.user = user.value.trim(); load(); });
  const backend = select([["", "Any"], ["local", "Local"], ["remote", "Remote"]], "");
  backend.addEventListener("change", () => { filters.backend = backend.value; load(); });
  const since = select([["1h", "Last hour"], ["1d", "Last day"], ["7d", "Last 7 days"], ["30d", "Last 30 days"]], "1d");
  since.addEventListener("change", () => { filters.since = since.value; load(); });
  fill(main, 
    head("Jobs", "From job records, kept for 30 days; open one for everything known about it"),
    el("div", { class: "pw-field" }, segmented([["", "All"], ["queued", "Queued"], ["processing", "Processing"], ["failed", "Failed"], ["completed", "Completed"], ["cancelled", "Cancelled"]],
      "", (value) => { filters.status = value; load(); })),
    el("div", { class: "filters" }, field("User", user), field("Backend", backend), field("Created", since)),
    results);
  load();
}

async function openJob(job) {
  const modal = openModal({ title: `Job ${shortId(job)}`, subtitle: job, wide: true });
  modal.body.append(loading());
  try {
    const details = await api(`/api/jobs/${encodeURIComponent(job)}`);
    const live = details.live || {};
    const record = details.record || {};
    const status = live.status || record.status;
    const parts = [
      facts([["Status", statusBadge(status)], ["User", record.email || record.user_id || live.user_id],
        ["Source", record.source_url || live.source_url || record.source], ["Backend", live.backend || record.backend],
        ["Progress", live.progress !== undefined ? `${Math.round(Number(live.progress) * 100)}%` : undefined],
        ["Created", when(record.created_at)], ["Started", when(record.started_at)], ["Finished", when(record.finished_at)],
        ["Length", span(record.duration_seconds)], ["Cost", record.cost_minutes !== undefined ? `${fmt(record.cost_minutes)} minutes` : undefined],
        ["Reservation", details.reservation ? `${details.reservation.status}, ${fmt(details.reservation.reserved_minutes)} reserved` : undefined],
        ["Files", details.files ? details.files.join(", ") || "none" : undefined],
        ["In Redis", details.live ? "yes" : "no: ended over an hour ago, or never queued"]]),
    ];
    if (record.error) parts.push(el("h3", { class: "section-title", text: "Full error (users never see this)" }), el("pre", { class: "error-text", text: record.error }));
    fill(modal.body, ...parts);
    if (canChange("job.cancel") && (status === "queued" || status === "processing")) {
      const refund = el("input", { type: "checkbox" });
      modal.foot.append(el("label", { class: "pw-check" }, refund, "Refund: charge nothing"),
        el("button", { type: "button", class: "btn danger", text: "Cancel job…", onclick: () => {
          change("job.cancel", { job: details.job_id, refund: refund.checked }, { title: `Cancel job ${shortId(details.job_id)}`,
            describe: el("p", { class: "secondary", text: refund.checked ? "Charged nothing." : "Charged like a user's own cancellation." }) });
        } }));
    }
    modal.foot.append(el("button", { type: "button", class: "btn", text: "Close", onclick: modal.close }));
  } catch (error) {
    fill(modal.body, errorBox(error));
  }
}

// --- Users -----------------------------------------------------------------------------------

function users() {
  const text = input({ placeholder: "part of an email address, at least 3 characters" });
  const results = el("div");
  const search = async () => {
    fill(results, loading());
    try {
      const rows = await api("/api/users" + query({ text: text.value.trim() }));
      fill(results, card(null, null, table([
        { label: "Email", render: (row) => row.email || "–" },
        { label: "User ID", render: (row) => el("span", { class: "mono", text: row.user_id }) },
        { label: "Minutes", num: true, render: (row) => fmt(row.minutes) },
      ], rows, { onRow: (row) => openUser(row.user_id), empty: "No user matches" })));
    } catch (error) {
      fill(results, errorBox(error));
    }
  };
  text.addEventListener("keydown", (event) => { if (event.key === "Enter") search(); });
  fill(main, 
    head("Users", "Opening an account is written to the audit log"),
    el("div", { class: "filters" }, field("Find", text), el("div", { class: "pw-field" }, el("button", { type: "button", class: "btn gold", text: "Find", onclick: search }))),
    results);
}

async function openUser(who) {
  currentView = "users";
  setCurrent("users");
  fill(main, head("Account", who), loading());
  let account;
  try {
    account = await api(`/api/users/${encodeURIComponent(who)}`);
  } catch (error) {
    fill(main, head("Account", who), errorBox(error));
    return;
  }
  const back = el("button", { type: "button", class: "btn", text: "← Users", onclick: () => open("users") });
  const actions = [];
  if (canChange("user.credit")) {
    actions.push(el("button", { type: "button", class: "btn gold", text: "Change balance…", onclick: () => creditDialog(account) }));
  }
  if (account.blocked && canChange("limits.unblock")) {
    actions.push(el("button", { type: "button", class: "btn", text: "Unblock…", onclick: () => change("limits.unblock", { user: account.user_id },
      { title: `Unblock ${account.email || account.user_id}`, after: () => openUser(account.user_id) }) }));
  } else if (!account.blocked && canChange("limits.block")) {
    actions.push(el("button", { type: "button", class: "btn danger", text: "Block…", onclick: () => blockDialog(account) }));
  }
  fill(main, 
    head(account.email || account.user_id, account.user_id, back, actions),
    account.blocked ? el("div", { class: "alert error", text: `Blocked${account.blocked.expires_in ? ` for ${span(account.blocked.expires_in)} more` : ""}: ${account.blocked.reason || "no reason given"}` }) : null,
    account.minutes < 0 ? el("div", { class: "alert warn", text: "The balance is negative: a refunded purchase's minutes were already used." }) : null,
    el("div", { class: "stats" },
      stat("Balance", `${fmt(account.minutes)} min`),
      stat("Reserved", `${fmt(account.reserved_minutes)} min`, "by running jobs"),
      stat("Available", `${fmt(account.available_minutes)} min`),
      account.free_minutes ? stat("Free minutes", account.free_minutes.next_grant_at ? `next ${when(account.free_minutes.next_grant_at)}` : "on next request",
        `last ${when(account.free_minutes.last_grant_at)}`) : null),
    el("div", { class: "grid two" },
      card("Balance changes", "Newest first", table([
        { label: "When", render: (row) => when(row.at) }, { label: "Kind", key: "kind" },
        { label: "Minutes", num: true, render: (row) => `${row.minutes > 0 ? "+" : ""}${fmt(row.minutes)}` },
        { label: "Note", render: (row) => row.note || "–" },
      ], account.balance_changes, { empty: "No changes recorded" })),
      card("Purchases", "Without purchase tokens", table([
        { label: "Product", key: "product_id" }, { label: "Order", render: (row) => el("span", { class: "mono", text: row.order_id || "–" }) },
        { label: "Minutes", num: true, render: (row) => fmt(row.minutes) },
        { label: "Voided", render: (row) => (row.voided_at ? badge(when(row.voided_at), "bad") : "–") },
      ], account.purchases, { empty: "No purchases" }))),
    el("h2", { class: "section-title", text: "Recent jobs" }),
    card(null, null, table([
      { label: "Created", render: (row) => when(row.created_at) },
      { label: "Job", render: (row) => el("span", { class: "mono", text: shortId(row.job_id) }) },
      { label: "Status", render: (row) => statusBadge(row.status) },
      { label: "Length", num: true, render: (row) => span(row.duration_seconds) },
      { label: "Cost", num: true, render: (row) => fmt(row.cost_minutes) },
    ], account.jobs, { onRow: (row) => openJob(row.job_id), empty: "No jobs" })));
}

function creditDialog(account) {
  const direction = { value: "add" };
  const minutes = input({ type: "number", min: "1", max: String(me.credit_limit), step: "1", inputmode: "numeric", placeholder: "minutes" });
  const modal = openModal({ title: `Change the balance of ${account.email || account.user_id}` });
  const message = el("div");
  modal.body.append(
    el("div", { class: "pw-field" }, segmented([["add", "Add minutes"], ["take", "Take minutes away"]], "add", (value) => { direction.value = value; })),
    field("Minutes", minutes, `At most ${me.credit_limit} at once from this page. Now ${fmt(account.minutes)}.`), message);
  modal.foot.append(el("button", { type: "button", class: "btn", text: "Cancel", onclick: modal.close }),
    el("button", { type: "button", class: "btn gold", text: "Next", onclick: () => {
      const count = Number.parseInt(minutes.value, 10);
      if (!Number.isInteger(count) || count < 1) { fill(message, errorBox(new Error("Give a whole number of minutes"))); return; }
      modal.close();
      change("user.credit", { user: account.user_id, minutes: direction.value === "add" ? count : -count },
        { title: `${direction.value === "add" ? "Add" : "Take away"} ${count} minutes`, after: () => openUser(account.user_id) });
    } }));
}

function blockDialog(account) {
  const duration = select(DURATIONS, "1d");
  const modal = openModal({ title: `Block ${account.email || account.user_id}` });
  modal.body.append(el("p", { class: "secondary", text: "They cannot start transcriptions or check a link's cost. Running jobs continue; they never see the reason." }),
    field("For", duration));
  modal.foot.append(el("button", { type: "button", class: "btn", text: "Cancel", onclick: modal.close }),
    el("button", { type: "button", class: "btn danger", text: "Next", onclick: () => {
      modal.close();
      change("limits.block", { user: account.user_id, for: duration.value || null }, { title: `Block ${account.email || account.user_id}`, after: () => openUser(account.user_id) });
    } }));
}

// --- Billing -----------------------------------------------------------------------------------

function billing() {
  const settings = { per: "day", since: "30d" };
  const results = el("div");
  const price = input({ type: "number", min: "0", step: "0.01", placeholder: "e.g. 1.20", inputmode: "decimal" });
  const load = async () => {
    fill(results, loading());
    let report;
    try {
      report = await api("/api/billing" + query(settings));
    } catch (error) {
      fill(results, errorBox(error));
      return;
    }
    const parts = [];
    const short = (label) => (settings.per === "day" ? label.slice(5) : label);
    if (report.minutes) {
      const totals = report.minutes.total;
      const periods = report.minutes.periods;
      parts.push(el("div", { class: "stats" },
        stat("Sold", `${fmt(totals.purchase)} min`), stat("Given free", `${fmt(totals.free)} min`),
        stat("Used", `${fmt(Math.abs(totals.used))} min`), stat("Voided", `${fmt(Math.abs(totals.voided))} min`),
        stat("By hand", `${totals.admin > 0 ? "+" : ""}${fmt(totals.admin)} min`)));
      if (report.minutes.recorded_since && report.minutes.recorded_since > report.since) {
        parts.push(el("div", { class: "alert info", text: `Balance changes are recorded since ${when(report.minutes.recorded_since)}; earlier ones are not counted.` }));
      }
      const labels = periods.map((row) => row.period);
      parts.push(el("div", { class: "grid two" },
        chartCard("Minutes added", `Per ${settings.per}, UTC`, {
          labels, shortLabel: short, unit: " min",
          series: [
            { name: "Sold", cls: "s1", values: periods.map((row) => row.purchase) },
            { name: "Free", cls: "s2", values: periods.map((row) => row.free) },
          ],
        }),
        chartCard("Minutes used", `By transcriptions, per ${settings.per}`, {
          labels, shortLabel: short, unit: " min",
          series: [{ name: "Used", cls: "s1", values: periods.map((row) => Math.abs(row.used)) }],
        })));
    } else {
      parts.push(el("div", { class: "alert info", text: "Billing is off, so there are no minutes to report." }));
    }
    const jobLabels = [...new Set(report.jobs.periods.map((row) => row.period))].sort();
    const jobsFor = (backend) => jobLabels.map((label) => {
      const row = report.jobs.periods.find((one) => one.period === label && one.backend === backend);
      return row ? row.jobs : 0;
    });
    const hours = report.jobs.remote_seconds / 3600;
    const cost = () => {
      const perHour = Number.parseFloat(price.value);
      return Number.isFinite(perHour) && perHour >= 0 ? `≈ ${(hours * perHour).toFixed(2)} at ${perHour}/h` : "enter your price per hour";
    };
    const costNote = el("span", { text: cost() });
    price.oninput = () => { costNote.textContent = cost(); };
    parts.push(el("div", { class: "grid two" },
      chartCard("Completed jobs", `Per ${settings.per} and backend; records are kept for ${span(report.jobs.records_kept_seconds)}`, {
        labels: jobLabels, shortLabel: short,
        series: [{ name: "Local", cls: "s1", values: jobsFor("local") }, { name: "Remote", cls: "s2", values: jobsFor("remote") }],
      }),
      card("Remote GPU time", "From start to finish, including the download, so it overstates billed GPU time",
        el("div", { class: "stats" }, stat("Hours", hours.toFixed(1)), el("div", { class: "stat" }, el("div", { class: "label", text: "Estimated cost" }), el("div", { class: "note" }, costNote))),
        field("Your price per GPU hour", price))));
    if (report.negative_balances) {
      const negative = report.negative_balances;
      parts.push(el("h2", { class: "section-title", text: "Negative balances" }), card(null,
        negative.count ? `${negative.count} ${negative.count === 1 ? "user owes" : "users owe"} ${fmt(negative.owed_minutes)} minutes: a refunded purchase's minutes were already used` : "No user owes minutes",
        table([{ label: "Email", render: (row) => row.email || row.user_id }, { label: "Minutes", num: true, render: (row) => fmt(row.minutes) }],
          negative.users, { onRow: (row) => openUser(row.user_id), empty: "None" }),
        report.voided_synced_at ? el("p", { class: "pw-hint", text: `Voided purchases last read from Google Play ${ago(report.voided_synced_at)}` }) : null));
    }
    fill(results, ...parts);
  };
  fill(main, 
    head("Billing", "Whether the price per minute covers the GPU time; all times UTC"),
    el("div", { class: "btn-row pw-field" },
      segmented([["day", "Per day"], ["month", "Per month"]], "day", (value) => { settings.per = value; load(); }),
      segmented([["7d", "7 days"], ["30d", "30 days"], ["90d", "90 days"], ["365d", "1 year"]], "30d", (value) => { settings.since = value; load(); })),
    results);
  load();
}

// --- Limits ------------------------------------------------------------------------------------

async function limitsView() {
  fill(main, head("Limits", "Temporary controls; they never stop running or queued jobs"), loading());
  let data;
  try {
    data = await api("/api/limits");
  } catch (error) {
    fill(main, head("Limits", null), errorBox(error));
    return;
  }
  const lasting = (expires) => (expires ? `for ${span(expires)} more` : "until removed");
  const paused = data.paused, links = data.links_off, perUser = data.per_user_limit;
  const messageDialog = (action, title, defaultHint) => {
    const message = input({ maxlength: "200", placeholder: defaultHint });
    const duration = select(DURATIONS, "1h");
    const modal = openModal({ title });
    modal.body.append(field("What users are told", message, "Leave empty for the default message"), field("For", duration));
    modal.foot.append(el("button", { type: "button", class: "btn", text: "Cancel", onclick: modal.close }),
      el("button", { type: "button", class: "btn gold", text: "Next", onclick: () => {
        modal.close();
        change(action, { message: message.value || null, for: duration.value || null }, { title });
      } }));
  };
  const perUserDialog = () => {
    const limit = input({ type: "number", min: "1", step: "1", inputmode: "numeric", placeholder: String(data.configured_per_user_limit) });
    const duration = select(DURATIONS, "6h");
    const modal = openModal({ title: "Per-user job limit" });
    modal.body.append(field("Jobs in progress per user", limit), field("For", duration));
    modal.foot.append(el("button", { type: "button", class: "btn", text: "Cancel", onclick: modal.close }),
      el("button", { type: "button", class: "btn gold", text: "Next", onclick: () => {
        modal.close();
        change("limits.per_user", { limit: Number.parseInt(limit.value, 10), for: duration.value || null }, { title: "Change the per-user limit" });
      } }));
  };
  const button = (action, text, kind, onclick) => (canChange(action) ? el("button", { type: "button", class: `btn ${kind}`, text, onclick }) : null);
  fill(main, 
    head("Limits", canChange("limits.pause") ? "Temporary controls; they never stop running or queued jobs" : "Read-only here; change limits on the server: python scripts/admin.py limits"),
    el("div", { class: "grid three" },
      card("Submissions", null,
        paused ? el("div", {}, badge("paused", "warn"), el("p", { class: "secondary", text: ` ${lasting(paused.expires_in)}: ${paused.message}` })) : badge("accepted", "good"),
        el("div", { class: "btn-row pw-field" },
          paused ? button("limits.resume", "Resume", "gold", () => change("limits.resume", {}, { title: "Accept new transcriptions" }))
            : button("limits.pause", "Pause…", "danger", () => messageDialog("limits.pause", "Pause new transcriptions", "New transcriptions are paused; try again later")))),
      card("Video links", "Uploads are accepted either way",
        links ? el("div", {}, badge("off", "warn"), el("p", { class: "secondary", text: ` ${lasting(links.expires_in)}: ${links.message}` })) : badge("accepted", "good"),
        el("div", { class: "btn-row pw-field" },
          links ? button("limits.links_on", "Turn on", "gold", () => change("limits.links_on", {}, { title: "Accept video links" }))
            : button("limits.links_off", "Turn off…", "danger", () => messageDialog("limits.links_off", "Turn video links off", "Video links are not accepted at the moment; upload the file instead")))),
      card("Per-user limit", "Jobs in progress per signed-in user",
        el("div", { class: "stat" }, el("div", { class: "value", text: fmt(perUser ? perUser.limit : data.configured_per_user_limit) }),
          el("div", { class: "note", text: perUser ? `${lasting(perUser.expires_in)}; MAX_ACTIVE_JOBS_PER_USER is ${data.configured_per_user_limit}` : "MAX_ACTIVE_JOBS_PER_USER" })),
        el("div", { class: "btn-row pw-field" },
          button("limits.per_user", "Change…", "", perUserDialog),
          perUser ? button("limits.per_user", "Back to the setting", "", () => change("limits.per_user", { limit: null }, { title: "Per-user limit back to the setting" })) : null))),
    el("h2", { class: "section-title", text: "Video sites" }),
    card(null, "A site that rate-limits the server is refused for a while, then read one link at a time",
      table([
        { label: "Site", key: "site" },
        { label: "State", render: (row) => (row.refused ? badge("refused", "bad") : row.recovering ? badge("recovering", "warn") : badge("accepted", "good")) },
        { label: "Until", render: (row) => (row.refused && row.refused.expires_in ? `${span(row.refused.expires_in)} more` : "–") },
        { label: "Reason", render: (row) => (row.refused && row.refused.reason) || row.last_reason || "–" },
        { label: "", render: (row) => button("limits.sites_reset", "Accept again…", "", () => change("limits.sites_reset", { site: row.site }, { title: `Accept ${row.site} again` })) },
      ], data.video_sites, { empty: "No site has refused the server" })),
    el("h2", { class: "section-title", text: "Blocked users" }),
    card(null, null, table([
      { label: "User", render: (row) => row.email || row.user_id },
      { label: "For", render: (row) => lasting(row.expires_in) },
      { label: "Reason", render: (row) => row.reason || "–" },
      { label: "", render: (row) => button("limits.unblock", "Unblock…", "", () => change("limits.unblock", { user: row.user_id }, { title: `Unblock ${row.email || row.user_id}` })) },
    ], data.blocked, { empty: "No one is blocked" })));
}

// --- Audit -----------------------------------------------------------------------------------------

async function auditView() {
  fill(main, head("Audit log", "Every admin action, newest first"), loading());
  let rows;
  try {
    rows = await api("/api/audit?limit=200");
  } catch (error) {
    fill(main, head("Audit log", null), errorBox(error));
    return;
  }
  const filter = input({ placeholder: "filter by action or who" });
  const results = el("div");
  const show = () => {
    const text = filter.value.trim().toLowerCase();
    const shown = text ? rows.filter((row) => `${row.action} ${row.actor}`.toLowerCase().includes(text)) : rows;
    fill(results, card(null, null, table([
      { label: "When", render: (row) => when(row.at) },
      { label: "Who", render: (row) => el("span", { class: "mono", text: row.actor }) },
      { label: "Action", render: (row) => badge(row.action, row.action.startsWith("web.") ? "info" : "busy") },
      { label: "Target", render: (row) => (row.target ? el("span", { class: "mono", text: shortId(row.target) }) : "–") },
      { label: "Detail", render: (row) => row.detail || "–" },
      { label: "Reason", render: (row) => row.reason || "–" },
    ], shown, { empty: "No admin actions match" })));
  };
  filter.addEventListener("input", show);
  fill(main, head("Audit log", "Every admin action, newest first; the billing backups include it"),
    el("div", { class: "filters" }, field("Filter", filter)), results);
  show();
}

// --- Access ---------------------------------------------------------------------------------------
// It can only go down, applies to every administrator, and only the server's
// command line raises it again. So it takes two steps, nothing is
// preselected, and the new access name must be typed exactly.

const MODE_TEXT = {
  "read-only": "The page shows everything but changes nothing.",
  disabled: "The page shows nothing but how to restore it.",
};

function accessView() {
  const now = el("div", { class: "stats" }, stat("Access now", me.effective), stat("ADMIN_WEB_MODE", me.configured));
  if (!me.lower_to.length) {
    fill(main, head("Access", "Lower this page's access"), now, el("div", { class: "alert info", text: "It cannot be lowered further." }));
    return;
  }
  const reason = input({ maxlength: "300", placeholder: "Required; no email addresses" });
  const choices = me.lower_to.map((mode) => el("label", {}, el("input", { type: "radio", name: "mode", value: mode }),
    el("span", {}, el("strong", { text: mode }), el("small", { text: MODE_TEXT[mode] }))));
  const message = el("div");
  const next = el("button", { type: "button", class: "btn", text: "Continue" });
  next.addEventListener("click", async () => {
    const chosen = choices.map((label) => label.querySelector("input")).find((radio) => radio.checked);
    if (!chosen) { fill(message, errorBox(new Error("Choose the new access"))); return; }
    try {
      const planned = await post("/api/access/plan", { mode: chosen.value, reason: reason.value });
      lowerConfirm(planned, reason.value);
    } catch (error) {
      fill(message, errorBox(error));
    }
  });
  fill(main, 
    head("Access", "Lower this page's access, for example when a signed-in device is lost"),
    now,
    el("div", { class: "alert warn", text: "Lowering applies at once to every administrator. Nothing on this page raises it again: only the server's command line does (python scripts/admin.py web restore)." }),
    card("Lower to", null, el("div", { class: "choice" }, choices), field("Reason", reason), message, el("div", { class: "btn-row" }, next)));
}

function lowerConfirm(planned, reason) {
  const modal = openModal({ title: `Lower access to ${planned.mode}` });
  const typed = input({ "aria-label": `Type ${planned.type_to_confirm}` });
  const apply = el("button", { type: "button", class: "btn danger solid", text: `Lower to ${planned.mode}`, disabled: true });
  const message = el("div");
  typed.addEventListener("input", () => { apply.disabled = typed.value.trim() !== planned.type_to_confirm; });
  // Enter never applies it; only the button does.
  typed.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
  apply.addEventListener("click", async () => {
    apply.disabled = true;
    try {
      me = await post("/api/access/lower", { mode: planned.mode, reason, token: planned.token, expires: planned.expires, confirm: typed.value });
      modal.close();
      toast(`Access lowered to ${planned.mode}`);
      start();
    } catch (error) {
      fill(message, errorBox(error));
    }
  });
  modal.body.append(
    facts([["From", planned.current], ["To", planned.mode], ["Applies to", planned.applies_to], ["Undo", planned.undo]]),
    field(el("span", {}, "Type ", el("strong", { class: "mono", text: planned.type_to_confirm }), " to confirm, within two minutes"), typed),
    message);
  modal.foot.append(el("button", { type: "button", class: "btn", text: "Back out", onclick: modal.close }), apply);
}

// --- Start -------------------------------------------------------------------------------------------

const VIEWS = { overview, jobs, users, billing, limits: limitsView, audit: auditView, access: accessView };

function setCurrent(view) {
  for (const button of document.querySelectorAll(".adm-nav button")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}

function open(view) {
  currentView = view;
  setCurrent(view);
  // Only the section's name: never an account or a job, which stay out of the history.
  history.replaceState(null, "", `#${view}`);
  clearInterval(refreshTimer);
  if (view === "overview") {
    // Only reads what is live now; nothing is kept between refreshes.
    refreshTimer = setInterval(() => { if (document.visibilityState === "visible" && currentView === "overview" && !modalRoot.firstChild) overview(); }, REFRESH_SECONDS * 1000);
  }
  VIEWS[view]();
}

function rerender() {
  if (currentView) VIEWS[currentView]();
}

async function start() {
  try {
    me = await api("/api/me");
  } catch (error) {
    document.getElementById("who").textContent = "Not signed in";
    fill(main, head("Server admin", null), errorBox(error));
    return;
  }
  document.getElementById("who").textContent = me.email;
  const mode = document.getElementById("mode");
  mode.textContent = me.effective;
  mode.className = `tag ${me.effective}`;
  const nav = document.getElementById("nav");
  if (me.effective === "disabled") {
    nav.hidden = true;
    clearInterval(refreshTimer);
    fill(main, head("Disabled", "This page's access was lowered"),
      (me.lowerings || []).map((row) => el("div", { class: "alert error", text: `Lowered to ${row.mode} by ${row.actor} at ${when(row.at)}: ${row.reason || "no reason"}` })),
      card("Restore it on the server", null, el("pre", { class: "error-text", text: "python scripts/admin.py web restore --reason \"…\"" })));
    return;
  }
  nav.hidden = false;
  const asked = location.hash.slice(1);
  open(currentView && VIEWS[currentView] ? currentView : Object.hasOwn(VIEWS, asked) ? asked : "overview");
}

document.getElementById("nav").addEventListener("click", (event) => {
  const view = event.target.closest("button")?.dataset.view;
  if (view) open(view);
});
start();
