"use strict";
/* DoseGuard web client. Plain JS, no build step. The browser only displays state and
   submits intents; every rule is enforced again on the server.
   Everyday screens (Today, Dose steps, top bar, assistant) are translated through t();
   the Setup screens keep English detail. */

const TOKEN = document.querySelector('meta[name="dg-token"]').content;
const $ = (sel) => document.querySelector(sel);
const main = $("#main");
const setMain = (...kids) => main.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));

/* ---------------------------------------------------------------- preferences */
function loadPrefs() {
  try { return JSON.parse(localStorage.getItem("dg-prefs") || "{}"); } catch { return {}; }
}
function guessLang() {
  const n = (navigator.language || "en").slice(0, 2).toLowerCase();
  return I18N[n] ? n : "en";
}
const PREFS = (window.PREFS = Object.assign({ lang: guessLang(), size: 0, contrast: false, simple: false, notify: false, auto: true, voice: true }, loadPrefs()));

function savePrefs() {
  try { localStorage.setItem("dg-prefs", JSON.stringify(PREFS)); } catch {}
  applyPrefs();
}
function applyPrefs() {
  const r = document.documentElement;
  if (PREFS.size) r.dataset.size = String(PREFS.size); else delete r.dataset.size;
  if (PREFS.contrast) r.dataset.contrast = "high"; else delete r.dataset.contrast;
  r.lang = PREFS.lang === "zh" ? "zh-CN" : PREFS.lang;
  document.body.classList.toggle("simple", !!PREFS.simple);
  applyI18n();
}
function applyI18n(root = document) {
  root.querySelectorAll("[data-i18n]").forEach((el) => (el.textContent = t(el.dataset.i18n)));
  root.querySelectorAll("[data-i18n-aria]").forEach((el) => el.setAttribute("aria-label", t(el.dataset.i18nAria)));
  root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => el.setAttribute("placeholder", t(el.dataset.i18nPlaceholder)));
}

const S = {
  status: null, offline: false, view: "today", keys: {}, audio: null, setup: null,
  history: null, historySel: null, historyDetail: null, historyVersion: -1,
  busy: null, chat: [], cameraImg: null, lastDue: 0, followSeen: new Set(), fill: null,
  spokenDue: new Set(), firstDueCheck: true, live: null,
};

const STEPS = ["PERSON_CHECK", "EMPTY_TRAY_CHECK", "DISPENSING", "DOSE_CHECK", "RECIPIENT_RECHECK", "HAND_CHECK", "COLLECTION_CHECK"];
const STEP_INDEX = { PERSON_CHECK: 0, EMPTY_TRAY_CHECK: 1, DISPENSING: 2, DOSE_CHECK: 3, RECIPIENT_RECHECK: 4, RELEASE_PENDING: 4, HAND_CHECK: 5, COLLECTION_CHECK: 6 };
// After release the person first shows the pills in their hand, then the empty tray (both in COLLECTION_CHECK).
const stepKey = (tx) => (tx.state === "COLLECTION_CHECK" && tx.hand_done === false ? "HAND_CHECK" : tx.state);
// With the in-box tray camera the tray is checked right after the hand, so "Show pills" is the last step.
// "Show pills" only appears when that step is switched on in Setup.
const stepsFor = (tx) => (!tx.hand_check ? STEPS.filter((x) => x !== "HAND_CHECK")
  : tx.merged_collect ? STEPS.filter((x) => x !== "COLLECTION_CHECK") : STEPS);
const stepIndex = (tx) => stepsFor(tx).indexOf(tx.hand_check && tx.merged_collect && stepKey(tx) === "COLLECTION_CHECK" ? "HAND_CHECK"
  : stepKey(tx) === "RELEASE_PENDING" ? "RECIPIENT_RECHECK" : stepKey(tx));
const ACTIVE_WF = ["PERSON_CHECK", "EMPTY_TRAY_CHECK", "DISPENSING", "DOSE_CHECK", "RECIPIENT_RECHECK", "RELEASE_PENDING"];
// Reason codes that mean "the dispenser itself needs checking". Everyday screens show one plain sentence.
const DEVICE_CODES = ["DEVICE_FAULT", "STOPPED", "OUTCOME_UNCERTAIN", "RESTART_DURING_MOTION", "COMMAND_REJECTED",
  "DEVICE_RESTARTED", "NOT_HOMED", "MOTOR_BUSY", "PENDING_OPERATION"];

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
const icon = (name) => h("i", { class: `ph ph-${name}`, "aria-hidden": "true" });
const uuid = () => (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "") : Math.random().toString(16).slice(2) + Date.now().toString(16));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const hasKey = (k) => I18N.en[k] !== undefined;

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
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), bad ? 7000 : 4000);
}

async function act(label, fn) {
  if (S.busy) return;
  S.busy = label;
  rerender(true);
  try {
    return await fn();
  } catch (e) {
    toast(friendlyError(e), true);
    return null;
  } finally {
    S.busy = null;
    rerender(true);
  }
}

function friendlyError(e) {
  const code = e && e.data && e.data.reason_codes && e.data.reason_codes[0];
  if (code && DEVICE_CODES.includes(code)) return t("reason.CAREGIVER");
  if (code && hasKey(`reason.${code}`)) {
    const d = { ...e.data, ...(e.data.details || {}) };
    const text = t(`reason.${code}`, { ...d, time: fmtWhen(d.next_available_at) });
    // Fall back to the server's sentence if a placeholder has nothing to fill it.
    const timed = ["TOO_SOON", "DAILY_LIMIT"].includes(code);
    return /\{\w+\}/.test(text) || (timed && !d.next_available_at) ? e.message : text;
  }
  return e.message;
}

function go(view) {
  if (location.hash !== "#" + view) location.hash = view;
  else route();
}

/* ---------------------------------------------------------------- time and words */
function locale() { return LANGS[PREFS.lang].locale; }
function tz() { return (S.status && S.status.timezone) || undefined; }
const LAPTOP_TZ = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || null; } catch { return null; } })();
function zoneOffset(zone) {
  try {
    return new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "shortOffset" }).formatToParts(new Date())
      .find((p) => p.type === "timeZoneName").value;
  } catch { return null; }
}
// True when the plan's clock and this laptop's clock disagree right now (e.g. a Chicago plan on a New York laptop).
function tzDiffers(zone = tz()) {
  return !!(zone && LAPTOP_TZ && zoneOffset(zone) !== zoneOffset(LAPTOP_TZ));
}
const zoneCity = (zone) => (zone || "").split("/").pop().replace(/_/g, " ");
// "Eastern Time (New York)" instead of "America/New_York".
function zoneName(zone) {
  if (!zone) return "";
  if (zone === "UTC") return "UTC";
  let generic = "";
  try {
    generic = new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "longGeneric" }).formatToParts(new Date())
      .find((p) => p.type === "timeZoneName").value;
  } catch {}
  return generic && !/^GMT/.test(generic) ? `${generic} (${zoneCity(zone)})` : zoneCity(zone);
}
function zoneOption(zone) {
  let now = "";
  try { now = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: zone }).format(new Date()); } catch {}
  return `${zoneName(zone)}, now ${now}${zone === LAPTOP_TZ ? ", this laptop" : ""}`;
}
function fmtClock(iso) {
  if (!iso) return "";
  // Name the zone whenever it is not the laptop's own, so "4:05 PM" can never be misread.
  const opts = { hour: "numeric", minute: "2-digit", timeZone: tz() };
  if (tzDiffers()) opts.timeZoneName = "short";
  return new Intl.DateTimeFormat(locale(), opts).format(new Date(iso));
}
function fmtWhen(iso) {
  // "2:15 PM" today, "Tomorrow 9:00 AM" otherwise, in the user's language.
  if (!iso) return "";
  const day = (d) => new Intl.DateTimeFormat("en-CA", { timeZone: tz() }).format(d);
  const at = new Date(iso);
  if (day(at) === day(new Date())) return fmtClock(iso);
  const tomorrow = new Date(Date.now() + 86400000);
  const word = day(at) === day(tomorrow) ? t("day.Tomorrow") : new Intl.DateTimeFormat(locale(), { weekday: "long", timeZone: tz() }).format(at);
  return `${word} ${fmtClock(iso)}`;
}
function hourOf(date) {
  // Local hour as a fraction (14.5 = 2:30 PM) in the plan's timezone.
  const parts = new Intl.DateTimeFormat("en-GB", { hour: "numeric", minute: "numeric", hour12: false, timeZone: tz() }).formatToParts(date);
  const get = (k) => Number((parts.find((p) => p.type === k) || {}).value || 0);
  return (get("hour") % 24) + get("minute") / 60;
}
/* ---------------------------------------------------------------- the real sun
   Sunrise and sunset for today, from the standard sunrise equation, at the plan timezone's city (or a point
   on that timezone's meridian at 40°N). Hours are counted from today's local midnight in the plan's timezone. */
const ZONE_COORDS = {
  "America/New_York": [40.71, -74.01], "America/Detroit": [42.33, -83.05], "America/Chicago": [41.88, -87.63],
  "America/Denver": [39.74, -104.99], "America/Phoenix": [33.45, -112.07], "America/Los_Angeles": [34.05, -118.24],
  "America/Anchorage": [61.22, -149.9], "Pacific/Honolulu": [21.31, -157.86], "America/Toronto": [43.65, -79.38],
  "America/Mexico_City": [19.43, -99.13], "Europe/London": [51.51, -0.13], "Europe/Madrid": [40.42, -3.7],
  "Asia/Shanghai": [31.23, 121.47], "Asia/Hong_Kong": [22.32, 114.17], "Asia/Taipei": [25.03, 121.57],
};
function zoneCoords(zone) {
  if (ZONE_COORDS[zone]) return ZONE_COORDS[zone];
  // Unknown zone: a point on its standard meridian (offset x 15°) at 40°N is close enough for a sky picture.
  const m = /GMT([+-]\d+)(?::(\d+))?/.exec(zoneOffset(zone || "UTC") || "");
  const off = m ? Number(m[1]) + Math.sign(Number(m[1])) * Number(m[2] || 0) / 60 : 0;
  return [40, off * 15];
}
function sunEvents(dayDate, lat, lon) {
  const rad = Math.PI / 180;
  const J = dayDate.getTime() / 86400000 + 2440587.5;
  const n = Math.round(J - 2451545.0 + 0.0008);
  const Js = n - lon / 360;
  const M = (357.5291 + 0.98560028 * Js) % 360;
  const C = 1.9148 * Math.sin(M * rad) + 0.02 * Math.sin(2 * M * rad) + 0.0003 * Math.sin(3 * M * rad);
  const L = (M + C + 180 + 102.9372) % 360;
  const Jt = 2451545.0 + Js + 0.0053 * Math.sin(M * rad) - 0.0069 * Math.sin(2 * L * rad);
  const dec = Math.asin(Math.sin(L * rad) * Math.sin(23.4397 * rad));
  const cosW = (Math.sin(-0.833 * rad) - Math.sin(lat * rad) * Math.sin(dec)) / (Math.cos(lat * rad) * Math.cos(dec));
  const toDate = (j) => new Date((j - 2440587.5) * 86400000);
  if (cosW > 1) return null; // polar night: treat as no day
  const w0 = Math.acos(Math.min(1, cosW)) / rad;
  return { rise: toDate(Jt - w0 / 360), set: toDate(Jt + w0 / 360) };
}
let SUN_CACHE = { key: "", v: null };
function sunToday() {
  const zone = tz() || LAPTOP_TZ || "UTC";
  const now = new Date();
  const key = `${zone}|${new Intl.DateTimeFormat("en-CA", { timeZone: zone }).format(now)}`;
  if (SUN_CACHE.key === key) return SUN_CACHE.v;
  const [lat, lon] = zoneCoords(zone);
  const nowHr = hourOf(now);
  const at = (hrs) => new Date(now.getTime() + (hrs - nowHr) * 3600000); // local hour (from today's midnight) -> Date
  const hr = (d) => nowHr + (d - now) / 3600000;
  const ev = (offsetDays) => sunEvents(at(12 + 24 * offsetDays), lat, lon);
  const today = ev(0), yesterday = ev(-1), tomorrow = ev(1);
  const v = today && yesterday && tomorrow
    ? { rise: hr(today.rise), set: hr(today.set), prevSet: hr(yesterday.set), nextRise: hr(tomorrow.rise) }
    : { rise: 6.5, set: 18.5, prevSet: -5.5, nextRise: 30.5 };
  SUN_CACHE = { key, v };
  return v;
}
// The sky stays light all day and night (the user preferred it); after dark it keeps the soft dusk colours and the
// moon shows that it is night.
function skyPhase(hr) {
  const sun = sunToday();
  if (hr >= sun.rise - 0.5 && hr < sun.rise + 2) return "dawn";
  if (hr >= sun.rise + 2 && hr < sun.set - 2) return "day";
  return "dusk";
}
function dayWord(c) {
  if (!c.due_day || c.due_day === "Today") return "";
  return hasKey(`day.${c.due_day}`) ? t(`day.${c.due_day}`) : new Intl.DateTimeFormat(locale(), { weekday: "short", month: "short", day: "numeric", timeZone: tz() }).format(new Date(c.due_at_utc));
}
function whenText(c) {
  const d = dayWord(c);
  return d ? `${d} ${fmtClock(c.due_at_utc)}` : fmtClock(c.due_at_utc);
}
function modeText(c) {
  if (c.schedule === "AS_NEEDED") return `${t("pill.as_needed")}, ${c.delivery_mode === "DEVICE" ? t("mode.device") : t("mode.reminder")}`;
  return c.delivery_mode === "DEVICE" ? t("mode.device") : t("mode.reminder");
}
const sentence = (x) => (x ? x.charAt(0).toUpperCase() + x.slice(1) : x);
function swatch(color) {
  // No colour means not from the dispenser: draw nothing rather than an empty ring that reads as "waiting".
  return color ? h("span", { class: "swatch", style: `background:${color}`, "aria-hidden": "true" }) : null;
}
function doseLine(c) {
  return [c.label, h("span", { class: "muted" }, ` ×\u00a0${c.quantity}`), c.strength_text ? h("span", { class: "muted" }, `, ${c.strength_text}`) : null];
}

/* Every status in words, and in form: solid done, dashed waiting, ringed now, struck skipped. */
function doseState(c) {
  const r = c.reminder || {}, ws = c.workflow_state;
  if (ws === "FAULT" || ws === "UNCERTAIN" || r.close_reason === "WORKFLOW_UNRESOLVED") return "missed";
  if (["RELEASED", "COMPLETE"].includes(ws) || c.self_report || ["RELEASED", "USER_REPORTED_TAKEN"].includes(r.close_reason)) return "done";
  if (r.close_reason === "REVIEWED_NOT_TAKEN") return "skip";
  if (r.state === "NEEDS_REVIEW") return "missed";
  if (ACTIVE_WF.includes(ws) || r.state === "DUE") return "due";
  return "wait";
}
function stateWords(c) {
  const r = c.reminder || {}, ws = c.workflow_state;
  if (ws === "FAULT" || ws === "UNCERTAIN" || r.close_reason === "WORKFLOW_UNRESOLVED") return t("pill.uncertain");
  if (ACTIVE_WF.includes(ws)) return t("pill.verifying");
  if (ws === "RELEASED") return t("pill.collect");
  if (ws === "COMPLETE") return c.self_report ? t("pill.self") : c.collection === "OBSERVED" ? t("pill.collected") : t("pill.released");
  if (c.self_report) return t("pill.self");
  return { SCHEDULED: t("pill.scheduled"), DUE: t("pill.due"), SNOOZED: t("pill.snoozed", { time: fmtClock(r.snoozed_until) }),
    ACKNOWLEDGED: t("pill.dismissed"), NEEDS_REVIEW: t("pill.review") }[r.state] ||
    ({ REVIEWED_NOT_TAKEN: t("pill.closed_not"), USER_REPORTED_TAKEN: t("pill.self"), RELEASED: t("pill.released") }[r.close_reason] || "");
}
function stateTag(c) {
  const st = doseState(c);
  return h("span", { class: "tag" }, h("span", { class: `mark ${st === "missed" ? "alert" : st}` }), stateWords(c));
}

function startBlockers(s) {
  const everyday = [], detail = [];
  if (!s.device.connected) detail.push("Dispenser disconnected.");
  else if (s.device.fault) detail.push(`Dispenser stopped: ${s.device.fault}. Use recovery in Setup, Dispenser.`);
  else if (!s.workflow.armed) detail.push("Dispenser not armed. Inspect it and arm it in Setup, Dispenser.");
  if (detail.length) everyday.push(t("blocked.patient"));
  if (s.look_alike_blocks && (s.look_alike || []).length) {
    everyday.push(t("blocked.lookalike"));
    detail.push(s.look_alike[0].message);
  }
  if (s.workflow.txn) everyday.push(t("blocked.busy"));
  return { everyday, detail, any: everyday.length > 0 };
}

async function startDose(c) {
  const r = await act("start", () => api("/api/doses/start", { dose_id: c.dose_id, plan_version: c.plan_version_id }));
  if (r) go("verify");
}
async function selfReport(c) {
  const r = await act("self", () => api("/api/doses/self-report", { dose_id: c.dose_id }));
  if (r) toast(t("toast.self"));
}
async function confirmTaken(c) {
  const r = await act("confirm", () => api("/api/doses/confirm", { dose_id: c.dose_id }));
  if (r) toast(t("toast.confirm"));
}
async function requestNeeded(item) {
  const r = await act("need", () => api("/api/doses/request", { entry_key: item.entry_key, plan_version: item.plan_version_id }));
  if (!r) return;
  if (item.delivery_mode === "DEVICE") go("verify");
  else toast(t("toast.need_noted"));
}

/* ---------------------------------------------------------------- read aloud */
function speak(text, quiet = false) {
  if (S.status && S.status.laptop_voice && S.status.laptop_voice.enabled) {
    // Spoken by the laptop itself: works without a click and in background tabs.
    api("/api/speak", { text: String(text).slice(0, 400), lang: PREFS.lang }, { noRetry: true }).catch(() => {});
    return;
  }
  if (quiet) return;
  if (!("speechSynthesis" in window)) return toast(t("prefs.voice_none"), true);
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = LANGS[PREFS.lang].voice;
  u.rate = 0.9;
  const voices = speechSynthesis.getVoices();
  const v = voices.find((x) => x.lang && x.lang.toLowerCase().startsWith(PREFS.lang));
  if (v) u.voice = v;
  else if (voices.length && PREFS.lang !== "en") toast(t("prefs.voice_none"), true);
  speechSynthesis.speak(u);
}
function readButton(getText) {
  return h("button", { class: "btn btn-quiet", type: "button", onclick: () => speak(getText()) }, icon("speaker-high"), t("due.read"));
}

