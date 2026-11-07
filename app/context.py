"""Approved help snippets, redacted status facts, and the deterministic explanations
that work without Gemini or an internet connection. The server picks the context; the
model never chooses what it can see."""
from __future__ import annotations

import re

HELP: list[dict] = [
    {"id": "H-due", "title": "Due reminders", "keywords": "due next reminder alert when time today schedule",
     "text": "At the scheduled time DoseGuard shows a due card and plays a sound every 60 seconds, up to three times. "
             "After 15 minutes without a response the card is marked Needs review. Reminders never move the dispenser."},
    {"id": "H-snooze", "title": "Snooze and dismiss", "keywords": "snooze dismiss acknowledge silence later return came back again",
     "text": "Snooze delays the alert by five minutes, then it returns as due. Dismiss reminder only acknowledges the notification. "
             "Neither one means the dose was taken or collected."},
    {"id": "H-review", "title": "Needs review", "keywords": "needs review late missed overdue",
     "text": "A reminder becomes Needs review after 15 minutes without a response, or if the app was off at the due time. "
             "DoseGuard never combines missed quantities or dispenses a catch-up dose automatically."},
    {"id": "H-verify", "title": "Verification steps", "keywords": "start verification steps how dispense release process",
     "text": "Start verification runs: recipient check, empty tray check, dispensing, dose check, then recipient recheck and release. "
             "Each step needs fresh camera evidence. The tray unlocks only after every check passes."},
    {"id": "H-face", "title": "Positioning for the recipient check", "keywords": "camera position face recipient person light frame",
     "text": "Sit about an arm's length from the laptop camera, face it directly, in even light, with nobody else in view. "
             "Hold still for about one second."},
    {"id": "H-tray", "title": "Positioning for tray checks", "keywords": "camera position tray markers count tokens view light blurry",
     "text": "The camera must see all four corner markers of the tray. Use diffuse light, keep the camera still, and make sure "
             "tokens are not touching or stacked."},
    {"id": "H-counts", "title": "Count checks", "keywords": "count wrong failed mismatch blocked extra missing class color",
     "text": "The dose check compares the visible count of each token class with the confirmed plan. A matching total with the "
             "wrong class still fails. Touching or blurred objects give an uncertain result, which also blocks release."},
    {"id": "H-recovery", "title": "Faults and recovery", "keywords": "fault stop uncertain recovery reset disconnected stuck jam",
     "text": "After STOP, a fault, or an uncertain outcome, nothing is repeated automatically. A supervisor inspects the device, "
             "then uses supervised recovery in Device controls. The controller restarts unarmed."},
    {"id": "H-arm", "title": "Arming the device", "keywords": "arm armed not armed enclosure inspect",
     "text": "Before dispensing, a supervisor inspects the enclosure and arms the device in Device controls. Arming is lost after a "
             "restart, a disconnect, STOP or a fault."},
    {"id": "H-taking", "title": "Collection and taking", "keywords": "taken swallow ingest collected took confirm",
     "text": "DoseGuard can observe that the tray was emptied and record your own confirmation. It cannot verify swallowing. "
             "Collection observed and User reported taken are separate records."},
    {"id": "H-reminder-only", "title": "Reminder-only entries", "keywords": "reminder only separate stored mark taken self report",
     "text": "Reminder-only entries are stored outside the dispenser. Mark taken records an unverified self report and never "
             "counts as a camera check."},
    {"id": "H-label", "title": "Label scan", "keywords": "label scan photo extract gemini fields",
     "text": "A fictional label can be scanned to suggest fields. Every suggested field must be reviewed and the plan confirmed "
             "by a person before it is used."},
    {"id": "H-demo", "title": "Demo mode", "keywords": "demo mode trigger 30 seconds fictional",
     "text": "Demo mode keeps fictional records separate from ordinary records and can trigger a reminder in 30 seconds using the "
             "real clock."},
    {"id": "H-limits", "title": "What the assistant cannot do", "keywords": "unlock change dose dispense bypass override skip",
     "text": "The assistant only explains. It cannot dispense, unlock, snooze, change a plan or confirm anything. Use the normal "
             "controls for every action."},
]

PAGES = ("today", "schedules", "setup", "verify", "history", "device")


def select_help(question: str, limit: int = 4) -> list[dict]:
    words = set(re.findall(r"[a-z]+", question.lower()))
    scored = []
    for h in HELP:
        kw = set(h["keywords"].split()) | set(re.findall(r"[a-z]+", h["title"].lower()))
        score = len(words & kw)
        if score:
            scored.append((score, h))
    scored.sort(key=lambda x: -x[0])
    picked = [h for _, h in scored[:limit]]
    if not any(h["id"] == "H-limits" for h in picked):
        picked.append(next(h for h in HELP if h["id"] == "H-limits"))
    return picked


