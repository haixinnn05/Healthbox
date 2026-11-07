---
name: DoseGuard
description: A local smart pillbox whose day is drawn as a sky, with each dose sitting on the sun's arc.
colors:
  ground: "#f4f8fc"
  cloud: "#fcfdff"
  cloud-inset: "#eef5fc"
  ink: "#16304f"
  ink-deep: "#0f2540"
  ink-2: "#33547a"
  ink-3: "#4f6b8c"
  line: "#d9e5f2"
  line-2: "#b7cce3"
  sky-dawn-1: "#a9cbea"
  sky-dawn-2: "#d6e8f7"
  sky-day-1: "#8ec2ee"
  sky-day-2: "#c9e3f8"
  sky-dusk-1: "#93a9dd"
  sky-dusk-2: "#cbd6f1"
  sky-night-1: "#142a4a"
  sky-night-2: "#2b4775"
  sky-ink-2: "#2a4b70"
  sky-dusk-ink-2: "#263f69"
  sky-night-ink-2: "#d3e0f0"
  sun-light: "#fff1c9"
  sun: "#ffd27a"
  sun-deep: "#e9a93a"
  alert: "#b42318"
  alert-bg: "#fdeceb"
  alert-line: "#f3c4bf"
  stop: "#c8322b"
  stop-hover: "#ad261f"
  focus: "#1f5fae"
typography:
  title:
    fontFamily: "Atkinson Hyperlegible Next, system-ui, -apple-system, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans, sans-serif"
    fontSize: "2.25rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.02em"
  lead:
    fontFamily: "Atkinson Hyperlegible Next, system-ui, -apple-system, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans, sans-serif"
    fontSize: "1.3rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Atkinson Hyperlegible Next, system-ui, -apple-system, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  small:
    fontFamily: "Atkinson Hyperlegible Next, system-ui, -apple-system, Segoe UI, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Noto Sans, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  control: "14px"
  inset: "18px"
  panel: "24px"
  panel-due: "28px"
  sky-band: "32px"
  pill: "999px"
spacing:
  gutter: "clamp(16px, 4vw, 56px)"
  row: "0.75rem"
  stack: "1.25rem"
  panel-pad: "clamp(1.25rem, 2.4vw, 2rem)"
  panel-due-pad: "clamp(1.5rem, 3vw, 2.5rem)"
  target: "48px"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.cloud}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0.65rem 1.25rem"
    height: "48px"
  button-primary-hover:
    backgroundColor: "{colors.ink-deep}"
  button-big:
    typography: "{typography.lead}"
    padding: "0.9rem 2rem"
    height: "60px"
  button-secondary:
    backgroundColor: "{colors.cloud}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.65rem 1.25rem"
    height: "48px"
  button-secondary-hover:
    backgroundColor: "{colors.cloud-inset}"
  button-quiet:
    textColor: "{colors.ink-2}"
    rounded: "{rounded.control}"
    padding: "0.65rem 0.8rem"
    height: "48px"
  button-stop:
    backgroundColor: "{colors.stop}"
    textColor: "#ffffff"
    rounded: "{rounded.pill}"
    padding: "0.55rem 1.5rem"
    height: "52px"
  button-stop-hover:
    backgroundColor: "{colors.stop-hover}"
  tab:
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.55rem 1.05rem"
    height: "48px"
  tag:
    backgroundColor: "{colors.cloud-inset}"
    textColor: "{colors.ink-2}"
    typography: "{typography.small}"
    rounded: "{rounded.pill}"
    padding: "0.25rem 0.75rem"
  panel:
    backgroundColor: "{colors.cloud}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "{spacing.panel-pad}"
  panel-due:
    backgroundColor: "{colors.cloud}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel-due}"
    padding: "{spacing.panel-due-pad}"
  inset:
    backgroundColor: "{colors.cloud-inset}"
    rounded: "{rounded.inset}"
    padding: "1rem 1.15rem"
  input:
    backgroundColor: "{colors.cloud}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.55rem 0.85rem"
    height: "48px"
  alert-box:
    backgroundColor: "{colors.alert-bg}"
    textColor: "{colors.ink}"
    rounded: "{rounded.inset}"
    padding: "1rem 1.2rem"
---

# Design System: DoseGuard

## Overview

**Creative North Star: "Morning Sky"**

The day is a sky. A gradient band across the top of every screen follows the real hour, the sun crosses a dashed arc from 5 AM to midnight, and each of today's doses sits on that arc at its own time. Everything below the sky is cloud-white panels on a pale sky ground, set in one navy ink. The world is calm, kind and plain: one obvious action per screen, large type, generous space, and nothing that needs decoding.

