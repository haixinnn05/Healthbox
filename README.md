# Healthbox (DoseGuard)

Local smart pillbox app for MakeCU 2026. It confirms fictional medication schedules, reminds when a dose is due,
then guides the enrolled recipient through a guarded workflow:

recipient check → empty tray check → dispense → dose check → recipient recheck → release → collection.

Everything runs on one laptop: a FastAPI server on `127.0.0.1`, SQLite, and one plain HTML/JS page.
Gemini is optional and only explains or suggests; it has no path to the hardware.

## Quick start

```bash
cd ~/doseguard
./run.sh                 # creates .venv on first run, keeps the Mac awake, serves http://127.0.0.1:8000
```

Open http://127.0.0.1:8000 and keep the page open. When a dose is due, the server plays the chime (macOS Glass sound, twice) through the laptop's speakers, so no click is needed and it sounds even with the page in the background. The top bar shows **Reminder sound on**, or warns when the laptop is muted or below 15% volume; click it to hear a test chime (`POST /api/sound/test`). `LAPTOP_SOUND=0` turns this off (then the page asks for one click to enable its own sound), and `LAPTOP_SOUND_FILE` picks another sound.

By default everything is simulated (`HARDWARE_MODE=fake`, `CAMERA_MODE=simulated`), so the whole workflow runs with
no hardware. Simulated readings are labeled SIMULATED everywhere.

### Try it in two minutes
DoseGuard is for one person. **Today** is the everyday screen; everything you do rarely sits behind **Setup**
(My medicines, Change plan, History, Dispenser).

1. **Setup → Change plan**: add your face (tick the agreement), add a medicine. Choose **At set times** and add a time
   2 minutes from now, or **When I need it** with a minimum gap and a daily maximum. Tick "I checked...", confirm the
   time zone, **Confirm my medicines**.
2. **Setup → Dispenser**: tick the inspection box, **Arm dispenser**.
3. When the dose is due, Today shows it large with **Take my dose**; follow the one primary button on each step.
   As-needed medicines sit under **When you need it** with when they are next allowed.
4. At Collect: Setup → Dispenser → simulator → **Open door**, **Take tokens**, then check the empty tray.

Or use **Demo mode** (Setup → Dispenser → Developer and demo mode), then **Remind A + B in 30 s** in the Demo panel.
Demo mode also adds a fictional as-needed "Headache relief".

### Your account (protects Setup)
On first run the page asks for a name and a password (8 to 128 characters). It stays on this laptop: the password
is hashed with scrypt, never logged, never sent anywhere and never shown to the assistant. The password is needed
**only for Setup**: changing medicines, face enrollment, label photos, arming and recovering the dispenser, pill
calibration and demo controls. Taking a dose, reminders, STOP and the assistant never ask for it; the face check
already guards the dose.

- **Signing in and out.** The account is made with a first and last name. You stay signed in until **Sign out**
  (Setup) or a server restart; `SETUP_UNLOCK_MINUTES` > 0 signs out after that many minutes without a Setup change.
  Signed out, the page shows only a due or in-progress dose (which can be taken with the face check), follow-ups
  and the sign-in panel ("Welcome back, Jackson" and the password). Medicines, as-needed requests, history, photos,
  Ask and Setup need sign-in, and the server refuses them with 423 `SIGNED_OUT` (`SIGNED_IN_READS` and
  `SIGNED_IN_POSTS` in `app/account.py`); `GET /api/status` leaves out the plan, other doses and cartridges.
  "Not Jackson?" explains the hand-over below. A forgotten password never blocks a due dose.
- **One person per Healthbox.** The account's name is the only name: the face is enrolled under it, and enrolling
  again ("Retake my face") replaces that person's face and keeps their plan. Every plan belongs to that person.
- **Someone else will use this Healthbox** (Setup → Change plan → Your face): with the password, the current name
  typed out, and both cartridges and the tray emptied, it removes the person's face, account, plan and pending
  reminders, forgets the cartridge counts and arming, and turns demo mode off. Their history and tray photos stay,
  shown in History under "Earlier: <name>", and never count toward the new person's as-needed limits. The welcome
  setup then starts for the new person. `POST /api/account/handover {request_id, password, confirm_name, emptied}`
  (refused during a dose).
- Five wrong passwords within five minutes pause password attempts for one minute.
- **Forgot the password?** With the laptop in front of you, run
  `.venv/bin/python scripts/reset_account.py` and type `RESET`. It removes only the account (name and password);
  medicines, faces, label photos and history are kept. Open the page and make a new account.

