"use strict";
/* DoseGuard web client. Plain JS, no build step. The browser only displays state and
   submits intents; every rule is enforced again on the server. */

const TOKEN = document.querySelector('meta[name="dg-token"]').content;
const $ = (sel) => document.querySelector(sel);
const main = $("#main");
const setMain = (...kids) => main.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));

const S = {
  status: null,
  offline: false,
  view: "today",
  keys: {},
  audio: null,
  setup: null,
  history: null,
  historySel: null,
  historyDetail: null,
  historyVersion: -1,
  busy: null,
  chat: [],
  cameraImg: null,
};

const STATE_LABEL = {
  PERSON_CHECK: "Recipient check", EMPTY_TRAY_CHECK: "Empty tray check", DISPENSING: "Dispensing",
  DOSE_CHECK: "Dose check", RECIPIENT_RECHECK: "Recheck and release", RELEASE_PENDING: "Releasing",
  COLLECTION_CHECK: "Collection", FAULT: "Fault", UNCERTAIN: "Outcome uncertain",
};
const STEPS = [
  ["PERSON_CHECK", "Recipient"], ["EMPTY_TRAY_CHECK", "Empty tray"], ["DISPENSING", "Dispense"],
  ["DOSE_CHECK", "Dose check"], ["RECIPIENT_RECHECK", "Recheck + release"], ["COLLECTION_CHECK", "Collection"],
];
const STEP_INDEX = { PERSON_CHECK: 0, EMPTY_TRAY_CHECK: 1, DISPENSING: 2, DOSE_CHECK: 3, RECIPIENT_RECHECK: 4, RELEASE_PENDING: 4, COLLECTION_CHECK: 5 };

/* ---------------------------------------------------------------- utilities */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value" || k === "checked" || k === "selected") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const uuid = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : Math.random().toString(16).slice(2) + Date.now().toString(16));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, body = {}, opts = {}) {
  const payload = { request_id: uuid(), ...body };
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const r = await fetch(path, {
        method: "POST",
        headers: { "content-type": "application/json", "x-doseguard-token": TOKEN },
        body: JSON.stringify(payload),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const err = new Error(data.message || `Request failed (${r.status})`);
        err.data = data; err.status = r.status;
        throw err;
      }
      return data;
    } catch (e) {
      // A network failure is retried once with the same request_id; the server dedupes it.
      if (e.status || opts.noRetry || attempt === 1) throw e;
      await sleep(400);
    }
  }
}

let toastTimer = null;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), bad ? 6000 : 3500);
}

async function act(label, fn) {
  if (S.busy) return;
  S.busy = label;
  rerender(true);
  try {
    const r = await fn();
    return r;
  } catch (e) {
    toast(e.message, true);
    return null;
  } finally {
    S.busy = null;
    rerender(true);
  }
}

function go(view) {
  if (location.hash !== "#" + view) location.hash = view;
  else route();
}

/* ---------------------------------------------------------------- sound */
function enableSound() {
  try {
    S.audio = new (window.AudioContext || window.webkitAudioContext)();
    S.audio.resume();
    chime(true);
    toast("Reminder sound is on. Keep this page open.");
  } catch {
    toast("This browser blocked sound. Visual alerts still work.", true);
  }
  renderChips();
}

function chime(short = false) {
  const ctx = S.audio;
  if (!ctx || ctx.state !== "running") return;
  const notes = short ? [880] : [660, 880, 990];
  notes.forEach((f, i) => {
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.type = "sine"; o.frequency.value = f;
    const t0 = ctx.currentTime + i * 0.28;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(0.25, t0 + 0.03);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.6);
    o.connect(g).connect(ctx.destination);
    o.start(t0); o.stop(t0 + 0.65);
  });
}

function handleSounds(s) {
  let last = Number(localStorage.getItem("dg-last-sound") || 0);
  if (s.last_event_id != null && last > s.last_event_id) last = 0; // database was reset
  const fresh = (s.sounds || []).filter((e) => e.event_id > last);
  if (!fresh.length) return;
  localStorage.setItem("dg-last-sound", String(Math.max(...fresh.map((e) => e.event_id))));
  chime();
}

/* ---------------------------------------------------------------- polling */
async function poll() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    if (!r.ok) throw new Error();
    const s = await r.json();
    S.status = s;
    S.offline = false;
    handleSounds(s);
    rerender();
  } catch {
    S.offline = true;
    renderChips();
  } finally {
    setTimeout(poll, 500);
  }
}

function rerender(force = false) {
  const s = S.status;
  if (!s) return;
  renderChips();
  document.title = s.due_count ? `(${s.due_count}) Dose due · DoseGuard` : "DoseGuard";
  $("#demo-badge").hidden = !s.demo_mode;
  const due = $("#nav-due");
  due.hidden = !s.due_count;
  due.textContent = s.due_count || "";
  $("#nav-verify").hidden = !(s.workflow && s.workflow.txn);
  $("#sidebar-foot").textContent = `${s.server_time_local}\n${s.timezone}`;
  const fn = VIEWS[S.view];
  if (!fn || !fn.key) return;
  const key = JSON.stringify([fn.key(s), S.busy]);
  if (!force && S.keys[S.view] === key) {
    if (fn.tick) fn.tick(s);
    return;
  }
  S.keys[S.view] = key;
  fn.render(s);
}

/* ---------------------------------------------------------------- top bar */
function renderChips() {
  const s = S.status;
  const box = $("#chips");
  box.replaceChildren();
  if (S.offline) {
    box.append(h("span", { class: "chip bad" }, h("span", { class: "dot" }), "App server not responding"));
    return;
  }
  if (!s) return;
  const soundOn = S.audio && S.audio.state === "running";
  box.append(soundOn
    ? h("span", { class: "chip ok" }, h("span", { class: "dot" }), "Reminder sound on")
    : h("button", { class: "chip warn", onclick: enableSound, title: "Browsers need one click before they can play sound" },
        h("span", { class: "dot" }), "Reminders muted · Enable sound"));
  const d = s.device, sim = d.simulated ? " (simulated)" : "";
  let devChip;
  if (!d.connected) devChip = ["bad", "Device disconnected" + sim];
  else if (d.fault) devChip = ["bad", `Fault: ${d.fault}${sim}`];
  else if (s.workflow.armed) devChip = ["ok", "Device armed" + sim];
  else devChip = ["warn", "Connected, not armed" + sim];
  box.append(h("a", { class: `chip ${devChip[0]} ${d.simulated ? "sim" : ""}`, href: "#device" }, h("span", { class: "dot" }), devChip[1]));
  const cam = s.camera;
  if (cam.simulated) box.append(h("span", { class: "chip sim" }, h("span", { class: "dot" }), "Camera simulated"));
  else if (cam.face && cam.face.error) box.append(h("span", { class: "chip bad" }, h("span", { class: "dot" }), "Camera unavailable"));
  else box.append(h("span", { class: "chip ok" }, h("span", { class: "dot" }), "Camera ready"));
  if (cam.pose_required) box.append(h("span", { class: "chip warn" }, h("span", { class: "dot" }), `Camera pose: ${cam.pose_required === "FACE" ? "face" : "tray"}`));
}

/* ---------------------------------------------------------------- shared bits */
function swatch(color) {
  return color ? h("span", { class: "swatch", style: `background:${color}` }) : h("span", { class: "swatch none" });
}

function cardPills(c) {
  const r = c.reminder || {};
  const out = [];
  const rs = {
    SCHEDULED: ["", "Scheduled"], DUE: ["due", "Due now"], SNOOZED: ["info", `Snoozed until ${r.snoozed_until_local || ""}`],
    ACKNOWLEDGED: ["", "Reminder dismissed"], NEEDS_REVIEW: ["bad", "Needs review"],
  }[r.state];
  if (rs) out.push(rs);
  if (r.state === "CLOSED") {
    const cr = {
      RELEASED: ["ok", "Released"], USER_REPORTED_TAKEN: ["info", "User reported taken"],
      REVIEWED_NOT_TAKEN: ["", "Closed, not taken"], WORKFLOW_UNRESOLVED: ["bad", "Outcome uncertain"],
    }[r.close_reason];
    if (cr) out.push(cr);
  }
  const ws = c.workflow_state;
  if (STATE_LABEL[ws] && !["RELEASED"].includes(ws)) out.push([ws === "FAULT" || ws === "UNCERTAIN" ? "bad" : "accent", `Verifying: ${STATE_LABEL[ws]}`]);
  if (ws === "RELEASED") out.push(["due", "Collection pending"]);
  if (ws === "COMPLETE") out.push(c.collection === "OBSERVED" ? ["ok", "Collection observed"] : ["", "Collection not checked"]);
  if (c.self_report && c.delivery_mode === "DEVICE") out.push(["info", "User reported taken"]);
  if (c.review_reason && c.review_reason.startsWith("DST")) out.push(["bad", "Daylight saving review"]);
  return h("span", { class: "pills" }, out.map(([cls, t]) => h("span", { class: `pill ${cls}` }, t)));
}

function doseLine(c) {
  return [c.label, ` × ${c.quantity}`, c.strength_text ? h("span", { class: "muted" }, ` · ${c.strength_text}`) : null];
}

function whenText(c) {
  return c.due_day && c.due_day !== "Today" ? `${c.due_day} ${c.due_local}` : c.due_local;
}

function modeText(c) {
  return c.delivery_mode === "DEVICE" ? `Dispenser · cartridge ${c.cartridge}` : "Reminder only · stored separately";
}

function startBlockers(s) {
  const out = [];
  if (!s.device.connected) out.push("Device disconnected.");
  else if (s.device.fault) out.push(`Device fault: ${s.device.fault}. Use supervised recovery.`);
  else if (!s.workflow.armed) out.push("Device not armed. Inspect the enclosure and arm it in Device.");
  if (s.workflow.txn) out.push("Another verification is in progress.");
  return out;
}