/* ---------------------------------------------------------------- sound */
function enableSound() {
  try {
    S.audio = new (window.AudioContext || window.webkitAudioContext)();
    S.audio.resume();
    chime(true);
    toast(t("toast.sound_on"));
  } catch {
    toast(t("toast.sound_blocked"), true);
  }
  rerender(true);
}
async function testSound() {
  try {
    const r = await api("/api/sound/test", {});
    toast(r.played ? t("toast.sound_test") : t("toast.sound_test_fail"), !r.played);
  } catch (e) {
    toast(friendlyError(e), true);
  }
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
function notifyDue(s) {
  // Optional desktop notification when a dose becomes due while this tab is in the background.
  // It only brings the page forward; every action still happens on the page.
  if (!PREFS.notify || !("Notification" in window) || Notification.permission !== "granted") return;
  let seen = [];
  try { seen = JSON.parse(localStorage.getItem("dg-notified") || "[]"); } catch {}
  const due = s.today.filter((c) => c.reminder.state === "DUE" && c.workflow_state === "NOT_STARTED" && !seen.includes(c.reminder.reminder_id));
  if (!due.length) return;
  for (const c of due) seen.push(c.reminder.reminder_id);
  try { localStorage.setItem("dg-notified", JSON.stringify(seen.slice(-50))); } catch {}
  if (!document.hidden) return;
  const n = new Notification(t("due.now"), { body: t("notify.body", { name: due.map((c) => c.label).join(", ") }), tag: "doseguard-due" });
  n.onclick = () => { window.focus(); go("today"); n.close(); };
}
async function setNotify(on) {
  if (on) {
    if (!("Notification" in window)) { toast(t("prefs.notify_unsupported"), true); on = false; }
    else if (Notification.permission !== "granted") {
      const p = await Notification.requestPermission().catch(() => "denied");
      if (p !== "granted") { toast(t("prefs.notify_denied"), true); on = false; }
    }
  }
  PREFS.notify = on;
  savePrefs();
  renderPrefs();
}
function handleSounds(s) {
  let last = 0;
  try { last = Number(localStorage.getItem("dg-last-sound") || 0); } catch {}
  if (s.last_event_id != null && last > s.last_event_id) last = 0; // database was reset
  const fresh = (s.sounds || []).filter((e) => e.event_id > last);
  if (!fresh.length) return;
  try { localStorage.setItem("dg-last-sound", String(Math.max(...fresh.map((e) => e.event_id)))); } catch {}
  if (s.laptop_sound && s.laptop_sound.enabled) return; // the laptop already chimed; don't double it
  chime();
}
function announce(text) {
  let el = document.getElementById("announcer");
  if (!el) {
    el = h("div", { id: "announcer", class: "sr-only", role: "alert", "aria-live": "assertive" });
    document.body.append(el);
  }
  el.textContent = "";
  setTimeout(() => (el.textContent = text), 50);
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
    notifyDue(s);
    notifyFollowups(s);
    speakDue(s);
    if (s.due_count > S.lastDue) {
      const c = s.today.find((x) => x.reminder.state === "DUE");
      if (c) announce(`${t("due.now")}: ${c.label}`);
    }
    S.lastDue = s.due_count;
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
  document.documentElement.dataset.sky = skyPhase(hourOf(new Date()));
  renderChips();
  document.title = s.due_count ? `(${s.due_count}) ${t("due.now")} - Healthbox` : "Healthbox";
  $("#demo-badge").hidden = !s.demo_mode;
  const waiting = s.today.filter((c) => ["DUE", "NEEDS_REVIEW"].includes(c.reminder.state) && c.workflow_state === "NOT_STARTED");
  const due = $("#nav-due");
  due.hidden = !waiting.length;
  due.textContent = waiting.length || "";
  due.classList.toggle("missed", waiting.some((c) => c.reminder.state === "NEEDS_REVIEW"));
  $("#nav-verify").hidden = !(s.workflow && s.workflow.txn);
  $("#assistant-toggle").hidden = !!s.signed_out;
  if (s.signed_out && !$("#assistant").hidden) openAssistant(false);
  $("#tab-verify").hidden = !(s.workflow && s.workflow.txn) && S.view !== "verify";
  const acct = s.account || null;
  const lockIc = $("#door-lock");
  if (lockIc) lockIc.hidden = !(acct && acct.exists && !acct.unlocked);
  if (needsWelcome(s)) {
    if (!S.keys.__welcome) { S.keys = { __welcome: 1 }; renderWelcome(); }
    return;
  }
  if (S.keys.__welcome) { S.keys = {}; route(); return; }
  const unlocked = !!(acct && acct.unlocked);
  if (S.lastUnlocked !== undefined && S.lastUnlocked !== unlocked && DOOR_VIEWS.includes(S.view)) { S.lastUnlocked = unlocked; route(); return; }
  S.lastUnlocked = unlocked;
  const publicNote = document.getElementById("public-note");
  if (publicNote) { publicNote.hidden = !s.public_demo; publicNote.textContent = s.public_demo ? t("public.note", { n: s.public_demo }) : ""; }
  if (DOOR_VIEWS.includes(S.view) && setupLocked()) {
    // Signed out: Setup, medicines and history show the sign-in panel only (their data needs sign-in).
    if (S.keys[S.view] !== "signed-out") { S.keys[S.view] = "signed-out"; doorMain(S.view); }
    return;
  }
  if (S.setupPending && S.view === "setup") { S.setupPending = false; setup.render(); return; }
  const fn = VIEWS[S.view];
  if (!fn || !fn.key) return;
  const key = JSON.stringify([fn.key(s), S.busy, PREFS.lang, PREFS.simple, acct && acct.exists, unlocked]);
  if (!force && S.keys[S.view] === key) {
    if (fn.tick) fn.tick(s);
    return;
  }
  S.keys[S.view] = key;
  fn.render(s);
}

/* ---------------------------------------------------------------- top bar */
function notice(alert, ic, text, href, calm = false) {
  // Calm statuses may shrink to an icon on narrow screens; their words stay for screen readers and as a tooltip.
  const kids = [icon(ic), h("span", { class: "txt" }, text)];
  const cls = `notice ${alert ? "alert" : ""} ${calm ? "calm" : ""}`;
  return href ? h("a", { class: cls, href, title: text }, kids) : h("span", { class: cls, title: text }, kids);
}
function renderChips() {
  // Always shows reminder sound, dispenser, camera (with its pose during a dose) and simulated hardware.
  // Calm words when all is well; alert colour only for a real problem. Simple view keeps only problems.
  const s = S.status;
  const box = $("#chips");
  box.replaceChildren();
  if (S.offline) { box.append(notice(true, "wifi-slash", t("chip.offline"))); return; }
  if (!s) return;
  const d = s.device, cam = s.camera, tx = s.workflow.txn;
  const sim = d.simulated ? ` (${t("chip.sim")})` : "";
  const href = PREFS.simple ? null : "#device";
  const items = [];
  const ls = s.laptop_sound || {};
  if (ls.enabled) {
    // The server plays reminders on this laptop's speakers, so no click is needed. Warn only if it can't be heard.
    const [alert, ic, key] = ls.muted ? [true, "speaker-slash", "chip.laptop_muted"] : ls.quiet ? [true, "speaker-low", "chip.laptop_low"]
      : [false, "bell-ringing", "chip.laptop_on"];
    box.append(h("button", { class: `notice ${alert ? "alert" : "calm"}`, type: "button", onclick: testSound, title: t("chip.laptop_test") },
      icon(ic), h("span", { class: "txt" }, t(key))));
  } else if (S.audio && S.audio.state === "running") items.push([false, "bell", t("chip.sound_on"), null, true]);
  else box.append(h("button", { class: "notice", type: "button", onclick: enableSound }, icon("bell-slash"), h("span", {}, t("chip.sound_off"))));
  if (!d.connected) items.push([true, "plugs", t("chip.dev_off") + sim, href]);
  else if (d.fault) items.push([true, "first-aid", t("chip.dev_fault") + sim, href]);
  else if (!s.workflow.armed) items.push([false, "pause-circle", t("chip.dev_notarmed") + sim, href, true]);
  else items.push([false, "check-circle", t("chip.dev_ready") + sim, href, true]);
  const pose = tx && tx.pose ? t("chip.cam_pose", { pose: tx.pose === "FACE" ? t("camera.face") : t("camera.tray") }) : null;
  if (!cam.simulated && cam.face && cam.face.error) items.push([true, "camera-slash", t("chip.cam_bad")]);
  else items.push([false, tx && tx.pose === "TRAY" ? "squares-four" : "camera", pose || (cam.simulated ? t("chip.cam_sim") : t("chip.cam_ok")), null, !pose]);
  if (tzDiffers()) items.push([false, "globe-hemisphere-west", t("chip.tz", { tz: zoneCity(tz()) }), PREFS.simple ? null : "#schedules"]);
  if (s.vision && s.vision.tray_bypass) items.push([true, "flask", t("chip.tray_bypass")]);
  for (const [alert, ic, text, link, calm] of items) {
    if (PREFS.simple && calm) continue;
    box.append(notice(alert, ic, text, link, !!calm));
  }
}

/* ---------------------------------------------------------------- display panel */
function renderPrefs() {
  const p = $("#prefs");
  const seg = (label, items, cur, onPick) => h("div", { class: "seg", role: "group", "aria-label": label },
    items.map(([v, l]) => h("button", { type: "button", "aria-pressed": String(cur === v), onclick: () => onPick(v) }, l)));
  p.replaceChildren(
    h("div", { class: "panel-head" },
      h("h2", { id: "prefs-title" }, t("prefs.title")),
      h("button", { class: "icon-btn", type: "button", "aria-label": t("prefs.close"), onclick: () => togglePrefs(false) }, icon("x"))),
    h("div", {}, h("div", { class: "glabel" }, icon("translate"), t("prefs.lang")),
      seg(t("prefs.lang"), Object.keys(I18N).map((k) => [k, I18N[k]["lang.name"]]), PREFS.lang,
        (v) => { PREFS.lang = v; savePrefs(); refreshAll(); renderPrefs(); })),
    h("div", {}, h("div", { class: "glabel" }, icon("text-aa"), t("prefs.size")),
      seg(t("prefs.size"), [[0, t("prefs.size0")], [1, t("prefs.size1")], [2, t("prefs.size2")]], Number(PREFS.size) || 0,
        (v) => { PREFS.size = v; savePrefs(); renderPrefs(); })),
    h("label", { class: "switch" }, h("span", { class: "row" }, icon("circle-half"), t("prefs.contrast")),
      h("input", { type: "checkbox", role: "switch", checked: !!PREFS.contrast, onchange: (e) => { PREFS.contrast = e.target.checked; savePrefs(); } })),
    h("div", {},
      h("label", { class: "switch" }, h("span", { class: "row" }, icon("bell-ringing"), t("prefs.notify")),
        h("input", { type: "checkbox", role: "switch", checked: !!PREFS.notify, onchange: (e) => setNotify(e.target.checked) })),
      h("p", { class: "switch-help" }, t("prefs.notify_help"))),
    h("div", {},
      h("label", { class: "switch" }, h("span", { class: "row" }, icon("fast-forward"), t("prefs.auto")),
        h("input", { type: "checkbox", role: "switch", checked: PREFS.auto !== false, onchange: (e) => { PREFS.auto = e.target.checked; savePrefs(); } })),
      h("p", { class: "switch-help" }, t("prefs.auto_help")),
      h("label", { class: "switch" }, h("span", { class: "row" }, icon("speaker-high"), t("prefs.voice")),
        h("input", { type: "checkbox", role: "switch", checked: PREFS.voice !== false, onchange: (e) => { PREFS.voice = e.target.checked; savePrefs(); } })),
      h("p", { class: "switch-help" }, t("prefs.voice_help")),
      h("label", { class: "switch" }, h("span", { class: "row" }, icon("eye"), t("prefs.simple")),
        h("input", { type: "checkbox", role: "switch", checked: !!PREFS.simple, onchange: (e) => { PREFS.simple = e.target.checked; savePrefs(); route(); } })),
      h("p", { class: "switch-help" }, t("prefs.simple_help"))));
}
function togglePrefs(open) {
  const p = $("#prefs");
  const btn = $("#prefs-toggle");
  if (open === undefined) open = p.hidden;
  if (open) renderPrefs();
  p.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) { const first = p.querySelector(".seg button[aria-pressed='true']"); if (first) first.focus(); }
  else btn.focus();
}
function refreshAll() {
  S.keys = {};
  applyI18n();
  renderChips();
  renderPrompts();
  renderChat();
  route();
}

/* ---------------------------------------------------------------- the sky (Today only) */
const ARC = { cx: 500, cy: 285, rx: 470, ry: 255 }; // a 1000 x 300 box
// The arc is one half of the day. Day: the sun crosses from today's real sunrise to sunset. Night: the moon crosses
// from sunset to the next sunrise, then the sun comes round again. Hours count from today's local midnight in the
// plan's timezone, so 26 is 2 AM tomorrow.
// One arc for the medicine day, 5 AM to 11 PM, read left to right. "Now" travels along it as the sun while it is
// light and as the moon after the real sunset. Before 5 AM it waits at the start; after 11 PM it rests at the end.
const MED_DAY = { from: 5, to: 23 };
function skyWindow(nowHr) {
  const sun = sunToday();
  return { night: nowHr < sun.rise || nowHr >= sun.set, from: MED_DAY.from, to: MED_DAY.to,
    ticks: [[5, "sky.h5"], [12, "sky.h12"], [18, "sky.h18"], [23, "sky.h23"]] };
}
function arcPoint(hr, w) {
  const f = Math.min(1, Math.max(0, (hr - w.from) / (w.to - w.from)));
  const th = Math.PI * (1 - f);
  return { x: ARC.cx + ARC.rx * Math.cos(th), y: ARC.cy - ARC.ry * Math.sin(th) };
}
// Labels sit above the curve near the top and inside it on the steep sides, so the stroke never runs through them.
function arcSide(hr, w) {
  const f = Math.min(1, Math.max(0, (hr - w.from) / (w.to - w.from)));
  const c = Math.cos(Math.PI * (1 - f));
  return c < -0.55 ? "in-l" : c > 0.55 ? "in-r" : "top";
}
function renderSky(s) {
  const scene = $("#sky-scene");
  if (S.view !== "today" || !s) { scene.replaceChildren(); return; }
  const nowDate = new Date();
  const now = hourOf(nowDate);
  const w = skyWindow(now);
  const hrOf = (iso) => now + (new Date(iso) - nowDate) / 3600000;
  const body = arcPoint(now, w);
  const start = arcPoint(w.from, w), end = arcPoint(w.to, w);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 1000 300");
  svg.setAttribute("preserveAspectRatio", "none");
  const path = (d, cls) => { const p = document.createElementNS(ns, "path"); p.setAttribute("d", d); if (cls) p.setAttribute("class", cls); return p; };
  // Solid for the part of the day already lived, dashed for the rest. Never both on top of each other.
  const bx = body.x.toFixed(1), by = body.y.toFixed(1);
  svg.append(path(`M ${start.x} ${start.y} A ${ARC.rx} ${ARC.ry} 0 0 1 ${bx} ${by}`, "walked"),
    path(`M ${bx} ${by} A ${ARC.rx} ${ARC.ry} 0 0 1 ${end.x} ${end.y}`, "ahead"));
  const arc = h("div", { class: `arc ${w.night ? "night" : "day"}`, "aria-hidden": "true" }, svg,
    h("span", { class: w.night ? "sun moon" : "sun", style: `left:${body.x / 10}%;top:${body.y / 3}%` }));
  // Doses inside this half of the day. Same-minute doses share a point; the strongest state wins.
  const rank = { missed: 4, due: 3, wait: 2, skip: 1, done: 0 };
  const seen = new Set();
  const doses = [...s.today, s.next_dose].filter((c) => c && !seen.has(c.dose_id) && seen.add(c.dose_id));
  const points = new Map();
  let nextOutside = null;
  for (const c of doses) {
    const hr = hrOf(c.due_at_utc);
    const st0 = doseState(c);
    const st = st0 === "skip" ? "missed" : st0; // not taken is not taken: older "skipped" records show as missed
    if (hr < w.from - 0.01) continue;
    if (hr > w.to) {
      if (["wait", "due"].includes(st) && (!nextOutside || hr < nextOutside.hr)) nextOutside = { c, hr };
      continue;
    }
    const p = points.get(c.due_at_utc);
    if (!p || rank[st] > rank[p.st]) points.set(c.due_at_utc, { c, st, hr });
  }
  // The dose due at the sun (or moon) is shown by the body itself; others that close slide clear (settleArc).
  let atBody = null;
  for (const p of points.values()) {
    const pt = arcPoint(p.hr, w);
    p.atBody = Math.abs(pt.x - body.x) < 30 && Math.abs(pt.y - body.y) < 30;
    if (p.atBody && (p.st === "due" || p.st === "missed") && (!atBody || rank[p.st] > rank[atBody.st])) atBody = p;
  }
  for (const p of points.values()) {
    const { c, st, hr } = p;
    const pt = arcPoint(hr, w);
    const cls = `arc-point ${st} ${arcSide(hr, w)}${p === atBody ? " at-sun" : ""}`;
    const near = p === atBody ? null : { "data-hr": hr, "data-dir": hr <= now ? -1 : 1 };
    arc.append(h("span", { class: cls, style: `left:${pt.x / 10}%;top:${pt.y / 3}%`, ...near },
      h("span", { class: "arc-dot" }, st === "missed" ? icon("x") : null), h("span", { class: "arc-time" }, fmtClock(c.due_at_utc))));
  }
  // Hour marks are the least important labels: one steps aside when the sun or moon (and its "Now" label) or a dose is near.
  const near = (a, b, dx, dy) => Math.abs(a.x - b.x) < dx && Math.abs(a.y - b.y) < dy;
  const dosePts = [...points.values()].map((p) => arcPoint(p.hr, w));
  for (const [hr, key] of w.ticks) {
    const pt = arcPoint(hr, w);
    if (near(pt, body, 120, 90) || dosePts.some((d) => near(pt, d, 75, 60))) continue;
    arc.append(h("span", { class: `arc-hour ${arcSide(hr, w)}`, style: `left:${pt.x / 10}%;top:${pt.y / 3}%` }, t(key)));
  }
  // The sun or moon says what time it is now, unless a dose due right now already labels that spot.
  if (!atBody) {
    const edge = body.x > 780 ? " at-end" : body.x < 220 ? " at-start" : "";
    arc.append(h("span", { class: `arc-now${edge}`, style: `left:${body.x / 10}%;top:${body.y / 3}%` },
      t("sky.now", { time: fmtClock(nowDate.toISOString()) })));
  }
  // The next dose in the other half of the day waits at the arc's end, so it is never out of sight.
  let nextLine = null;
  const comingOnArc = [...points.values()].some((p) => (p.st === "wait" || p.st === "due") && p.hr >= now - 0.01);
  if (nextOutside && !comingOnArc) {
    // "Tonight" only for a dose later this evening; otherwise name the day ("Next: Tomorrow 4:14 PM").
    const iso = nextOutside.c.due_at_utc;
    const tonight = nextOutside.hr < 24;
    const [ic, text] = tonight ? ["moon", t("sky.tonight", { time: fmtClock(iso) })]
      : ["calendar-blank", t("sky.next", { when: fmtWhen(iso) })];
    nextLine = h("p", { class: "sky-next" }, icon(ic), text);
  }
  Object.assign(arc.dataset, { sunHr: String(now), from: String(w.from), to: String(w.to) });
  const greet = greeting(s);
  if (nextLine) greet.append(nextLine);
  scene.replaceChildren(h("div", { class: "scene" }, greet, arc));
  settleArc(arc);
  declutterArc(arc);
}

// Slide any dose that would touch the sun or moon (disc + glow + a visible gap) along the arc, in real pixels,
// so it stays a dot at its own place whatever the screen width.
function settleArc(arc) {
  if (!arc || !arc.isConnected) return;
  const w = arc.clientWidth, ht = arc.clientHeight;
  if (!w || !ht) return;
  const win = { from: Number(arc.dataset.from), to: Number(arc.dataset.to) };
  const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
  const clear = 3.9 * rem; // disc 1.7 + glow .7 + dot .6 + a .9rem gap
  const sp = arcPoint(Number(arc.dataset.sunHr), win);
  const sx = (sp.x / 1000) * w, sy = (sp.y / 300) * ht;
  arc.querySelectorAll(".arc-point[data-hr]").forEach((el) => {
    const dir = Number(el.dataset.dir);
    let hr = Number(el.dataset.hr);
    for (let i = 0; i < 400; i++) {
      const p = arcPoint(hr, win);
      if (Math.hypot((p.x / 1000) * w - sx, (p.y / 300) * ht - sy) >= clear || hr <= win.from || hr >= win.to) {
        el.style.left = `${p.x / 10}%`;
        el.style.top = `${p.y / 3}%`;
        break;
      }
      hr += dir * 0.05;
    }
  });
}

// After layout, hide whichever label is less important wherever two labels touch on screen.
// Priority: "Now", then missed or upcoming dose times, then taken dose times, then hour marks.
function declutterArc(arc) {
  if (!arc || !arc.isConnected) return;
  const rank = (el) => el.classList.contains("arc-now") ? 4
    : el.classList.contains("arc-hour") ? 1
    : el.closest(".arc-point.done, .arc-point.skip") ? 2 : 3;
  // "Now" first finds a place beside the sun or moon that covers no dose dot.
  const nowEl = arc.querySelector(".arc-now");
  if (nowEl) {
    const dots = [...arc.querySelectorAll(".arc-dot")].filter((d) => getComputedStyle(d).visibility !== "hidden").map((d) => d.getBoundingClientRect());
    const clear = () => { const r = nowEl.getBoundingClientRect(); return !dots.some((d) => r.left < d.right + 3 && r.right > d.left - 3 && r.top < d.bottom + 3 && r.bottom > d.top - 3); };
    const sideward = nowEl.classList.contains("at-start") ? "pos-right" : "pos-left";
    for (const pos of ["", "pos-below", sideward, "pos-right"]) {
      nowEl.classList.remove("pos-below", "pos-left", "pos-right");
      if (pos) nowEl.classList.add(pos);
      if (clear()) break;
    }
  }
  const labels = [...arc.querySelectorAll(".arc-now, .arc-time, .arc-hour")]
    .filter((el) => getComputedStyle(el).display !== "none")
    .sort((a, b) => rank(b) - rank(a));
  const kept = [];
  const pad = 4;
  for (const el of labels) {
    el.style.visibility = "";
    const r = el.getBoundingClientRect();
    const hit = kept.some((k) => r.left < k.right + pad && r.right > k.left - pad && r.top < k.bottom + pad && r.bottom > k.top - pad);
    if (hit) el.style.visibility = "hidden";
    else kept.push(r);
  }
}
window.addEventListener("resize", () => { const arc = document.querySelector("#sky-scene .arc"); settleArc(arc); declutterArc(arc); });