API: `GET /api/account` returns `{exists, name, unlocked, unlocked_until, locked_out_until}`, and
`POST /api/account/create {request_id, name, password}`, `/unlock {request_id, password}`, `/lock {request_id}`,
`/password {request_id, current_password, new_password}` return the same fields. A wrong password is 401
`WRONG_PASSWORD` with `attempts_left`, a pause is 429 `TOO_MANY_ATTEMPTS` with `locked_out_until`, and a Setup
route while locked is 423 `SETUP_LOCKED`. A replayed `request_id` never runs again and returns the current state
(`replayed: true`). `GET /api/status` includes `account: {exists, name, unlocked}`. With no account, Setup is open
(the page asks for one first). The guarded routes are listed in `app/account.py` (`SETUP_ROUTES`).

### Suggested times from the label
When a label is read, `POST /api/labels/read` also returns `suggested_schedule`, worked out from the label's
directions by fixed rules (no AI), e.g. "twice daily" → 08:00 and 20:00, "at bedtime" → 21:00, "every 4 to 6 hours
as needed, no more than 4 a day" → as needed, 6 hours apart, at most 4 in 24 hours. Anything unclear gives `null`.
It only pre-fills the form: every entry is still confirmed by the person.

### Simulating failures
Setup → Dispenser → Hardware simulator: double feed, empty feed, wrong pill type, motor stall, lost completion, lost
command, USB unplug, power cycle, forced door. Dose steps (simulated camera): unknown person, two faces, nobody,
touching tokens, hidden markers, blur, and whether a hand-to-mouth movement happens after release.

### Proof, who collected, and taking
- **Tray photos.** Every tray check (empty tray, dose count, collection, cartridge fill) keeps one photo of the tray
  area with each counted pill ringed and tagged (A1, B1...). It shows under "What the camera checked" and in History.
  Only the perspective-corrected tray inside the four markers is stored, never a face, in `data/evidence/`
  (gitignored); the newest `EVIDENCE_KEEP` (400) are kept. With the simulated camera the picture is drawn and
  labelled SIMULATED. `GET /api/evidence/<name>.jpg` serves them.
- **Live view.** On the dose steps the camera preview shows what the camera sees, a few times a second
  (`GET /api/camera/live`): a box around your face, the hand or dish outlined with each counted pill ringed in its
  colour, or the tray outlined with its pills, and a line such as "✓ 1 of 1 blue". It is display only; the step's own
  check takes its own frames and decides.
- **Moves on by itself** (Display → "Move on by itself", on by default): when the live view shows exactly what a step
  needs for 1.2 seconds, the step's check runs without a press. After a failed try it waits for the view to change.
  Unlocking the tray always needs a press.
- **Speaks each step** (Display → "Speak each step", on by default): the laptop says each step, any problem, and
  "It's time for your medicine" when a dose is due, with macOS `say` in English, Spanish or Chinese (Samantha,
  Paulina, Tingting when installed). Like the chime, this needs no click. `LAPTOP_VOICE=0` turns it off.
- **Pill dish.** Print `docs/dish-markers.png` (ids 4-7, about 1.5 cm each) and stick them around a small dark, matte
  dish. Pouring the pills into it and holding it up is the most reliable "show your pills": any pill colour counts,
  and it can never be mistaken for the tray (ids 0-3). The open palm still works as a fallback.
- **Practice and tuning** (Setup → Dispenser): run face, hand-or-dish and tray checks without dispensing, and see
  pass counts, face match scores against `FACE_MATCH_THRESHOLD` with advice, and how often each check passed first
  try in real doses. Practice results are kept apart from doses and can be cleared.
- **Pills in your hand** (optional, **off by default**; Setup → Dispenser → Dose steps, or `HAND_CHECK=1`). Holding several pills up is hard for many people, and the tray was already counted, so it is a choice for supervised use. When on, after release the person first shows the pills on an open palm to the face camera
  (`POST /api/doses/hand`). The palm detector finds the hand and the pills on it are counted by colour, for this
  dose's pill types only (other colours, orange especially, also match skin). It must pass before the tray check,
  unless the person chooses "I can't show them" (`POST /api/doses/hand-skip`, recorded as `HAND_SKIPPED`).
  Orange, beige and white pills are hard to see on skin, and a failed check says so. A moving hand smears small pills into the skin, so palm frames below `HAND_BLUR_THRESHOLD` (8; a moving hand measured 3-6) are not counted and the person is asked to hold still (`HAND_BLURRY`). The hand-to-mouth watch is
  paused while the hand is held up. A photo of the hand region is kept only if no face overlaps it.
  `HAND_CHECK=0` turns the step off.