async function startDose(c) {
  const r = await act("start", () => api("/api/doses/start", { dose_id: c.dose_id, plan_version: c.plan_version_id }));
  if (r) go("verify");
}

/* ---------------------------------------------------------------- Today */
const today = {
  key: (s) => [s.today, s.next_dose && s.next_dose.dose_id, s.demo_mode, s.workflow.armed, s.workflow.txn && s.workflow.txn.state,
    s.device.connected, s.device.fault, s.plan && s.plan.plan_version_id, s.server_time_local.split(",")[0]],
  render(s) {
    const [day] = s.server_time_local.split(",");
    const active = s.today.filter((c) => ["DUE", "SNOOZED", "NEEDS_REVIEW"].includes(c.reminder.state) && c.workflow_state === "NOT_STARTED");
    const head = h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, day),
        h("p", { class: "sub" }, s.next_dose ? ["Next: ", s.next_dose.label, " · ", whenText(s.next_dose)] : "Nothing else scheduled")));
    const left = h("div", { class: "stack" });
    if (!s.plan) {
      left.append(h("div", { class: "card" }, h("h2", {}, "Set up a confirmed schedule"),
        h("p", { class: "muted", style: "margin:6px 0 14px" }, "Enroll the recipient, add each entry, review it, then confirm the plan. Nothing is scheduled until you confirm."),
        h("button", { class: "btn btn-primary", onclick: () => go("setup") }, "Open Setup")));
    }
    for (const c of active) left.append(dueCard(c, s));
    if (s.workflow.txn) {
      const t = s.workflow.txn;
      left.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Verification in progress"), h("span", { class: "pill accent" }, t.stage)),
        h("p", { class: "muted", style: "margin-bottom:12px" }, t.doses.map((d) => `${d.label} × ${d.quantity}`).join(" + ")),
        h("button", { class: "btn btn-primary", onclick: () => go("verify") }, "Open verification")));
    }
    left.append(timelineCard(s));
    setMain(head, h("div", { class: "grid-2" }, left, sideColumn(s)));
  },
};

function dueCard(c, s) {
  const r = c.reminder;
  const review = r.state === "NEEDS_REVIEW";
  const when = review ? "Needs review" : r.state === "SNOOZED" ? `Snoozed until ${r.snoozed_until_local}` : "Due now";
  const card = h("div", { class: `card due-card ${review ? "review" : "due"}` },
    h("div", { class: "when", style: review ? "color:var(--danger)" : "" }, when),
    h("div", { class: "what" }, ...doseLine(c)),
    h("div", { class: "meta" }, `${c.due_local} · ${modeText(c)}`, review ? " · Missed doses are never combined or dispensed automatically." : ""));
  const row = h("div", { class: "btn-row" });
  if (c.delivery_mode === "DEVICE") {
    const blockers = startBlockers(s);
    row.append(h("button", { class: "btn btn-primary btn-large", disabled: !!blockers.length || !!S.busy, onclick: () => startDose(c) }, "Start verification"));
    if (blockers.length) card.append(h("p", { class: "small", style: "color:var(--due);margin:-6px 0 12px;font-weight:600" }, blockers.join(" ")));
  } else {
    row.append(h("button", { class: "btn btn-primary btn-large", disabled: !!S.busy, onclick: () => act("self", () => api("/api/doses/self-report", { dose_id: c.dose_id })).then((x) => x && toast("Recorded as your own report. It is not verified.")) }, "Mark taken"));
  }
  if (r.state === "DUE") row.append(h("button", { class: "btn", disabled: !!S.busy, onclick: () => act("snooze", () => api("/api/reminders/snooze", { reminder_id: r.reminder_id })) }, `Snooze ${s.config.snooze_minutes} min`));
  row.append(h("button", { class: "btn", disabled: !!S.busy, onclick: () => act("ack", () => api("/api/reminders/ack", { reminder_id: r.reminder_id })).then((x) => x && toast("Reminder dismissed. The dose is not marked taken.")) }, "Dismiss reminder"));
  if (review) row.append(h("button", { class: "btn btn-quiet", disabled: !!S.busy, onclick: () => act("close", () => api("/api/reminders/close", { reminder_id: r.reminder_id })) }, "Close without taking"));
  card.append(row);
  if (c.delivery_mode === "REMINDER_ONLY") card.append(h("p", { class: "tiny muted", style: "margin-top:10px" }, "Mark taken is your own report. DoseGuard does not verify it."));
  return card;
}

function timelineCard(s) {
  const card = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Today"),
    s.plan ? h("span", { class: "muted small" }, `Plan v${s.plan.version}${s.plan.recipient ? " · " + s.plan.recipient : ""}`) : null));
  if (!s.today.length) {
    card.append(h("div", { class: "empty" }, s.demo_mode ? "No demo doses yet. Trigger a reminder to start." : "Nothing scheduled today."));
    return card;
  }
  const ul = h("ul", { class: "timeline" });
  for (const c of s.today) {
    const acts = [];
    const r = c.reminder;
    if (r.state === "ACKNOWLEDGED" && c.workflow_state === "NOT_STARTED" && !c.outcome) {
      if (c.delivery_mode === "DEVICE") acts.push(h("button", { class: "btn btn-quiet small", disabled: !!startBlockers(s).length || !!S.busy, onclick: () => startDose(c) }, "Start verification"));
      else acts.push(h("button", { class: "btn btn-quiet small", onclick: () => act("self", () => api("/api/doses/self-report", { dose_id: c.dose_id })) }, "Mark taken"));
    }
    if (["RELEASED", "COMPLETE"].includes(c.workflow_state) && !c.self_report && c.delivery_mode === "DEVICE") {
      acts.push(h("button", { class: "btn btn-quiet small", onclick: () => confirmTaken(c) }, "I took it"));
    }
    ul.append(h("li", {},
      h("span", { class: "t" }, c.due_local),
      swatch(c.class_color),
      h("div", {}, h("div", { class: "name" }, ...doseLine(c)), h("div", { class: "sub" }, modeText(c))),
      h("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end" }, acts, cardPills(c))));
  }
  card.append(ul);
  return card;
}

async function confirmTaken(c) {
  const r = await act("confirm", () => api("/api/doses/confirm", { dose_id: c.dose_id }));
  if (r) toast("Recorded as user reported taken. Swallowing is not verified.");
}

function sideColumn(s) {
  const col = h("div", { class: "stack" });
  const d = s.device;
  col.append(h("div", { class: "card" }, h("h3", { style: "margin-bottom:12px" }, "Status"),
    h("dl", { class: "kv" },
      h("dt", {}, "Next"), h("dd", {}, s.next_dose ? `${whenText(s.next_dose)} · ${s.next_dose.label}` : "None scheduled"),
      h("dt", {}, "Recipient"), h("dd", {}, (s.plan && s.plan.recipient) || "Not set"),
      h("dt", {}, "Device"), h("dd", {}, !d.connected ? "Disconnected" : d.fault ? `Fault: ${d.fault}` : s.workflow.armed ? "Armed" : "Not armed",
        d.simulated ? h("span", { class: "badge badge-sim", style: "margin-left:8px" }, "SIMULATED") : null),
      h("dt", {}, "Sound"), h("dd", {}, S.audio && S.audio.state === "running" ? "On" : "Muted (visual alerts only)"))));
  if (s.demo_mode) {
    const entries = (s.plan && s.plan.entries) || [];
    const dev = entries.filter((e) => e.delivery_mode === "DEVICE").map((e) => e.entry_key);
    const rem = entries.filter((e) => e.delivery_mode !== "DEVICE").map((e) => e.entry_key);
    const trig = (keys, label) => act("demo", () => api("/api/demo/trigger", { entry_keys: keys })).then((x) => x && toast(`${label}: reminder in 30 seconds.`));
    col.append(h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Demo"), h("span", { class: "badge badge-demo" }, "DEMO MODE")),
      h("p", { class: "small muted", style: "margin-bottom:12px" }, "Fictional records, kept apart from ordinary ones. Uses the real clock."),
      h("div", { class: "stack", style: "gap:8px" },
        h("button", { class: "btn btn-primary", disabled: !!S.busy || !dev.length, onclick: () => trig(dev, "Tokens A + B") }, "Trigger A + B reminder in 30 s"),
        h("button", { class: "btn", disabled: !!S.busy || !dev.length, onclick: () => trig(dev.slice(0, 1), "Token A") }, "Trigger token A only"),
        h("button", { class: "btn", disabled: !!S.busy || !rem.length, onclick: () => trig(rem, "Reminder-only") }, "Trigger reminder-only entry"),
        h("button", { class: "btn btn-quiet btn-danger", disabled: !!S.busy || !!s.workflow.txn, onclick: () => act("demo", () => api("/api/demo/reset", {})).then((x) => x && toast("Demo records cleared.")) }, "Reset demo records"))));
  }
  return col;
}