function greeting(s) {
  const hour = hourOf(new Date());
  const word = hour < 12 && hour >= 4 ? t("greet.morning") : hour >= 12 && hour < 18 ? t("greet.afternoon") : t("greet.evening");
  const name = (s.account && s.account.first_name) || (s.plan && s.plan.recipient);
  const date = new Intl.DateTimeFormat(locale(), { weekday: "long", month: "long", day: "numeric", timeZone: tz() }).format(new Date());
  return h("div", { class: "greet" }, h("h1", {}, name ? `${word}, ${name}` : word), h("p", {}, date));
}

/* ---------------------------------------------------------------- Today */
const today = {
  key: (s) => [s.today, s.as_needed, s.next_dose && s.next_dose.dose_id, s.demo_mode, s.workflow.armed, s.workflow.txn && s.workflow.txn.state,
    watchKey(s.watch), (s.followups || []).map((f) => [f.dose_id, f.followup]), (s.cartridges || []).map((c) => [c.cartridge, c.low, c.empty, c.count]),
    s.look_alike_blocks && (s.look_alike || []).length,
    s.device.connected, s.device.fault, s.plan && s.plan.plan_version_id, s.plan && s.plan.recipient, Math.floor(hourOf(new Date()) * 12),
    !!(S.audio && S.audio.state === "running"), s.signed_out, s.account && s.account.first_name],
  render(s) {
    renderSky(s);
    if (s.signed_out) return renderSignedOutToday(s);
    const tx = s.workflow.txn;
    const active = s.today.filter((c) => ["DUE", "SNOOZED", "NEEDS_REVIEW"].includes(c.reminder.state) && c.workflow_state === "NOT_STARTED");
    const lead = h("div", { class: "stack" });
    if (!s.plan) {
      lead.append(calmPanel(t("today.no_plan_title"), t("today.no_plan_body"),
        PREFS.simple ? null : h("button", { class: "btn btn-primary btn-big", onclick: () => go("setup") }, t("today.no_plan_btn"), icon("arrow-right"))));
    }
    if (tx) lead.append(progressPanel(s, tx));
    const taking = takingPanel(s);
    if (taking) lead.append(taking);
    followPanels(s).forEach((p) => lead.append(p));
    // Doses due at the same time come out of the dispenser together, so they share one panel.
    const groups = [];
    for (const c of active) {
      const g = c.delivery_mode === "DEVICE" && groups.find((x) => x[0].delivery_mode === "DEVICE" && x[0].due_at_utc === c.due_at_utc && x[0].reminder.state === c.reminder.state);
      if (g) g.push(c); else groups.push([c]);
    }
    // One primary action per screen: while a dose is in progress, other panels step back.
    groups.forEach((g, i) => lead.append(duePanel(g, s, !!tx || i > 0)));
    const stock = stockNotes(s);
    if (stock) lead.append(stock);
    if (s.plan && !active.length && !tx && !taking && !(s.followups || []).length) {
      const nd = s.next_dose;
      lead.append(calmPanel(t("today.all_set"), nd ? t("today.next", { name: nd.label, time: whenText(nd) }) : t("today.all_set_sub")));
    }
    const shown = new Set([...active.map((c) => c.dose_id), ...(tx ? tx.doses.map((d) => d.dose_id) : [])]);
    setMain(h("div", { class: "today-grid" }, lead, h("div", {}, laterPanel(s, shown), needPanel(s))));
  },
};

/* Signed out: only what a due dose needs, then the sign-in panel. A forgotten password never blocks a dose. */
function renderSignedOutToday(s) {
  const tx = s.workflow.txn;
  const active = s.today.filter((c) => ["DUE", "SNOOZED", "NEEDS_REVIEW"].includes(c.reminder.state) && c.workflow_state === "NOT_STARTED");
  const lead = h("div", { class: "stack" });
  if (tx) lead.append(progressPanel(s, tx));
  const taking = takingPanel(s);
  if (taking) lead.append(taking);
  followPanels(s).forEach((p) => lead.append(p));
  const groups = [];
  for (const c of active) {
    const g = c.delivery_mode === "DEVICE" && groups.find((x) => x[0].delivery_mode === "DEVICE" && x[0].due_at_utc === c.due_at_utc && x[0].reminder.state === c.reminder.state);
    if (g) g.push(c); else groups.push([c]);
  }
  groups.forEach((g, i) => lead.append(duePanel(g, s, !!tx || i > 0)));
  if (!lead.childNodes.length) lead.append(calmPanel(t("signedout.none_title"), t("signedout.none_body")));
  // One sign-in panel kept across refreshes, so a half-typed password is not lost when the page updates.
  if (!S.signin) S.signin = unlockPanel(false);
  setMain(h("div", { class: "today-grid" }, lead, h("div", {}, S.signin)));
}

function calmPanel(title, body, action) {
  return h("section", { class: "due-panel calm" }, h("h2", {}, title), h("p", {}, body),
    action ? h("div", { class: "due-actions" }, action) : null);
}

function doseItems(doses, s) {
  return h("ul", { class: "due-items" }, doses.map((d) => {
    const c = s.today.find((x) => x.dose_id === d.dose_id) || d;
    return h("li", {}, swatch(c.class_color), d.label, h("span", { class: "qty" }, `×\u00a0${d.quantity}`));
  }));
}

function progressPanel(s, tx) {
  const first = s.today.find((x) => x.dose_id === (tx.doses[0] || {}).dose_id);
  return h("section", { class: "due-panel", "aria-label": t("today.in_progress") },
    h("h2", { class: "due-name" }, t("today.in_progress")),
    h("p", { class: "due-when" }, h("span", { class: "sun-chip", "aria-hidden": "true" }),
      first ? t("due.now_at", { time: fmtClock(first.due_at_utc) }) : t("pill.verifying")),
    h("p", { class: "due-sub" }, t(`stage.${stepKey(tx)}.title`)),
    doseItems(tx.doses, s),
    h("div", { class: "due-actions" }, h("button", { class: "btn btn-primary btn-big ringed", onclick: () => go("verify") }, t("today.continue"), icon("arrow-right"))));
}

function duePanel(group, s, demoted = false) {
  const c = group[0];
  const r = c.reminder;
  const review = r.state === "NEEDS_REVIEW";
  const snoozed = r.state === "SNOOZED";
  const device = c.delivery_mode === "DEVICE";
  const each = (path) => () => Promise.all(group.map((x) => api(path, { reminder_id: x.reminder.reminder_id })));
  const names = group.map((x) => `${x.label}, ${x.quantity}`).join(". ");
  const speech = () => t(device ? "due.speak" : "due.speak_reminder", { name: names, n: group.reduce((a, x) => a + x.quantity, 0) });
  const when = review ? h("p", { class: "due-when missed" }, icon("warning-circle"), t("due.review"))
    : snoozed ? h("p", { class: "due-when paused" }, icon("pause-circle"), t("due.snoozed", { time: fmtClock(r.snoozed_until) }))
    : h("p", { class: "due-when" }, h("span", { class: "sun-chip", "aria-hidden": "true" }), t("due.now_at", { time: fmtClock(c.due_at_utc) }));
  const title = group.length === 1 ? c.label : t("rx.group2", { n: group.length });
  const sub = [group.length === 1 && c.strength_text ? `${sentence(c.strength_text)}. ` : "", device ? t("rx.from_dispenser") : t("rx.reminder")].join("");

  const actions = h("div", { class: "due-actions" });
  let blocker = null;
  const primary = (attrs, ...kids) => h("button", { class: demoted ? "btn btn-big" : "btn btn-primary btn-big ringed", ...attrs }, ...kids);
  if (device) {
    const b = startBlockers(s);
    actions.append(primary({ disabled: b.any || !!S.busy, onclick: () => startDose(c) }, t("due.get"), icon("arrow-right")));
    if (b.any && !demoted) {
      blocker = h("div", { class: "blocker" }, icon("info"),
        h("div", {}, b.everyday.join(" "), !PREFS.simple && b.detail.length ? h("div", { class: "detail" }, b.detail.join(" ")) : null));
    }
  } else {
    actions.append(primary({ disabled: !!S.busy, onclick: () => selfReport(c) }, icon("check"), t("due.mark")));
  }
  if (r.state === "DUE") {
    actions.append(h("button", { class: "btn btn-quiet", disabled: !!S.busy,
      onclick: () => act("snooze", each("/api/reminders/snooze")).then((x) => x && toast(t("toast.snoozed", { n: s.config.snooze_minutes }))) },
      icon("clock"), t("due.snooze", { n: s.config.snooze_minutes })));
  }
  actions.append(readButton(speech));
  const help = review ? t("due.review_help") : device ? t("due.silence_help") : `${t("due.mark_help")} ${t("due.silence_help")}`;
  const more = h("details", { class: "more" }, h("summary", {}, icon("caret-right"), t("rx.more")),
    h("div", { class: "more-body" },
      h("div", { class: "due-actions" },
        h("button", { class: "btn btn-quiet", disabled: !!S.busy, onclick: () => act("ack", each("/api/reminders/ack")).then((x) => x && toast(t("toast.silenced"))) },
          icon("bell-slash"), t("due.silence"))),
      h("p", { class: "due-help" }, help)));
  return h("section", { class: "due-panel", "aria-label": `${t("due.now")}: ${names}` },
    h("h2", { class: "due-name" }, title), when, h("p", { class: "due-sub" }, sub),
    group.length > 1 ? doseItems(group, s)
      : h("ul", { class: "due-items" }, h("li", {}, swatch(c.class_color), t("due.count", { n: c.quantity }))),
    actions, blocker, more);
}

function laterPanel(s, shown) {
  const rest = s.today.filter((c) => !shown.has(c.dose_id));
  const box = h("section", { class: "panel later", "aria-labelledby": "later-title" }, h("h2", { id: "later-title" }, t("today.later")));
  if (!rest.length) {
    box.append(h("p", { class: "empty" }, s.today.length ? t("today.nothing_next") : s.demo_mode ? t("list.empty_demo") : t("list.empty")));
    return box;
  }
  const ol = h("ol");
  for (const c of rest) {
    const r = c.reminder;
    const acts = [];
    if (r.state === "ACKNOWLEDGED" && c.workflow_state === "NOT_STARTED" && !c.outcome) {
      acts.push(c.delivery_mode === "DEVICE"
        ? h("button", { class: "btn btn-quiet", disabled: startBlockers(s).any || !!S.busy, onclick: () => startDose(c) }, t("due.get"))
        : h("button", { class: "btn btn-quiet", disabled: !!S.busy, onclick: () => selfReport(c) }, t("due.mark")));
    }
    if (["RELEASED", "COMPLETE"].includes(c.workflow_state) && !c.self_report && c.delivery_mode === "DEVICE") {
      acts.push(h("button", { class: "btn btn-quiet", onclick: () => confirmTaken(c) }, icon("check"), t("due.mark")));
    }
    const st = doseState(c);
    ol.append(h("li", { class: st === "skip" ? "struck" : null },
      h("span", { class: "t" }, whenText(c)),
      h("div", {}, h("div", { class: "name" }, swatch(c.class_color), h("span", { class: "lbl" }, c.label), h("span", { class: "muted" }, ` ×\u00a0${c.quantity}`)),
        h("div", { class: "sub" }, h("span", { class: `mark ${st === "missed" ? "alert" : st}`, "aria-hidden": "true" }), stateWords(c))),
      acts.length ? h("div", { class: "row-end" }, acts) : null));
  }
  box.append(ol);
  return box;
}

/* As-needed medicines: no schedule, but the server enforces how often. */
function needPanel(s) {
  const items = s.as_needed || [];
  if (!items.length) return null;
  const b = startBlockers(s);
  return h("section", { class: "panel need", "aria-labelledby": "need-title" }, h("h2", { id: "need-title" }, t("need.title")),
    items.map((it) => {
      const device = it.delivery_mode === "DEVICE";
      const rule = t(it.min_interval_hours === 1 ? "need.rule_1" : "need.rule_n", { max: it.max_per_day, hours: it.min_interval_hours });
      const busyHere = !!it.in_progress_dose_id;
      const when = busyHere ? h("span", { class: "when" }, icon("hourglass-medium"), t("pill.verifying"))
        : it.available ? h("span", { class: "when" }, h("span", { class: "sun-chip", "aria-hidden": "true" }), t("need.ready"))
        : h("span", { class: "when wait" }, icon("clock"),
            it.blocked_reason === "DAILY_LIMIT" ? t("need.limit", { time: fmtWhen(it.next_available_at) })
              : it.next_available_at ? t("need.next", { time: fmtWhen(it.next_available_at) }) : (it.blocked_message || ""));
      const can = it.available && !S.busy && !busyHere && !(device && b.any);
      const btn = busyHere
        ? h("button", { class: "btn", onclick: () => go("verify") }, t("today.continue"), icon("arrow-right"))
        : h("button", { class: "btn", disabled: !can, onclick: () => requestNeeded(it) }, device ? t("need.take") : t("need.took"));
      return h("div", { class: "need-item" },
        h("div", { class: "name" }, swatch(it.class_color), it.label, h("span", { class: "muted" }, ` ×\u00a0${it.quantity}`)),
        h("p", { class: "rule" }, [sentence(it.strength_text), rule].filter(Boolean).join(". ")),
        h("p", { class: "rule" }, t("need.count", { n: it.taken_last_24h, max: it.max_per_day })),
        h("div", { class: "foot" }, when, btn),
        device && b.any && it.available ? h("p", { class: "rule" }, b.everyday.join(" ")) : null);
    }));
}

/* ---------------------------------------------------------------- Dose steps */
const verify = {
  key: (s) => {
    const tx = s.workflow.txn;
    return [tx && { ...tx, watch: watchKey(tx.watch), observations: Object.fromEntries(Object.entries(tx.observations || {}).map(([k, o]) => [k, { ...o, age_s: null }])) },
      watchKey(s.watch), (s.followups || []).map((f) => f.dose_id),
      s.workflow.armed, s.workflow.busy, s.workflow.pending_operation, s.device.connected, s.device.fault, s.device.door_closed,
      s.camera.mode, s.vision.sim, s.today.map((c) => [c.dose_id, c.reminder.state, c.workflow_state, c.self_report])];
  },
  tick(s) {
    const tx = s.workflow.txn;
    if (!tx) return;
    for (const [k, o] of Object.entries(tx.observations || {})) {
      const el = document.getElementById(`age-${k}`);
      if (el) el.textContent = ageText(o);
    }
  },
  render(s) {
    renderSky(s);
    const tx = s.workflow.txn;
    if (!tx) return renderNoTxn(s);
    const failed = tx.state === "FAULT" || tx.state === "UNCERTAIN";
    const key = stepKey(tx);
    const steps = stepsFor(tx);
    const idx = stepIndex(tx) >= 0 ? stepIndex(tx) : null;
    const where = idx != null && !failed ? t("step.progress", { n: idx + 1, total: steps.length }) : null;
    const track = h("ol", { class: `track ${failed ? "stopped" : ""}`, style: `grid-template-columns:repeat(${steps.length},1fr)`,
      "aria-label": where || t(`stage.${key}.title`) },
      steps.map((st, i) => {
        const cls = failed ? "" : i < idx ? "done" : i === idx ? "current" : "";
        return h("li", { class: cls, "aria-current": cls === "current" ? "step" : null },
          h("span", { class: "dot", "aria-hidden": "true" }), h("span", { class: "name" }, t(`step.${st}`)));
      }));
    const head = h("div", { class: "steps-head" }, h("div", {}, h("h1", {}, t(`stage.${key}.title`)),
      where ? h("p", { class: "lede" }, where) : null));
    const cam = cameraPanel(s, tx);
    cam.append(checkedPanel(tx));
    drawLive(S.live);
    speakStep(s);
    setMain(head, track, h("div", { class: "steps-grid" }, stepPanel(s, tx), cam));
  },
};

function recoverText(code) {
  if (DEVICE_CODES.includes(code)) return t("recover.CAREGIVER");
  return hasKey(`recover.${code}`) ? t(`recover.${code}`) : t("recover.DEFAULT");
}

function blockText(b) {
  if (DEVICE_CODES.includes(b.code)) return t("reason.CAREGIVER");
  const d = b.details || {};
  if (b.code === "COUNT_MISMATCH" && d.expected && d.observed) {
    const tx = S.status.workflow.txn;
    const labels = (tx && tx.class_labels) || {};
    const lines = Object.keys({ ...d.expected, ...d.observed })
      .filter((k) => Number(d.expected[k] || 0) !== Number(d.observed[k] || 0))
      .map((k) => t("count.line", { e: d.expected[k] || 0, o: d.observed[k] || 0, name: labels[k] || k }));
    return [t("reason.COUNT_MISMATCH"), ...lines].join(" ");
  }
  return hasKey(`reason.${b.code}`) ? t(`reason.${b.code}`, d) : b.message;
}

function stepPanel(s, tx) {
  const wfBusy = s.workflow.busy || S.busy;
  const blocks = tx.blocks || [];
  const key = stepKey(tx);
  const say = t(`stage.${key}.instr`);
  const panel = h("section", { class: "step-panel", "aria-labelledby": "stage-say" });
  panel.append(h("ul", { class: "step-for", "aria-label": t("rx.your_dose") }, tx.doses.map((d) => {
    const c = s.today.find((x) => x.dose_id === d.dose_id) || {};
    return h("li", {}, swatch(c.class_color), d.label, h("span", { class: "qty" }, `×\u00a0${d.quantity}`));
  })));
  for (const b of blocks) {
    panel.append(h("div", { class: "alert-box", role: "alert" }, icon("warning-circle"),
      h("div", {}, h("div", { class: "what" }, blockText(b)), h("div", { class: "next" }, recoverText(b.code)),
        !PREFS.simple && DEVICE_CODES.includes(b.code) ? h("div", { class: "detail" }, b.message) : null)));
  }
  panel.append(h("p", { class: "say", id: "stage-say" }, say));
  if (tx.state === "COLLECTION_CHECK") panel.append(watchBox(tx.watch));
  const handObs = tx.observations.HAND_CHECK;
  if (handObs && handObs.photo) panel.append(proofPhoto(handObs));
  const zone = h("div", { class: "step-actions" });
  const row = h("div", { class: "due-actions" });
  if (tx.state === "FAULT" || tx.state === "UNCERTAIN") {
    if (!PREFS.simple) row.append(h("button", { class: "btn btn-primary btn-big", onclick: () => go("device") }, icon("wrench"), t("verify.caregiver_btn")));
  } else if (tx.state === "DISPENSING" || tx.state === "RELEASE_PENDING") {
    zone.append(h("div", { class: "waiting", role: "status" }, h("span", { class: "breath", "aria-hidden": "true" }), t("verify.wait")));
  } else if (wfBusy) {
    zone.append(h("div", { class: "waiting", role: "status" }, h("span", { class: "breath", "aria-hidden": "true" }),
      s.workflow.busy === "HAND_CHECK" ? t("verify.checking_hand")
        : tx.pose === "FACE" && tx.state !== "COLLECTION_CHECK" ? t("verify.checking_face") : t("verify.checking_tray")));
  } else if (tx.state === "RECIPIENT_RECHECK") {
    row.append(h("button", { class: "btn btn-primary btn-big ringed", disabled: !tx.release_available, onclick: () => release(tx) }, icon("lock-key-open"), t(`stage.${tx.state}.btn`)));
    if (!tx.release_available) zone.append(h("p", { class: "muted" }, t("verify.unlock_unavailable")));
  } else if (key === "HAND_CHECK") {
    row.append(h("button", { class: "btn btn-primary btn-big ringed", onclick: () => runHand(tx) }, icon("hand-palm"), t("stage.HAND_CHECK.btn")));
  } else if (tx.next_action) {
    row.append(h("button", { class: "btn btn-primary btn-big ringed", onclick: () => runCheck(tx) },
      icon(tx.pose === "FACE" && tx.state !== "COLLECTION_CHECK" ? "user-focus" : "camera"), t(`stage.${tx.state}.btn`)));
  }
  row.append(readButton(() => [say, ...blocks.map(blockText)].join(" ")));
  zone.prepend(row);
  const quiet = h("div", { class: "due-actions" });
  if (key === "HAND_CHECK" && !wfBusy) {
    quiet.append(h("button", { class: "btn btn-quiet", onclick: () => act("handskip", () => api("/api/doses/hand-skip", { txn_id: tx.txn_id })) }, t("hand.skip")));
  }
  if (tx.state === "COLLECTION_CHECK" && !wfBusy) {
    quiet.append(h("button", { class: "btn btn-quiet", onclick: () => act("finish", () => api("/api/doses/finish", { txn_id: tx.txn_id })) }, t("verify.finish")));
  }
  if (tx.can_cancel && !wfBusy) {
    quiet.append(h("button", { class: "btn btn-quiet", onclick: () => act("cancel", () => api("/api/doses/cancel", { txn_id: tx.txn_id })) }, icon("x"), t("verify.cancel")));
  }
  if (quiet.childNodes.length) zone.append(quiet);
  panel.append(zone);
  return panel;
}

async function release(tx) {
  const r = await act("release", () => api("/api/doses/release", { txn_id: tx.txn_id }));
  if (r) toast(t("verify.release_req"));
}

