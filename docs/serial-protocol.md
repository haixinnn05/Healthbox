# DoseGuard controller protocol, version 3

This is what the ESP-WROOM-32 firmware must speak to work with the app. The app's
simulated controller (`app/hardware.py`, `FakeController`) does exactly this, so it
works as a reference implementation.

## Transport

- USB serial (UART0), 115200 baud, 8N1.
- One JSON object per line, ending in `\n`. At most 256 bytes per line, newline included.
- Every message carries `"v":3`. If the controller gets another version, it answers `NACK` with reason `BAD_VERSION`.
  If the app gets another version, it treats the device as disconnected.
- `s` is the **controller session**: 4 random hex characters picked at every boot or reset.
  Controller messages always include it. Host commands (all except `PING`) must echo it.
  A wrong `s` gets `NACK` with `BAD_SESSION`. A new session means the app disarms and invalidates
  everything it inspected before.

## Host to controller

| cmd | fields | meaning |
|---|---|---|
| `PING` | (no `id`) | Heartbeat, sent every 250 ms. Answer with `PONG`. |
| `STATUS` | `id` | Answer with `STATUS`. |
| `DISPENSE` | `id`, `t`, `c` (`"A"`/`"B"`), `n` (1..3) | Turn channel `c`'s rotor through `n` calibrated pocket cycles. |
| `RELEASE` | `id`, `t`, `rev` | Unlock the shared tray latch (servo), only if every check below passes. |
| `LOCK` | `id` | Command the latch locked. Door must be closed. |
| `STOP` | optional `id` | Highest priority. Stop all motion at once and enter fault `STOPPED`. |
| `RESET` | `id` | Supervised recovery: clear the fault, rehome both rotors, lock the latch, start a **new session** (send `HELLO`). |

`t` is a short transaction tag (6 hex characters). `id` is an integer that is unique for the session.

Examples:
```
{"v":3,"s":"a73f","id":10,"cmd":"DISPENSE","t":"demo01","c":"A","n":1}
{"v":3,"s":"a73f","id":12,"cmd":"RELEASE","t":"demo01","rev":5}
```

### Duplicate IDs

- Same `id` with an identical payload: reply `ACK` with `"dup":1`. Do **not** repeat the action.
- Same `id` with a different payload: `NACK` with `ID_REUSED`.

## Controller to host

| event | fields | meaning |
|---|---|---|
| `HELLO` | `fw`, `ch` | Sent at boot and after `RESET`. New `s`. |
| `PONG` | status fields (below) | Heartbeat reply. Doubles as live status. |
| `STATUS` | status fields | Reply to `STATUS`. |
| `ACK` | `id` | Command **accepted**. Not finished. |
| `NACK` | `id`, `reason` | Rejected; nothing moved. Reasons: `BAD_VERSION`, `BAD_SESSION`, `MISSING_ID`, `ID_REUSED`, `UNKNOWN_CMD`, `UNKNOWN_CHANNEL`, `BAD_QUANTITY`, `FAULT`, `NOT_READY`, `MOTOR_BUSY`, `DOOR_OPEN`, `BAD_REV`, `ALREADY_RELEASED`. |
| `CYCLE_DONE` | `id`, `t`, `c`, `rev` | The calibrated motion finished. This does **not** mean a token fell. |
| `RELEASED` | `id`, `t`, `rev`, `servo:"U"` | The latch was commanded open. |
| `LOCKED` | `id`, `servo:"L"` | The latch was commanded locked. |
| `STOPPED` | `id` (may be null) | STOP was handled. |
| `FAULT` | `reason`, optional `c`, `id` | For example `MOTOR_STALL`, `HOMING_TIMEOUT`, `HEARTBEAT_LOST`. |
| `SWITCH` | `door`, `hA`, `hB`, `rev` | A debounced switch edge. |

Status fields (compact, so the line stays under 256 bytes):
```
{"v":3,"s":"a73f","event":"PONG","mA":"idle","mB":"idle","hA":1,"hB":1,"door":1,"servo":"L","rev":4,"txn":null,"fault":null}
```
- `mA`/`mB`: `idle`, `moving`, `homing` or `fault`.
- `hA`/`hB`: home switch for each rotor (1 = at home).
- `door`: access door switch (1 = closed).
- `servo`: **commanded** latch state, `L` or `U`. No feedback switch is fitted, so this is not a measurement.
- `rev`: tray revision. Add 1 on **every** door edge (open or close) and every rotor motion step that could move a token.
- There is no lid sensor; the tray top is fixed. Never send a lid field.

## Rules the firmware must enforce

1. **Before RELEASE**, all must hold: both motors idle, door closed, `rev` in the command equals the current `rev`,
   no fault, heartbeat healthy, and this `t` has not been released before. Otherwise `NACK`. Release at most once per `t`.
2. **Watchdog:** if no `PING` arrives for 1 second, stop motion and raise `FAULT HEARTBEAT_LOST`. Recovery needs `RESET`.
3. **STOP** is handled even in the middle of a step sequence. The stepping must be non-blocking (no long `delay()`).
4. Debounce switches. Bound homing and motion time; a timeout is a fault, not a retry.
5. Never repeat a timed-out feed by yourself. Uncertain motion blocks release until rehoming and a new inspection.
6. After `RESET`, rehome each rotor separately and start unarmed (new `s`).
7. Steps per pocket are calibrated separately for A and B. Do not assume one constant.

## Suggested pin map (classic ESP-WROOM-32, verify on your board)

| Function | GPIO |
|---|---|
| Motor A IN1..IN4 | 18, 19, 21, 22 |
| Motor B IN1..IN4 | 25, 26, 27, 32 |
| Latch servo | 23 |
| Home A switch | 33 (external pull-up to 3.3 V) |
| Home B switch | 34 (input only, external pull-up) |
| Door switch | 35 (input only, external pull-up) |
| Status LEDs | 13, 14 (with resistors) |

UART0 stays reserved for USB. 3.3 V logic only. Motors and servo run from their own regulated supply with a common ground.

## Tray camera (ESP32-CAM, dual_camera mode)

`GET <TRAY_CAMERA_URL>` returns a fresh JPEG with these headers. Without them the app rejects the frame:

- `X-Boot-Id`: random per boot
- `X-Frame-Seq`: increasing integer
- `X-Capture-Age-Ms`: milliseconds between capture and response

Capture on request (discard any buffered frame first). The app rejects repeated sequence numbers and frames older than 500 ms.

## Testing the firmware against the app

1. `HARDWARE_MODE=serial SERIAL_PORT=/dev/cu.usbserial-XXXX ./run.sh`
2. Device page: Connection should say Connected, the session and firmware should show, heartbeat under 300 ms.
3. Unplug USB: within 1 s the app shows Disconnected. Plug back in: a new session, unarmed.
4. Run a dose with an empty cartridge: dose check must fail; release stays locked.