/* ---------------------------------------------------------------- Verification */
const verify = {
  key: (s) => {
    const t = s.workflow.txn;
    return [t && { ...t, observations: Object.fromEntries(Object.entries(t.observations || {}).map(([k, o]) => [k, { ...o, age_s: null }])) },
      s.workflow.armed, s.workflow.busy, s.workflow.pending_operation, s.device.connected, s.device.fault, s.device.door_closed,
      s.camera.mode, s.vision.sim, s.today.map((c) => [c.dose_id, c.reminder.state, c.workflow_state, c.self_report])];
  },
  tick(s) {
    const t = s.workflow.txn;
    if (!t) return;
    for (const [k, o] of Object.entries(t.observations || {})) {
      const el = document.getElementById(`age-${k}`);
      if (el) el.textContent = ageText(o);
    }
  },
  render(s) {
    const t = s.workflow.txn;
    if (!t) return renderNoTxn(s);
    const failed = t.state === "FAULT" || t.state === "UNCERTAIN";
    const idx = STEP_INDEX[t.state];
    const stepper = h("div", { class: "stepper", role: "list" }, STEPS.map(([st, label], i) =>
      h("div", { role: "listitem", class: "step " + (failed ? "" : i < idx ? "done" : i === idx ? "current" : ""), "aria-current": i === idx ? "step" : null },
        h("span", { class: "n" }, i < idx && !failed ? "Done" : `Step ${i + 1}`), label)));
    const head = h("div", { class: "page-head" }, h("div", {}, h("h1", {}, t.stage),
      h("p", { class: "sub" }, t.doses.map((d) => `${d.label} × ${d.quantity}`).join(" + "))));
    const left = h("div", { class: "stack" }, cameraCard(s, t));
    const right = h("div", { class: "stack" }, actionCard(s, t), countsCard(t), evidenceCard(t));
    setMain(head, stepper, h("div", { class: "grid-2" }, left, right));
  },
};

function ageText(o) {
  if (o.age_s == null) return "from an earlier session";
  if (o.age_s < 60) return `${Math.round(o.age_s)} s ago`;
  return `${Math.round(o.age_s / 60)} min ago`;
}

const POSE_HELP = {
  FACE: "Recipient faces the laptop camera, about an arm's length away, alone in frame, in even light.",
  TRAY: "Point the camera at the tray so all four corner markers are visible. Keep it still.",
};

function cameraCard(s, t) {
  const pose = t.pose;
  const cam = h("div", { class: "camera" });
  if (s.camera.simulated) {
    cam.append(h("div", { class: "camera-sim" }, h("div", { class: "big" }, pose === "TRAY" ? "▦" : "◉"),
      h("div", {}, "Simulated camera"), h("div", { class: "tiny" }, "Results come from the simulator, not a real image.")),
      h("span", { class: "sim-label" }, "SIMULATED"));
  } else {
    if (!S.cameraImg) S.cameraImg = h("img", { alt: "Live camera preview" });
    cam.append(S.cameraImg);
  }
  if (pose) cam.append(h("span", { class: "pose" }, pose === "FACE" ? "Pose: recipient face" : "Pose: tray"));
  const card = h("div", { class: "card" }, cam, pose ? h("p", { class: "small", style: "margin-top:12px" }, POSE_HELP[pose]) : null);
  if (s.vision.sim) {
    const face = h("select", { "aria-label": "Simulated face", onchange: (e) => api("/api/sim/vision", { face: e.target.value }) },
      [["recipient", "Enrolled recipient"], ["unknown", "Unknown person"], ["two_faces", "Two faces"], ["none", "Nobody in view"]]
        .map(([v, l]) => h("option", { value: v, selected: s.vision.sim.face === v }, l)));
    const tray = h("select", { "aria-label": "Simulated tray view", onchange: (e) => api("/api/sim/vision", { tray: e.target.value }) },
      [["auto", "Tray as simulated"], ["touching", "Objects touching"], ["markers_missing", "Markers hidden"], ["blurry", "Blurry"]]
        .map(([v, l]) => h("option", { value: v, selected: s.vision.sim.tray === v }, l)));
    card.append(h("div", { class: "btn-row", style: "margin-top:12px" }, h("span", { class: "badge badge-sim" }, "SIMULATOR"), face, tray));
  }
  return card;
}

function actionCard(s, t) {
  const card = h("div", { class: "card" });
  const wfBusy = s.workflow.busy || S.busy;
  for (const b of t.blocks || []) card.append(h("div", { class: "block-box", role: "alert" }, b.message));
  if ((t.blocks || []).length) card.append(h("div", { style: "height:14px" }));
  const zone = h("div", { class: "action-zone" });
  if (t.state === "FAULT" || t.state === "UNCERTAIN") {
    zone.append(h("p", {}, "Nothing will be repeated automatically. A supervisor inspects the device, then uses supervised recovery."),
      h("button", { class: "btn btn-primary btn-large", onclick: () => go("device") }, "Open Device controls"));
  } else if (t.state === "DISPENSING" || t.state === "RELEASE_PENDING") {
    zone.append(h("div", { class: "busy-line" }, h("span", { class: "spinner" }),
      t.state === "DISPENSING" ? "Dispensing. Wait for the device to finish." : "Waiting for the device to confirm release."));
  } else if (wfBusy) {
    zone.append(h("div", { class: "busy-line" }, h("span", { class: "spinner" }),
      t.pose === "FACE" ? "Checking. Hold still and face the camera." : "Checking the tray. Keep it still."));
  } else if (t.state === "RECIPIENT_RECHECK") {
    zone.append(h("button", { class: "btn btn-primary btn-large", disabled: !t.release_available, onclick: release }, "Recheck recipient and release"),
      h("p", { class: "small muted" }, t.release_available ? "The recipient faces the camera. The tray unlocks only if the match passes now."
        : "Release is unavailable until every condition above is met."));
  } else if (t.next_action) {
    zone.append(h("button", { class: "btn btn-primary btn-large", onclick: () => runCheck(t) }, t.next_action));
  }
  if (t.state === "COLLECTION_CHECK" && !wfBusy) {
    zone.append(h("p", { class: "small" }, "The door is unlocked. Take the tokens, then check that the tray is empty."),
      h("button", { class: "btn btn-quiet", onclick: () => act("finish", () => api("/api/doses/finish", { txn_id: t.txn_id })) }, "Finish without collection check"));
  }
  if (t.can_cancel && !wfBusy) zone.append(h("button", { class: "btn btn-quiet", onclick: () => act("cancel", () => api("/api/doses/cancel", { txn_id: t.txn_id })) }, "Cancel verification"));
  card.append(zone);
  return card;

  async function release() {
    const r = await act("release", () => api("/api/doses/release", { txn_id: t.txn_id }, { noRetry: false }));
    if (r) toast("Release requested. Waiting for the device to confirm.");
  }
}

async function runCheck(t) {
  const r = await act("check", () => api("/api/doses/check", { txn_id: t.txn_id }));
  if (!r) return;
  if (r.result === "PASS") toast("Check passed.");
  else toast(r.message || `Check result: ${r.result}`, true);
}

function countsCard(t) {
  const obs = t.observations.CLASS_COUNTS || (t.state === "EMPTY_TRAY_CHECK" || t.state === "COLLECTION_CHECK" ? t.observations.TRAY_EMPTY : null);
  const observed = obs && obs.observed;
  const tbody = h("tbody");
  for (const [cls, n] of Object.entries(t.expected.by_class)) {
    const seen = observed ? observed[cls] : null;
    const match = seen == null ? null : seen === n;
    tbody.append(h("tr", {},
      h("td", {}, h("span", { style: "display:inline-flex;gap:8px;align-items:center" }, swatch(t.class_colors[cls]), t.class_labels[cls])),
      h("td", { class: "num" }, n),
      h("td", { class: "num" }, seen == null ? "–" : seen),
      h("td", {}, match == null ? h("span", { class: "muted small" }, "Not checked") : match ? h("span", { class: "pill ok" }, "Match") : h("span", { class: "pill bad" }, "Mismatch"))));
  }
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h3", {}, "Dose contents"), h("span", { class: "muted small" }, "Expected after dispensing")),
    h("table", { class: "counts" }, h("thead", {}, h("tr", {}, h("th", {}, "Token"), h("th", {}, "Expected"), h("th", {}, "Observed"), h("th", {}, ""))), tbody),
    obs && obs !== t.observations.CLASS_COUNTS ? h("p", { class: "tiny muted", style: "margin-top:8px" }, "Observed column shows the latest empty-tray check.") : null);
}

function evidenceCard(t) {
  const rows = [["FACE_MATCH", "Recipient"], ["TRAY_EMPTY", "Empty tray"], ["CLASS_COUNTS", "Dose counts"]];
  const list = h("dl", { class: "kv" });
  for (const [k, label] of rows) {
    const o = t.observations[k];
    list.append(h("dt", {}, label), h("dd", {}, o ? [
      h("span", { class: `pill ${o.result === "PASS" ? "ok" : o.result === "FAIL" ? "bad" : "due"}` }, o.result.replace("_", " ").toLowerCase()),
      " ", h("span", { class: "muted small", id: `age-${k}` }, ageText(o)),
      o.simulated ? h("span", { class: "badge badge-sim", style: "margin-left:6px" }, "SIM") : null,
    ] : h("span", { class: "muted" }, "Not yet")));
  }
  return h("div", { class: "card" }, h("h3", { style: "margin-bottom:12px" }, "Evidence"), list,
    h("p", { class: "tiny muted", style: "margin-top:12px" }, "Release needs a recipient match from the last few seconds and a dose check from the last 15 seconds, with the tray unchanged since."));
}