async function runHand(tx) {
  const r = await act("hand", () => api("/api/doses/hand", { txn_id: tx.txn_id }));
  if (!r) return;
  if (r.result === "PASS") { toast(t("hand.passed")); announce(t("hand.passed")); }
  else toast(blockText({ code: (r.reason_codes || [])[0], message: r.message }), true);
}

async function runCheck(tx) {
  const r = await act("check", () => api("/api/doses/check", { txn_id: tx.txn_id }));
  if (!r) return;
  if (r.result === "PASS") { toast(t("toast.check_passed")); announce(t("toast.check_passed")); }
  else toast(blockText({ code: (r.reason_codes || [])[0], message: r.message }), true);
}

function ageText(o) {
  if (o.age_s == null) return "from an earlier session";
  if (o.age_s < 60) return `${Math.round(o.age_s)} s ago`;
  return `${Math.round(o.age_s / 60)} min ago`;
}

function cameraPanel(s, tx) {
  const pose = tx.pose;
  const trayBypass = s.vision && s.vision.tray_bypass && pose === "TRAY";
  if (s.camera.simulated || trayBypass) {
    const panel = h("section", { class: "panel cam-panel" },
      h("div", { class: "sim-cam" }, h("span", { class: "glyph", "aria-hidden": "true" }, icon(pose === "TRAY" ? "squares-four" : "user-focus")),
        h("div", {}, h("h3", {}, pose === "FACE" ? t("camera.face") : t("camera.tray")),
          h("p", { class: "muted" }, trayBypass ? t("camera.bypass_sub") : t("camera.sim_sub")))));
    panel.append(liveCaption());
    if (s.vision.sim && !PREFS.simple) panel.append(simVisionControls(s, trayBypass));
    return panel;
  }
  const cam = h("div", { class: "camera" });
  if (!S.cameraImg) S.cameraImg = h("img", { alt: t("camera.preview") });
  cam.append(S.cameraImg, liveOverlay());
  if (pose) cam.append(h("span", { class: "pose" }, icon(pose === "FACE" ? "user-focus" : "squares-four"), pose === "FACE" ? t("camera.face") : t("camera.tray")));
  return h("section", { class: "panel cam-panel" }, cam, liveCaption());
}

/* ---------------------------------------------------------------- live view, moving on by itself, speaking */
const SVG_NS = "http://www.w3.org/2000/svg";
const svg = (tag, attrs) => { const el = document.createElementNS(SVG_NS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); return el; };
function liveOverlay() {
  if (!S.liveSvg) S.liveSvg = svg("svg", { class: "live-overlay", "aria-hidden": "true", preserveAspectRatio: "xMidYMid slice" });
  return S.liveSvg;
}
function liveCaption() {
  if (!S.liveCap) S.liveCap = h("p", { class: "live-cap", role: "status", "aria-live": "polite" });
  return S.liveCap;
}

function liveWords(L) {
  if (!L || !L.kind) return "";
  if (L.busy) return t("live.checking");
  if (L.simulated) return L.ready ? t("live.sim_ready") : t("live.sim_wait");
  if (L.available === false) return t("live.no_camera");
  const names = (S.status.workflow.txn || {}).class_labels || {};
  if (L.kind === "face") return L.faces === 1 ? t("live.face_ok") : L.faces > 1 ? t("live.many_faces") : t("live.no_face");
  if (L.hint === "markers_missing") return L.kind === "hand" ? t("live.no_hand") : t("live.no_tray");
  if (L.hint === "blurry") return t("live.blurry");
  if (L.hint === "no_hand") return t("live.no_hand");
  if (L.hint && L.hint !== "ok") return t("live.unclear");
  const parts = Object.entries(L.expected || {}).filter(([, n]) => n > 0)
    .map(([k, n]) => t("live.count", { seen: (L.counts || {})[k] || 0, want: n, name: (names[k] || k).toLowerCase() }));
  if (L.kind === "tray" && !Object.values(L.expected || {}).some((n) => n > 0)) return L.ready ? t("live.tray_empty") : t("live.tray_not_empty");
  return (L.ready ? "✓ " : "") + parts.join(", ");
}

function drawLive(L) {
  const cap = S.liveCap;
  const holding = L && L.ready && S.autoSince && PREFS.auto;
  if (cap) {
    cap.textContent = [liveWords(L), holding ? t("live.hold") : ""].filter(Boolean).join(" ");
    cap.classList.toggle("ok", !!(L && L.ready));
  }
  const el = S.liveSvg;
  if (!el) return;
  el.replaceChildren();
  if (!L || !L.frame) return;
  const W = 1000, H = Math.round(1000 * L.frame.h / L.frame.w);
  el.setAttribute("viewBox", `0 0 ${W} ${H}`);
  el.classList.toggle("camera-mirrored", !!(S.cameraImg && S.cameraImg.classList.contains("camera-mirrored")));
  const stroke = L.ready ? "#ffffff" : "#ffd27a";
  for (const b of L.boxes || []) el.append(svg("rect", { x: b.x * W, y: b.y * H, width: b.w * W, height: b.h * H, rx: 12, fill: "none", stroke, "stroke-width": 5 }));
  if (L.box) el.append(svg("rect", { x: L.box.x * W, y: L.box.y * H, width: L.box.w * W, height: L.box.h * H, rx: 18, fill: "none", stroke, "stroke-width": 5, "stroke-dasharray": "18 12" }));
  if (L.quad) el.append(svg("polygon", { points: L.quad.map(([x, y]) => `${x * W},${y * H}`).join(" "), fill: "none", stroke, "stroke-width": 5 }));
  for (const p of L.pills || []) {
    el.append(svg("circle", { cx: p.x * W, cy: p.y * H, r: p.r * W + 10, fill: "none", stroke: "#ffffff", "stroke-width": 9 }),
      svg("circle", { cx: p.x * W, cy: p.y * H, r: p.r * W + 10, fill: "none", stroke: p.color || stroke, "stroke-width": 5 }));
  }
}

// Steps that may move on by themselves once the camera has seen the right thing for a moment.
// Unlocking the tray always needs a press.
const AUTO_STEPS = ["PERSON_CHECK", "EMPTY_TRAY_CHECK", "DOSE_CHECK", "HAND_CHECK", "COLLECTION_CHECK"];
const AUTO_HOLD_MS = 1200;
function autoAdvance(s, L) {
  const tx = s.workflow.txn;
  const key = tx ? stepKey(tx) : null;
  const sig = tx ? `${tx.txn_id}:${key}` : null;
  if (!PREFS.auto || !tx || !AUTO_STEPS.includes(key) || S.busy || s.workflow.busy || s.workflow.pending_operation ||
      (tx.blocks || []).some((b) => DEVICE_CODES.includes(b.code)) || !L || !L.kind) { S.autoSince = null; return; }
  if (!L.ready) {
    S.autoSince = null;
    if (S.autoFailed === sig) S.autoRearmed = true; // after a failed try, wait for the view to change first
    return;
  }
  if (S.autoFailed === sig && !S.autoRearmed) return;
  if (!S.autoSince || S.autoSig !== sig) { S.autoSince = Date.now(); S.autoSig = sig; return; }
  if (Date.now() - S.autoSince < AUTO_HOLD_MS) return;
  S.autoSince = null;
  const run = key === "HAND_CHECK" ? runHand(tx) : runCheck(tx);
  run.then((r) => { if (!r || r.result !== "PASS") { S.autoFailed = sig; S.autoRearmed = false; } else S.autoFailed = null; });
}

async function liveLoop() {
  const s = S.status;
  try {
    if (s && S.view === "verify" && s.workflow.txn && !document.hidden && !S.offline) {
      const L = await fetch("/api/camera/live").then((r) => r.json());
      S.live = L;
      drawLive(L);
      autoAdvance(S.status, L);
    } else if (S.live) {
      S.live = null;
      drawLive(null);
    }
  } catch { /* the next poll tries again */ }
  setTimeout(liveLoop, 300);
}

/* Each dose step is said aloud once when it appears (and again if a problem shows), through the laptop. */
function speakStep(s) {
  const tx = s.workflow.txn;
  if (!PREFS.voice || !tx || S.view !== "verify") return;
  const key = stepKey(tx);
  const blocks = (tx.blocks || []).map((b) => b.code).join(",");
  const sig = `${tx.txn_id}:${key}:${blocks}`;
  if (S.spokenSig === sig) return;
  S.spokenSig = sig;
  if (["DISPENSING", "RELEASE_PENDING"].includes(key)) return;
  const text = [t(`stage.${key}.instr`), ...(tx.blocks || []).map(blockText)].join(" ");
  if (s.laptop_voice && s.laptop_voice.enabled) speak(text, true);
}

function simVisionControls(s, trayOnly = false) {
  // Testing only: hidden in Simple view and folded away.
  const face = h("select", { class: "plain-select", "aria-label": "Simulated face", onchange: (e) => api("/api/sim/vision", { face: e.target.value }) },
    [["recipient", "Enrolled person"], ["unknown", "Unknown person"], ["two_faces", "Two faces"], ["none", "Nobody in view"]]
      .map(([v, l]) => h("option", { value: v, selected: s.vision.sim.face === v }, l)));
  const tray = h("select", { class: "plain-select", "aria-label": "Simulated tray view", onchange: (e) => api("/api/sim/vision", { tray: e.target.value }) },
    [["auto", "Tray as simulated"], ["touching", "Objects touching"], ["markers_missing", "Markers hidden"], ["blurry", "Blurry"]]
      .map(([v, l]) => h("option", { value: v, selected: s.vision.sim.tray === v }, l)));
  const action = h("select", { class: "plain-select", "aria-label": "Simulated taking", onchange: (e) => api("/api/sim/vision", { action: e.target.value }) },
    [["taken", "Hand to mouth after release"], ["not_seen", "No hand-to-mouth movement"]]
      .map(([v, l]) => h("option", { value: v, selected: s.vision.sim.action === v }, l)));
  const handSel = h("select", { class: "plain-select", "aria-label": "Simulated hand", onchange: (e) => api("/api/sim/vision", { hand: e.target.value }) },
    [["shown", "Pills shown in hand"], ["wrong", "Wrong count in hand"], ["none", "No hand shown"]]
      .map(([v, l]) => h("option", { value: v, selected: s.vision.sim.hand === v }, l)));
  return h("details", { class: "more" }, h("summary", {}, icon("caret-right"), "Camera simulator (testing)"),
    h("div", { class: "more-body row" }, trayOnly ? null : face, tray, trayOnly ? null : action, trayOnly ? null : handSel));
}

