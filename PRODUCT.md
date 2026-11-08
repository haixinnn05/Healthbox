# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One person managing their own medicine at home, on one laptop. They may be any age; some are older, anxious, have low vision or read in a second language. They use DoseGuard in two moments: every day, when a dose is due or when they need an as-needed medicine (a headache pill, for example); and rarely, when they set things up (their face, their medicines and label photos, the dispenser) or something needs fixing. There are no separate patient and caregiver roles. A family member or nurse may help with setup, using the same screens.

## Product Purpose

DoseGuard is a local smart pillbox. It reminds the person when a confirmed dose is due, lets them ask for an as-needed medicine within safe limits (minimum hours apart, a daily maximum), checks that the right person is present and that the tray holds exactly the confirmed pills, then unlocks the tray. Success: the person takes the right dose at the right time, nothing is ever dispensed twice or too soon, and nobody is left unsure whether a dose was taken.

## Positioning

The dose is physically guarded: face check, empty-tray check, counted dose check, recipient recheck, then release. Every tray check keeps a photo of the tray with each counted pill circled. After release the person can be asked to show the pills in a marked pill dish or on their open palm (an optional step, off by default because holding pills up is hard for many people); a live view rings what the camera sees, steps move on by themselves once the camera sees the right thing, and each step is spoken aloud; the camera records who emptied the tray and whether a hand moved to the person's mouth, and asks "Did you take it?" if neither that nor "I took it" happened. Reminders never move hardware, and released, collected, hand-to-mouth seen and self-reported are recorded separately. DoseGuard never claims to verify swallowing.

## Operating Context

- Runs on one laptop at `http://127.0.0.1:8000`, kept awake with the page open. The reminder chime is played by the server through the laptop's speakers (macOS), so it needs no click and sounds even with the page in the background; the top bar warns if the laptop is muted or nearly silent.
- Hardware: ESP32 controller with two cartridges (A, B) and a latched tray; a laptop camera (later an ESP32-CAM) watches face and tray.
- Built for the MakeCU 2026 hackathon with fictional labels and non-ingestible demo tokens; demo mode keeps fictional records separate.

## Capabilities and Constraints

Everyday: Today (due doses, remind me later, silence, I took it for reminder-only, as-needed medicines with when they are next allowed, "Now take your medicine" while the camera watches, "Did you take it?" follow-ups, low-cartridge notes), Dose steps (six guarded steps, seven with the optional "show the pills in your hand", live "what the camera sees" while collecting, tray photos), Ask (read-only assistant), Display (language EN/ES/ZH, text size, high contrast, simple view), read aloud.
Setup (one door, used rarely): My medicines (confirmed plan, scheduled and as-needed), Set up (face enrollment, label photos with multi-angle bottle capture, entries, timezone), History (per-dose timeline, camera record and tray photos, CSV), Dispenser (status, arm, supervised recovery, cartridges with fill check and estimated pill count, pill-type calibration with look-alike warning, simulator, developer info).
Account: one local account per laptop, for one person (name and password, hashed, never leaves the laptop). The account's name is the only name; the face belongs to it. Sign out fully signs out: until the password is entered only a due dose (taken with the face check), follow-ups and the sign-in panel show; medicines, history, photos, Ask and Setup are hidden and refused by the server. A forgotten password never blocks a due dose. "Someone else will use this Healthbox" archives the person (history kept under their name) and starts the welcome for the next person. You stay signed in until Sign out or a server restart; taking a due dose, reminders and STOP never need the password. A forgotten password is reset on the laptop with `scripts/reset_account.py` (medicines and history are kept). Label directions can pre-fill suggested times; the person still confirms each entry.
Constraints: vanilla HTML/CSS/JS, no build step; every rule is enforced on the server; STOP is always visible.

## Brand Commitments

Name: Healthbox (the product was called DoseGuard; code, repo and file names keep `doseguard`). Logo: the user's orbit-clock pill mark, static/brand/healthbox-mark.png. Personality: calm, kind, trustworthy. The user asked for a light blue and white theme with a little red; red is reserved for real alerts and STOP. Voice: plain, short sentences, never technical on everyday screens.

## Evidence on Hand

No real patient data, testimonials or photography. Demo records are fictional and labelled. Do not fabricate statistics or medical claims.

## Product Principles

1. One thing per screen: each screen has one obvious next action.
2. Everyday first: setup and device tools sit behind one Setup door, out of the daily path.
3. Plain words: say what to do, not how the system works.
4. Red means attention: alerts and STOP only, never decoration.
5. Never ambiguous: whether a dose was released, collected or reported is always explicit.

## Accessibility & Inclusion

WCAG 2.2 AA. Large default type with a three-step size control, high-contrast mode, every status in words and icon (never colour alone), 48px targets, full keyboard and screen-reader support, reduced motion respected, read aloud, and English, Español and 中文 on the everyday screens.