def build_facts(snap: dict, recent_events: list[dict]) -> list[dict]:
    """Redacted facts with IDs. No faces, embeddings, keys, or unconfirmed label guesses."""
    facts: list[dict] = []
    t = snap.get("server_time")

    def add(fid: str, text: str) -> None:
        facts.append({"id": fid, "text": text, "as_of": t})

    nd = snap.get("next_dose")
    if nd:
        when = nd["due_local"] if nd.get("due_day") in (None, "Today") else f"{nd['due_day'].lower() if nd['due_day'] == 'Tomorrow' else nd['due_day']} at {nd['due_local']}"
        add("F-next", f"Next confirmed dose: {nd['label']} x{nd['quantity']}, {when} ({_mode(nd)}).")
    else:
        add("F-next", "No upcoming confirmed dose today.")
    for card in snap.get("today", [])[:8]:
        r = card.get("reminder") or {}
        extra = f", snoozed until {r['snoozed_until_local']}" if r.get("snoozed_until_local") else ""
        add(f"F-dose-{card['dose_id']}", f"{card['label']} x{card['quantity']} due {card['due_local']}: reminder {r.get('state', 'none').lower()}"
            f"{extra}; workflow {card['workflow_state'].lower().replace('_', ' ')}; {_mode(card)}.")
    wf = (snap.get("workflow") or {}).get("txn")
    if wf:
        add("F-stage", f"Verification stage: {wf['stage']}.")
        exp = ", ".join(f"{n} {wf['class_labels'].get(c, c).lower()}" for c, n in wf["expected"]["by_class"].items())
        add("F-expected", f"Expected tray contents: {exp}.")
        for kind, o in (wf.get("observations") or {}).items():
            seen = ""
            if o.get("observed"):
                seen = " Observed " + ", ".join(f"{n} {wf['class_labels'].get(c, c).lower()}" for c, n in o["observed"].items()) + "."
            add(f"F-obs-{kind}", f"Latest {kind.replace('_', ' ').lower()} result: {o['result']}"
                + (f" ({o['reason_code']})" if o.get("reason_code") else "") + "." + seen
                + (" Simulated." if o.get("simulated") else ""))
        for b in wf.get("blocks") or []:
            add(f"F-block-{b['code']}", f"Current block: {b['message']}")
    dev = snap.get("device") or {}
    add("F-device", "Device " + ("connected" if dev.get("connected") else "disconnected")
        + (", armed" if (snap.get("workflow") or {}).get("armed") else ", not armed")
        + (f", fault {dev['fault']}" if dev.get("fault") else "")
        + (" (simulator)" if dev.get("simulated") else "") + ".")
    for e in recent_events[:6]:
        add(f"F-ev-{e['event_id']}", f"{e['ts_local']}: {e['kind'].replace('_', ' ').lower()}"
            + (f" ({e['reason_code']})" if e.get("reason_code") else "") + ".")
    return facts


def _mode(card: dict) -> str:
    return "dispenser" if card.get("delivery_mode") == "DEVICE" else "reminder only"


def authoritative_explanation(snap: dict) -> str | None:
    """The deterministic sentence for the current block. Shown separately from model text."""
    wf = (snap.get("workflow") or {}).get("txn")
    if not wf or not wf.get("blocks"):
        return None
    return " ".join(b["message"] for b in wf["blocks"])


def local_answer(question: str, snap: dict, facts: list[dict]) -> dict:
    """Offline fallback built only from facts and help text."""
    q = question.lower()
    ids = {f["id"]: f for f in facts}
    if any(w in q for w in ("unlock", "dispense now", "bypass", "override", "change my dose", "skip the check", "api key", "key")):
        return _ans("I can only explain. I can't dispense, unlock, change a plan or reveal settings. Use the normal controls.",
                    ["F-device"], None, "out_of_scope")
    if any(w in q for w in ("block", "fail", "why can't", "why cant", "locked", "count")):
        auth = authoritative_explanation(snap)
        if auth:
            blk = [f for f in ids if f.startswith("F-block-")]
            return _ans(auth, blk, "verify")
        return _ans("Nothing is blocked right now.", ["F-device"], None)
    if any(w in q for w in ("position", "camera", "where do i", "markers")):
        wf = (snap.get("workflow") or {}).get("txn") or {}
        if wf.get("pose") == "TRAY":
            return _ans(next(h["text"] for h in HELP if h["id"] == "H-tray"), ["F-stage"] if "F-stage" in ids else [], "verify")
        return _ans(next(h["text"] for h in HELP if h["id"] == "H-face"), ["F-stage"] if "F-stage" in ids else [], "verify")
    if any(w in q for w in ("reminder", "alert", "return", "again", "snooze")):
        due = [f for fid, f in ids.items() if fid.startswith("F-dose-") and ("reminder due" in f["text"] or "snoozed" in f["text"])]
        if due:
            return _ans(due[0]["text"] + " Snooze brings an alert back after five minutes; Dismiss reminder only silences it.",
                        [due[0]["id"]], "today")
        return _ans("No reminder is active right now.", ["F-next"], "today")
    return _ans(ids["F-next"]["text"], ["F-next"], "today")


def _ans(text: str, fact_ids: list[str], page: str | None, scope: str = "in_scope") -> dict:
    return {"answer_text": text, "supporting_fact_ids": fact_ids, "suggested_page": page,
            "needs_more_information": False, "scope": scope}