function renderNoTxn(s) {
  const startable = s.today.filter((c) => c.delivery_mode === "DEVICE" && c.workflow_state === "NOT_STARTED" && !c.outcome &&
    ["DUE", "SNOOZED", "ACKNOWLEDGED", "NEEDS_REVIEW"].includes(c.reminder.state));
  const done = s.today.filter((c) => ["RELEASED", "COMPLETE"].includes(c.workflow_state) && c.delivery_mode === "DEVICE");
  const blockers = startBlockers(s);
  const card = h("div", { class: "card" }, h("h2", { style: "margin-bottom:12px" }, "Due for verification"));
  if (!startable.length) card.append(h("div", { class: "empty" }, "No device dose is due right now."));
  for (const c of startable) {
    card.append(h("div", { class: "hist-row", style: "cursor:default" }, h("span", { class: "t" }, c.due_local),
      h("div", {}, h("div", { style: "font-weight:600" }, ...doseLine(c)), h("div", { class: "muted small" }, modeText(c))),
      h("button", { class: "btn btn-primary", disabled: !!blockers.length || !!S.busy, onclick: () => startDose(c) }, "Start verification")));
  }
  if (startable.length && blockers.length) card.append(h("p", { class: "small", style: "color:var(--due);font-weight:600;margin-top:10px" }, blockers.join(" ")));
  const ready = h("div", { class: "card" }, h("h3", { style: "margin-bottom:12px" }, "Readiness"),
    h("dl", { class: "kv" },
      h("dt", {}, "Device"), h("dd", {}, s.device.connected ? (s.device.fault ? `Fault: ${s.device.fault}` : "Connected") : "Disconnected"),
      h("dt", {}, "Armed"), h("dd", {}, s.workflow.armed ? "Yes" : h("a", { href: "#device" }, "No · arm in Device")),
      h("dt", {}, "Camera"), h("dd", {}, s.camera.simulated ? "Simulated" : s.camera.face && s.camera.face.error ? s.camera.face.error : "Ready"),
      h("dt", {}, "Recipient"), h("dd", {}, (s.plan && s.plan.recipient) || h("a", { href: "#setup" }, "Not enrolled"))));
  const stack = h("div", { class: "stack" }, card);
  if (done.length) {
    const dc = h("div", { class: "card" }, h("h3", { style: "margin-bottom:6px" }, "Released today"),
      h("p", { class: "small muted", style: "margin-bottom:10px" }, "Collection observed, Released and User reported taken are separate records. Swallowing is never verified."));
    for (const c of done) {
      dc.append(h("div", { class: "hist-row", style: "cursor:default" }, h("span", {}, c.due_local), h("div", {}, ...doseLine(c)),
        h("div", { class: "btn-row" }, cardPills(c), !c.self_report ? h("button", { class: "btn", onclick: () => confirmTaken(c) }, "I took it") : null)));
    }
    stack.append(dc);
  }
  setMain(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Verification"),
    h("p", { class: "sub" }, "Recipient check, empty tray, dispense, dose check, then recheck and release."))),
    h("div", { class: "grid-2" }, stack, ready));
}

/* ---------------------------------------------------------------- Schedules */
const schedules = {
  key: (s) => [s.plan, s.timezone],
  render(s) {
    const head = h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Schedules"),
      h("p", { class: "sub" }, s.plan ? `Plan version ${s.plan.version}, confirmed ${new Date(s.plan.confirmed_at).toLocaleString()} · ${s.timezone}` : "No confirmed plan")),
      h("button", { class: "btn btn-primary", onclick: () => { S.setup = null; go("setup"); } }, s.plan ? "Edit schedule" : "Create schedule"));
    if (!s.plan || !s.plan.entries.length) {
      setMain(head, h("div", { class: "card empty" }, s.demo_mode ? "The demo plan has fictional entries with no recurring times. Use Trigger on Today." : "Nothing is scheduled until a plan is confirmed."));
      return;
    }
    const classes = {};
    const rows = s.plan.entries.map((e) => h("li", {},
      h("span", { class: "t" }, e.times.length ? e.times.map(fmtTime).join(", ") : "On demand"),
      swatch(e.delivery_mode === "DEVICE" ? colorFor(e.class_id) : null),
      h("div", {}, h("div", { class: "name" }, e.label, ` × ${e.quantity}`, e.strength_text ? h("span", { class: "muted" }, ` · ${e.strength_text}`) : null),
        h("div", { class: "sub" }, e.delivery_mode === "DEVICE" ? `Dispenser · cartridge ${e.cartridge} · ${e.class_id} tokens` : "Reminder only · stored separately")),
      h("div", { class: "btn-row" }, e.label_id ? h("a", { href: `/api/labels/${e.label_id}/image`, target: "_blank", title: "Label photo" },
        h("img", { class: "label-mini", src: `/api/labels/${e.label_id}/image`, alt: `Label photo for ${e.label}` })) : null,
        h("span", { class: `pill ${e.delivery_mode === "DEVICE" ? "accent" : ""}` }, e.delivery_mode === "DEVICE" ? "Device" : "Reminder only"))));
    setMain(head, h("div", { class: "card" }, h("ul", { class: "timeline" }, rows)),
      h("p", { class: "small muted", style: "margin-top:14px" }, "Editing creates a new plan version. Doses that already started, or reminders already issued, are kept as they were."));
  },
};
function fmtTime(hhmm) {
  const [H, M] = hhmm.split(":").map(Number);
  return `${((H + 11) % 12) + 1}:${String(M).padStart(2, "0")} ${H < 12 ? "AM" : "PM"}`;
}
function colorFor(classId) {
  return { blue: "#2F6FD6", orange: "#E8822A" }[classId] || "#999";
}

/* ---------------------------------------------------------------- Setup */
const setup = {
  key: null,
  async render() {
    setMain(h("div", { class: "busy-line" }, h("span", { class: "spinner" }), "Loading setup"));
    const data = await fetch("/api/setup").then((r) => r.json());
    if (!S.setup || S.setup.plan_version_id !== (data.plan && data.plan.plan_version_id) || S.setup.reload) {
      const plan = data.plan;
      S.setup = {
        plan_version_id: plan ? plan.plan_version_id : null,
        draft: {
          person_id: plan ? plan.person_id : (data.persons.at(-1) || {}).person_id || null,
          timezone: plan ? plan.timezone : data.default_timezone,
          timezone_confirmed: false, dst_reviewed: false,
          entries: plan && plan.entries.length && plan.plan_id !== "demo"
            ? plan.entries.map((e) => ({ ...e, confirmed: false }))
            : [blankEntry(data)],
        },
        errors: [], dst: null,
      };
    }
    S.setup.data = data;
    drawSetup();
  },
};

function blankEntry(data) {
  return { entry_key: "", label: "", strength_text: "", quantity: 1, delivery_mode: "DEVICE",
    class_id: (data.token_classes[0] || {}).class_id || null, times: ["08:00"], confirmed: false, suggested: {}, label_id: null,
    label_photos: 0, container: "bottle" };
}