- **Who emptied the tray.** From release until the collection check, the face camera is sampled a few times a second.
  The result is stored as `COLLECTED_BY`: `RECIPIENT`, `OTHER_PERSON` (a second face, or a stranger seen more than
  you, staying in view for 5 samples in a row, about a second; scattered single frames are detector noise) or
  `NOT_SEEN`. Someone else being in view does not undo the dose (the pills are out); it is recorded and shown.
- **Hand to mouth.** The same watch looks for a palm (MediaPipe palm detector from the OpenCV Zoo, run with
  `cv2.dnn`) near the enrolled person's mouth (YuNet landmarks), after the hand was first seen away from it, in at
  least 2 samples within 2 s, with the face matched. It keeps watching for `ACTION_WATCH_S` (180 s) after collection.
  Stored as `ACTION_OBSERVED` (`ingestion: UNVERIFIED`), separate from the person's own "I took it". It never means
  swallowed. Without `palm_detection_mediapipe_2023feb.onnx` it records `ACTION_UNAVAILABLE`.
- **Follow-up.** `FOLLOWUP_MINUTES` (5) after release, a dose with no "I took it" and no movement seen shows
  "Did you take your ...?" on Today (or "... is still in the tray" if it was never collected), with "Yes, I took it"
  and "Not yet" (`POST /api/doses/followup-later`, asks again in 5 minutes). It stops after 12 hours.
- **Look-alike pill types.** Counting is by colour, so if one type's colour falls in another's range, one pill would
  count as both. Calibration warns, Setup → Dispenser shows it, and with the real tray camera doses can't start
  (`LOOK_ALIKE_PILLS`) until the types are recalibrated with clearly different pills.
- **Filling a cartridge.** Setup → Dispenser → Cartridges → Fill cartridge: compare the bottle with the saved label
  photo, put one pill from it in the tray, and the camera checks it matches that cartridge's pill type
  (`POST /api/cartridges/check-pill`). Only then can the count be saved (`POST /api/cartridges/fill`, within 15
  minutes of a passing check). Both are Setup routes (password).
- **Pill count.** The count drops by the quantity of each finished dispense motion, so it is an estimate. Today shows
  "running low" at 3 or fewer, or less than two days of scheduled doses, and "may be empty" at 0.

### Status and metrics
- The sky always shows reminder sound, the dispenser (ready, not armed, not connected, needs checking, simulated),
  and the camera, including the pose during a dose ("Camera: Show the tray"). Problems turn red; on a phone the
  calm ones shrink to icons.
- **Setup → Dispenser → How it's running** shows two separate measures from the local record: how late reminders
  fired (median and max) and how long each cartridge's dispense cycle takes, with how many finished.
  `GET /api/metrics` returns them as JSON and `/api/export/metrics.csv` as rows. Cycle time runs from the command
  being written to the controller to `CYCLE_DONE`, which proves motion finished, not that a pill fell.
- **Desktop notifications** (Display panel, off by default): a notice when a dose becomes due while the page is in
  the background. Clicking it only brings DoseGuard forward.

## Public demo (hosted)