State is never carried by colour alone. Every dose state has a form as well as words: a solid mark is done, a dashed mark is waiting, a ring means act now, a strike means skipped. The only warm colour is the sun, and it means "now". Red is kept back for the STOP button and for real alerts, so that when it appears it is believed.

The world replaces the retired pharmacy-label look (labels, stickers, identical white pill-app cards). It is rendered with soft neutral elevation, rounded cloud panels and a single typeface, Atkinson Hyperlegible Next, self-hosted.

**Key Characteristics:**
- A sky band keyed to the real hour (dawn, day, dusk, night) holds the top bar, the sun's arc and the greeting.
- Cloud-white panels (24px corners; the due dose 28px) on a pale sky ground, with soft navy-tinted shadows.
- One ink navy for type and primary actions; the warm sun only for "now"; red only for STOP and real alerts.
- State in form plus words: solid done, dashed waiting, ringed act now, struck skipped.
- One typeface on a four-step scale that grows with the user's Display text size.
- A plain black-on-white high-contrast mode with no sky.

## Colors

A cool, airy palette of sky blues and cloud whites, held together by one deep navy ink, with a single warm sun and a reserved red.

### Primary
- **Ink Navy** (ink): all type, primary buttons, the selected wizard step, checked switches, done marks and the walked part of a progress track. Its darker hover (ink-deep) is used only on the primary button hover.

### Secondary
- **Sky Phases** (sky-dawn, sky-day, sky-dusk, sky-night; each a top stop -1 and a lower stop -2): the sky band gradient, set by `html[data-sky]` from the real hour (night before 5 AM and from 9 PM, dawn before 9 AM, day before 5 PM, dusk after). The band fades into the ground colour at its foot, except at night, when it stays a deep navy band and its type turns to the ground colour with a pale blue secondary (sky-night-ink-2). Type on the band uses the sky ink pair (ink plus sky-ink-2, or sky-dusk-ink-2 at dusk).