function drawSetup() {
  const st = S.setup, data = st.data, d = st.draft;
  const s = S.status;
  const errFor = (i, f) => st.errors.filter((e) => e.entry === i && e.field === f).map((e) => h("div", { class: "field-error" }, e.message));
  const globalErr = st.errors.filter((e) => e.entry == null).map((e) => h("div", { class: "field-error" }, e.message));

  // 1. Recipient
  const nameIn = h("input", { placeholder: "Display name, e.g. Teammate A", maxlength: 40 });
  const consent = h("input", { type: "checkbox", id: "consent" });
  const persons = data.persons.length
    ? h("div", { class: "stack", style: "gap:8px;margin-top:6px" }, data.persons.map((p) =>
      h("label", { class: "check" }, h("input", { type: "radio", name: "person", checked: d.person_id === p.person_id, onchange: () => { d.person_id = p.person_id; } }),
        h("span", {}, p.label, p.simulated ? h("span", { class: "badge badge-sim", style: "margin-left:8px" }, "SIMULATED ENROLLMENT") : null,
          h("span", { class: "muted small" }, ` · consent recorded ${new Date(p.consent_at).toLocaleDateString()}`)))))
    : h("p", { class: "muted small" }, "No one is enrolled yet.");
  const enrollBtn = h("button", { class: "btn", disabled: !!S.busy, onclick: enroll }, data.camera_simulated ? "Enroll (simulated camera)" : "Capture reference images");
  let livePreview = null;
  if (!data.camera_simulated) {
    if (!S.cameraImg) S.cameraImg = h("img", { alt: "Live camera preview" });
    S.faceHint = S.faceHint || h("p", { class: "small", style: "font-weight:600;margin-top:8px", "aria-live": "polite" }, "Looking for a face…");
    livePreview = h("div", { style: "max-width:360px;margin-top:12px" }, h("div", { class: "camera" }, S.cameraImg), S.faceHint);
  }
  const step1 = setupStep(1, "Recipient", [
    h("p", { class: "small muted", style: "margin-bottom:10px" }, "Use consenting teammates only. Reference embeddings stay on this laptop. A face match is a prototype check, not secure identity."),
    persons,
    livePreview,
    h("div", { class: "btn-row", style: "margin-top:14px" }, nameIn, enrollBtn),
    h("label", { class: "check", style: "margin-top:10px" }, consent, "This person agrees to have reference images captured and stored locally for this demo."),
    data.camera_simulated ? null : h("p", { class: "tiny muted", style: "margin-top:6px" }, "During capture, face the camera alone and turn slightly left and right."),
    errFor(undefined, "person_id"),
  ]);
  async function enroll() {
    if (!nameIn.value.trim()) return toast("Enter a display name.", true);
    if (!consent.checked) return toast("Consent is required before capture.", true);
    if (!data.camera_simulated) toast("Capturing. Look at the camera and turn your head slightly left and right.");
    const r = await act("enroll", () => api("/api/enroll", { label: nameIn.value.trim(), consent: true }));
    if (r) { d.person_id = r.person_id; toast("Enrolled."); setup.render(); }
  }

  // 2. Tokens
  const step2 = setupStep(2, "Token mapping", [
    h("div", { class: "btn-row" }, data.token_classes.map((c) => h("span", { class: "pill" }, swatch(c.color), `${c.label} tokens → cartridge ${c.cartridge}`))),
    h("p", { class: "tiny muted", style: "margin-top:8px" }, "Non-ingestible demo tokens only. Token colour does not establish real medication identity or strength."),
  ]);

  // 3. Entries
  const entriesBox = h("div", {});
  d.entries.forEach((e, i) => entriesBox.append(entryEditor(e, i, data, errFor)));
  const aiReady = data.ai && data.ai.ready;
  const step3 = setupStep(3, "Schedule entries", [
    h("p", { class: "small muted", style: "margin-bottom:12px" },
      data.require_label_photo ? "Each entry needs a photo of its medication label. " : "",
      aiReady ? "Gemini reads the photo and suggests fields; you review every one." : "AI is off, so type the fields from the label photo.",
      " Use fictional demo labels."),
    entriesBox,
    h("div", { class: "btn-row", style: "margin-top:12px" },
      h("button", { class: "btn", onclick: () => { d.entries.push(blankEntry(data)); S.capturingFor = d.entries.length - 1; drawSetup(); } }, "Add entry")),
  ]);

  // 4. Review
  const zones = Array.from(new Set([d.timezone, "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "UTC"]));
  const tzSel = h("select", { onchange: (e) => { d.timezone = e.target.value; d.timezone_confirmed = false; drawSetup(); } },
    zones.map((z) => h("option", { value: z, selected: z === d.timezone }, z)));
  const dstBox = st.dst ? h("div", { class: "note-box", style: "margin-top:12px" },
    h("strong", {}, "Daylight saving review"),
    h("ul", { class: "small" }, st.dst.slice(0, 6).map((x) => h("li", {}, `${x.date} at ${fmtTime(x.time)}: this local time ${x.issue === "NONEXISTENT" ? "does not exist (clocks skip it)" : "happens twice"}.`))),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: d.dst_reviewed, onchange: (e) => (d.dst_reviewed = e.target.checked) }),
      "I reviewed these. Those occurrences will be marked Needs review instead of guessing.")) : null;
  const step4 = setupStep(4, "Review and confirm", [
    h("div", { class: "btn-row" }, h("label", { class: "field" }, "Timezone", tzSel)),
    h("label", { class: "check", style: "margin-top:10px" }, h("input", { type: "checkbox", checked: d.timezone_confirmed, onchange: (e) => (d.timezone_confirmed = e.target.checked) }),
      `All times above are local times in ${d.timezone}.`),
    errFor(undefined, "timezone"), dstBox, globalErr,
    h("div", { class: "btn-row", style: "margin-top:16px" },
      h("button", { class: "btn btn-primary btn-large", disabled: !!S.busy, onclick: confirmPlan }, st.plan_version_id ? "Confirm new plan version" : "Confirm plan"),
      h("span", { class: "small muted" }, "Nothing is scheduled until you confirm.")),
  ]);
  async function confirmPlan() {
    st.errors = [];
    const body = {
      base_plan_version_id: st.plan_version_id, person_id: d.person_id, timezone: d.timezone,
      timezone_confirmed: d.timezone_confirmed, dst_reviewed: d.dst_reviewed,
      entries: d.entries.map((e) => ({ entry_key: e.entry_key, label: e.label, strength_text: e.strength_text, quantity: Number(e.quantity),
        delivery_mode: e.delivery_mode, class_id: e.delivery_mode === "DEVICE" ? e.class_id : null, times: e.times, confirmed: e.confirmed,
        label_id: e.label_id || null })),
    };
    try {
      S.busy = "plan";
      const r = await api("/api/plans/confirm", body);
      toast(`Plan version ${r.version} confirmed.`);
      S.setup = null;
      S.busy = null;
      go("schedules");
    } catch (e) {
      S.busy = null;
      st.errors = (e.data && e.data.errors) || [{ message: e.message }];
      if (e.data && e.data.dst_issues) st.dst = e.data.dst_issues;
      if (e.data && e.data.reason_codes && e.data.reason_codes[0] === "PLAN_VERSION_MISMATCH") st.reload = true;
      toast(e.message, true);
      drawSetup();
    }
  }

  setMain(
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Setup"),
      h("p", { class: "sub" }, st.plan_version_id ? "Editing creates a new version. Already issued doses keep their old instructions." : "Enroll, map tokens, add entries, review, confirm."))),
    s && s.demo_mode ? h("div", { class: "note-box", style: "margin-bottom:16px" }, "Demo mode is on. A plan confirmed here is a demo plan and stays separate from ordinary records.") : null,
    h("div", { class: "stack" }, step1, step2, step3, step4));
}

function setupStep(n, title, body) {
  return h("section", { class: "card setup-step" }, h("div", { class: "num" }, n), h("div", { class: "body" }, h("h2", { style: "margin-bottom:12px" }, title), body));
}

const BOTTLE_STEPS = [
  "Front of the label: the medicine name facing the camera.",
  "Turn the bottle about a quarter turn, so the next part of the label faces the camera.",
  "Turn another quarter turn to show the rest of the label (directions, pharmacy, refills).",
  "One more angle if any text is still hidden.",
];

function applyLabelResult(e, r) {
  e.label_id = r.label_id;
  e.confirmed = false;
  const x = r.fields;
  e.suggested = {};
  if (x) {
    if (x.medication_label) { e.label = x.medication_label; e.suggested.label = true; }
    if (x.strength_text) { e.strength_text = x.strength_text; e.suggested.strength_text = true; }
    if (x.units_per_dose) { e.quantity = x.units_per_dose; e.suggested.quantity = true; }
  }
  e.notes = [x && x.frequency_text ? `Label says: “${x.frequency_text}”. Choose the times yourself.` : null,
    x && x.ambiguity_notes, r.note].filter(Boolean).join(" ");
}

function labelTarget(e) {
  return e.container === "flat" ? 1 : 3;
}

async function readLabel(e) {
  const r = await act("label-read", () => api("/api/labels/read", { label_id: e.label_id, container: e.container || "bottle" }));
  if (r) {
    applyLabelResult(e, r);
    S.capturingFor = null;
    toast(r.fields ? `Read ${r.photos} photo${r.photos > 1 ? "s" : ""}. Review the suggested fields.` : "Label photos saved. Type the fields from them.");
  }
  drawSetup();
}

function photoAdded(e, r) {
  e.label_id = r.label_id;
  e.label_photos = r.photos;
  e.confirmed = false;
}

async function captureLabel(e, btn) {
  for (let n = 3; n > 0; n--) { btn.textContent = `Capturing in ${n}…`; await sleep(1000); }
  btn.textContent = "Capturing…";
  const r = await act("label", () => api("/api/labels/capture", { label_id: e.label_id || null }));
  if (r) {
    photoAdded(e, r);
    if (r.photos >= labelTarget(e)) return readLabel(e);
    toast(`Photo ${r.photos} saved. ${BOTTLE_STEPS[r.photos] || ""}`);
  }
  drawSetup();
}

async function uploadLabels(e, files) {
  for (const file of Array.from(files).slice(0, 4)) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("request_id", uuid());
    if (e.label_id) fd.append("label_id", e.label_id);
    const r = await act("label", async () => {
      const res = await fetch("/api/labels/extract", { method: "POST", headers: { "x-doseguard-token": TOKEN }, body: fd });
      const j = await res.json();
      if (!res.ok) throw new Error(j.message || "Upload failed.");
      return j;
    });
    if (!r) return drawSetup();
    photoAdded(e, r);
  }
  return readLabel(e);
}

function thumbs(e) {
  const n = e.label_photos || (e.label_id ? 1 : 0);
  return h("div", { class: "btn-row", style: "gap:6px" }, Array.from({ length: n }, (_, k) =>
    h("a", { href: `/api/labels/${e.label_id}/image?i=${k}`, target: "_blank", title: `Photo ${k + 1}` },
      h("img", { class: "label-thumb small-thumb", src: `/api/labels/${e.label_id}/image?i=${k}`, alt: `Label photo ${k + 1}` }))));
}

function containerPicker(e) {
  const opt = (val, title, sub) => h("label", { class: `choice ${(e.container || "bottle") === val ? "on" : ""}` },
    h("input", { type: "radio", name: `container-${e._uid}`, checked: (e.container || "bottle") === val,
      onchange: () => { e.container = val; drawSetup(); } }),
    h("span", {}, h("strong", {}, title), h("span", { class: "small muted", style: "display:block" }, sub)));
  return h("div", { class: "btn-row", style: "margin:8px 0 10px" },
    opt("bottle", "Round bottle", "3 photos, turning the bottle"),
    opt("flat", "Box or flat label", "1 photo"));
}