function checkedPanel(tx) {
  const obs = tx.observations.CLASS_COUNTS || (tx.state === "EMPTY_TRAY_CHECK" || tx.state === "COLLECTION_CHECK" ? tx.observations.TRAY_EMPTY : null);
  const observed = obs && obs.observed;
  const tbody = h("tbody");
  let mismatch = false;
  for (const [cls, n] of Object.entries(tx.expected.by_class)) {
    const seen = observed ? observed[cls] : null;
    const match = seen == null ? null : seen === n;
    if (match === false) mismatch = true;
    tbody.append(h("tr", {},
      h("td", {}, h("span", { class: "row" }, swatch(tx.class_colors[cls]), tx.class_labels[cls])),
      h("td", { class: "num" }, n),
      h("td", { class: "num" }, seen == null ? "-" : seen),
      h("td", {}, match == null ? h("span", { class: "verdict none" }, t("verify.notchecked"))
        : match ? h("span", { class: "verdict ok" }, icon("check-circle"), t("verify.match")) : h("span", { class: "verdict bad" }, icon("x-circle"), t("verify.mismatch")))));
  }
  const table = h("table", { class: "counts" },
    h("caption", { class: "sr-only" }, t("verify.contents")),
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Type")), h("th", { scope: "col" }, t("verify.expected")),
      h("th", { scope: "col" }, t("verify.seen")), h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Result")))), tbody);
  const open = mismatch || tx.state === "DOSE_CHECK";
  return h("details", { class: "more", open },
    h("summary", {}, icon("caret-right"), t("verify.checked")),
    h("div", { class: "more-body" }, h("p", { class: "muted" }, t("verify.contents")), table, proofPhoto(obs), PREFS.simple ? null : evidenceList(tx)));
}

/* ---------------------------------------------------------------- proof, collection watch, taking */
function proofPhoto(o) {
  if (!o || !o.photo) return null;
  const src = `/api/${o.photo}`;
  return h("figure", { class: "proof" },
    h("a", { href: src, target: "_blank", rel: "noopener" }, h("img", { src, alt: t("proof.title"), loading: "lazy" })),
    h("figcaption", {}, t("proof.caption"), o.simulated ? h("span", { class: "sim-note" }, t("proof.sim")) : null));
}

function watchKey(w) {
  return w ? [w.txn_id, w.active, w.available, w.palms_available, w.collector_so_far, w.action_observed,
    (w.doses || []).map((d) => [d.dose_id, d.self_report, d.action])] : null;
}

function takingRow(w) {
  if (w.action_observed) return h("span", { class: "verdict ok" }, icon("check-circle"), t("watch.taking.yes"));
  if (!w.palms_available) return h("span", { class: "verdict none" }, icon("minus-circle"), t("watch.taking.off"));
  return h("span", { class: "verdict none" }, icon("hourglass-medium"), t("watch.taking.no"));
}

function watchBox(w) {
  const box = h("div", { class: "watch-box", role: "status", "aria-live": "polite" }, h("h3", {}, t("watch.title")));
  if (!w || !w.available) {
    box.append(h("p", { class: "muted" }, t("watch.off")));
    return box;
  }
  const who = w.collector_so_far;
  const whoView = who === "RECIPIENT" ? ["ok", "check-circle"] : who === "OTHER_PERSON" ? ["bad", "warning-circle"] : ["none", "hourglass-medium"];
  box.append(h("dl", { class: "evidence" },
    h("dt", {}, t("watch.who")), h("dd", {}, h("span", { class: `verdict ${whoView[0]}` }, icon(whoView[1]), t(`watch.who.${who}`))),
    h("dt", {}, t("watch.taking")), h("dd", {}, takingRow(w))),
    h("p", { class: "muted small" }, t("watch.never_swallow"), w.simulated ? [" ", h("span", { class: "sim-note" }, "simulated")] : null));
  return box;
}

/* After the tray is emptied the camera keeps watching for a few minutes, so the person can take the
   medicine in front of it. Shown on Today and on the dose page until they say "I took it". */
function takingPanel(s) {
  const w = s.watch;
  if (!w || !w.active || s.workflow.txn || !w.available) return null;
  const doses = w.doses || [];
  const pending = doses.filter((d) => !d.self_report);
  if (!pending.length) return null;
  const say = w.action_observed ? t("taking.seen") : t("taking.body");
  return h("section", { class: "due-panel", "aria-label": t("taking.title") },
    h("h2", { class: "due-name" }, t("taking.title")),
    h("p", { class: "due-sub" }, say),
    h("ul", { class: "due-items" }, doses.map((d) => h("li", {}, swatch(d.class_color), d.label, h("span", { class: "qty" }, `×\u00a0${d.quantity}`)))),
    h("dl", { class: "evidence" }, h("dt", {}, t("watch.taking")), h("dd", {}, takingRow(w))),
    h("p", { class: "muted small", style: "margin:.75rem 0 1.25rem" }, t("watch.never_swallow")),
    h("div", { class: "due-actions" },
      h("button", { class: "btn btn-primary btn-big ringed", disabled: !!S.busy,
        onclick: () => act("confirm", () => Promise.all(pending.map((d) => api("/api/doses/confirm", { dose_id: d.dose_id })))).then((x) => x && toast(t("toast.confirm"))) },
        icon("check"), t("due.mark")),
      readButton(() => `${t("taking.title")}. ${say}`)));
}

/* "Did you take it?": released doses with no "I took it" and no hand-to-mouth movement seen. */
function followPanels(s) {
  return (s.followups || []).map((c) => {
    const collect = c.followup === "collect";
    const title = t(collect ? "follow.collect_title" : "follow.taken_title", { name: c.label });
    const body = t(collect ? "follow.collect_body" : "follow.taken_body");
    const later = h("button", { class: "btn btn-quiet", disabled: !!S.busy,
      onclick: () => act("later", () => api("/api/doses/followup-later", { dose_id: c.dose_id })).then((x) => x && toast(t("follow.toast_later", { n: s.config.followup_minutes || 5 }))) },
      icon("clock"), t("follow.later"));
    const primary = collect && c.workflow_state === "RELEASED"
      ? h("button", { class: "btn btn-primary btn-big ringed", onclick: () => go("verify") }, t("follow.go"), icon("arrow-right"))
      : h("button", { class: "btn btn-primary btn-big ringed", disabled: !!S.busy, onclick: () => confirmTaken(c) }, icon("check"), t("follow.yes"));
    return h("section", { class: "due-panel", "aria-label": title },
      h("h2", { class: "due-name" }, title),
      h("p", { class: "due-when" }, h("span", { class: "sun-chip", "aria-hidden": "true" }), whenText(c)),
      h("p", { class: "due-sub" }, body),
      h("ul", { class: "due-items" }, h("li", {}, swatch(c.class_color), t("due.count", { n: c.quantity }))),
      h("div", { class: "due-actions" }, primary, later, readButton(() => `${title} ${body}`)));
  });
}

function speakDue(s) {
  if (!PREFS.voice || !(s.laptop_voice && s.laptop_voice.enabled)) return;
  const due = s.today.filter((c) => c.reminder && c.reminder.state === "DUE" && c.workflow_state === "NOT_STARTED");
  const fresh = due.filter((c) => !S.spokenDue.has(c.reminder.reminder_id));
  if (!fresh.length) return;
  fresh.forEach((c) => S.spokenDue.add(c.reminder.reminder_id));
  if (S.firstDueCheck) { S.firstDueCheck = false; return; } // don't announce doses that were already due at page load
  speak(t("voice.due", { name: fresh.map((c) => c.label).join(", ") }), true);
}

function notifyFollowups(s) {
  // Announce each follow-up once (screen readers), and as a desktop notification if those are on.
  const fresh = (s.followups || []).filter((c) => !S.followSeen.has(`${c.dose_id}:${c.followup}`));
  if (!fresh.length) return;
  for (const c of fresh) S.followSeen.add(`${c.dose_id}:${c.followup}`);
  const c = fresh[0];
  const title = t(c.followup === "collect" ? "follow.collect_title" : "follow.taken_title", { name: c.label });
  announce(title);
  if (PREFS.notify && "Notification" in window && Notification.permission === "granted" && document.hidden) {
    const n = new Notification(title, { body: t(c.followup === "collect" ? "follow.collect_body" : "follow.taken_body"), tag: "doseguard-follow" });
    n.onclick = () => { window.focus(); go("today"); n.close(); };
  }
}

function stockNotes(s) {
  const low = (s.cartridges || []).filter((c) => c.used && (c.low || c.empty));
  if (!low.length) return null;
  return h("div", { class: "blocker" }, icon("package"), h("div", {}, low.map((c) =>
    h("div", {}, t(c.empty ? "stock.empty" : "stock.low", { cart: c.cartridge, name: c.label, n: c.count })))));
}

function evidenceList(tx) {
  const rows = [["FACE_MATCH", "Your face"], ["TRAY_EMPTY", "Empty tray"], ["CLASS_COUNTS", "Dose count"]];
  const words = { PASS: "Passed", FAIL: "Did not pass", UNCERTAIN: "Unclear", NOT_OBSERVED: "Not seen" };
  const list = h("dl", { class: "evidence" });
  for (const [k, label] of rows) {
    const o = tx.observations[k];
    list.append(h("dt", {}, label), h("dd", {}, o ? [
      h("span", { class: `verdict ${o.result === "PASS" ? "ok" : o.result === "FAIL" ? "bad" : "none"}` }, words[o.result] || o.result),
      " ", h("span", { class: "muted small", id: `age-${k}` }, ageText(o)), " ",
      o.simulated ? h("span", { class: "sim-note" }, "simulated") : null,
    ] : h("span", { class: "muted" }, "Not yet")));
  }
  return h("div", {}, list, h("p", { class: "muted small", style: "margin-top:.75rem" },
    "The tray unlocks only with a face match from the last few seconds and a dose check from the last 15 seconds, with the tray unchanged since."));
}

function renderNoTxn(s) {
  const startable = s.today.filter((c) => c.delivery_mode === "DEVICE" && c.workflow_state === "NOT_STARTED" && !c.outcome &&
    ["DUE", "SNOOZED", "ACKNOWLEDGED", "NEEDS_REVIEW"].includes(c.reminder.state));
  const done = s.today.filter((c) => ["RELEASED", "COMPLETE"].includes(c.workflow_state) && c.delivery_mode === "DEVICE");
  const b = startBlockers(s);
  const stack = h("div", { class: "stack" });
  const taking = takingPanel(s);
  if (taking) stack.append(taking);
  followPanels(s).forEach((p) => stack.append(p));
  if (!startable.length && !taking) {
    stack.append(calmPanel(t("verify.none_title"), t("verify.none_body"),
      h("button", { class: "btn", onclick: () => go("today") }, icon("sun-horizon"), t("nav.today"))));
  } else {
    const panel = h("section", { class: "panel later" }, h("h2", {}, t("verify.ready")));
    const ol = h("ol");
    for (const c of startable) {
      ol.append(h("li", {}, h("span", { class: "t" }, whenText(c)),
        h("div", {}, h("div", { class: "name" }, swatch(c.class_color), ...doseLine(c)), h("div", { class: "sub" }, modeText(c))),
        h("div", { class: "row-end" }, h("button", { class: "btn btn-primary", disabled: b.any || !!S.busy, onclick: () => startDose(c) }, t("due.get")))));
    }
    panel.append(ol);
    if (b.any) panel.append(h("div", { class: "blocker" }, icon("info"), h("div", {}, b.everyday.join(" "))));
    stack.append(panel);
  }
  if (done.length) {
    const panel = h("section", { class: "panel later" }, h("h2", {}, t("verify.released_today")), h("p", { class: "muted", style: "margin-bottom:.5rem" }, t("verify.separate")));
    const ol = h("ol");
    for (const c of done) {
      ol.append(h("li", {}, h("span", { class: "t" }, whenText(c)), h("div", {}, h("div", { class: "name" }, ...doseLine(c)), h("div", { class: "sub" }, stateWords(c))),
        !c.self_report ? h("div", { class: "row-end" }, h("button", { class: "btn btn-quiet", onclick: () => confirmTaken(c) }, icon("check"), t("due.mark"))) : null));
    }
    panel.append(ol);
    stack.append(panel);
  }
  const ready = PREFS.simple || s.signed_out ? null : h("section", { class: "panel" }, h("h2", {}, t("status.title")),
    h("dl", { class: "kv", style: "margin-top:.5rem" },
      h("dt", {}, t("status.device")), h("dd", {}, s.device.connected ? (s.device.fault ? `Stopped: ${s.device.fault}` : "Connected") : "Not connected"),
      h("dt", {}, "Armed"), h("dd", {}, s.workflow.armed ? "Yes" : h("a", { href: "#device" }, "No. Arm it in Dispenser")),
      h("dt", {}, "Camera"), h("dd", {}, s.camera.simulated ? "Simulated" : s.camera.face && s.camera.face.error ? s.camera.face.error : "Ready"),
      h("dt", {}, t("status.recipient")), h("dd", {}, (s.plan && s.plan.recipient) || h("a", { href: "#setup" }, "Nobody yet"))));
  setMain(h("div", { class: "steps-head" }, h("h1", {}, t("nav.verify"))), ready ? h("div", { class: "today-grid" }, stack, ready) : stack);
}

/* ---------------------------------------------------------------- Setup door */
const DOOR_TABS = [["schedules", "pill"], ["setup", "pencil-simple-line"], ["history", "clock-counter-clockwise"], ["device", "cpu"]];
function doorHead(active, locked = false) {
  const a = S.status && S.status.account;
  return h("div", { class: "door-head" },
    h("div", { class: "row" }, h("h1", {}, t("care.title")),
      a && a.exists && !locked && !(S.status && S.status.public_demo) ? h("button", { class: "btn btn-quiet", type: "button", onclick: lockSetup }, icon("sign-out"), t("lock.lock")) : null),
    locked ? null : h("nav", { class: "subtabs", "aria-label": t("care.title") }, DOOR_TABS.map(([v, ic]) =>
      h("a", { href: `#${v}`, "aria-current": v === active ? "page" : null }, icon(ic), t(`nav.${v}`)))));
}
function setupLocked() {
  const a = S.status && S.status.account;
  return !!(a && a.exists && !a.unlocked);
}
function doorMain(active, ...kids) {
  renderSky(null);
  if (setupLocked()) { setMain(doorHead(active, true), unlockPanel()); return; }
  setMain(doorHead(active), onboardNote(active), ...kids);
}

/* ---------------------------------------------------------------- account: first run, lock, unlock */
// A local account protects Setup only. Today, taking a dose, reminders, STOP and Ask never need it.
const ONBOARD_KEY = "dg-onboarding";
const onboarding = () => { try { return localStorage.getItem(ONBOARD_KEY) === "1"; } catch { return false; } };
const setOnboarding = (on) => { try { on ? localStorage.setItem(ONBOARD_KEY, "1") : localStorage.removeItem(ONBOARD_KEY); } catch {} };
function needsWelcome(s) { return !!(s && s.account && !s.account.exists); }

function onboardSteps(current) {
  return h("ol", { class: "track onboard-track", "aria-label": t("welcome.title") }, ["welcome.s1", "welcome.s2", "welcome.s3", "welcome.s4"].map((k, i) =>
    h("li", { class: i < current ? "done" : i === current ? "current" : "", "aria-current": i === current ? "step" : null },
      h("span", { class: "dot", "aria-hidden": "true" }), h("span", { class: "name" }, t(k)))));
}
function onboardNote(active) {
  if (!onboarding()) return null;
  const name = (S.status && S.status.account && S.status.account.name) || "";
  const hasFace = !!(S.setup && S.setup.draft && S.setup.draft.person_id);
  const [step, text] = active === "device" ? [3, t("onboard.dispenser")] : hasFace ? [2, t("onboard.medicines")] : [1, t("onboard.face", { name })];
  return h("section", { class: "panel onboard" }, onboardSteps(step), h("p", { class: "lede" }, text));
}

function renderWelcome() {
  renderSky(null);
  const name = h("input", { id: "acct-name", maxlength: 29, autocomplete: "given-name" });
  const last = h("input", { id: "acct-last", maxlength: 30, autocomplete: "family-name" });
  const pw = h("input", { id: "acct-pw", type: "password", maxlength: 128, autocomplete: "new-password" });
  const pw2 = h("input", { id: "acct-pw2", type: "password", maxlength: 128, autocomplete: "new-password" });
  const needsCode = !!(S.status && S.status.account && S.status.account.needs_code);
  const code = needsCode ? h("input", { id: "acct-code", maxlength: 64, autocomplete: "off", autocapitalize: "off", spellcheck: "false" }) : null;
  const err = h("p", { class: "field-error", role: "alert" });
  async function create(ev) {
    ev.preventDefault();
    err.textContent = "";
    const n = name.value.trim(), l = last.value.trim();
    if (!n) return (err.textContent = t("account.name_needed"));
    if (!l) return (err.textContent = t("account.last_needed"));
    if (pw.value.length < 8) return (err.textContent = t("account.short"));
    if (pw.value !== pw2.value) return (err.textContent = t("account.mismatch"));
    try {
      S.busy = "account";
      const made = await api("/api/account/create", { first_name: n, last_name: l, password: pw.value, setup_code: code ? code.value : "" }, { noRetry: true });
      setOnboarding(true);
      S.setup = null;
      S.prefillName = n;
      S.busy = null;
      if (S.status) S.status.account = { exists: true, name: made.name, first_name: made.first_name, unlocked: true };
      S.keys = {};
      go("setup");
    } catch (e) {
      S.busy = null;
      err.textContent = friendlyError(e);
    }
  }
  setMain(h("section", { class: "welcome" },
    h("div", { class: "welcome-head" },
      h("img", { class: "welcome-mark", src: "/static/brand/healthbox-mark.png", alt: "" }),
      h("div", {}, h("h1", {}, t("welcome.title")), h("p", { class: "lede" }, t("welcome.sub")))),
    onboardSteps(0),
    h("form", { class: "panel welcome-form", onsubmit: create },
      h("div", { class: "two" },
        h("label", { class: "field", for: "acct-name" }, t("account.first"), name),
        h("label", { class: "field", for: "acct-last" }, t("account.last"), last)),
      h("label", { class: "field", for: "acct-pw" }, t("account.password"), pw),
      h("label", { class: "field", for: "acct-pw2" }, t("account.password2"), pw2),
      h("p", { class: "muted" }, t("account.password_help")),
      code ? h("label", { class: "field", for: "acct-code" }, t("account.setup_code"), code) : null,
      code ? h("p", { class: "muted" }, t("account.setup_code_help")) : null,
      err,
      h("div", { class: "row" }, h("button", { class: "btn btn-primary btn-big", type: "submit" }, t("account.create"), icon("arrow-right"))))));
  name.focus();
}

function unlockPanel(autofocus = true) {
  const a = S.status.account;
  const pw = h("input", { id: "unlock-pw", type: "password", maxlength: 128, autocomplete: "current-password" });
  const err = h("p", { class: "field-error", role: "alert" });
  async function unlock(ev) {
    ev.preventDefault();
    err.textContent = "";
    try {
      const r = await api("/api/account/unlock", { password: pw.value }, { noRetry: true });
      S.status.account = { ...S.status.account, ...r, unlocked: true };
      S.lastUnlocked = true;
      S.signin = null;
      toast(t("lock.unlocked"));
      route();
    } catch (e) {
      const d = (e && e.data) || {};
      const code = (d.reason_codes || [])[0];
      err.textContent = code === "WRONG_PASSWORD" && d.attempts_left != null ? t("lock.wrong", { n: d.attempts_left })
        : code === "TOO_MANY_ATTEMPTS" && d.locked_out_until ? t("lock.wait", { time: fmtClock(d.locked_out_until) })
        : friendlyError(e);
      pw.value = "";
      pw.focus();
    }
  }
  if (autofocus) setTimeout(() => pw.isConnected && pw.focus(), 0);
  const first = a.first_name || a.name || "";
  return h("section", { class: "panel unlock" },
    h("div", { class: "status-big" }, h("span", { class: "orb", "aria-hidden": "true" }, icon("user-circle")),
      h("div", {}, h("h2", {}, first ? t("lock.title", { name: first }) : t("lock.title_anon")), h("p", {}, t("lock.sub")))),
    h("form", { class: "unlock-form", onsubmit: unlock },
      h("p", { class: "strong" }, a.name || ""),
      h("label", { class: "field", for: "unlock-pw" }, t("account.password"), pw),
      err,
      h("div", { class: "row" }, h("button", { class: "btn btn-primary", type: "submit" }, icon("sign-in"), t("lock.unlock")))),
    h("p", { class: "muted small", style: "margin-top:1rem" }, t("lock.forgot")),
    !first ? null : h("details", { class: "more" }, h("summary", {}, icon("caret-right"), t("lock.not_me", { name: first })),
      h("div", { class: "more-body" }, h("p", { class: "muted" }, t("lock.not_me_body", { name: first })))));
}

async function lockSetup() {
  try {
    await api("/api/account/lock", {}, { noRetry: true });
    if (S.status && S.status.account) S.status.account.unlocked = false;
    S.lastUnlocked = false;
    S.signin = null;
    route();
  } catch (e) {
    toast(friendlyError(e), true);
  }
}

/* ---------------------------------------------------------------- My medicines */
function fmtTime(hhmm) {
  const [H, M] = hhmm.split(":").map(Number);
  return `${((H + 11) % 12) + 1}:${String(M).padStart(2, "0")} ${H < 12 ? "AM" : "PM"}`;
}
function colorFor(classId) {
  const c = ((S.status && S.status.token_classes) || []).find((x) => x.class_id === classId);
  return c ? c.color : null;
}
function needRule(e) {
  const hrs = e.min_interval_hours;
  return `When needed: up to ${e.max_per_day} a day, at least ${hrs} hour${hrs === 1 ? "" : "s"} apart`;
}

const schedules = {
  key: (s) => [s.plan, s.timezone],
  render(s) {
    const head = h("div", { class: "page-head" }, h("div", {}, h("h2", {}, t("nav.schedules")),
      h("p", { class: "lede" }, s.plan ? `Version ${s.plan.version}, confirmed ${new Date(s.plan.confirmed_at).toLocaleDateString()}. Times are in ${s.timezone}.` : "Nothing is confirmed yet.")),
      h("button", { class: "btn btn-primary", onclick: () => { S.setup = null; go("setup"); } }, icon(s.plan ? "pencil-simple-line" : "plus"), s.plan ? "Change my medicines" : "Add my medicines"));
    if (!s.plan || !s.plan.entries.length) {
      doorMain("schedules", head, h("section", { class: "panel" }, h("p", { class: "empty" },
        s.demo_mode ? "The demo plan has fictional entries with no set times. Use Trigger in Dispenser, Demo." : "Nothing is scheduled until you add medicines and confirm them.")));
      return;
    }
    const rows = s.plan.entries.map((e) => {
      const asNeeded = e.schedule === "AS_NEEDED";
      return h("li", {},
        h("span", { class: "t" }, asNeeded ? "When needed" : e.times.length ? e.times.map(fmtTime).join(", ") : "No set time"),
        swatch(e.delivery_mode === "DEVICE" ? colorFor(e.class_id) : null) || h("span", { class: "swatch-gap", "aria-hidden": "true" }),
        h("div", {}, h("div", { class: "name" }, e.label, h("span", { class: "muted" }, ` ×\u00a0${e.quantity}`), e.strength_text ? h("span", { class: "muted" }, `, ${e.strength_text}`) : null),
          h("div", { class: "sub" }, [asNeeded ? needRule(e) : null, e.delivery_mode === "DEVICE" ? `From the dispenser, cartridge ${e.cartridge}` : "Reminder only, kept outside the dispenser"].filter(Boolean).join(". "))),
        h("div", { class: "row" }, e.label_id ? h("a", { href: `/api/labels/${e.label_id}/image`, target: "_blank", title: "Label photo" },
          h("img", { class: "label-mini", src: `/api/labels/${e.label_id}/image`, alt: `Label photo for ${e.label}` })) : null));
    });
    doorMain("schedules", head, h("section", { class: "panel" }, h("ul", { class: "med-list" }, rows)),
      h("p", { class: "muted", style: "margin-top:1rem" }, "Changing your medicines creates a new version. Doses that already started, and reminders already shown, stay as they were."));
  },
};

/* ---------------------------------------------------------------- Change plan (wizard) */
const setup = {
  key: null,
  async render() {
    doorMain("setup", h("div", { class: "waiting" }, h("span", { class: "breath", "aria-hidden": "true" }), "Loading"));
    const data = await fetch("/api/setup").then((r) => r.json());
    if (!S.setup || S.setup.plan_version_id !== (data.plan && data.plan.plan_version_id) || S.setup.reload) {
      const plan = data.plan;
      S.setup = {
        plan_version_id: plan ? plan.plan_version_id : null,
        draft: {
          person_id: plan ? plan.person_id : (data.persons.at(-1) || {}).person_id || null,
          timezone: plan ? plan.timezone : (LAPTOP_TZ && zoneOffset(LAPTOP_TZ) ? LAPTOP_TZ : data.default_timezone),
          timezone_confirmed: false, dst_reviewed: false,
          entries: plan && plan.entries.length && plan.plan_id !== "demo"
            ? plan.entries.map((e) => ({ schedule: "SCHEDULED", min_interval_hours: 4, max_per_day: 3, ...e, times: e.times || [], confirmed: false }))
            : [blankEntry(data)],
        },
        errors: [], dst: null, step: 0,
      };
    }
    S.setup.data = data;
    drawSetup();
  },
};

/* Someone else will use this Healthbox: archive this person, keep their history, start the welcome again. */
function handoverBlock(name) {
  const emptied = h("input", { type: "checkbox" });
  const typed = h("input", { id: "ho-name", maxlength: 40, autocomplete: "off" });
  const pw = h("input", { id: "ho-pw", type: "password", maxlength: 128, autocomplete: "current-password" });
  const go = async () => {
    try {
      S.busy = "handover";
      await api("/api/account/handover", { password: pw.value, confirm_name: typed.value, emptied: emptied.checked }, { noRetry: true });
      pw.value = "";
      setOnboarding(true);
      S.setup = null;
      toast(`Done. ${name}'s history is kept. Now set up Healthbox for the new person.`);
      route();
    } catch (e) {
      pw.value = "";
      toast(friendlyError(e), true);
    } finally {
      S.busy = null;
    }
  };
  return h("details", { class: "more", style: "margin-top:1.5rem" },
    h("summary", {}, icon("caret-right"), "Someone else will use this Healthbox"),
    h("div", { class: "more-body stack", style: "gap:1rem" },
      h("p", { class: "muted" }, `This removes ${name}'s face, medicines, reminders and account from this laptop. ${name}'s history and tray photos stay readable in History. Then the welcome setup starts for the new person, who makes their own account.`),
      h("label", { class: "check" }, emptied, h("span", {}, `I emptied both cartridges and the tray. ${name}'s medicine must not go to the next person.`)),
      h("label", { class: "field", for: "ho-name" }, `Type ${name} to confirm`, typed),
      h("label", { class: "field", for: "ho-pw" }, `${name}'s password`, pw),
      h("div", { class: "row" }, h("button", { class: "btn btn-danger", type: "button", onclick: go }, icon("user-switch"), "Give Healthbox to someone else"))));
}

function blankEntry(data) {
  return { entry_key: "", label: "", strength_text: "", quantity: 1, delivery_mode: "DEVICE", schedule: "SCHEDULED",
    min_interval_hours: 4, max_per_day: 3,
    class_id: (data.token_classes[0] || {}).class_id || null, times: ["08:00"], confirmed: false, suggested: {}, label_id: null,
    label_photos: 0, container: "bottle" };
}

function drawSetup() {
  const st = S.setup, data = st.data, d = st.draft;
  const s = S.status;
  const errFor = (i, f) => st.errors.filter((e) => e.entry === i && e.field === f).map((e) => h("div", { class: "field-error" }, e.message));
  const globalErr = st.errors.filter((e) => e.entry == null && e.field !== "person_id" && e.field !== "timezone").map((e) => h("div", { class: "field-error" }, e.message));

  // 1. Whose medicine: one person per Healthbox. The account's name is the only name; enrolling again
  //    retakes the same person's face. Someone new takes over with "Give Healthbox to someone else".
  const acctName = (S.status && S.status.account && S.status.account.name) || "";
  const me = data.persons.at(-1) || null;
  if (me) d.person_id = me.person_id;
  const nameIn = acctName || me ? null : h("input", { id: "enroll-name", placeholder: "e.g. Jackson", maxlength: 40, value: S.prefillName || "" });
  const consent = h("input", { type: "checkbox", id: "consent" });
  const who = me
    ? h("div", { class: "choice on" }, icon("user-circle"),
        h("span", {}, h("strong", {}, me.label), me.simulated ? h("span", { class: "sim-note", style: "margin-left:.5rem" }, "simulated") : null,
          h("small", {}, `Face added ${new Date(me.consent_at).toLocaleDateString()}`)))
    : acctName ? h("p", { class: "strong" }, `For ${acctName}`) : null;
  const enrollBtn = h("button", { class: me ? "btn" : "btn btn-primary", disabled: !!S.busy, onclick: enroll }, icon(me ? "arrow-counter-clockwise" : "user-focus"),
    me ? "Retake my face" : data.camera_simulated ? "Add face (simulated camera)" : "Take reference pictures");
  let livePreview = null;
  if (!data.camera_simulated) {
    if (!S.cameraImg) S.cameraImg = h("img", { alt: "Live camera preview" });
    S.faceHint = S.faceHint || h("p", { class: "strong", style: "margin-top:.5rem", "aria-live": "polite" }, "Looking for a face.");
    livePreview = h("div", { style: "max-width:360px" }, h("div", { class: "camera" }, S.cameraImg), S.faceHint);
  }
  const step1 = h("section", { class: "panel" }, h("h3", {}, me ? "Your face" : "Whose medicine is this?"),
    h("p", { class: "muted", style: "margin:.25rem 0 1rem" }, "Healthbox is for one person. The camera checks this face before the tray opens. Pictures stay on this laptop. A face match is a prototype check, not secure identity."),
    who,
    h("div", { class: "two", style: "margin-top:1.25rem" },
      h("div", { class: "stack", style: "gap:1rem" },
        nameIn ? h("label", { class: "field", for: "enroll-name" }, "Your name for the face check", nameIn) : null,
        h("label", { class: "check" }, consent, h("span", {}, me ? "I agree to new reference pictures, kept only on this laptop. They replace the old ones."
          : "I agree to have reference pictures taken and kept only on this laptop.")),
        h("div", { class: "row" }, enrollBtn),
        data.camera_simulated ? null : h("p", { class: "muted small" }, "While capturing, face the camera alone and turn slightly left and right.")),
      livePreview),
    acctName && me && !(S.status && S.status.public_demo) ? handoverBlock(acctName) : null,
    errFor(undefined, "person_id"),
    st.errors.filter((e) => e.entry == null && e.field === "person_id").map((e) => h("div", { class: "field-error" }, e.message)));
  async function enroll() {
    const label = acctName || (me && me.label) || (nameIn && nameIn.value.trim()) || "";
    if (!label) return toast("Enter a name first.", true);
    if (!consent.checked) return toast("Tick the agreement box first.", true);
    if (!data.camera_simulated) toast("Capturing. Look at the camera and turn your head slightly left and right.");
    const r = await act("enroll", () => api("/api/enroll", { label, consent: true }));
    if (r) { d.person_id = r.person_id; toast(r.retaken ? "Face updated." : "Face added."); if (onboarding()) st.step = 1; setup.render(); }
  }

  // 2. Medicines
  const entriesBox = h("div", {});
  d.entries.forEach((e, i) => entriesBox.append(entryEditor(e, i, data, errFor)));
  const aiReady = data.ai && data.ai.ready;
  const step2 = h("section", { class: "panel" }, h("h3", {}, "Your medicines"),
    h("p", { class: "muted", style: "margin:.25rem 0 1rem" },
      data.require_label_photo ? "Each medicine needs a photo of its label. " : "",
      aiReady ? "Gemini reads the photo and suggests the fields; you check every one." : "Type the details from the label photo.",
      " Use fictional demo labels."),
    h("div", { class: "row", style: "margin-bottom:1rem" }, data.token_classes.map((c) => h("span", { class: "tag" }, swatch(c.color), `${c.label} pills in cartridge ${c.cartridge}`))),
    entriesBox,
    h("div", { class: "row", style: "margin-top:1rem" },
      h("button", { class: "btn", onclick: () => { d.entries.push(blankEntry(data)); S.capturingFor = d.entries.length - 1; drawSetup(); } }, icon("plus"), "Add another medicine")),
    h("p", { class: "muted small", style: "margin-top:1rem" }, "Demo tokens only, never real pills. Colour and shape do not prove what a medicine is."));

  // 3. Confirm
  const zones = Array.from(new Set([LAPTOP_TZ, d.timezone, "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "UTC"].filter((z) => z && zoneOffset(z))));
  const tzWarn = tzDiffers(d.timezone) ? h("div", { class: "alert-box", style: "margin-top:1rem" }, icon("warning-circle"),
    h("div", {}, h("div", { class: "what" }, `This laptop is on ${zoneName(LAPTOP_TZ)}, but the plan uses ${zoneName(d.timezone)}.`),
      h("div", { class: "next" }, `Reminders will follow ${zoneName(d.timezone)}. Pick ${zoneName(LAPTOP_TZ)} unless that is really what you want.`))) : null;
  const tzSel = h("select", { class: "tz-select", onchange: (e) => { d.timezone = e.target.value; d.timezone_confirmed = false; drawSetup(); } },
    zones.map((z) => h("option", { value: z, selected: z === d.timezone }, zoneOption(z))));
  const dstBox = st.dst ? h("div", { class: "note", style: "margin-top:1rem" },
    h("strong", {}, "Clock change check"),
    h("ul", {}, st.dst.slice(0, 6).map((x) => h("li", {}, `${x.date} at ${fmtTime(x.time)}: this time ${x.issue === "NONEXISTENT" ? "does not exist (the clock skips it)" : "happens twice"}.`))),
    h("label", { class: "check", style: "margin-top:.5rem" }, h("input", { type: "checkbox", checked: d.dst_reviewed, onchange: (e) => (d.dst_reviewed = e.target.checked) }),
      h("span", {}, "I checked these. Those doses will be marked for review instead of guessing."))) : null;
  const step3 = h("section", { class: "panel" }, h("h3", {}, "Check and confirm"),
    h("ul", { class: "med-list", style: "margin:.5rem 0 1rem" }, d.entries.map((e) => h("li", {},
      h("span", { class: "t" }, e.schedule === "AS_NEEDED" ? "When needed" : (e.times || []).map(fmtTime).join(", ")),
      swatch(e.delivery_mode === "DEVICE" ? colorFor(e.class_id) : null) || h("span", { class: "swatch-gap", "aria-hidden": "true" }),
      h("div", {}, h("div", { class: "name" }, e.label || "Unnamed medicine", h("span", { class: "muted" }, ` ×\u00a0${e.quantity}`)),
        h("div", { class: "sub" }, e.schedule === "AS_NEEDED" ? needRule(e) : e.delivery_mode === "DEVICE" ? "From the dispenser" : "Reminder only")),
      e.confirmed ? h("span", { class: "tag" }, icon("check"), "Checked") : h("span", { class: "tag" }, h("span", { class: "mark wait" }), "Not checked yet")))),
    h("div", { class: "row" }, h("label", { class: "field" }, "Time zone", tzSel)),
    h("label", { class: "check", style: "margin-top:1rem" }, h("input", { type: "checkbox", checked: d.timezone_confirmed, onchange: (e) => (d.timezone_confirmed = e.target.checked) }),
      h("span", {}, `All times above are ${zoneName(d.timezone)}.`)),
    tzWarn,
    errFor(undefined, "timezone"), st.errors.filter((e) => e.entry == null && e.field === "timezone").map((e) => h("div", { class: "field-error" }, e.message)),
    dstBox, globalErr,
    h("div", { class: "row", style: "margin-top:1.5rem" },
      h("button", { class: "btn btn-primary btn-big", disabled: !!S.busy, onclick: confirmPlan }, icon("check"), st.plan_version_id ? "Confirm changes" : "Confirm my medicines"),
      h("span", { class: "muted" }, "Nothing is scheduled until you confirm.")));
  async function confirmPlan() {
    st.errors = [];
    const body = {
      base_plan_version_id: st.plan_version_id, person_id: d.person_id, timezone: d.timezone,
      timezone_confirmed: d.timezone_confirmed, dst_reviewed: d.dst_reviewed,
      entries: d.entries.map((e) => {
        const asNeeded = e.schedule === "AS_NEEDED";
        return { entry_key: e.entry_key, label: e.label, strength_text: e.strength_text, quantity: Number(e.quantity),
          delivery_mode: e.delivery_mode, class_id: e.delivery_mode === "DEVICE" ? e.class_id : null,
          schedule: e.schedule || "SCHEDULED", times: asNeeded ? [] : e.times,
          min_interval_hours: asNeeded ? Number(e.min_interval_hours) : undefined, max_per_day: asNeeded ? Number(e.max_per_day) : undefined,
          confirmed: e.confirmed, label_id: e.label_id || null };
      }),
    };
    try {
      S.busy = "plan";
      const r = await api("/api/plans/confirm", body);
      toast(`Confirmed. Version ${r.version}.`);
      S.setup = null;
      S.busy = null;
      go(onboarding() ? "device" : "schedules");
    } catch (e) {
      S.busy = null;
      st.errors = (e.data && e.data.errors) || [{ message: e.message }];
      if (st.errors.some((x) => x.entry != null)) st.step = 1;
      else if (st.errors.some((x) => x.field === "person_id")) st.step = 0;
      if (e.data && e.data.dst_issues) st.dst = e.data.dst_issues;
      if (e.data && e.data.reason_codes && e.data.reason_codes[0] === "PLAN_VERSION_MISMATCH") st.reload = true;
      toast(e.message, true);
      drawSetup();
    }
  }

  const groups = [step1, step2, step3];
  st.step = Math.min(st.step || 0, groups.length - 1);
  const names = [t("setup.step1"), t("setup.step2"), t("setup.step3")];
  const wizard = h("ol", { class: "wizard" }, names.map((n, i) => h("li", { class: i < st.step ? "done" : null },
    h("button", { type: "button", "aria-current": i === st.step ? "step" : null, onclick: () => { st.step = i; drawSetup(); } },
      h("span", { class: "n" }, i < st.step ? [icon("check"), h("span", { class: "sr-only" }, "Done: ")] : i + 1), n))));
  const foot = h("div", { class: "wizard-foot" },
    st.step > 0 ? h("button", { class: "btn", onclick: () => { st.step -= 1; drawSetup(); } }, icon("arrow-left"), t("setup.back")) : h("span"),
    st.step < groups.length - 1 ? h("button", { class: "btn btn-primary", onclick: () => { st.step += 1; drawSetup(); } }, t("setup.next"), icon("arrow-right")) : null);
  doorMain("setup",
    h("div", { class: "section-head" }, h("h2", {}, t("nav.setup")),
      h("p", {}, st.plan_version_id ? "Changes make a new version. Doses already shown keep their old instructions." : "Three short steps. Nothing is scheduled until you confirm.")),
    s && s.demo_mode ? h("p", { class: "note", style: "margin-bottom:1rem" }, "Demo mode is on. What you confirm here is a demo plan, kept apart from real records.") : null,
    wizard, groups[st.step], foot);
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
  const sug = r.suggested_schedule;
  if (sug) {
    e.schedule = sug.schedule; e.suggested.schedule = true;
    if (sug.schedule === "AS_NEEDED") {
      if (sug.min_interval_hours) e.min_interval_hours = sug.min_interval_hours;
      if (sug.max_per_day) e.max_per_day = sug.max_per_day;
      e.suggested.limits = true;
    } else if (sug.times && sug.times.length) {
      e.times = [...sug.times]; e.suggested.times = true;
    }
  }
  e.notes = [x && x.frequency_text
      ? sug ? `The label says: “${x.frequency_text}”. The dashed ${sug.schedule === "AS_NEEDED" ? "limits are" : "times are"} a suggestion from that; change them if your doctor said otherwise.`
        : `The label says: “${x.frequency_text}”. Choose the times yourself.`
      : null,
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
    toast(r.fields ? `Read ${r.photos} photo${r.photos > 1 ? "s" : ""}. Check the suggested fields.` : "Label photos saved. Type the details from them.");
  }
  drawSetup();
}

function photoAdded(e, r) {
  e.label_id = r.label_id;
  e.label_photos = r.photos;
  e.confirmed = false;
}

async function captureLabel(e, btn) {
  for (let n = 3; n > 0; n--) { btn.textContent = `Taking photo in ${n}`; await sleep(1000); }
  btn.textContent = "Taking photo";
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
  return h("div", { class: "thumbs" }, Array.from({ length: n }, (_, k) =>
    h("a", { href: `/api/labels/${e.label_id}/image?i=${k}`, target: "_blank", title: `Photo ${k + 1}` },
      h("img", { src: `/api/labels/${e.label_id}/image?i=${k}`, alt: `Label photo ${k + 1}` }))));
}

function containerPicker(e) {
  const opt = (val, title, sub) => h("label", { class: `choice ${(e.container || "bottle") === val ? "on" : ""}` },
    h("input", { type: "radio", name: `container-${e._uid}`, checked: (e.container || "bottle") === val,
      onchange: () => { e.container = val; drawSetup(); } }),
    h("span", {}, h("strong", {}, title), h("small", {}, sub)));
  return h("div", { class: "choice-row", style: "margin:.5rem 0 .75rem" },
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
      h("div", { class: "strong" }, `${have} label photo${have > 1 ? "s" : ""} saved`),
      h("p", { class: "muted small", style: "margin:.15rem 0 .5rem" }, "Check the fields above against these photos."),
      thumbs(e),
      h("div", { class: "row", style: "margin-top:.5rem" },
        have < 4 && !data.camera_simulated ? h("button", { class: "btn btn-quiet", onclick: () => { S.capturingFor = i; drawSetup(); } }, "Add another angle") : null,
        h("button", { class: "btn btn-quiet", onclick: () => { e.label_id = null; e.label_photos = 0; e.confirmed = false; S.capturingFor = data.camera_simulated ? null : i; drawSetup(); } }, "Retake all"))));
    return box;
  }

  if (data.camera_simulated) {
    box.append(h("div", {}, h("div", { class: "strong" }, "Label photo needed"),
      containerPicker(e),
      h("p", { class: "muted small", style: "margin-bottom:.5rem" }, e.container === "flat"
        ? "The camera is simulated, so upload a photo of the label."
        : "The camera is simulated. Upload up to 3 photos of the bottle, turned a quarter turn between each."),
      upload, h("label", { for: `label-up-${e._uid}`, class: "btn", role: "button", tabindex: 0 }, icon("upload-simple"), "Upload label photos")));
    return box;
  }

  if (S.capturingFor === i) {
    if (!S.labelImg) S.labelImg = h("img", { alt: "Live camera preview for the label" });
    const step = Math.min(have, BOTTLE_STEPS.length - 1);
    const instruction = e.container === "flat" ? "Hold the label flat, facing the camera." : BOTTLE_STEPS[step];
    const capBtn = h("button", { class: "btn btn-primary", disabled: !!S.busy, onclick: (ev) => captureLabel(e, ev.currentTarget) },
      e.container === "flat" ? "Take label photo" : `Take photo ${have + 1} of ${Math.max(target, have + 1)}`);
    box.append(h("div", { class: "camera label-cam" }, S.labelImg, h("span", { class: "pose" }, e.container === "flat" ? "Label" : `Photo ${have + 1}`)),
      h("div", { style: "flex:1;min-width:240px" },
        h("div", { class: "strong" }, e.container === "flat" ? "Show the label" : "Show the bottle's label"),
        have === 0 ? containerPicker(e) : null,
        h("p", { class: "strong", style: "margin:.4rem 0 .2rem" }, instruction),
        h("p", { class: "muted small", style: "margin-bottom:.6rem" }, "About 20 cm from the camera, label text facing it. Tilt away from lights to avoid glare."),
        have ? thumbs(e) : null,
        h("div", { class: "row", style: "margin-top:.6rem" }, capBtn,
          have ? h("button", { class: "btn", disabled: !!S.busy, onclick: () => readLabel(e) }, `Done, use ${have} photo${have > 1 ? "s" : ""}`) : null,
          upload, h("label", { for: `label-up-${e._uid}`, class: "btn btn-quiet", role: "button", tabindex: 0 }, "Upload instead"),
          h("button", { class: "btn btn-quiet", onclick: () => { S.capturingFor = null; drawSetup(); } }, "Cancel"))));
    return box;
  }

  box.append(h("div", {}, h("div", { class: "strong" }, data.require_label_photo ? "Label photo needed" : "Label photo (optional)"),
    h("p", { class: "muted small", style: "margin:.15rem 0 .25rem" }, "Show the medicine's label to the camera so the app keeps a record of it."),
    containerPicker(e),
    h("button", { class: "btn", onclick: () => { S.capturingFor = i; drawSetup(); } }, icon("camera"), "Show label to camera")));
  return box;
}

function entryEditor(e, i, data, errFor) {
  const d = S.setup.draft;
  const unconfirm = () => { e.confirmed = false; if (chk) chk.checked = false; };
  const sug = (f) => (e.suggested && e.suggested[f] ? "suggested" : null);
  const label = h("input", { value: e.label, maxlength: 60, class: sug("label"), placeholder: "e.g. Morning tablet", oninput: (ev) => { e.label = ev.target.value; e.suggested && (e.suggested.label = false); unconfirm(); } });
  const strength = h("input", { value: e.strength_text || "", maxlength: 40, class: sug("strength_text"), placeholder: "Optional", oninput: (ev) => { e.strength_text = ev.target.value; unconfirm(); } });
  const qty = h("select", { class: sug("quantity"), onchange: (ev) => { e.quantity = Number(ev.target.value); unconfirm(); } },
    Array.from({ length: data.max_quantity }, (_, k) => h("option", { value: k + 1, selected: Number(e.quantity) === k + 1 }, k + 1)));
  const mode = h("select", { onchange: (ev) => { e.delivery_mode = ev.target.value; e.confirmed = false; drawSetup(); } },
    h("option", { value: "DEVICE", selected: e.delivery_mode === "DEVICE" }, "From the dispenser"),
    h("option", { value: "REMINDER_ONLY", selected: e.delivery_mode === "REMINDER_ONLY" }, "Reminder only"));
  const cls = h("select", { disabled: e.delivery_mode !== "DEVICE", onchange: (ev) => { e.class_id = ev.target.value; unconfirm(); } },
    data.token_classes.map((c) => h("option", { value: c.class_id, selected: e.class_id === c.class_id }, `${c.label}, cartridge ${c.cartridge}`)));
  const chk = h("input", { type: "checkbox", checked: !!e.confirmed, onchange: (ev) => (e.confirmed = ev.target.checked) });
  const asNeeded = e.schedule === "AS_NEEDED";
  const schedOpt = (val, title, sub) => h("label", { class: `choice ${(e.schedule || "SCHEDULED") === val ? "on" : ""}` },
    h("input", { type: "radio", name: `sched-${e._uid || (e._uid = uuid().slice(0, 8))}`, checked: (e.schedule || "SCHEDULED") === val,
      onchange: () => { e.schedule = val; e.confirmed = false; drawSetup(); } }),
    h("span", {}, h("strong", {}, title), h("small", {}, sub)));
  let when;
  if (asNeeded) {
    const num = (key, min, max, unit) => h("select", { onchange: (ev) => { e[key] = Number(ev.target.value); unconfirm(); } },
      Array.from({ length: max - min + 1 }, (_, k) => h("option", { value: min + k, selected: Number(e[key]) === min + k }, `${min + k} ${unit(min + k)}`)));
    when = h("div", { class: "limits" },
      h("label", { class: "field" }, "Wait at least", num("min_interval_hours", 1, 24, (n) => (n === 1 ? "hour" : "hours")), h("span", { class: "help" }, "between doses"), errFor(i, "min_interval_hours")),
      h("label", { class: "field" }, "No more than", num("max_per_day", 1, 8, () => "a day"), h("span", { class: "help" }, "in any 24 hours"), errFor(i, "max_per_day")));
  } else {
    const timeIn = h("input", { type: "time", value: "08:00", "aria-label": "Add a time" });
    when = h("div", { class: "times" }, h("span", { class: "strong" }, "Times"),
      e.times.map((tm, k) => h("span", { class: `time-chip ${e.suggested && e.suggested.times ? "suggested" : ""}` }, fmtTime(tm),
        h("button", { type: "button", "aria-label": `Remove ${fmtTime(tm)}`, onclick: () => { e.times.splice(k, 1); e.confirmed = false; drawSetup(); } }, icon("x")))),
      timeIn,
      h("button", { class: "btn btn-quiet", type: "button", onclick: () => { const v = timeIn.value; if (v && !e.times.includes(v)) { e.times.push(v); e.times.sort(); e.confirmed = false; drawSetup(); } } }, icon("plus"), "Add time"));
  }
  return h("div", { class: "entry" },
    h("div", { class: "entry-grid" },
      h("label", { class: "field" }, "Name", label, errFor(i, "label")),
      h("label", { class: "field" }, "Strength", strength),
      h("label", { class: "field" }, "How many", qty, errFor(i, "quantity")),
      h("label", { class: "field" }, "Where it comes from", mode, errFor(i, "delivery_mode")),
      h("label", { class: "field" }, "Pill type", cls, errFor(i, "class_id"))),
    h("div", { class: "field", style: "margin-top:1rem" }, "When to take it",
      h("div", { class: "choice-row" },
        schedOpt("SCHEDULED", "At set times", "Reminders at the times you choose"),
        schedOpt("AS_NEEDED", "When I need it", "No reminders, with safe limits")), errFor(i, "schedule")),
    when, errFor(i, "times"),
    labelPhotoBlock(e, i, data),
    errFor(i, "label_id"),
    e.notes ? h("p", { class: "note", style: "margin-top:.75rem" }, e.notes) : null,
    h("div", { class: "row", style: "margin-top:1rem;justify-content:space-between" },
      h("label", { class: "check" }, chk, h("span", {}, "I checked the name, amount, timing and where it comes from",
        e.suggested && Object.values(e.suggested).some(Boolean) ? h("span", { class: "muted small" }, " (dashed fields came from the label photo)") : null)),
      d.entries.length > 1 ? h("button", { class: "btn btn-quiet btn-danger", onclick: () => { d.entries.splice(i, 1); drawSetup(); } }, icon("trash"), "Remove") : null),
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
    const list = h("section", { class: "panel" });
    if (!S.history.length) list.append(h("p", { class: "empty" }, "No doses recorded yet."));
    // Records of someone this Healthbox was handed over from sit at the end, under their name.
    const mine = S.history.filter((c) => !c.earlier_person), earlier = S.history.filter((c) => c.earlier_person);
    if (!mine.length && earlier.length) list.append(h("p", { class: "empty" }, "No doses of yours yet."));
    let lastOwner = null;
    for (const c of [...mine, ...earlier]) {
      if (c.earlier_person && c.earlier_person !== lastOwner) {
        lastOwner = c.earlier_person;
        list.append(h("h3", { class: "hist-owner" }, `Earlier: ${c.earlier_person}`),
          h("p", { class: "muted small" }, `From before Healthbox was handed over. Kept for reference; nothing here affects the current person.`));
      }
      list.append(h("button", { type: "button", class: "hist-row" + (S.historySel === c.dose_id ? " sel" : ""), onclick: () => selectHistory(c.dose_id) },
        h("div", {}, h("div", { class: "when" }, c.schedule === "AS_NEEDED" ? "As needed" : c.due_local), h("div", { class: "date" }, c.local_date)),
        h("div", {}, h("div", { class: "strong" }, ...doseLine(c)), h("div", { class: "muted small" }, modeText(c))),
        stateTag(c)));
    }
    doorMain("history", h("div", { class: "page-head" }, h("div", {}, h("h2", {}, t("nav.history")),
      h("p", { class: "lede" }, s.demo_mode ? "Demo records only." : "Your records on this laptop.")),
      h("a", { class: "btn", href: "/api/export/events.csv" }, icon("download-simple"), "Download CSV")),
      h("div", { class: "hist-grid" }, list, detailPanel()));
  },
};

async function selectHistory(id) {
  S.historySel = id;
  S.historyDetail = await fetch(`/api/history/${id}`).then((r) => (r.ok ? r.json() : null));
  historyView.render(S.status);
}

const EVENT_TEXT = {
  OCCURRENCE_CREATED: "Scheduled", REMINDER_DUE: "Reminder due", REMINDER_ALERT: "Reminder sound", REMINDER_SNOOZED: "Reminder paused",
  REMINDER_ACKNOWLEDGED: "Reminder silenced (not taken)", REMINDER_NEEDS_REVIEW: "Marked missed", REMINDER_CLOSED: "Reminder closed",
  DOSE_STARTED: "Dose steps started", DOSE_REQUESTED: "Asked for an as-needed dose", WORKFLOW_STATE: "Step", WORKFLOW_BLOCKED: "Blocked", OBSERVATION: "Camera check",
  DISPENSE_RESERVED: "Dispense reserved", CYCLE_DONE: "Motion finished", RELEASE_RESERVED: "Unlock reserved", RELEASED: "Tray unlocked",
  DOSE_COMPLETE: "Complete", USER_CONFIRMED: "You said you took it", SELF_REPORT: "You said you took it", WORKFLOW_CANCELLED: "Cancelled",
  SUPERVISED_RESET: "Recovery run", LATCH_LOCKED: "Tray locked", DISPENSE_CYCLE_DONE: "Motion finished",
  ACTION_OBSERVED: "Hand-to-mouth movement seen (not proof of swallowing)", ACTION_NOT_OBSERVED: "No hand-to-mouth movement seen",
  FOLLOWUP_LATER: "Asked again later", CARTRIDGE_FILLED: "Cartridge filled", FILL_CHECK: "Pill checked before filling",
};

function eventLine(e) {
  const d = e.details || {};
  let extra = "";
  if (e.kind === "WORKFLOW_STATE") extra = `${d.from} to ${d.to}`;
  else if (e.kind === "OBSERVATION") extra = `${(d.kind || "").replace("_", " ").toLowerCase()}: ${d.result}${d.simulated ? " (simulated)" : ""}`;
  else if (e.kind === "REMINDER_ALERT") extra = `sound ${d.repeat}`;
  else if (e.kind === "REMINDER_SNOOZED") extra = "for 5 minutes";
  else if (e.kind === "CYCLE_DONE") extra = `cartridge ${d.channel}; a pill falling is not proven`;
  else if (e.kind === "WORKFLOW_CANCELLED") extra = (e.reason_code || "").replace(/_/g, " ").toLowerCase(), e = { ...e, reason_code: null };
  if (e.reason_code) extra += (extra ? ", " : "") + e.reason_code;
  return h("li", {}, h("span", { class: "muted tnum" }, e.ts_local), h("span", {}, h("span", { class: "k" }, EVENT_TEXT[e.kind] || e.kind), extra ? h("span", { class: "muted" }, `. ${extra}`) : null));
}

const OBS_TEXT = { FACE_MATCH: "Your face", TRAY_EMPTY: "Empty tray", CLASS_COUNTS: "Dose count", COLLECTED_BY: "Who emptied the tray",
  HAND_CHECK: "Pills in your hand",
  ACTION_OBSERVED: "Hand to mouth" };
const RESULT_TEXT = { PASS: "passed", FAIL: "did not pass", UNCERTAIN: "unclear", NOT_OBSERVED: "not seen" };

function cameraRecord(det) {
  const c = det.dose;
  if (c.delivery_mode !== "DEVICE" || !det.observations.length) return null;
  const who = { RECIPIENT: "You: your face was in view", OTHER_PERSON: "Someone else was in view", NOT_SEEN: "Not seen: no face in view" }[c.collected_by];
  const act = { OBSERVED: "Seen. This is not proof of swallowing.", NOT_OBSERVED: "Not seen" }[c.action];
  const photos = det.observations.filter((o) => o.details && o.details.photo);
  return h("div", {},
    h("h3", { style: "margin:1.5rem 0 .25rem" }, "What the camera recorded"),
    h("dl", { class: "evidence" },
      h("dt", {}, "Who took the pills out"), h("dd", {}, who || (c.collection === "PENDING" ? "Not checked (finished without checking)" : "Not recorded")),
      h("dt", {}, "Hand to mouth"), h("dd", {}, act || "Not recorded"),
      h("dt", {}, "You said you took it"), h("dd", {}, c.self_report ? "Yes" : "No")),
    photos.length ? h("div", { class: "proof-grid" }, photos.map((o) => {
      const fig = proofPhoto({ photo: o.details.photo, simulated: !!o.simulated });
      fig.querySelector("figcaption").replaceChildren(`${OBS_TEXT[o.kind] || o.kind}: ${RESULT_TEXT[o.result] || o.result}`,
        o.simulated ? h("span", { class: "sim-note" }, "simulated") : null);
      return fig;
    })) : null);
}

function detailPanel() {
  const det = S.historyDetail;
  if (!det) return h("section", { class: "panel" }, h("p", { class: "empty" }, "Choose a dose to see everything that happened."));
  const c = det.dose;
  return h("section", { class: "panel" },
    h("h2", {}, ...doseLine(c)), h("p", { class: "muted", style: "margin:.25rem 0 .75rem" }, `${c.local_date} ${c.due_local}. ${modeText(c)}.`),
    stateTag(c),
    cameraRecord(det),
    h("h3", { style: "margin:1.5rem 0 .5rem" }, "What happened"),
    h("ul", { class: "events" }, det.events.map(eventLine)),
    det.commands.length ? [h("h3", { style: "margin:1.5rem 0 .5rem" }, "Dispenser commands"),
      h("ul", { class: "events" }, det.commands.map((m) => h("li", {}, h("span", { class: "muted tnum" }, `#${m.command_id}`),
        h("span", {}, h("span", { class: "k" }, m.kind), m.channel ? ` ${m.channel}` : "", h("span", { class: "muted" }, `. ${m.state.toLowerCase()}${m.detail ? ", " + m.detail : ""}`)))))] : null,
    h("p", { class: "muted small", style: "margin-top:1.25rem" }, "Unlocked, collected and \"you said you took it\" are kept separately. Healthbox never sees you swallow."));
}

/* ---------------------------------------------------------------- Dispenser */
const device = {
  key: (s) => [{ ...s.device, heartbeat_age_ms: null }, s.simulator, s.vision, s.ai, s.workflow.armed, s.config && s.config.hand_check, s.workflow.pending_operation, !!s.workflow.txn, s.camera.mode, s.token_classes, s.demo_mode, s.plan && s.plan.entries,
    s.cartridges, s.look_alike, s.look_alike_blocks, S.fill],
  tick(s) {
    const el = document.getElementById("hb-age");
    if (el) el.textContent = s.device.heartbeat_age_ms == null ? "-" : `${s.device.heartbeat_age_ms} ms`;
  },
  render(s) {
    const d = s.device;
    const stat = (label, value, id) => h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value", id }, value ?? "-"));
    const yes = (v, a = "Yes", b = "No") => (v == null ? "Unknown" : v ? a : b);
    const readings = h("div", { class: "stats" },
      stat("Connection", d.connected ? "Connected" : "Not connected"),
      stat("Session", d.session), stat("Firmware", d.firmware), stat("Heartbeat", d.heartbeat_age_ms == null ? "-" : `${d.heartbeat_age_ms} ms`, "hb-age"),
      stat("Motor A", d.motor.A), stat("Motor B", d.motor.B), stat("Rotor A homed", yes(d.home.A)), stat("Rotor B homed", yes(d.home.B)),
      stat("Access door", yes(d.door_closed, "Closed", "Open")), stat("Latch (commanded)", d.servo_commanded),
      stat("Tray revision", d.tray_revision), stat("Transaction", d.transaction), stat("Fault", d.fault || "None"),
      stat("Armed", s.workflow.armed ? "Yes" : "No"), d.port ? stat("Port", d.port) : null);

    const [orb, title, body] = !d.connected ? ["bad", "The dispenser is not connected", "Check the USB cable. The app reconnects on its own."]
      : d.fault ? ["bad", `The dispenser stopped: ${d.fault}`, "Look inside and clear anything stuck, then run recovery below."]
      : s.workflow.armed ? ["ok", "The dispenser is ready", "Connected and armed, with no problems."]
      : ["", "Connected, not armed yet", "Look over the enclosure and arm it below before the next dose."];
    const summary = h("section", { class: "panel" },
      h("div", { class: "status-big" }, h("span", { class: `orb ${orb}`, "aria-hidden": "true" }, icon(orb === "ok" ? "check" : orb === "bad" ? "warning-circle" : "pause")),
        h("div", {}, h("h2", {}, title), h("p", {}, body))),
      d.simulated ? h("p", { style: "margin-top:.75rem" }, h("span", { class: "sim-note" }, "simulated hardware")) : null,
      d.protocol_error ? h("div", { class: "alert-box", style: "margin:1rem 0 0" }, icon("warning-circle"), h("div", { class: "what" }, d.protocol_error)) : null,
      d.port_error ? h("div", { class: "alert-box", style: "margin:1rem 0 0" }, icon("warning-circle"), h("div", { class: "what" }, `Serial port: ${d.port_error}`)) : null,
      h("details", { class: "more" }, h("summary", {}, icon("caret-right"), "All dispenser readings"), h("div", { class: "more-body" }, readings,
        h("p", { class: "muted small", style: "margin-top:.75rem" }, "The latch position and the fixed tray top have no sensors, so they are not measured."))));

    const armChk = h("input", { type: "checkbox" });
    const armPanel = s.workflow.armed
      ? h("section", { class: "panel" }, h("h3", {}, "Arm for dispensing"), h("p", { class: "row", style: "margin-top:.5rem" }, h("span", { class: "tag" }, h("span", { class: "mark done" }), "Armed")),
          h("p", { class: "muted", style: "margin-top:.5rem" }, "Armed until the dispenser restarts, disconnects, stops or reports a problem."))
      : h("section", { class: "panel" }, h("h3", {}, "Arm for dispensing"),
          h("p", { class: "muted", style: "margin:.4rem 0 1rem" }, "Arming lasts until the dispenser restarts, disconnects, stops or reports a problem."),
          h("label", { class: "check" }, armChk, h("span", {}, "I looked it over: tray top and panels fixed, door closed, no way to reach into the tray.")),
          h("div", { class: "row", style: "margin-top:1rem" },
            h("button", { class: "btn btn-primary", disabled: !!S.busy, onclick: () => act("arm", () => api("/api/device/arm", { inspected: armChk.checked })).then((x) => { if (!x) return; if (onboarding()) { setOnboarding(false); toast(t("onboard.done")); go("today"); } else toast("Dispenser armed."); }) }, "Arm dispenser")));

    // Recovery is dangerous enough to stand apart from everyday buttons.
    const resetChk = h("input", { type: "checkbox" });
    const resetPanel = h("section", { class: "panel" }, h("h3", {}, "Recovery"),
      h("p", { class: "muted", style: "margin:.4rem 0 1rem" }, "Use after STOP, a problem or an unclear result. Nothing is repeated. Doses with an unknown result are marked for review. The dispenser restarts unarmed."),
      h("label", { class: "check" }, resetChk, h("span", {}, "I looked inside, cleared any jam, and no hand is in the mechanism.")),
      h("div", { class: "row", style: "margin-top:1.5rem" },
        h("button", { class: "btn btn-danger", disabled: !!S.busy, onclick: () => act("reset", () => api("/api/device/reset", { inspected: resetChk.checked })).then((x) => x && toast("Recovery sent. Arm it again when ready.")) }, icon("arrow-counter-clockwise"), "Run recovery")));

    const blocks = [summary, h("div", { class: "two" }, armPanel, resetPanel), doseStepsPanel(s), cartridgesPanel(s), pillTypesPanel(s), practicePanel(s), metricsPanel(s)];
    if (s.demo_mode) blocks.push(demoPanel(s));
    if (s.simulator) blocks.push(h("section", { class: "panel" }, h("details", { class: "more", style: "margin-top:0" }, h("summary", {}, icon("caret-right"), "Hardware simulator (for testing)"), h("div", { class: "more-body" }, simPanel(s)))));
    blocks.push(h("section", { class: "panel" }, h("details", { class: "more", style: "margin-top:0" }, h("summary", {}, icon("caret-right"), "Developer and demo mode"), h("div", { class: "more-body" }, devPanel(s)))));
    doorMain("device", h("div", { class: "stack" }, blocks));
  },
};

function metricsPanel(s) {
  // Two separate measures from the local record: how late reminders fired, and how long each dispense took.
  const body = h("div", { class: "stats stats-wide" }, h("p", { class: "muted" }, "Loading"));
  const fmt = (ms) => (ms == null ? "-" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`);
  const stat = (label, value, sub) => h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    sub ? h("div", { class: "label" }, sub) : null);
  fetch("/api/metrics").then((r) => r.json()).then((m) => {
    const rd = m.reminder_delay;
    const cells = [stat("Reminders fired", rd.count ? `${fmt(rd.median_ms)} late (median)` : "None yet",
      rd.count ? `max ${fmt(rd.max_ms)} over ${rd.count}${rd.late_over_60s ? `, ${rd.late_over_60s} over a minute` : ""}` : null)];
    for (const ch of ["A", "B"]) {
      const c = m.dispense_cycles[ch];
      const problems = c.commands - (c.outcomes.DONE || 0);
      cells.push(stat(`Cartridge ${ch} cycle`, c.count ? `${fmt(c.median_ms)} (median)` : "None yet",
        c.commands ? `${c.outcomes.DONE || 0} of ${c.commands} finished${problems ? `, ${problems} not` : ""}${c.count ? `, max ${fmt(c.max_ms)}` : ""}` : null));
    }
    body.replaceChildren(...cells);
  }).catch(() => body.replaceChildren(h("p", { class: "muted" }, "Metrics are not available right now.")));
  return h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", {}, "How it's running"),
      h("a", { class: "btn btn-quiet", href: "/api/export/metrics.csv" }, icon("download-simple"), "Download CSV")),
    h("p", { class: "muted", style: "margin-bottom:.75rem" },
      `Reminder delay is when a reminder fired minus when it was due. Cycle time runs from the dispense command to the motion finishing; it does not prove a pill fell.${s.device.simulated ? " These come from simulated hardware." : ""}`),
    body);
}

/* Cartridges: an estimated pill count, and a fill step that checks one pill from the bottle first. */
function cartridgesPanel(s) {
  const rows = (s.cartridges || []).map((c) => {
    const count = c.count == null ? "Not counted yet" : `About ${c.count} pill${c.count === 1 ? "" : "s"} left`;
    const sub = [c.used ? null : "Not used by your medicines",
      c.days_left != null ? `about ${c.days_left} day${c.days_left === 1 ? "" : "s"} at ${c.per_day} a day` : null,
      c.filled_at ? `filled ${fmtWhen(c.filled_at)}` : null].filter(Boolean).join(", ");
    const tag = c.empty ? h("span", { class: "tag" }, h("span", { class: "mark alert" }), "May be empty")
      : c.low ? h("span", { class: "tag" }, h("span", { class: "mark wait" }), "Running low") : null;
    const open = S.fill && S.fill.cartridge === c.cartridge;
    return h("div", { class: "cart-row" },
      h("div", { class: "pill-type" }, swatch(c.color),
        h("div", {}, h("div", { class: "row" }, h("span", { class: "strong" }, `Cartridge ${c.cartridge}`), h("span", { class: "muted" }, c.label), tag),
          h("div", {}, count), sub ? h("div", { class: "muted small" }, sub) : null),
        open ? null : h("button", { class: "btn", disabled: !!S.busy || !!s.workflow.txn, onclick: () => { S.fill = { cartridge: c.cartridge, result: null }; rerender(true); } },
          icon("plus-circle"), "Fill cartridge")),
      open ? fillFlow(s, c) : null);
  });
  return h("section", { class: "panel" }, h("h3", {}, "Cartridges"),
    h("p", { class: "muted", style: "margin:.4rem 0 .75rem" },
      "Before filling a cartridge, the camera checks one pill from the bottle against that cartridge's pill type. The count goes down by one for each pill the dispenser moves, so it is an estimate. Check it when you refill."),
    rows);
}

function fillFlow(s, c) {
  const f = S.fill;
  const meds = ((s.plan && s.plan.entries) || []).filter((e) => e.delivery_mode === "DEVICE" && e.cartridge === c.cartridge);
  const box = h("div", { class: "fill-flow" });
  box.append(h("p", { class: "strong" }, `1. Find the bottle for cartridge ${c.cartridge}`),
    meds.length ? h("ul", { class: "fill-meds" }, meds.map((e) => h("li", {},
      e.label_id ? h("a", { href: `/api/labels/${e.label_id}/image?i=0`, target: "_blank" }, h("img", { src: `/api/labels/${e.label_id}/image?i=0`, alt: `Label photo for ${e.label}` })) : null,
      h("span", {}, h("span", { class: "strong" }, e.label), e.strength_text ? h("span", { class: "muted" }, `, ${e.strength_text}`) : null,
        h("span", { class: "muted small" }, e.label_id ? " Compare this label with the bottle in your hand." : " No label photo saved."))))) 
      : h("p", { class: "muted" }, "No medicine in your plan uses this cartridge."),
    h("p", { class: "strong", style: "margin-top:1rem" }, "2. Put one pill from that bottle in the tray"),
    h("p", { class: "muted small" }, "Open the door, place one pill where the camera can see all four corner squares, and check it. Take the pill back out afterwards."));
  if (s.simulator) {
    const sim = (body) => api("/api/sim/hardware", body).then(() => toast("Simulator updated.")).catch((e) => toast(e.message, true));
    const other = c.cartridge === "A" ? "B" : "A";
    box.append(h("div", { class: "row", style: "margin-top:.5rem" },
      h("span", { class: "sim-note" }, "simulator"),
      h("button", { class: "btn btn-quiet", onclick: () => sim({ action: "place_token", channel: c.cartridge, n: 1 }) }, `Put one cartridge ${c.cartridge} pill in the tray`),
      h("button", { class: "btn btn-quiet", onclick: () => sim({ action: "place_token", channel: other, n: 1 }) }, `Put a cartridge ${other} pill in instead`),
      h("button", { class: "btn btn-quiet", onclick: () => Promise.all(["A", "B"].map((ch) => sim({ action: "place_token", channel: ch, n: -9 }))) }, "Empty the tray")));
  }
  const checkBtn = h("button", { class: "btn btn-primary", disabled: !!S.busy,
    onclick: () => act("fillcheck", () => api("/api/cartridges/check-pill", { cartridge: c.cartridge })).then((x) => { if (x) { S.fill = { cartridge: c.cartridge, result: x }; rerender(true); } }) },
    icon("camera"), f.result ? "Check again" : "Check this pill");
  box.append(h("div", { class: "row", style: "margin-top:.75rem" }, checkBtn,
    h("button", { class: "btn btn-quiet", onclick: () => { S.fill = null; rerender(true); } }, "Cancel")));
  const r = f.result;
  if (r) {
    const pass = r.result === "PASS";
    box.append(pass
      ? h("div", { class: "blocker", role: "status" }, icon("check-circle"), h("div", {}, h("div", { class: "strong" }, "This pill matches"), r.message))
      : h("div", { class: "alert-box", role: "alert", style: "margin:1rem 0 0" }, icon("warning-circle"), h("div", {}, h("div", { class: "what" }, "Don't fill yet"), h("div", { class: "next" }, r.message))),
      proofPhoto({ photo: r.photo, simulated: r.simulated }));
    if (pass) {
      const n = h("input", { type: "number", min: 0, max: 500, value: c.count ?? "", inputmode: "numeric", "aria-label": `Pills in cartridge ${c.cartridge} now`, style: "max-width:8rem" });
      box.append(h("p", { class: "strong", style: "margin-top:1rem" }, `3. Pour the bottle into cartridge ${c.cartridge}, then count`),
        h("label", { class: "field" }, "How many pills are in the cartridge now?", n),
        h("div", { class: "row", style: "margin-top:.75rem" },
          h("button", { class: "btn btn-primary", disabled: !!S.busy, onclick: () => {
            const v = Number(n.value);
            if (!Number.isInteger(v) || v < 0 || v > 500) { toast("Enter a whole number from 0 to 500.", true); return; }
            act("fill", () => api("/api/cartridges/fill", { cartridge: c.cartridge, count: v })).then((x) => {
              if (!x) return;
              S.fill = null;
              toast(`Cartridge ${c.cartridge}: ${v} pills recorded. Take the checked pill out of the tray.`);
            });
          } }, "Save count")));
    }
  }
  return box;
}

/* Optional dose steps. Showing the pills in the hand is off by default: holding several pills up is hard. */
function doseStepsPanel(s) {
  const on = !!(s.config && s.config.hand_check);
  return h("section", { class: "panel" }, h("h3", {}, "Dose steps"),
    h("label", { class: "switch", style: "margin-top:.75rem" }, h("span", { class: "row" }, icon("hand-palm"), "Ask to show the pills before taking them"),
      h("input", { type: "checkbox", role: "switch", checked: on, disabled: !!S.busy,
        onchange: (e) => act("handcheck", () => api("/api/settings/hand-check", { enabled: e.target.checked }))
          .then((x) => x && toast(x.hand_check ? "The next dose asks to show the pills." : "Showing the pills is off.")) })),
    h("p", { class: "switch-help" },
      "After the tray unlocks, the person holds the pills up to the camera, in the pill dish or on an open palm, before the empty-tray check. ",
      "Off by default: holding several pills up is hard for many people, and the camera has already counted them in the tray. ",
      "White pills need the dark pill dish."));
}

/* Practice the camera checks (nothing is dispensed) and see how reliable each is on this laptop. */
function practicePanel(s) {
  const body = h("div", { class: "stack", style: "gap:1rem" }, h("p", { class: "muted" }, "Loading"));
  const classes = s.token_classes || [];
  const pill = h("select", { "aria-label": "Pill type" }, classes.map((c) => h("option", { value: c.class_id }, `${c.label} (cartridge ${c.cartridge})`)));
  const count = h("select", { "aria-label": "How many" }, [0, 1, 2, 3].map((n) => h("option", { value: n, selected: n === 1 }, n === 0 ? "none (empty)" : String(n))));
  const run = (kind) => act(`practice-${kind}`, () => api("/api/practice/run", kind === "face" ? { kind } : { kind, class_id: pill.value, count: Number(count.value) }))
    .then((x) => { if (!x) return; toast(x.result === "PASS" ? "Practice check passed." : `Practice check: ${x.message || x.result}`, x.result !== "PASS"); draw(x.summary); });
  const pct = (a, b) => (b ? `${Math.round((100 * a) / b)}%` : "-");
  function draw(sm) {
    const p = sm.practice;
    const line = (label, e) => h("li", {}, h("span", { class: "strong" }, label), ` ${e.passed} of ${e.runs} passed`,
      e.recent.length ? h("span", { class: "muted" }, `. Last: ${e.recent.slice(0, 5).map((r) => (r.result === "PASS" ? "✓" : "✗")).join(" ")}`) : null);
    const face = p.face.similarity;
    body.replaceChildren(...[
      h("ul", { class: "plain-list" }, line("Face", p.face), line("Pills in hand or dish", p.hand), line("Tray", p.tray)),
      s.camera.simulated ? h("p", { class: "muted small" }, "The camera is simulated, so there are no face match scores to tune yet.") : null,
      face ? h("p", {}, `Face match scores ${face.min}–${face.max} (median ${face.median}); the threshold is ${face.threshold}.`) : null,
      p.face.advice ? h("p", { class: "note" }, p.face.advice) : null,
      h("h4", {}, "Real doses so far"),
      h("table", { class: "counts" }, h("thead", {}, h("tr", {}, h("th", {}, "Check"), h("th", {}, "Passed first try"), h("th", {}, "All tries"))),
        h("tbody", {}, sm.doses.map((d) => h("tr", {}, h("td", {}, d.label), h("td", { class: "num" }, d.doses ? `${d.first_try} of ${d.doses} (${pct(d.first_try, d.doses)})` : "-"),
          h("td", { class: "num" }, d.checks ? `${d.passed} of ${d.checks}` : "-"))))),
      sm.dose_face && sm.dose_face.advice ? h("p", { class: "muted small" }, `From real doses: ${sm.dose_face.advice}`) : null,
    ].filter(Boolean));
  }
  fetch("/api/practice").then((r) => (r.ok ? r.json() : Promise.reject())).then(draw)
    .catch(() => body.replaceChildren(h("p", { class: "muted" }, "Practice results are not available right now.")));
  const busy = !!S.busy || !!s.workflow.txn;
  return h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", {}, "Practice and tuning"),
      h("button", { class: "btn btn-quiet", disabled: busy, onclick: () => act("practice-clear", () => api("/api/practice/clear", {})).then((x) => x && rerender(true)) }, icon("trash"), "Clear practice results")),
    h("p", { class: "muted", style: "margin:.4rem 0 1rem" },
      "Run the camera checks without dispensing anything, in the light and place you will really use, to see how reliable they are before a demo. Nothing here changes a dose."),
    h("div", { class: "row", style: "flex-wrap:wrap;gap:.75rem;margin-bottom:1rem" },
      h("button", { class: "btn", disabled: busy, onclick: () => run("face") }, icon("user-focus"), "Practice face check"),
      pill, count,
      h("button", { class: "btn", disabled: busy, onclick: () => run("hand") }, icon("hand-palm"), "Practice hand or dish"),
      h("button", { class: "btn", disabled: busy, onclick: () => run("tray") }, icon("squares-four"), "Practice tray")),
    body);
}

function pillTypesPanel(s) {
  const sim = s.camera.simulated;
  const rows = s.token_classes.map((c) => {
    const name = h("input", { value: c.label, maxlength: 24, "aria-label": `Name for cartridge ${c.cartridge}`, style: "max-width:12rem" });
    const sh = c.shape;
    const desc = sh ? `${sh.kind}, size ${Math.round(sh.area)} px, length to width ${Number(sh.elongation).toFixed(2)}` : "Not calibrated yet (assumes a round pill)";
    return h("div", { class: "pill-type" },
      swatch(c.color),
      h("div", {}, h("div", { class: "row" }, name, h("span", { class: "muted" }, `Cartridge ${c.cartridge}`),
        h("button", { class: "btn btn-quiet", onclick: () => act("rename", () => api("/api/tray/rename", { class_id: c.class_id, label: name.value })).then((x) => x && toast("Name saved.")) }, "Save name")),
        h("div", { class: "muted small", style: "margin-top:.25rem" }, desc)),
      h("button", { class: "btn", disabled: sim || !!S.busy || !!s.workflow.txn,
        onclick: () => act("calibrate", () => api("/api/tray/calibrate", { class_id: c.class_id, label: name.value }))
          .then((x) => {
            if (!x) return;
            if (x.look_alike && x.look_alike.length) toast(x.look_alike[0].message, true);
            else toast(`Calibrated: ${x.shape.kind}, size ${Math.round(x.shape.area)} px.`);
          }) }, "Calibrate with one pill"));
  });
  const alike = (s.look_alike || []).map((p) => h("div", { class: "alert-box", role: "alert" }, icon("warning-circle"),
    h("div", {}, h("div", { class: "what" }, "These pill types look alike to the camera"), h("div", { class: "next" }, p.message),
      s.look_alike_blocks ? h("div", { class: "detail" }, "Doses can't start until this is fixed, because one pill would be counted as both types.")
        : h("div", { class: "detail" }, "The tray is simulated right now, so this does not block doses yet. It will with the real camera."))));
  return h("section", { class: "panel" }, h("h3", {}, "Pill types"),
    h("p", { class: "muted", style: "margin:.4rem 0 .75rem" },
      "Teach the camera each pill's colour and shape. Put exactly one pill of that type in the tray, point the camera so all four corner squares show, and press Calibrate. White pills need a dark matte tray. Use clearly different colours for cartridges A and B."),
    sim ? h("p", { class: "note", style: "margin-bottom:.75rem" }, "Calibration needs the real camera, which is turned off in this demo.") : null,
    alike,
    rows,
    h("p", { class: "muted small", style: "margin-top:.75rem" }, "Colour and shape only check that the right kind of object is in the tray. They do not prove which medicine or strength it is."));
}

function demoPanel(s) {
  const entries = (s.plan && s.plan.entries) || [];
  const sched = entries.filter((e) => e.schedule !== "AS_NEEDED");
  const dev = sched.filter((e) => e.delivery_mode === "DEVICE").map((e) => e.entry_key);
  const rem = sched.filter((e) => e.delivery_mode !== "DEVICE").map((e) => e.entry_key);
  const trig = (keys) => act("demo", () => api("/api/demo/trigger", { entry_keys: keys })).then((x) => x && toast(t("demo.started")));
  return h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", {}, t("demo.title")), h("span", { class: "demo-tag" }, t("demo.badge"))),
    h("p", { class: "muted", style: "margin-bottom:1rem" }, t("demo.help")),
    h("div", { class: "row" },
      h("button", { class: "btn btn-primary", disabled: !!S.busy || !dev.length, onclick: () => trig(dev) }, icon("bell-ringing"), t("demo.ab")),
      h("button", { class: "btn", disabled: !!S.busy || !dev.length, onclick: () => trig(dev.slice(0, 1)) }, t("demo.a")),
      h("button", { class: "btn", disabled: !!S.busy || !rem.length, onclick: () => trig(rem) }, t("demo.r")),
      h("span", { class: "spacer" }),
      h("button", { class: "btn btn-quiet btn-danger", disabled: !!S.busy || !!s.workflow.txn,
        onclick: () => act("demo", () => api("/api/demo/reset", {})) }, icon("trash"), t("demo.reset"))));
}

function simPanel(s) {
  const sim = s.simulator;
  const send = (body) => api("/api/sim/hardware", body).catch((e) => toast(e.message, true));
  const modeSel = (ch) => h("select", { "aria-label": `Next feeds on ${ch}`, onchange: (e) => send({ action: "feed_mode", channel: ch, mode: e.target.value }) },
    [["ok", "Normal"], ["empty", "Empty feed"], ["double", "Double feed"], ["wrong_class", "Wrong pill type"], ["stall", "Motor stall"], ["lost_done", "Completion message lost"], ["no_ack", "Command lost"]]
      .map(([v, l]) => h("option", { value: v, selected: sim.feed_mode[ch] === v }, l)));
  return h("div", {},
    h("p", { class: "muted", style: "margin-bottom:1rem" }, "These change the simulated dispenser only. Everything it reports is marked simulated."),
    h("div", { class: "stats" },
      h("label", { class: "field" }, "Cartridge A feeds", modeSel("A")),
      h("label", { class: "field" }, "Cartridge B feeds", modeSel("B")),
      h("label", { class: "field" }, "Reply delay", h("select", { onchange: (e) => send({ action: "ack_delay", seconds: Number(e.target.value) }) },
        [0, 0.5, 1, 3].map((v) => h("option", { value: v, selected: sim.ack_delay_s === v }, `${v} s`)))),
      h("div", { class: "stat" }, h("div", { class: "label" }, "Simulated tray"), h("div", { class: "value" }, `A: ${sim.tray.A}, B: ${sim.tray.B}`))),
    h("div", { class: "row", style: "margin-top:1rem" },
      sim.link_up ? h("button", { class: "btn", onclick: () => send({ action: "disconnect" }) }, "Unplug USB")
        : h("button", { class: "btn", onclick: () => send({ action: "reconnect" }) }, "Plug USB back in"),
      h("button", { class: "btn", onclick: () => send({ action: "power_cycle" }) }, "Power cycle"),
      sim.door_closed ? h("button", { class: "btn", onclick: () => send({ action: "open_door" }) }, "Open door")
        : h("button", { class: "btn", onclick: () => send({ action: "close_door" }) }, "Close door"),
      sim.door_closed ? h("button", { class: "btn btn-quiet btn-danger", onclick: () => send({ action: "open_door", force: true }) }, "Force door open") : null,
      h("button", { class: "btn", onclick: () => send({ action: "collect" }) }, "Take tokens"),
      h("button", { class: "btn btn-quiet", onclick: () => send({ action: "place_token", channel: "A", n: 1 }) }, "Drop a stray token in A")));
}

function devPanel(s) {
  const ai = s.ai;
  return h("div", {},
    h("dl", { class: "kv" },
      h("dt", {}, "Hardware mode"), h("dd", {}, s.config.hardware_mode),
      h("dt", {}, "Camera mode"), h("dd", {}, s.config.camera_mode + (s.vision.face_models ? ", face models present" : ", face models missing")),
      h("dt", {}, "AI"), h("dd", {}, ai.backend === "off" ? "Off" : `${ai.backend}, ${ai.model}, ${ai.ready ? "ready" : "not ready"}`),
      ai.error ? [h("dt", {}, "AI note"), h("dd", {}, ai.error)] : null,
      h("dt", {}, "AI requests"), h("dd", {}, `${ai.requests_used} of ${ai.request_limit}. Tokens in ${ai.tokens.input}, out ${ai.tokens.output}${ai.last_outcome ? `. Last: ${ai.last_outcome}` : ""}`),
      s.public_demo ? null : h("dt", {}, "Demo mode"), s.public_demo ? null : h("dd", {}, h("button", { class: "btn btn-quiet", disabled: !!s.workflow.txn || !!S.busy,
        onclick: () => act("demo", () => api("/api/demo/mode", { enabled: !s.demo_mode })) }, s.demo_mode ? "Turn off demo mode" : "Turn on demo mode"))),
    h("p", { class: "muted small", style: "margin-top:.75rem" }, "Check real spending in the provider console. These counts are local limits, not a money cap."));
}

/* ---------------------------------------------------------------- Assistant */
function renderPrompts() {
  const box = $("#assistant-prompts");
  box.replaceChildren(...["ask.p1", "ask.p2", "ask.p3", "ask.p4"].map((k) =>
    h("button", { type: "button", onclick: () => { ask(t(k)); $("#assistant-input").focus(); } }, t(k))));
}

function openAssistant(open) {
  const pane = $("#assistant");
  pane.hidden = !open;
  $("#assistant-toggle").setAttribute("aria-expanded", String(open));
  if (open) $("#assistant-input").focus();
  else $("#assistant-toggle").focus();
}

function renderChat() {
  const log = $("#assistant-log");
  log.replaceChildren();
  if (!S.chat.length) {
    log.append(h("p", { class: "muted" }, t("ask.intro")));
    if (PREFS.lang !== "en") log.append(h("p", { class: "muted small" }, t("ask.english_note")));
  }
  for (const m of S.chat) {
    if (m.q) { log.append(h("div", { class: "msg-q" }, m.q)); continue; }
    if (m.pending) { log.append(h("div", { class: "msg-a" }, h("div", { class: "waiting", style: "min-height:0;font-size:1rem" }, h("span", { class: "breath", "aria-hidden": "true" }), t("ask.thinking")))); continue; }
    if (m.error) { log.append(h("div", { class: "msg-a" }, m.error)); continue; }
    const r = m.r;
    const stale = S.status && contextKey(S.status) !== m.ctx;
    log.append(h("div", { class: "msg-a" },
      r.authoritative ? h("div", { class: "auth" }, h("span", { class: "lbl" }, t("ask.recorded")), r.authoritative) : null,
      h("div", {}, r.answer.answer_text),
      r.supporting_facts.length ? h("ul", { class: "facts" }, r.supporting_facts.map((f) => h("li", {}, f.text))) : null,
      r.answer.suggested_page ? h("button", { class: "btn btn-quiet", style: "margin-top:.4rem",
        onclick: () => go(r.answer.suggested_page) }, t("ask.open", { page: t(`nav.${r.answer.suggested_page}`) })) : null,
      h("div", { class: "foot" }, h("span", {}, t("ask.asof", { time: fmtClock(r.snapshot_time) }), stale ? `. ${t("ask.changed")}` : ""),
        h("span", {}, r.source === "gemini" ? "Gemini" : t("ask.offline"))),
      r.note ? h("div", { class: "muted small", style: "margin-top:.25rem" }, r.note) : null));
  }
  log.scrollTop = log.scrollHeight;
}

function contextKey(s) {
  if (!s) return "";
  const tx = s.workflow.txn;
  return JSON.stringify([tx && tx.state, tx && tx.blocks.map((b) => b.code), s.next_dose && s.next_dose.dose_id,
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
const DOOR_VIEWS = ["schedules", "setup", "history", "device"];

function route() {
  let v = (location.hash || "#today").slice(1);
  if (!VIEWS[v]) v = "today";
  if (PREFS.simple && DOOR_VIEWS.includes(v)) { location.hash = "today"; return; }
  S.view = v;
  document.querySelectorAll(".tabs .tab[data-view]").forEach((a) => {
    if (a.dataset.view === S.view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  const door = $("#care-link");
  if (DOOR_VIEWS.includes(S.view)) door.setAttribute("aria-current", "page"); else door.removeAttribute("aria-current");
  S.keys = {};
  if (needsWelcome(S.status)) { S.keys.__welcome = 1; renderWelcome(); main.focus({ preventScroll: true }); return; }
  if (S.view !== "today") renderSky(null);
  if (DOOR_VIEWS.includes(S.view) && setupLocked()) { S.keys[S.view] = "signed-out"; doorMain(S.view); main.focus({ preventScroll: true }); return; }
  // Setup draws once (its forms keep their own state), so it waits for the first status: it needs the
  // account's name and the Sign out button.
  if (S.view === "setup") { if (S.status) setup.render(); else S.setupPending = true; }
  else if (S.status) VIEWS[S.view].render(S.status);
  main.focus({ preventScroll: true });
}

async function faceHintLoop() {
  if (!document.hidden && S.view === "setup" && S.faceHint && S.faceHint.isConnected && S.status && !S.status.camera.simulated) {
    const hint = S.faceHint;
    try {
      const r = await fetch("/api/camera/face-preview", { signal: AbortSignal.timeout(3000) }).then((x) => x.json());
      if (S.view !== "setup" || S.faceHint !== hint || !hint.isConnected) return setTimeout(faceHintLoop, 200);
      const [txt, color] = !r.available ? ["Face models missing. Run scripts/download_models.py.", "var(--alert)"]
        : r.busy ? ["Capturing. Keep facing the camera.", "var(--ink-3)"]
        : r.faces == null ? ["Waiting for the camera.", "var(--ink-3)"]
        : r.faces === 0 ? ["No face found. Move into the frame, with light in front of you.", "var(--ink-2)"]
        : r.faces > 1 ? ["More than one face. Only the person being added should be in view.", "var(--alert)"]
        : [`Face found (confidence ${r.score}). Ready to capture.`, "var(--ink)"];
      if (hint.textContent !== txt) hint.textContent = txt;
      hint.style.color = color;
    } catch {}
  }
  setTimeout(faceHintLoop, 200);
}

function cameraStream(img, role, mirrored = false) {
  if (!img) return;
  img.classList.toggle("camera-mirrored", mirrored);
  if (!role || !img.isConnected || document.hidden) {
    img.removeAttribute("src");
    delete img.dataset.streamRole;
    return;
  }
  if (img.dataset.streamRole === role && img.hasAttribute("src")) return;
  img.dataset.streamRole = role;
  img.onerror = () => {
    // The loop reconnects only if this image still belongs to the active view.
    img.removeAttribute("src");
    delete img.dataset.streamRole;
  };
  img.src = `/api/camera/stream?role=${role}`;
}

function cameraLoop() {
  const s = S.status;
  const live = s && !s.camera.simulated;
  const wantPreview = live && ((S.view === "verify" && s.workflow.txn) || S.view === "setup");
  // With the in-box tray camera, tray steps show that camera; everything else shows the laptop camera.
  const trayStep = s && s.workflow.txn && (s.workflow.txn.pose === "TRAY" || (S.live && S.live.kind === "tray"));
  const role = S.view === "verify" && wantPreview && trayStep && s.camera.mode === "dual_camera" ? "tray" : "face";
  // Every live preview is mirrored, so moving something left moves it left on screen. Display only:
  // the checks and the saved proof photos use the real, unmirrored picture.
  cameraStream(S.cameraImg, wantPreview ? role : null, !!wantPreview);
  cameraStream(S.labelImg, live && S.view === "setup" && S.capturingFor != null ? "face" : null);
  setTimeout(cameraLoop, 250);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    cameraStream(S.cameraImg, null);
    cameraStream(S.labelImg, null);
  }
});
window.addEventListener("pagehide", () => {
  cameraStream(S.cameraImg, null);
  cameraStream(S.labelImg, null);
});

/* ---------------------------------------------------------------- wiring */
$("#stop-btn").addEventListener("click", async () => {
  try {
    await api("/api/device/stop", {});
    toast(t("toast.stop_sent"));
  } catch (e) {
    toast(`${t("toast.stop_fail")} ${e.message}`, true);
  }
});
$("#prefs-toggle").addEventListener("click", () => togglePrefs());
$("#assistant-toggle").addEventListener("click", () => openAssistant($("#assistant").hidden));
$("#assistant-close").addEventListener("click", () => openAssistant(false));
$("#assistant-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const inp = $("#assistant-input");
  ask(inp.value);
  inp.value = "";
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#prefs").hidden) togglePrefs(false);
  else if (!$("#assistant").hidden) openAssistant(false);
});
document.addEventListener("click", (e) => {
  const p = $("#prefs");
  if (!p.hidden && !p.contains(e.target) && !$("#prefs-toggle").contains(e.target)) togglePrefs(false);
});
window.addEventListener("hashchange", route);
// The sky follows the clock even when nothing else changes.
setInterval(() => { S.keys.today = null; }, 60000);
if ("speechSynthesis" in window) speechSynthesis.getVoices(); // warm up the voice list
applyPrefs();
renderPrompts();
renderChat();
route();
poll();
cameraLoop();
liveLoop();
faceHintLoop();