`PUBLIC_DEMO=1` runs a shared demo on a host such as Railway (`Dockerfile`). It always uses the simulated dispenser and cameras and cannot talk to a real box. Visitors share one built-in guest: accounts, sign-out, hand-over and leaving demo mode are closed. Everything, including uploaded label photos, is wiped every `DEMO_RESET_MINUTES` (30). It needs `PUBLIC_HOST` (or Railway's `RAILWAY_PUBLIC_DOMAIN`). Optional: `GENAI_BACKEND=developer_api`, `GEMINI_API_KEY` and `AI_REQUEST_LIMIT=100`, set in the host's private variables. The AI count restarts when the server restarts. Without `PUBLIC_DEMO`, the app still refuses anything but loopback.

## Look, accessibility and language

The interface is a sky: a band across the top follows the real time of day (dawn, day, dusk, night), today's doses
sit on the sun's arc, and the sun marks now. Below it, the dose due now is the one big thing on the screen.
Red is used only for STOP and real alerts. Every state is in words and in shape (solid done, dashed waiting,
ringed now, struck skipped), never colour alone.

The **Display** button in the top bar holds settings that are remembered on this laptop:

- **Language:** English, Español, 中文 for the everyday screens (Today, Dose steps, top bar, assistant).
  Setup screens keep English detail.
- **Text size:** Standard, Large, Extra large. The whole layout scales.
- **High contrast:** plain black and white with thicker borders, no sky.
- **Simple view:** shows only today's medicine and the dose steps; Setup is hidden.

Also built in: Atkinson Hyperlegible (a typeface designed for low vision), a **Read aloud** button on the due
dose and on every step, plain-language instructions, icons and words on every status (never colour alone),
48 px touch targets, a skip link, visible keyboard focus, screen-reader announcements when a dose is due,
and reduced motion. Text and status colours meet WCAG AA contrast, including on the night sky.

## Configuration

Copy `.env.example` to `.env`. Main switches:

| Variable | Values |
|---|---|
| `HARDWARE_MODE` | `fake` (default) or `serial` |
| `SERIAL_PORT` | e.g. `/dev/cu.usbserial-0001` (`ls /dev/cu.*`) |
| `CAMERA_MODE` | `simulated`, `single_laptop`, `dual_camera` |
| `TRAY_BYPASS` | `1` = testing only: real face check, tray results from the simulator. Refused with `HARDWARE_MODE=serial`. |
| `TRAY_CAMERA_URL` | ESP32-CAM snapshot URL (dual_camera) |
| `GENAI_BACKEND` | `off`, `developer_api` (AI Studio key), `google_cloud` |
| `GEMINI_MODEL`, `GEMINI_API_KEY` | default `gemini-3.5-flash` (older 2.5 models are closed to new keys); check the ID in AI Studio |
| `GEMINI_THINKING` | `off` (default, about 1 s per request), `low`, `default` (newer models can take a minute when they think) |
| `SETUP_UNLOCK_MINUTES` | how long Setup stays unlocked after the last Setup change (default 15) |

### Real camera
```bash
.venv/bin/python scripts/download_models.py      # YuNet, SFace and the palm detector from the OpenCV Zoo
.venv/bin/python scripts/make_tray_markers.py    # docs/tray-markers.png, print and stick at tray corners
CAMERA_MODE=single_laptop ./run.sh
```
macOS asks for camera permission for the terminal app the first time. Re-enroll the recipient with the real camera
(simulated enrollments don't carry an embedding). Calibrate `FACE_MATCH_THRESHOLD` with a few positive and negative
tries, and the token HSV ranges for your lighting.

The live preview uses a continuous local stream (up to 24 fps) instead of polling snapshots.
Positioning hints run detection only; enrollment and identity checks take priority over hints.
Preview streams pause when their image is off-screen or the browser tab is hidden. Camera
checks still require distinct fresh frames over the configured observation window.

### Tray camera (ESP32-CAM inside the box, `CAMERA_MODE=dual_camera`)
The laptop camera checks the person; an ESP32-CAM inside the box, above the tray, checks the dose. The sketch keeps
the board's white LED on, dim and steady (`LIGHT_DUTY`), because the box is dark and counting is by colour. With this
camera the tray-empty check runs straight after the hand check, so "Show pills" is the last of 6 steps
(`AUTO_TRAY_AFTER_HAND`, on by default only in `dual_camera`). Flash
`firmware/doseguard_tray_cam` (see `firmware/README.md`), then check it and start the app:
```bash
.venv/bin/python scripts/tray_cam_check.py --url http://doseguard-tray.local/snapshot --tray
CAMERA_MODE=dual_camera TRAY_CAMERA_URL=http://doseguard-tray.local/snapshot ./run.sh
```
WiFi loss blocks the physical check (never silently falls back); reminders keep working.

### Real hardware
Give `docs/serial-protocol.md` to the firmware owner. The reference sketch in `firmware/` compiles for
`esp32:esp32:esp32` (core 3.3.12, ArduinoJson 7) but has not run on a board yet.

1. **Bench check** with the app closed (one owner per port). It moves both rotors and the latch:
   ```bash
   .venv/bin/python scripts/bench_check.py --port /dev/cu.usbserial-XXXX
   .venv/bin/python scripts/bench_check.py --port ... --trials 20 --channel A --ask   # then --channel B
   ```
   Conformance prints PASS/FAIL per rule. Trials write a CSV to `data/bench/` with cycle times and,
   with `--ask`, how many tokens actually fell.
2. **Run the app:** `HARDWARE_MODE=serial SERIAL_PORT=... ./run.sh`. Setup → Dispenser shows session,
   firmware, heartbeat, switches, revision and faults.

### Serial mode without hardware
`scripts/virtual_controller.py` puts the simulated controller behind a virtual serial port, so the
app's real serial code (and the bench check) can run before the board exists:
```bash
.venv/bin/python scripts/virtual_controller.py        # prints the port; type 'door open', 'feed A double', ...
HARDWARE_MODE=serial SERIAL_PORT=/dev/ttysNNN ./run.sh
```

## Tests

```bash
.venv/bin/python -m pytest -q
node --test tests/test_camera_ui.cjs  # preview lifecycle checks; no packages needed
```
`tests/test_serial.py` runs the real serial adapter and the bench check over a virtual port.
They cover reminders (due, 3 sounds, snooze, dismiss, needs review, restart without duplicates, late start, plan edits,
DST), idempotent requests, and the full workflow against the fake controller: happy path, wrong person, double feed,
wrong class, occlusion, door opening, release once, reminder-only never dispensing, stall, USB loss, STOP, lost
completion, combined A+B, and the assistant having no write path.
`tests/test_as_needed.py` covers as-needed entries: validation, the guarded workflow on request, duplicate
request IDs, too soon, the rolling 24 h limit, cancel-before-dispense not counting, uncertain outcomes counting,
reminder-only never touching hardware, stale plans, scheduled behaviour unchanged, and the old-database migration.
`tests/test_account.py` covers the account: create, unlock, lock, wrong passwords and the pause, password change,
every Setup route locked and unlocked, everyday actions never needing the password, sliding expiry, restart locking,
replays, the reset script, and the password and hash never appearing in responses, events or exports.
`tests/test_label_schedule.py` covers the label-directions parser, including unclear text giving no suggestion.
`tests/test_taking.py` covers tray photos (served, pruned, never kept without markers, removed on demo reset), who
emptied the tray, the hand-to-mouth rules (away first, two samples, stale mouth, strangers, missing model), the
follow-up and "Not yet", look-alike pill types blocking a start, and cartridge fill checks and the pill count.

### As-needed medicine
A plan entry is `"schedule": "SCHEDULED"` (default, recurring times) or `"AS_NEEDED"` (no times, plus
`min_interval_hours` 1-24 and `max_per_day` 1-8). As-needed entries never create reminders.
`POST /api/doses/request {request_id, entry_key, plan_version}` checks the limits on the server, then either starts
the normal guarded workflow (Device) or records an unverified "I took it" (Reminder only). A dose counts toward the
limits whenever medicine may have been delivered; only a verification cancelled before dispensing does not.
`GET /api/status` lists each entry's availability under `as_needed`. Demo mode adds "Headache relief (fictional)"
(cartridge B, 4 h apart, at most 3 in 24 h).

## Layout

```
app/main.py        routes, lifespan, token/origin checks (loopback only)
app/workflow.py    rules engine: the only owner of movement commands
app/reminders.py   scheduler: due, snooze, dismiss, review, restart recovery; never moves hardware
app/hardware.py    serial adapter + simulated controller, protocol v3
app/camera.py      one owner per camera, frame ids, freshness
app/vision.py      face (YuNet/SFace) and tray (ArUco + colour contours) observations
app/store.py       SQLite schema, the authoritative record
app/plans.py       plan validation and versioned confirmation
app/as_needed.py   as-needed limits (minimum interval, rolling 24 h maximum); reading only
app/account.py     local account: scrypt password, Setup unlock (in memory), wrong-password pause, guarded routes
app/labels.py      label directions to suggested times (fixed rules, no AI)
app/ai.py          Gemini adapter (label extraction, assistant). No hardware or plan writes
app/context.py     approved help, redacted facts, offline answers
app/status.py      the snapshot the page polls every 500 ms
app/telemetry.py   read-only metrics: reminder delivery delay, dispense cycle duration
app/virtual_controller.py  simulated controller on a virtual serial port (tests, rehearsal)
scripts/           bench_check.py (controller conformance + feed trials), tray_cam_check.py (ESP32-CAM),
                   virtual_controller.py, reset_account.py (forgotten password), models, markers
firmware/          controller sketch (ESP32) and tray camera sketch (ESP32-CAM)
static/            the page
docs/              protocol for the firmware, tray markers
```

## Safety limits (say these in the demo)
- Face matching here is a prototype check, not secure identity. Photo spoofing is untested.
- Token colour does not establish real medication identity or strength. Use non-ingestible demo tokens; nobody puts them in their mouth.
- Released, Collection observed, Hand-to-mouth seen and User reported taken are separate. DoseGuard never verifies swallowing.
- "Hand to mouth seen" is a palm near the mouth of the matched face. It can be fooled (scratching your chin) and it is never proof of taking.
- After STOP, a fault or an unknown outcome, nothing replays. A person inspects and runs supervised recovery.