function labelPhotoBlock(e, i, data) {
  e._uid = e._uid || uuid().slice(0, 8);
  const box = h("div", { class: "label-box" });
  const target = labelTarget(e);
  const have = e.label_photos || (e.label_id ? 1 : 0);
  const upload = h("input", { type: "file", multiple: true, accept: "image/jpeg,image/png,image/webp,image/heic", class: "sr-only",
    id: `label-up-${e._uid}`, onchange: (ev) => ev.target.files.length && uploadLabels(e, ev.target.files) });

  if (e.label_id && S.capturingFor !== i) {
    box.append(h("div", { style: "flex:1;min-width:240px" },
      h("div", { style: "font-weight:650" }, `${have} label photo${have > 1 ? "s" : ""} saved`),
      h("div", { class: "small muted", style: "margin:2px 0 8px" }, "Check the fields above against these photos."),
      thumbs(e),
      h("div", { class: "btn-row", style: "margin-top:8px" },
        have < 4 && !data.camera_simulated ? h("button", { class: "btn btn-quiet", onclick: () => { S.capturingFor = i; drawSetup(); } }, "Add another angle") : null,
        h("button", { class: "btn btn-quiet", onclick: () => { e.label_id = null; e.label_photos = 0; e.confirmed = false; S.capturingFor = data.camera_simulated ? null : i; drawSetup(); } }, "Retake all"))));
    return box;
  }

  if (data.camera_simulated) {
    box.append(h("div", {}, h("div", { style: "font-weight:650" }, "Label photo required"),
      containerPicker(e),
      h("div", { class: "small muted", style: "margin-bottom:8px" }, e.container === "flat"
        ? "The camera is simulated, so upload a photo of the label."
        : "The camera is simulated. Upload up to 3 photos of the bottle, turned a quarter turn between each."),
      upload, h("label", { for: `label-up-${e._uid}`, class: "btn", role: "button", tabindex: 0 }, "Upload label photos")));
    return box;
  }

  if (S.capturingFor === i) {
    if (!S.labelImg) S.labelImg = h("img", { alt: "Live camera preview for the label" });
    const step = Math.min(have, BOTTLE_STEPS.length - 1);
    const instruction = e.container === "flat" ? "Hold the label flat, facing the camera." : BOTTLE_STEPS[step];
    const capBtn = h("button", { class: "btn btn-primary", disabled: !!S.busy, onclick: (ev) => captureLabel(e, ev.currentTarget) },
      e.container === "flat" ? "Capture label" : `Capture photo ${have + 1} of ${Math.max(target, have + 1)}`);
    box.append(h("div", { class: "camera label-cam" }, S.labelImg, h("span", { class: "pose" }, e.container === "flat" ? "Label" : `Photo ${have + 1}`)),
      h("div", { style: "flex:1;min-width:240px" },
        h("div", { style: "font-weight:650" }, e.container === "flat" ? "Show the label" : "Show the bottle's label"),
        have === 0 ? containerPicker(e) : null,
        h("p", { class: "small", style: "margin:6px 0 4px;font-weight:600" }, instruction),
        h("p", { class: "small muted", style: "margin-bottom:10px" }, "About 20 cm from the camera, label text facing it. Tilt away from lights to avoid glare."),
        have ? thumbs(e) : null,
        h("div", { class: "btn-row", style: "margin-top:10px" }, capBtn,
          have ? h("button", { class: "btn", disabled: !!S.busy, onclick: () => readLabel(e) }, `Done, use ${have} photo${have > 1 ? "s" : ""}`) : null,
          upload, h("label", { for: `label-up-${e._uid}`, class: "btn btn-quiet", role: "button", tabindex: 0 }, "Upload instead"),
          h("button", { class: "btn btn-quiet", onclick: () => { S.capturingFor = null; drawSetup(); } }, "Cancel"))));
    return box;
  }

  box.append(h("div", {}, h("div", { style: "font-weight:650" }, data.require_label_photo ? "Label photo required" : "Label photo (optional)"),
    h("div", { class: "small muted", style: "margin:2px 0 4px" }, "Show the medicine's label to the camera so the app keeps a record of it."),
    containerPicker(e),
    h("button", { class: "btn", onclick: () => { S.capturingFor = i; drawSetup(); } }, "Show label to camera")));
  return box;
}

function entryEditor(e, i, data, errFor) {
  const d = S.setup.draft;
  const unconfirm = () => { e.confirmed = false; if (chk) chk.checked = false; };
  const sug = (f) => (e.suggested && e.suggested[f] ? "suggested" : null);
  const label = h("input", { value: e.label, maxlength: 60, class: sug("label"), placeholder: "e.g. Demo token A", oninput: (ev) => { e.label = ev.target.value; e.suggested && (e.suggested.label = false); unconfirm(); } });
  const strength = h("input", { value: e.strength_text || "", maxlength: 40, class: sug("strength_text"), placeholder: "optional", oninput: (ev) => { e.strength_text = ev.target.value; unconfirm(); } });
  const qty = h("select", { class: sug("quantity"), onchange: (ev) => { e.quantity = Number(ev.target.value); unconfirm(); } },
    Array.from({ length: data.max_quantity }, (_, k) => h("option", { value: k + 1, selected: Number(e.quantity) === k + 1 }, k + 1)));
  const mode = h("select", { onchange: (ev) => { e.delivery_mode = ev.target.value; e.confirmed = false; drawSetup(); } },
    h("option", { value: "DEVICE", selected: e.delivery_mode === "DEVICE" }, "Dispenser"),
    h("option", { value: "REMINDER_ONLY", selected: e.delivery_mode === "REMINDER_ONLY" }, "Reminder only"));
  const cls = h("select", { disabled: e.delivery_mode !== "DEVICE", onchange: (ev) => { e.class_id = ev.target.value; unconfirm(); } },
    data.token_classes.map((c) => h("option", { value: c.class_id, selected: e.class_id === c.class_id }, `${c.label} · cartridge ${c.cartridge}`)));
  const timeIn = h("input", { type: "time", value: "08:00", "aria-label": "Add time" });
  const chk = h("input", { type: "checkbox", checked: !!e.confirmed, onchange: (ev) => (e.confirmed = ev.target.checked) });
  return h("div", { class: "entry" },
    h("div", { class: "entry-grid" },
      h("label", { class: "field" }, "Label", label, errFor(i, "label")),
      h("label", { class: "field" }, "Strength text", strength),
      h("label", { class: "field" }, "Quantity", qty, errFor(i, "quantity")),
      h("label", { class: "field" }, "Delivery", mode, errFor(i, "delivery_mode")),
      h("label", { class: "field" }, "Token", cls, errFor(i, "class_id"))),
    labelPhotoBlock(e, i, data),
    errFor(i, "label_id"),
    e.notes ? h("p", { class: "small", style: "margin-top:10px;color:var(--info)" }, e.notes) : null,
    h("div", { class: "times" }, h("span", { class: "small muted" }, "Times:"),
      e.times.map((t, k) => h("span", { class: "time-chip" }, fmtTime(t),
        h("button", { "aria-label": `Remove ${fmtTime(t)}`, onclick: () => { e.times.splice(k, 1); e.confirmed = false; drawSetup(); } }, "×"))),
      timeIn,
      h("button", { class: "btn btn-quiet", onclick: () => { const v = timeIn.value; if (v && !e.times.includes(v)) { e.times.push(v); e.times.sort(); e.confirmed = false; drawSetup(); } } }, "Add time")),
    errFor(i, "times"),
    h("div", { class: "btn-row", style: "margin-top:12px;justify-content:space-between" },
      h("label", { class: "check" }, chk, h("span", {}, "I reviewed this entry: name, quantity, times and delivery mode", e.suggested && Object.values(e.suggested).some(Boolean) ? h("span", { class: "muted small" }, " (dashed fields came from a label scan)") : null)),
      d.entries.length > 1 ? h("button", { class: "btn btn-quiet btn-danger", onclick: () => { d.entries.splice(i, 1); drawSetup(); } }, "Remove entry") : null),
    errFor(i, "confirmed"));
}

/* ---------------------------------------------------------------- History */
const historyView = {
  key: (s) => [s.state_version, S.historySel],
  async render(s) {
    if (!S.history || s.state_version !== S.historyVersion) {
      S.historyVersion = s.state_version;
      S.history = (await fetch("/api/history?limit=60").then((r) => r.json())).doses;
      if (S.historySel) S.historyDetail = await fetch(`/api/history/${S.historySel}`).then((r) => (r.ok ? r.json() : null));
    }
    const list = h("div", { class: "card" });
    if (!S.history.length) list.append(h("div", { class: "empty" }, "No dose history yet."));
    for (const c of S.history) {
      list.append(h("div", { class: "hist-row" + (S.historySel === c.dose_id ? " sel" : ""), tabindex: 0, role: "button",
        onclick: () => selectHistory(c.dose_id), onkeydown: (e) => e.key === "Enter" && selectHistory(c.dose_id) },
        h("div", {}, h("div", { style: "font-weight:650" }, c.due_local), h("div", { class: "muted tiny" }, c.local_date)),
        h("div", {}, h("div", { style: "font-weight:600" }, ...doseLine(c)), h("div", { class: "muted small" }, modeText(c))),
        cardPills(c)));
    }
    setMain(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "History"),
      h("p", { class: "sub" }, s.demo_mode ? "Demo records only" : "Ordinary records")),
      h("a", { class: "btn", href: "/api/export/events.csv" }, "Export events CSV")),
      h("div", { class: "grid-2" }, list, detailCard()));
  },
};

async function selectHistory(id) {
  S.historySel = id;
  S.historyDetail = await fetch(`/api/history/${id}`).then((r) => (r.ok ? r.json() : null));
  historyView.render(S.status);
}

const EVENT_TEXT = {
  OCCURRENCE_CREATED: "Scheduled", REMINDER_DUE: "Reminder due", REMINDER_ALERT: "Alert sound", REMINDER_SNOOZED: "Snoozed",
  REMINDER_ACKNOWLEDGED: "Reminder dismissed (not taken)", REMINDER_NEEDS_REVIEW: "Needs review", REMINDER_CLOSED: "Reminder closed",
  DOSE_STARTED: "Verification started", WORKFLOW_STATE: "Stage", WORKFLOW_BLOCKED: "Blocked", OBSERVATION: "Camera check",
  DISPENSE_RESERVED: "Dispense reserved", CYCLE_DONE: "Motion finished", RELEASE_RESERVED: "Release reserved", RELEASED: "Released",
  DOSE_COMPLETE: "Complete", USER_CONFIRMED: "User reported taken", SELF_REPORT: "User reported taken", WORKFLOW_CANCELLED: "Cancelled",
  SUPERVISED_RESET: "Supervised recovery", LATCH_LOCKED: "Latch locked", DISPENSE_CYCLE_DONE: "Motion finished",
};

