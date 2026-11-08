# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

One person managing their own medicine at home, on one laptop. They may be any age; some are older, anxious, have low vision or read in a second language. They use DoseGuard in two moments: every day, when a dose is due or when they need an as-needed medicine (a headache pill, for example); and rarely, when they set things up (their face, their medicines and label photos, the dispenser) or something needs fixing. There are no separate patient and caregiver roles. A family member or nurse may help with setup, using the same screens.

## Product Purpose

DoseGuard is a local smart pillbox. It reminds the person when a confirmed dose is due, lets them ask for an as-needed medicine within safe limits (minimum hours apart, a daily maximum), checks that the right person is present and that the tray holds exactly the confirmed pills, then unlocks the tray. Success: the person takes the right dose at the right time, nothing is ever dispensed twice or too soon, and nobody is left unsure whether a dose was taken.

## Positioning

The dose is physically guarded: face check, empty-tray check, counted dose check, recipient recheck, then release. Reminders never move hardware, and released, collected and self-reported are recorded separately. DoseGuard never claims to verify swallowing.

## Operating Context

- Runs on one laptop at `http://127.0.0.1:8000`, kept awake with the page open; reminder sound needs one click per session.
- Hardware: ESP32 controller with two cartridges (A, B) and a latched tray; a laptop camera (later an ESP32-CAM) watches face and tray.
- Built for the MakeCU 2026 hackathon with fictional labels and non-ingestible demo tokens; demo mode keeps fictional records separate.

## Capabilities and Constraints

Everyday: Today (due doses, remind me later, silence, I took it for reminder-only, as-needed medicines with when they are next allowed), Dose steps (six guarded steps), Ask (read-only assistant), Display (language EN/ES/ZH, text size, high contrast, simple view), read aloud.
Setup (one door, used rarely): My medicines (confirmed plan, scheduled and as-needed), Set up (face enrollment, label photos with multi-angle bottle capture, entries, timezone), History (per-dose timeline, CSV), Dispenser (status, arm, supervised recovery, pill-type calibration, simulator, developer info).
Account: one local account per laptop (name and password, hashed, never leaves the laptop). The password guards Setup changes only and stays unlocked for 15 minutes after the last change; taking a dose, reminders, STOP and Ask never need it. A forgotten password is reset on the laptop with `scripts/reset_account.py` (medicines and history are kept). Label directions can pre-fill suggested times; the person still confirms each entry.
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