### Tertiary
- **Warm Sun** (sun, with sun-light at the disc's highlight and sun-deep at its rim): the sun on the arc, the brand mark (the same disc), and only things that are now: the due-now chip, "ready now", the live dot on the Dose steps tab, the breathing wait indicator, the current step on the track, the dose dot due now, and the ring around the one action to take now (sun-deep). At night the sun becomes a pale moon disc.

### Neutral
- **Pale Sky Ground** (ground): the page under the panels; the sky band dissolves into it.
- **Cloud White** (cloud): panels, inputs, secondary buttons, popovers, the assistant.
- **Quiet Cloud** (cloud-inset): inset surfaces inside panels (pill chips, notes, blockers, stats, segmented-control wells), and the hover of secondary and quiet buttons.
- **Second Ink** (ink-2, 7:1 on cloud): secondary text, quiet button text, ledes.
- **Third Ink** (ink-3, 5:1 on cloud): tertiary text, help, quantities, empty states.
- **Hairline** (line) and **Field Line** (line-2): row dividers, and input borders, switch tracks and unreached track dots.
- **Focus Blue** (focus): the 3px focus outline, focused input borders, links, and the dashed border of a value the app suggested.

### Alert and STOP
- **Alert Red** (alert, with alert-bg and alert-line): missed or uncertain doses, failed checks, field errors, bad toasts. Always paired with an icon and words.
- **STOP Red** (stop, stop-hover): the STOP button only.

### High Contrast

Plain black on white, no sky. All surfaces turn white and all inks black; lines turn black; alert deepens to #a00000; shadows are removed; focus turns black. Panels, the due panel, the sky band's foot, tabs and every non-quiet button take a 2px black border. The sun becomes a white disc with a 3px black border. Quiet actions are underlined so they still read as actions without a border.

### Named Rules
**The Sun Means Now Rule.** The warm sun palette appears only on the sun itself and on whatever is happening now. A thing that is not current never turns warm.

**The Red Is Believed Rule.** Red is STOP and real alerts, nothing else. A destructive but ordinary action (the danger button) is drawn in ink, not red; the build recoloured it for exactly this reason.

**The Pills Are Data Rule.** A medicine's colour swatch shows the real pill's colour from its label. It is data, not palette, and is exempt from the colour jobs above.

## Typography

**Display Font:** Atkinson Hyperlegible Next (self-hosted woff2, weights 400 and 700, latin and latin-ext), with system-ui and CJK system fallbacks for Chinese.
**Body Font:** the same family.

**Character:** One hyperlegible family for everything, chosen for low vision and second-language readers. Hierarchy comes from size and weight (400 and 700 only), never from a second face.

### Hierarchy
- **Title** (700, 2.25rem, line-height 1.2, -0.02em): page headings, the greeting in the sky, the name of the dose due now, the calm "nothing due" heading.
- **Lead** (700 for headings and labels, 400 for spoken instructions; 1.3rem): panel headings, the due time, the big button label, pill chips in the due panel, page ledes, and the one instruction on each dose step (400, line-height 1.35, max 32ch).
- **Body** (400 or 700, 1rem, line-height 1.5): running text and controls; paragraphs cap at 68ch.
- **Small** (0.875rem): help, sub-lines, tags, arc time labels, track step names.

Numbers that change (times, counts) use tabular figures. Icons come from the self-hosted Phosphor icon font at two glyph sizes: icon (1.3rem) inside buttons, tabs and notices, and icon-lg (1.7rem) inside status orbs and the simulated-camera glyph. Icons always sit beside words or carry an accessible name.

### Named Rules
**The Four Steps Rule.** There are four type steps (small, body, lead, title), all in rem. The user's Display text size scales the root (112.5%, 125%, 143.75%), so every step and icon grows with it. Do not add steps or set type in px.

**Decision record: the "three size steps" wording.** The direction contract's OWN-WORLD line originally said "three size steps". After the first finish review it was reworded to "a four-step type scale (small, body, lead, title), scaled by the three Display text sizes", because the original line was ambiguous: "three" referred to the Display setting, not to the type scale. The reviewer accepted the rewording and asked that it be recorded here as a decision rather than carried silently.

## Layout

Content sits in a centred column up to 1320px plus side gutters (clamp(16px, 4vw, 56px); 16px under 760px). The sky band runs full width with its bottom corners rounded (32px); its top bar, notices and scene share the same column.

Today is a two-column grid: the due dose (flexible) beside a right column of 20 to 26rem holding "later today" and "when you need it". Dose steps use a 1.35 : 1 split (instruction panel beside camera and counts). At 1080px and below every two-column grid collapses to one column.

At 760px and below the top bar becomes a grid: brand, Display and STOP on the first row, tabs scrolling horizontally on the second; the arc moves above the greeting at 9.5rem tall; big buttons go full width; dense list rows reflow to two columns.

Rhythm: stacked panels and grid gaps 1.25rem; row gaps 0.75rem; panel padding clamp(1.25rem, 2.4vw, 2rem); the due and step panels clamp(1.5rem, 3vw, 2.5rem). Every target is at least 48px (44px for in-panel secondary controls such as segmented buttons and summaries); STOP is 52px and the big action 60px.

### The Sun's Arc
The arc is a dashed half-ellipse drawn in sky ink at 35% opacity; the part already walked is solid. It runs from 5 AM at the left foot to midnight at the right, with hour labels at 6 AM, noon and 6 PM. The sun sits at the current time. Doses at the same minute share one point and the strongest state wins.

- A dose due at the sun is represented by the sun itself: its own dot is hidden and its time label sits under the sun.
- Any other dose that would touch the sun is moved along the arc, in real pixels after layout, until it clears the sun by 3.9rem (disc, glow, dot and a visible gap). Past doses move back along the arc, future doses forward. This re-runs on resize.
- Labels sit above the curve near noon and inside it on the steep sides, so the stroke never crosses text.
- On narrow screens only the dose at the sun keeps its time label; the list below the sky names every time.

## Elevation & Depth

Soft, ambient, navy-tinted elevation. Panels float slightly above the pale ground; the dose due now floats a little higher. Shadows never carry meaning on their own and disappear entirely in high contrast.

### Shadow Vocabulary
- **Rest** (`box-shadow: 0 1px 2px rgba(22, 48, 79, .05), 0 10px 28px rgba(22, 48, 79, .07)`): every panel, and the selected segment or sub-tab lifting out of its well.
- **Lift** (`box-shadow: 0 2px 4px rgba(22, 48, 79, .06), 0 16px 36px rgba(22, 48, 79, .10)`): the due panel, the dose-step panel, the Display popover, toasts.
- **Overlay** (`box-shadow: 0 4px 12px rgba(22, 48, 79, .10), 0 24px 60px rgba(22, 48, 79, .18)`): the Ask assistant side panel only.

### Named Rules
**The Soft Cloud Rule.** Elevation is soft and blurred, tinted with the ink navy. No hard offset shadows.

## Shapes

Rounded, cloud-like forms with a strict ladder of corners: panels 24px, the due panel and dose-step panel 28px, inset surfaces inside panels 18px, controls (buttons, inputs, tabs, list rows) 14px, and chips, tags, notices and STOP a full pill. The sky band rounds only its bottom corners (32px). Dots, marks, swatches, the sun and status orbs are circles.

### State in Form
Every dose state has a shape as well as words, wherever it appears (arc dots, list marks, step track, wizard numbers):
- **Solid** (filled ink): done.
- **Dashed** outline: waiting, not yet. Dashed also marks what is not yet real or confirmed: the demo-mode tag, simulated readings, and a value the app suggested for the user to confirm.
- **Ringed** (3px sun-deep ring, 6px out, following the control's corners): act now. Only the one primary action per screen carries the ring.
- **Struck** (a diagonal stroke through the mark, line-through on the name): skipped.
- **Alert**: a missed or uncertain dose uses the alert colour (alert border on alert-bg), with words.

### Named Rules
**The One Ring Rule.** One ringed action per screen, and it is the primary action. If a screen has two ringed things, one of them is wrong.

## Components

### Buttons
- **Shape:** gently rounded controls (14px), 2px border, bold label, icon optional at 1.3rem.
- **Primary:** ink fill with cloud text; hover deepens to ink-deep. The big variant (60px, lead type) is the one action of a screen and carries the ring.
- **Secondary:** cloud fill, 2px ink border, ink text; hover to quiet cloud.
- **Quiet:** no border or fill, ink-2 text; hover gains a quiet cloud fill. Used for "remind me later", "read aloud" and other side actions.
- **Danger:** an ordinary destructive action in ink, quiet or bordered; never red.
- **Press:** buttons settle 1px on press; disabled is 45% opacity.
- **Focus:** a 3px focus-blue outline offset 3px on every focusable element.

### STOP
A full red pill (52px tall, letter-spaced bold label, hand icon), always visible at the right end of the top bar with clear space on its left (1.75rem) and nothing everyday beside it.

### Tabs and Top Bar
Tabs sit directly on the sky in sky ink, 48px tall, 14px corners. Hover washes them with translucent cloud; the current tab is a solid cloud tile with ink text. A small ink count pill (alert red when a dose is missed) and a sun-deep live dot ride inside tabs. Notices under the bar are cloud pills on the sky.

### Chips and Tags
Full-pill quiet cloud chips with bold ink-2 small text for tags; time chips carry a round remove button. Pill chips in the due panel are 18px insets at lead size with the pill's colour swatch.

### Cards / Containers
- **Corner Style:** panels 24px; the due and step panels 28px; insets 18px.
- **Background:** cloud on the ground; insets in quiet cloud.
- **Shadow Strategy:** Rest for panels, Lift for the due panel (see Elevation & Depth).
- **Border:** none, except in high contrast.
- **Internal Padding:** clamp(1.25rem, 2.4vw, 2rem); due panel clamp(1.5rem, 3vw, 2.5rem).

### Inputs / Fields
- **Style:** cloud fill, 2px field-line border, 14px corners, 48px tall, bold label above with small ink-3 help.
- **Focus:** border turns focus blue with a 3px 25% focus-blue halo.
- **Suggested:** dashed focus-blue border until the user confirms.
- **Error:** bold small alert text under the field.
- **Choices:** 18px bordered option tiles; the chosen one gets an ink border. Switches are pill tracks that fill ink when on. Segmented controls are a quiet-cloud well with the pressed option lifted as a cloud tile.

### Dose Step Track
Six steps on a line: unreached steps are dashed circles on a field-line rule, done steps solid ink, the current step a larger sun disc ringed in ink. Under 760px the step names hide and only the dots remain.

### Alert Box
An 18px inset in alert-bg with an alert-line border and an alert-coloured icon; the headline stays in ink, then what to do next in ink-2.

### Ask Assistant
A floating side panel (24px corners, overlay shadow) whose head fades from the sky into cloud. Questions are ink bubbles on the right, answers quiet-cloud bubbles on the left, each with one tighter corner toward the speaker.

## Do's and Don'ts

### Do:
- **Do** key the sky band to the real hour through `html[data-sky]` (dawn, day, dusk, night) and keep type on the band in the sky ink pair.
- **Do** give every state a form and words: solid done, dashed waiting, ringed act now, struck skipped, alert colour for missed or uncertain.
- **Do** ring exactly one action per screen, the primary one.
- **Do** keep the corner ladder: panels 24px (due panel 28px), insets 18px, controls 14px, chips and STOP full pill.
- **Do** set all type in the four rem steps (small .875rem, body 1rem, lead 1.3rem, title 2.25rem) and icons at 1.3rem or 1.7rem, so the Display text sizes scale everything.
- **Do** keep targets at 48px or more and STOP always visible and isolated.
- **Do** let the sun stand for a dose due at the sun, and move any other dose clear of it along the arc rather than overlapping.
- **Do** keep high contrast plain: black on white, no sky, 2px borders, underlined quiet actions.

### Don't:
- **Don't** use red for anything but STOP and real alerts; draw ordinary destructive actions in ink.
- **Don't** use the warm sun colours for anything that is not the sun or happening now.
- **Don't** carry state by colour alone.
- **Don't** add a second typeface, a fifth type step, or px font sizes.
- **Don't** use hard offset shadows; elevation is soft and navy-tinted.
- **Don't** return to label-and-sticker chrome or a list of identical white pill cards.