function eventLine(e) {
  const d = e.details || {};
  let extra = "";
  if (e.kind === "WORKFLOW_STATE") extra = `${d.from} → ${d.to}`;
  else if (e.kind === "OBSERVATION") extra = `${(d.kind || "").replace("_", " ").toLowerCase()}: ${d.result}${d.simulated ? " (simulated)" : ""}`;
  else if (e.kind === "REMINDER_ALERT") extra = `repeat ${d.repeat}`;
  else if (e.kind === "REMINDER_SNOOZED") extra = "for 5 minutes";
  else if (e.kind === "CYCLE_DONE") extra = `cartridge ${d.channel}; delivery not proven`;
  else if (e.kind === "WORKFLOW_CANCELLED") extra = (e.reason_code || "").replace(/_/g, " ").toLowerCase(), e = { ...e, reason_code: null };
  if (e.reason_code) extra += (extra ? " · " : "") + e.reason_code;
  return h("li", {}, h("span", { class: "muted" }, e.ts_local), h("span", {}, h("span", { class: "k" }, EVENT_TEXT[e.kind] || e.kind), extra ? h("span", { class: "muted" }, ` · ${extra}`) : null));
}

function detailCard() {
  const det = S.historyDetail;
  if (!det) return h("div", { class: "card empty" }, "Select a dose to see its record.");
  const c = det.dose;
  return h("div", { class: "card" },
    h("h2", {}, ...doseLine(c)), h("p", { class: "muted", style: "margin:4px 0 12px" }, `${c.local_date} ${c.due_local} · ${modeText(c)}`),
    cardPills(c),
    h("h3", { style: "margin:18px 0 6px" }, "Timeline"),
    h("ul", { class: "events" }, det.events.map(eventLine)),
    det.commands.length ? [h("h3", { style: "margin:18px 0 6px" }, "Device commands"),
      h("ul", { class: "events" }, det.commands.map((m) => h("li", {}, h("span", { class: "mono" }, `#${m.command_id}`),
        h("span", {}, h("span", { class: "k" }, m.kind), m.channel ? ` ${m.channel}` : "", h("span", { class: "muted" }, ` · ${m.state.toLowerCase()}${m.detail ? " · " + m.detail : ""}`)))))] : null,
    h("p", { class: "tiny muted", style: "margin-top:14px" }, "Released, Collection observed and User reported taken are separate. Ingestion is never verified."));
}

/* ---------------------------------------------------------------- Device */
const device = {
  key: (s) => [{ ...s.device, heartbeat_age_ms: null }, s.simulator, s.vision, s.ai, s.workflow.armed, s.workflow.pending_operation, !!s.workflow.txn, s.camera.mode, s.token_classes],
  tick(s) {
    const el = document.getElementById("hb-age");
    if (el) el.textContent = s.device.heartbeat_age_ms == null ? "–" : `${s.device.heartbeat_age_ms} ms`;
  },
  render(s) {
    const d = s.device;
    const stat = (label, value, id) => h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value", id }, value ?? "–"));
    const yes = (v, t = "Yes", f = "No") => (v == null ? "Unknown" : v ? t : f);
    const statusCard = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Controller"),
      d.simulated ? h("span", { class: "badge badge-sim" }, "SIMULATED HARDWARE") : h("span", { class: "muted small mono" }, d.port || "")),
      h("div", { class: "kv-wide" },
        stat("Connection", d.connected ? "Connected" : "Disconnected"),
        stat("Session", d.session), stat("Firmware", d.firmware), stat("Heartbeat", d.heartbeat_age_ms == null ? "–" : `${d.heartbeat_age_ms} ms`, "hb-age"),
        stat("Motor A", d.motor.A), stat("Motor B", d.motor.B), stat("Home A", yes(d.home.A)), stat("Home B", yes(d.home.B)),
        stat("Access door", yes(d.door_closed, "Closed", "Open")), stat("Latch (commanded)", d.servo_commanded),
        stat("Tray revision", d.tray_revision), stat("Transaction", d.transaction), stat("Fault", d.fault || "None"),
        stat("Armed", s.workflow.armed ? "Yes" : "No")),
      h("p", { class: "tiny muted", style: "margin-top:12px" }, "Latch position and the fixed tray top are not measured; no sensors are fitted for them."),
      d.protocol_error ? h("div", { class: "block-box", style: "margin-top:10px" }, d.protocol_error) : null,
      d.port_error ? h("div", { class: "block-box", style: "margin-top:10px" }, `Serial port: ${d.port_error}`) : null);

    const armChk = h("input", { type: "checkbox" });
    const armCard = h("div", { class: "card" }, h("h3", {}, "Arm for dispensing"),
      h("p", { class: "small muted", style: "margin:6px 0 12px" }, "Arming lasts for this controller session. A restart, disconnect, STOP or fault disarms it."),
      h("label", { class: "check" }, armChk, "I inspected the enclosure: tray top and panels fixed, door closed, no reachable path into the tray."),
      h("div", { class: "btn-row", style: "margin-top:12px" },
        h("button", { class: "btn btn-primary", disabled: !!S.busy || s.workflow.armed, onclick: () => act("arm", () => api("/api/device/arm", { inspected: armChk.checked })).then((x) => x && toast("Device armed.")) },
          s.workflow.armed ? "Armed" : "Arm device")));

    const resetChk = h("input", { type: "checkbox" });
    const resetCard = h("div", { class: "card" }, h("h3", {}, "Supervised recovery"),
      h("p", { class: "small muted", style: "margin:6px 0 12px" }, "Use after STOP, a fault or an uncertain outcome. Nothing is replayed. Doses with an unknown outcome are marked for review. The controller restarts unarmed and rehomes."),
      h("label", { class: "check" }, resetChk, "I physically inspected the device, cleared any jam, and nobody's hand is in the mechanism."),
      h("div", { class: "btn-row", style: "margin-top:12px" },
        h("button", { class: "btn btn-danger", disabled: !!S.busy, onclick: () => act("reset", () => api("/api/device/reset", { inspected: resetChk.checked })).then((x) => x && toast("Recovery sent. Re-arm when ready.")) }, "Run supervised recovery")));

    const cards = [statusCard, h("div", { class: "grid-2", style: "grid-template-columns:1fr 1fr" }, armCard, resetCard)];
    cards.push(pillTypesCard(s));
    if (s.simulator) cards.push(simCard(s));
    cards.push(devCard(s));
    setMain(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Device"),
      h("p", { class: "sub" }, "Controller state, arming, recovery and STOP."))), h("div", { class: "stack" }, cards));
  },
};

function pillTypesCard(s) {
  const sim = s.camera.simulated;
  const rows = s.token_classes.map((c) => {
    const name = h("input", { value: c.label, maxlength: 24, "aria-label": `Name for cartridge ${c.cartridge}`, style: "max-width:200px" });
    const sh = c.shape;
    const desc = sh ? `${sh.kind} · size ${Math.round(sh.area)} px · length/width ${Number(sh.elongation).toFixed(2)}` : "Not calibrated (assumes a round pill)";
    return h("div", { class: "hist-row", style: "cursor:default;grid-template-columns:auto 1fr auto" },
      swatch(c.color),
      h("div", {}, h("div", { class: "btn-row" }, name, h("span", { class: "muted small" }, `Cartridge ${c.cartridge}`),
        h("button", { class: "btn btn-quiet", onclick: () => act("rename", () => api("/api/tray/rename", { class_id: c.class_id, label: name.value })).then((x) => x && toast("Renamed.")) }, "Save name")),
        h("div", { class: `small ${sh ? "" : "muted"}`, style: "margin-top:4px" }, desc)),
      h("button", { class: "btn", disabled: sim || !!S.busy || !!s.workflow.txn,
        onclick: () => act("calibrate", () => api("/api/tray/calibrate", { class_id: c.class_id, label: name.value }))
          .then((x) => x && toast(`Calibrated: ${x.shape.kind}, size ${Math.round(x.shape.area)} px.`)) }, "Calibrate with one pill"));
  });
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Pill types"), h("span", { class: "muted small" }, "Tray calibration")),
    h("p", { class: "small muted", style: "margin-bottom:8px" },
      "Teach the camera each pill's colour and shape: round tablets, capsules and oblong tablets. Put exactly one pill of that type in the tray, ",
      "point the camera so all four markers show, and click Calibrate. White pills need a dark matte tray."),
    sim ? h("div", { class: "note-box", style: "margin-bottom:8px" }, "Calibration needs the real camera. Restart with CAMERA_MODE=single_laptop.") : null,
    rows,
    h("p", { class: "tiny muted", style: "margin-top:10px" }, "Colour and shape only check that the right kind of object is in the tray. They do not prove what medication or strength it is."));
}

