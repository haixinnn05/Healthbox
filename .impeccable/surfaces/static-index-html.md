---
version: 1
slug: "static-index-html"
primary_target: "static/index.html"
related_targets: ["static/app.js","static/styles.css","static/i18n.js"]
---

# DoseGuard app shell (all screens)

Scope: the whole web app for one person: Today (scheduled doses and as-needed medicines), Dose steps, Ask, Display, and one Setup door (My medicines, Set up, History, Dispenser). Mode: operate.
Audience: one person managing their own medicine at home on a laptop; may be older, anxious, in pain, low vision or second-language. A helper may use Setup with the same screens.
Task: take the due dose or an allowed as-needed medicine safely; occasionally set up medicines, face and dispenser, or fix the device.
Constraints: vanilla JS, no build; server APIs unchanged except the new as-needed contract; EN/ES/ZH and Display settings kept; STOP always visible; light blue and white with red only for alerts and STOP.
Chosen direction: Morning Sky (round 3, the IMPECCABLE'S PICK card), replacing the round 2 pharmacy label world.
Memorable moment: the sky band across Today, its colour following the real time of day, with today's doses placed on the sun's arc and the sun sitting at now.

## Direction contract

THESIS: Your day is a sky. Doses sit on the sun's arc, the sky band follows the real hour, and the next dose is where the sun is heading. Refuses the pill-app list of identical white cards and the old label-and-sticker chrome.

OWN-WORLD: Sky band gradients keyed to the hour (dawn pale blue, midday clear blue, dusk soft periwinkle, night deep navy band with white type); cloud-white panels with soft neutral elevation and 24px radii; one ink navy for type; a warm sun disc as the only warm colour, used only for "now"; red only for STOP and real alerts. Atkinson Hyperlegible Next on a four-step type scale (small, body, lead, title), scaled by the three Display text sizes.

STORY: The person opens DoseGuard and sees where they are in the day at a glance, the dose due now large and calm with one button, and anything as-needed with a plain "ready now" or "ready at 2:15 PM". The dose steps feel like a short calm walk with one instruction each. Setup sits behind one door.

FIRST VIEWPORT: Sky band (about 330px) holding the top bar (DoseGuard, Today, Ask, Setup, then Display and an isolated red STOP), the dashed sun arc with dose dots at their times, the sun at the current time, and the greeting bottom-left. Below: the due dose as a large white panel with time, name, pills and Take my dose; right column, later today plus a When you need it panel.

FORM: Morning Sky, my first-ranked grounded candidate (pick card), chosen by the user. Raises carried: dangerous actions sit alone with space; one ringed thing per screen; state in form (solid done, dashed waiting, struck cancelled) plus words. seed:17185e91ce25e6fd

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
