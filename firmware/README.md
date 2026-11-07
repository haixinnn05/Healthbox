# Controller firmware (reference)

`doseguard_controller/doseguard_controller.ino` is a **starting point** for the hardware owner. It implements
protocol v3 from `docs/serial-protocol.md` the same way the app's simulated controller does.

It has **not been compiled or bench-tested**. Before trusting it:

1. Arduino IDE: board "ESP32 Dev Module" (Arduino-ESP32 core 3.x), install the **ArduinoJson 7** library.
   On core 2.x, replace `ledcAttach` with `ledcSetup` + `ledcAttachPin`.
2. Confirm the GPIO map on your exact board. GPIO 34/35 are input-only and need external pull-ups.
3. Calibrate `STEPS_PER_POCKET` separately for A and B with tokens loaded.
4. Check switch polarity (the sketch assumes switch closed = LOW) and servo pulse widths for your latch.
5. Bring-up order (from the design doc): A unloaded, A home/feed, B home/feed, servo + door,
   then serial duplicate/STOP/watchdog checks against the app with `HARDWARE_MODE=serial`.

Quick serial check without the app (Arduino Serial Monitor, newline ending):
```
{"v":3,"cmd":"PING"}
```
should answer with a `PONG` line that includes the session `s`. Use that `s` in the next commands.
