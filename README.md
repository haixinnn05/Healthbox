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
1. **Setup**: enroll a teammate (tick consent), add an entry, add a time 2 minutes from now, tick "I reviewed this entry",
   confirm the timezone, **Confirm plan**.
2. **Device**: tick the enclosure inspection box, **Arm device**.
3. Wait for the due card on **Today**, then **Start verification** and follow the one primary button on each step.
4. At Collection: Device → simulator → **Open door**, **Take tokens**, then **Check collection**.

Or use **Demo mode** (Device → Developer → Turn on demo mode), then Today → **Trigger A + B reminder in 30 s**.

### Simulating failures
Device → Hardware simulator: double feed, empty feed, wrong token class, motor stall, lost completion, lost command,
USB unplug, power cycle, forced door. Verification page (simulated camera): unknown person, two faces, nobody,
touching tokens, hidden markers, blur.

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

### Real hardware
Give `docs/serial-protocol.md` to the firmware owner. Then `HARDWARE_MODE=serial SERIAL_PORT=... ./run.sh`.
The Device page shows session, heartbeat, switches, revision and faults.

## Tests

```bash
.venv/bin/python -m pytest -q
```
They cover reminders (due, 3 sounds, snooze, dismiss, needs review, restart without duplicates, late start, plan edits,
DST), idempotent requests, and the full workflow against the fake controller: happy path, wrong person, double feed,
wrong class, occlusion, door opening, release once, reminder-only never dispensing, stall, USB loss, STOP, lost
completion, combined A+B, and the assistant having no write path.

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
app/ai.py          Gemini adapter (label extraction, assistant). No hardware or plan writes
app/context.py     approved help, redacted facts, offline answers
app/status.py      the snapshot the page polls every 500 ms
static/            the page
docs/              protocol for the firmware, tray markers
```

## Safety limits (say these in the demo)
- Face matching here is a prototype check, not secure identity. Photo spoofing is untested.
- Token colour does not establish real medication identity or strength. Use non-ingestible demo tokens; nobody puts them in their mouth.
- Released, Collection observed and User reported taken are separate. DoseGuard never verifies swallowing.
- After STOP, a fault or an unknown outcome, nothing replays. A person inspects and runs supervised recovery.
