// The admin web page. Everything shown is built with DOM nodes and
// textContent, never parsed as HTML: job errors, links, and emails come from users.
"use strict";

const main = document.getElementById("main");
let me = null;

// --- Building blocks -----------------------------------------------------------

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "text") node.textContent = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (value === true) node.setAttribute(name, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(name, value);
  }
  for (const child of children) node.append(child);
  return node;
}

function isTime(key, value) {
  return typeof value === "number" && value > 1e9 && /(^at$|_at$)/.test(key);
}

function show(value, key = "") {
  if (value === null || value === undefined || value === "") return document.createTextNode("–");
  if (isTime(key, value)) return document.createTextNode(new Date(value * 1000).toISOString().replace("T", " ").slice(0, 16) + " UTC");
  if (Array.isArray(value)) {
    if (value.length === 0) return document.createTextNode("none");
    if (value.every((item) => item && typeof item === "object" && !Array.isArray(item))) return table(value);
    return document.createTextNode(value.join(", "));
  }
  if (typeof value === "object") return details(value);
  return document.createTextNode(String(value));
}

function details(object) {
  const list = el("dl");
  for (const [key, value] of Object.entries(object)) {
    list.append(el("dt", { text: key.replaceAll("_", " ") }), el("dd", {}, show(value, key)));
  }
  return list;
}

function table(rows, onRow) {
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const head = el("tr", {}, ...columns.map((column) => el("th", { scope: "col", text: column.replaceAll("_", " ") })));
  const body = rows.map((row) => el("tr", {}, ...columns.map((column) => {
    const cell = el("td");
    if (onRow && column === onRow.column && row[column]) {
      cell.append(el("button", { type: "button", class: "link", text: String(row[column]), onclick: () => onRow.open(row[column]) }));
    } else {
      cell.append(show(row[column], column));
    }
    return cell;
  })));
  return el("div", { class: "scroll" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...body)));
}

function field(label, input) {
  return el("label", {}, label, input);
}

function message(text, kind = "error") {
  return el("p", { class: kind, role: kind === "error" ? "alert" : null, text });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return data;
}

function query(parameters) {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(parameters)) if (value) search.set(name, value);
  return search.toString() ? `?${search}` : "";
}

async function render(title, load, ...before) {
  const output = el("div", {}, message("Loading…", "muted"));
  main.replaceChildren(el("h2", { text: title }), ...before, output);
  try {
    const content = await load();
    output.replaceChildren(content);
  } catch (error) {
    output.replaceChildren(message(error.message));
  }
}

// --- Views ---------------------------------------------------------------------

const views = {
  status: () => render("Status", async () => details(await api("/api/status"))),

  jobs: () => {
    const status = el("select", {}, el("option", { value: "", text: "any" }),
      ...["queued", "processing", "completed", "failed", "cancelled"].map((value) => el("option", { value, text: value })));
    const backend = el("select", {}, ...["", "local", "remote"].map((value) => el("option", { value, text: value || "any" })));
    const user = el("input", { type: "text", autocomplete: "off", placeholder: "email or user ID" });
    const since = el("input", { type: "text", autocomplete: "off", placeholder: "1d", size: 5 });
    const results = el("div");
    const load = async () => {
      results.replaceChildren(message("Loading…", "muted"));
      try {
        const rows = await api("/api/jobs" + query({ status: status.value, backend: backend.value, user: user.value.trim(), since: since.value.trim() }));
        results.replaceChildren(rows.length ? table(rows, { column: "job_id", open: openJob }) : message("No jobs match", "muted"));
      } catch (error) {
        results.replaceChildren(message(error.message));
      }
    };
    main.replaceChildren(el("h2", { text: "Jobs" }), el("div", { class: "controls" },
      field("Status", status), field("Backend", backend), field("User", user), field("Since", since),
      el("button", { type: "button", class: "button", text: "Show", onclick: load })), results);
    load();
  },

  users: () => {
    const text = el("input", { type: "text", autocomplete: "off", placeholder: "part of an email address" });
    const results = el("div");
    const search = async () => {
      results.replaceChildren(message("Loading…", "muted"));
      try {
        const rows = await api("/api/users" + query({ text: text.value.trim() }));
        results.replaceChildren(rows.length ? table(rows, { column: "user_id", open: openUser }) : message("No user matches", "muted"));
      } catch (error) {
        results.replaceChildren(message(error.message));
      }
    };
    main.replaceChildren(el("h2", { text: "Users" }),
      el("p", { class: "muted", text: "Opening an account is recorded in the audit log." }),
      el("div", { class: "controls" }, field("Find", text), el("button", { type: "button", class: "button", text: "Find", onclick: search })),
      results);
  },

  billing: () => {
    const per = el("select", {}, el("option", { value: "day", text: "day" }), el("option", { value: "month", text: "month" }));
    const since = el("input", { type: "text", value: "30d", size: 5, autocomplete: "off" });
    const controls = el("div", { class: "controls" }, field("Per", per), field("Since", since),
      el("button", { type: "button", class: "button", text: "Show", onclick: () => load() }));
    const load = () => render("Billing", async () => details(await api("/api/billing" + query({ per: per.value, since: since.value.trim() }))), controls);
    load();
  },

  limits: () => render("Limits", async () => details(await api("/api/limits")),
    el("p", { class: "muted", text: "Read-only here. Change limits on the server: python scripts/admin.py limits --help" })),

  audit: () => render("Audit log", async () => {
    const rows = await api("/api/audit?limit=100");
    return rows.length ? table(rows) : message("No admin actions yet", "muted");
  }),

  access: accessView,
};