function simCard(s) {
  const sim = s.simulator;
  const send = (body) => api("/api/sim/hardware", body).catch((e) => toast(e.message, true));
  const modeSel = (ch) => h("select", { "aria-label": `Next feeds on ${ch}`, onchange: (e) => send({ action: "feed_mode", channel: ch, mode: e.target.value }) },
    [["ok", "Normal"], ["empty", "Empty feed"], ["double", "Double feed"], ["wrong_class", "Wrong token class"], ["stall", "Motor stall"], ["lost_done", "Completion message lost"], ["no_ack", "Command lost"]]
      .map(([v, l]) => h("option", { value: v, selected: sim.feed_mode[ch] === v }, l)));
  return h("div", { class: "card sim-panel" }, h("div", { class: "card-head" }, h("h2", {}, "Hardware simulator"), h("span", { class: "badge badge-sim" }, "SIMULATED")),
    h("p", { class: "small muted", style: "margin-bottom:14px" }, "These controls change the simulated controller only. Every reading it produces is labeled simulated."),
    h("div", { class: "kv-wide" },
      h("label", { class: "field" }, "Cartridge A feeds", modeSel("A")),
      h("label", { class: "field" }, "Cartridge B feeds", modeSel("B")),
      h("label", { class: "field" }, "ACK delay", h("select", { onchange: (e) => send({ action: "ack_delay", seconds: Number(e.target.value) }) },
        [0, 0.5, 1, 3].map((v) => h("option", { value: v, selected: sim.ack_delay_s === v }, `${v} s`)))),
      h("div", { class: "stat" }, h("div", { class: "label" }, "Simulated tray"), h("div", { class: "value" }, `A: ${sim.tray.A} · B: ${sim.tray.B}`))),
    h("div", { class: "btn-row", style: "margin-top:14px" },
      sim.link_up ? h("button", { class: "btn", onclick: () => send({ action: "disconnect" }) }, "Unplug USB")
        : h("button", { class: "btn", onclick: () => send({ action: "reconnect" }) }, "Plug USB back in"),
      h("button", { class: "btn", onclick: () => send({ action: "power_cycle" }) }, "Power cycle controller"),
      sim.door_closed ? h("button", { class: "btn", onclick: () => send({ action: "open_door" }) }, "Open door")
        : h("button", { class: "btn", onclick: () => send({ action: "close_door" }) }, "Close door"),
      sim.door_closed ? h("button", { class: "btn btn-quiet btn-danger", onclick: () => send({ action: "open_door", force: true }) }, "Force door (maintenance)") : null,
      h("button", { class: "btn", onclick: () => send({ action: "collect" }) }, "Take tokens"),
      h("button", { class: "btn btn-quiet", onclick: () => send({ action: "place_token", channel: "A", n: 1 }) }, "Drop a stray blue token")));
}

function devCard(s) {
  const ai = s.ai;
  return h("div", { class: "card" }, h("h3", { style: "margin-bottom:12px" }, "Developer"),
    h("dl", { class: "kv" },
      h("dt", {}, "Hardware mode"), h("dd", {}, s.config.hardware_mode),
      h("dt", {}, "Camera mode"), h("dd", {}, s.config.camera_mode + (s.vision.face_models ? " · face models present" : " · face models missing")),
      h("dt", {}, "AI backend"), h("dd", {}, ai.backend === "off" ? "Off" : `${ai.backend} · ${ai.model} · ${ai.ready ? "ready" : "not ready"}`),
      ai.error ? [h("dt", {}, "AI note"), h("dd", {}, ai.error)] : null,
      h("dt", {}, "AI requests"), h("dd", {}, `${ai.requests_used} of ${ai.request_limit} · tokens in ${ai.tokens.input}, out ${ai.tokens.output}${ai.last_outcome ? " · last " + ai.last_outcome : ""}`),
      h("dt", {}, "Demo mode"), h("dd", {}, h("button", { class: "btn btn-quiet", disabled: !!s.workflow.txn || !!S.busy,
        onclick: () => act("demo", () => api("/api/demo/mode", { enabled: !s.demo_mode })) }, s.demo_mode ? "Turn off demo mode" : "Turn on demo mode"))),
    h("p", { class: "tiny muted", style: "margin-top:10px" }, "Check actual spending in the provider console. Request counts here are local limits, not a dollar cap."));
}

/* ---------------------------------------------------------------- Assistant */
const PROMPTS = ["What is due next?", "Why am I getting this reminder?", "Explain this block", "Help position the camera"];

function openAssistant(open) {
  const pane = $("#assistant");
  pane.hidden = !open;
  $("#assistant-toggle").setAttribute("aria-expanded", String(open));
  if (open) $("#assistant-input").focus();
}

function renderChat() {
  const log = $("#assistant-log");
  log.replaceChildren();
  if (!S.chat.length) log.append(h("p", { class: "muted small" }, "Answers use your confirmed schedule and recorded events. The recorded reason for a block is shown separately and always wins."));
  for (const m of S.chat) {
    if (m.q) { log.append(h("div", { class: "msg-q" }, m.q)); continue; }
    if (m.pending) { log.append(h("div", { class: "msg-a" }, h("div", { class: "busy-line" }, h("span", { class: "spinner" }), "Thinking"))); continue; }
    if (m.error) { log.append(h("div", { class: "msg-a" }, m.error)); continue; }
    const r = m.r;
    const stale = S.status && contextKey(S.status) !== m.ctx;
    log.append(h("div", { class: "msg-a" },
      r.authoritative ? h("div", { class: "auth" }, h("span", { class: "lbl" }, "Recorded reason"), r.authoritative) : null,
      h("div", {}, r.answer.answer_text),
      r.supporting_facts.length ? h("ul", { class: "facts" }, r.supporting_facts.map((f) => h("li", {}, f.text))) : null,
      r.answer.suggested_page ? h("button", { class: "btn btn-quiet small", style: "margin-top:6px", onclick: () => go(r.answer.suggested_page) }, `Open ${r.answer.suggested_page}`) : null,
      h("div", { class: "foot" }, h("span", {}, `As of ${r.snapshot_time_local}${stale ? " · status has changed since" : ""}`),
        h("span", {}, r.source === "gemini" ? "Gemini" : "Offline help")),
      r.note ? h("div", { class: "tiny muted", style: "margin-top:4px" }, `Gemini unavailable: ${r.note}`) : null));
  }
  log.scrollTop = log.scrollHeight;
}

function contextKey(s) {
  if (!s) return "";
  const t = s.workflow.txn;
  return JSON.stringify([t && t.state, t && t.blocks.map((b) => b.code), s.next_dose && s.next_dose.dose_id,
    s.today.map((c) => [c.dose_id, c.reminder.state, c.workflow_state])]);
}

async function ask(q) {
  q = q.trim();
  if (!q || S.asking) return;
  S.asking = true;
  const history = S.chat.filter((m) => m.q || m.r).slice(-4).map((m) => (m.q ? { role: "user", text: m.q } : { role: "assistant", text: m.r.answer.answer_text }));
  const slot = { pending: true };
  S.chat.push({ q }, slot);
  renderChat();
  try {
    const r = await api("/api/assistant", { question: q, history }, { noRetry: true });
    Object.assign(slot, { pending: false, r, ctx: contextKey(S.status) });
  } catch (e) {
    Object.assign(slot, { pending: false, error: e.message });
  } finally {
    S.asking = false;
  }
  renderChat();
  $("#assistant-input").focus();
}

/* ---------------------------------------------------------------- routing */
const VIEWS = { today, verify, schedules, setup, history: historyView, device };

function route() {
  const v = (location.hash || "#today").slice(1);
  S.view = VIEWS[v] ? v : "today";
  document.querySelectorAll(".sidebar a").forEach((a) => a.classList.toggle("active", a.dataset.view === S.view));
  S.keys = {};
  if (S.view === "setup") setup.render();
  else if (S.status) VIEWS[S.view].render(S.status);
  main.focus({ preventScroll: true });
}

async function faceHintLoop() {
  if (S.view === "setup" && S.faceHint && S.status && !S.status.camera.simulated) {
    try {
      const r = await fetch("/api/camera/face-preview").then((x) => x.json());
      const [txt, color] = !r.available ? ["Face models missing. Run scripts/download_models.py.", "var(--danger)"]
        : r.faces == null ? ["Waiting for the camera…", "var(--muted)"]
        : r.faces === 0 ? ["No face found. Move into the frame, with light in front of you.", "var(--due)"]
        : r.faces > 1 ? ["More than one face. Only the person enrolling should be in view.", "var(--danger)"]
        : [`Face found (confidence ${r.score}). Ready to capture.`, "var(--ok)"];
      S.faceHint.textContent = txt;
      S.faceHint.style.color = color;
    } catch {}
  }
  setTimeout(faceHintLoop, 600);
}

function cameraLoop() {
  const s = S.status;
  const wantPreview = (S.view === "verify" && s && s.workflow.txn) || S.view === "setup";
  if (S.view === "setup" && S.capturingFor != null && S.labelImg && s && !s.camera.simulated) {
    const nxt = new Image();
    nxt.onload = () => { if (S.labelImg) S.labelImg.src = nxt.src; };
    nxt.src = `/api/camera/frame?role=face&t=${Date.now()}`;
  }
  if (wantPreview && s && !s.camera.simulated && S.cameraImg) {
    const role = s.workflow.txn && s.workflow.txn.pose === "TRAY" && s.camera.mode === "dual_camera" ? "tray" : "face";
    const next = new Image();
    next.onload = () => { if (S.cameraImg) S.cameraImg.src = next.src; };
    next.src = `/api/camera/frame?role=${role}&t=${Date.now()}`;
  }
  setTimeout(cameraLoop, 400);
}

/* ---------------------------------------------------------------- wiring */
$("#stop-btn").addEventListener("click", async () => {
  try {
    await api("/api/device/stop", {});
    toast("STOP sent. Inspect the device, then use supervised recovery.");
  } catch (e) {
    toast("STOP could not be confirmed: " + e.message, true);
  }
});
$("#assistant-toggle").addEventListener("click", () => openAssistant($("#assistant").hidden));
$("#assistant-close").addEventListener("click", () => openAssistant(false));
$("#assistant-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const inp = $("#assistant-input");
  ask(inp.value);
  inp.value = "";
});
$("#assistant-prompts").append(...PROMPTS.map((p) => h("button", { type: "button", onclick: () => { ask(p); $("#assistant-input").focus(); } }, p)));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#assistant").hidden) openAssistant(false); });
window.addEventListener("hashchange", route);
renderChat();
route();
poll();
cameraLoop();
faceHintLoop();
