# Firmware (reference)

Two boards, two sketches. Neither has run on real hardware yet; both compile with arduino-cli (core 3.3.12).

| Sketch | Board | Job |
|---|---|---|
| `doseguard_controller/` | ESP-WROOM-32 (`esp32:esp32:esp32`) | motors, latch servo, switches; USB serial protocol v3 |
| `doseguard_tray_cam/` | AI Thinker ESP32-CAM (`esp32:esp32:esp32cam`) | fresh tray snapshots over WiFi for `CAMERA_MODE=dual_camera` |

## Controller

`doseguard_controller/doseguard_controller.ino` is a **starting point** for the hardware owner. It implements
protocol v3 from `docs/serial-protocol.md` the same way the app's simulated controller does.

It **compiles** (7 Nov 2026, arduino-cli, `esp32:esp32:esp32` core 3.3.12, ArduinoJson 7, no warnings, 22% flash)
but has **not run on a board**. Compile it yourself with:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32 firmware/doseguard_controller
arduino-cli upload  --fqbn esp32:esp32:esp32 -p /dev/cu.usbserial-XXXX firmware/doseguard_controller
```

Before trusting it:

1. Arduino IDE: board "ESP32 Dev Module" (Arduino-ESP32 core 3.x), install the **ArduinoJson 7** library.
   On core 2.x, replace `ledcAttach` with `ledcSetup` + `ledcAttachPin`.
2. Confirm the GPIO map on your exact board. GPIO 34/35 are input-only and need external pull-ups.
3. Calibrate `STEPS_PER_POCKET` separately for A and B with tokens loaded, and set `POCKETS_PER_REV` to the
   number of pockets between home-switch presses (0 turns the once-per-revolution `HOME_MISMATCH` check off).
   `hA`/`hB` mean "position known", not "switch pressed", so a rotor resting between pockets can still dispense.
4. Check switch polarity (the sketch assumes switch closed = LOW) and servo pulse widths for your latch.
5. Run `scripts/bench_check.py` (see `docs/serial-protocol.md`) before connecting the app.
6. Bring-up order (from the design doc): A unloaded, A home/feed, B home/feed, servo + door,
   then serial duplicate/STOP/watchdog checks against the app with `HARDWARE_MODE=serial`.

Quick serial check without the app (Arduino Serial Monitor, newline ending):
```
{"v":3,"cmd":"PING"}
```
should answer with a `PONG` line that includes the session `s`. Use that `s` in the next commands.

## Tray camera (ESP32-CAM)

`doseguard_tray_cam/doseguard_tray_cam.ino` serves `GET /snapshot`: a JPEG captured after the request arrived
(any frame already buffered is thrown away), with `X-Boot-Id`, `X-Frame-Seq` and `X-Capture-Age-Ms` headers.
`GET /status` returns JSON health (boot, frames served, stale frames discarded, WiFi signal, memory).
It has no connection to the motors or the latch: pictures only.

It **compiles** (`esp32:esp32:esp32cam`, no extra libraries, no warnings, 33% flash) but has **not run on a board**.

1. Copy `doseguard_tray_cam/wifi_secrets.example.h` to `wifi_secrets.h` (gitignored) and set the network.
   Use the network the laptop will be on at the demo; a phone hotspot is the safest choice.
2. Flash it. The ESP32-CAM has no USB port of its own: use an ESP32-CAM-MB adapter or a 3.3 V USB-serial
   programmer with GPIO0 tied to GND while it resets.
   ```bash
   arduino-cli compile --fqbn esp32:esp32:esp32cam firmware/doseguard_tray_cam
   arduino-cli upload  --fqbn esp32:esp32:esp32cam -p /dev/cu.usbserial-XXXX firmware/doseguard_tray_cam
   ```
3. Open the serial monitor at 115200: it prints its address, e.g. `http://192.168.1.42/snapshot`
   (also `http://doseguard-tray.local/snapshot`).
4. Mount it above the tray, print `docs/tray-markers.png`, stick the four markers at the tray corners, then check:
   ```bash
   .venv/bin/python scripts/tray_cam_check.py --url http://doseguard-tray.local/snapshot --tray
   ```
   It fetches frames exactly as the app does and reports freshness, frame order, repeats, frame rate,
   size, and whether all four markers are visible and sharp. The last frame is saved in `data/bench/`.
5. Run the app: `CAMERA_MODE=dual_camera TRAY_CAMERA_URL=http://doseguard-tray.local/snapshot ./run.sh`.

Power it from a solid 5 V supply (it browns out on weak USB ports when WiFi and the camera start together).
If the image is soft, the lens can be turned gently to refocus; diffuse light over the tray helps colour counts.