function openJob(job) {
  render(`Job ${job}`, async () => details(await api(`/api/jobs/${encodeURIComponent(job)}`)));
}

function openUser(user) {
  render(`User ${user}`, async () => details(await api(`/api/users/${encodeURIComponent(user)}`)));
}

// --- Lowering the page's own access ---------------------------------------------
// It can only go down, applies to every administrator, and only the server's
// command line can raise it again. So it is on a page of its own, nothing is
// preselected, it takes two steps, and the mode is typed before it applies.

function accessView() {
  setCurrent(null);
  const heading = el("h2", { text: "Access" });
  const now = el("p", {}, "This page is ", el("strong", { text: me.effective }), ` (ADMIN_WEB_MODE is ${me.configured}).`);
  if (!me.lower_to.length) {
    main.replaceChildren(heading, now, message("It cannot be lowered further.", "notice"));
    return;
  }
  const choices = me.lower_to.map((mode) => el("label", {},
    el("input", { type: "radio", name: "mode", value: mode }), ` ${mode}`));
  const reason = el("input", { type: "text", autocomplete: "off", size: 40, placeholder: "why; no email addresses" });
  const step = el("div");
  const next = el("button", { type: "button", class: "button secondary", text: "Continue" });
  next.addEventListener("click", async () => {
    const chosen = choices.map((label) => label.querySelector("input")).find((input) => input.checked);
    if (!chosen) { step.replaceChildren(message("Choose a mode.")); return; }
    try {
      const plan = await api("/api/access/plan", { method: "POST", body: JSON.stringify({ mode: chosen.value, reason: reason.value }) });
      confirmStep(step, plan, reason.value);
    } catch (error) {
      step.replaceChildren(message(error.message));
    }
  });
  main.replaceChildren(heading, now,
    el("div", { class: "warning" },
      el("p", { text: "Lowering applies at once to every administrator. It cannot be undone from this page: only the server's command line raises it again." })),
    el("fieldset", {}, el("legend", { text: "Lower to" }), ...choices),
    el("div", { class: "controls" }, field("Reason", reason), next),
    step);
}

function confirmStep(step, plan, reason) {
  const typed = el("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": `Type ${plan.type_to_confirm}` });
  const apply = el("button", { type: "button", class: "button danger", text: `Lower to ${plan.mode}`, disabled: true });
  typed.addEventListener("input", () => { apply.disabled = typed.value.trim() !== plan.type_to_confirm; });
  // Enter never applies it; only the button does.
  typed.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
  apply.addEventListener("click", async () => {
    apply.disabled = true;
    try {
      me = await api("/api/access/lower", { method: "POST", body: JSON.stringify({
        mode: plan.mode, reason, token: plan.token, expires: plan.expires, confirm: typed.value,
      }) });
      start();
    } catch (error) {
      step.append(message(error.message));
    }
  });
  step.replaceChildren(el("div", { class: "notice" },
    details({ from: plan.current, to: plan.mode, applies_to: plan.applies_to, undo: plan.undo }),
    el("p", {}, "Type ", el("strong", { text: plan.type_to_confirm }), " to confirm, within two minutes:"),
    el("div", { class: "controls" }, typed, apply)));
}

// --- Start -----------------------------------------------------------------------

function setCurrent(view) {
  for (const button of document.querySelectorAll("nav button")) {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}

function open(view) {
  setCurrent(view);
  views[view]();
}

async function start() {
  try {
    me = await api("/api/me");
  } catch (error) {
    document.getElementById("who").textContent = "";
    main.replaceChildren(message(error.message));
    return;
  }
  document.getElementById("who").textContent = `Signed in as ${me.email}`;
  document.getElementById("mode").textContent = me.effective;
  document.getElementById("access-link").hidden = false;
  if (me.effective === "disabled") {
    document.getElementById("nav").hidden = true;
    document.getElementById("access-link").hidden = true;
    const lowered = (me.lowerings || []).map((row) => `Disabled by ${row.actor} at ${new Date(row.at * 1000).toISOString().slice(0, 16).replace("T", " ")} UTC: ${row.reason || "no reason"}`);
    main.replaceChildren(el("h2", { text: "Disabled" }), ...lowered.map((text) => el("p", { text })),
      el("p", { class: "notice", text: "Restore it on the server: python scripts/admin.py web restore" }));
    return;
  }
  document.getElementById("nav").hidden = false;
  open("status");
}

document.getElementById("nav").addEventListener("click", (event) => {
  const view = event.target.closest("button")?.dataset.view;
  if (view) open(view);
});
document.getElementById("access-link").addEventListener("click", () => views.access());
start();
