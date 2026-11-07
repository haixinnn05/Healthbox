# DoseGuard

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

Open http://127.0.0.1:8000 and click **Reminders muted · Enable sound** once. Keep the page open.

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

### Simulating failures
Setup → Dispenser → Hardware simulator: double feed, empty feed, wrong pill type, motor stall, lost completion, lost
command, USB unplug, power cycle, forced door. Dose steps (simulated camera): unknown person, two faces, nobody,
touching tokens, hidden markers, blur.

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
| `TRAY_CAMERA_URL` | ESP32-CAM snapshot URL (dual_camera) |
| `GENAI_BACKEND` | `off`, `developer_api` (AI Studio key), `google_cloud` |
| `GEMINI_MODEL`, `GEMINI_API_KEY` | check the model ID in AI Studio first |

### Real camera
```bash
.venv/bin/python scripts/download_models.py      # YuNet + SFace from the OpenCV Zoo
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

### Tray camera (ESP32-CAM, `CAMERA_MODE=dual_camera`)
The laptop camera checks the person; an ESP32-CAM above the tray checks the dose. Flash
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
app/ai.py          Gemini adapter (label extraction, assistant). No hardware or plan writes
app/context.py     approved help, redacted facts, offline answers
app/status.py      the snapshot the page polls every 500 ms
app/virtual_controller.py  simulated controller on a virtual serial port (tests, rehearsal)
scripts/           bench_check.py (controller conformance + feed trials), tray_cam_check.py (ESP32-CAM),
                   virtual_controller.py, models, markers
firmware/          controller sketch (ESP32) and tray camera sketch (ESP32-CAM)
static/            the page
docs/              protocol for the firmware, tray markers
```

## Safety limits (say these in the demo)
- Face matching here is a prototype check, not secure identity. Photo spoofing is untested.
- Token colour does not establish real medication identity or strength. Use non-ingestible demo tokens; nobody puts them in their mouth.
- Released, Collection observed and User reported taken are separate. DoseGuard never verifies swallowing.
- After STOP, a fault or an unknown outcome, nothing replays. A person inspects and runs supervised recovery.
