/*
  DoseGuard controller reference firmware, protocol v3 (see docs/serial-protocol.md).
  Board: classic ESP32 (ESP-WROOM-32), Arduino-ESP32 core 3.x. Library: ArduinoJson 7.x.

  STATUS: starting point for the hardware owner. Written to match the app's simulated
  controller, compiles (arduino-cli, core 3.3.12) but NOT bench-tested yet. Verify pins, steps per pocket, servo angles
  and switch polarity on the real build before trusting it.

  Design rules it follows:
  - Non-blocking stepping (micros()), so STOP, serial and the watchdog run during motion.
  - Every door edge and every pocket of rotor motion increments the tray revision.
  - RELEASE only when motors idle, door closed, rev matches, no fault, once per transaction.
  - No PING for 1 s during motion -> stop and FAULT HEARTBEAT_LOST.
  - hA/hB report "position known" (homed since boot/RESET, no interrupted motion), not the raw
    switch: after one pocket the rotor rests off its home switch. Once per full revolution the
    switch must be pressed again, otherwise FAULT HOME_MISMATCH.
  - Never repeats a feed by itself. RESET reboots into a new unarmed session.
*/
#include <Arduino.h>
#include <ArduinoJson.h>

// ---------------------------------------------------------------- pins (verify on your board)
const uint8_t MOTOR_PINS[2][4] = {{18, 19, 21, 22}, {25, 26, 27, 32}};  // A, B -> ULN2003 IN1..IN4
const uint8_t PIN_SERVO = 23;
const uint8_t PIN_HOME[2] = {33, 34};  // external 10k pull-ups to 3.3 V; switch to GND when at home
const uint8_t PIN_DOOR = 35;           // external pull-up; switch closed (LOW) when the door is closed
const uint8_t PIN_LED_OK = 13, PIN_LED_FAULT = 14;
const char* FW_VERSION = "doseguard-0.2";

// ---------------------------------------------------------------- calibration (per channel)
long STEPS_PER_POCKET[2] = {2048, 2048};  // 180 degrees per dispense: 4096 half-steps per rev / 2 pockets
// 28BYJ-48 half-step interval; slower = more torque. Bench-measured: 12 V clean at 600 us, stalls at 500;
// 5 V clean at 850, buzzes at 800. Chosen: 850 (raise it if a loaded rotor skips).
const uint32_t STEP_INTERVAL_US = 850;
const uint32_t POCKET_TIMEOUT_MS = 4000;
const int POCKETS_PER_REV[2] = {2, 2};  // pockets between home-switch presses; 0 disables the check
const long HOMING_MAX_STEPS = 4096 + 512;
const int SERVO_LOCK_US = 1000, SERVO_UNLOCK_US = 2000;  // pulse widths; check latch travel
const uint32_t WATCHDOG_MS = 1000, DEBOUNCE_MS = 30;

const int8_t HALF_STEP[8][4] = {{1,0,0,0},{1,1,0,0},{0,1,0,0},{0,1,1,0},{0,0,1,0},{0,0,1,1},{0,0,0,1},{1,0,0,1}};

// ---------------------------------------------------------------- state
enum MotorState { IDLE, MOVING, HOMING, MFAULT };
const char* MOTOR_NAMES[] = {"idle", "moving", "homing", "fault"};

struct Channel {
  MotorState state = IDLE;
  long stepsLeft = 0, stepsInPocket = 0;
  int phase = 0;
  uint32_t lastStepUs = 0, pocketStartMs = 0;
  long cmdId = -1;
  bool home = false;   // raw switch (debounce not needed: only sampled at rest points)
  bool homed = false;  // position known: what the host sees as hA/hB
  int pocketIdx = 0;   // pockets since the home switch
} ch[2];

char session[5];
long rev = 0;
String fault = "";        // empty = none
String txn = "";
bool servoUnlocked = false;
bool doorClosed = false, doorRaw = false;
uint32_t doorChangedMs = 0;
uint32_t lastPingMs = 0;
String releasedTxns[8]; int releasedIdx = 0;
struct Seen { long id; uint32_t hash; } seen[16]; int seenIdx = 0;
char lineBuf[257]; size_t lineLen = 0;

// ---------------------------------------------------------------- helpers
uint32_t fnv(const String& s) { uint32_t h = 2166136261u; for (size_t i = 0; i < s.length(); i++) { h ^= (uint8_t)s[i]; h *= 16777619u; } return h; }

void send(JsonDocument& doc) {
  doc["v"] = 3; doc["s"] = session;
  char out[257];
  size_t n = serializeJson(doc, out, sizeof(out) - 1);
  if (n == 0 || n > 255) return;  // never exceed 256 bytes including newline
  Serial.write(out, n); Serial.write('\n');
}

void addStatus(JsonDocument& d) {
  d["mA"] = MOTOR_NAMES[ch[0].state]; d["mB"] = MOTOR_NAMES[ch[1].state];
  d["hA"] = ch[0].homed ? 1 : 0; d["hB"] = ch[1].homed ? 1 : 0;
  d["door"] = doorClosed ? 1 : 0; d["servo"] = servoUnlocked ? "U" : "L"; d["rev"] = rev;
  if (txn.length()) d["txn"] = txn; else d["txn"] = nullptr;
  if (fault.length()) d["fault"] = fault; else d["fault"] = nullptr;
}

void event(const char* ev, long id = -1) { JsonDocument d; d["event"] = ev; if (id >= 0) d["id"] = id; send(d); }
void nack(long id, const char* reason) { JsonDocument d; d["event"] = "NACK"; if (id >= 0) d["id"] = id; d["reason"] = reason; send(d); }

void coilsOff(int c) { for (int i = 0; i < 4; i++) digitalWrite(MOTOR_PINS[c][i], LOW); }
void stepOnce(int c) {
  ch[c].phase = (ch[c].phase + 1) & 7;
  for (int i = 0; i < 4; i++) digitalWrite(MOTOR_PINS[c][i], HALF_STEP[ch[c].phase][i]);
}

void servoWriteUs(int us) { ledcWrite(PIN_SERVO, (uint32_t)((65535UL * us) / 20000UL)); }
void setServo(bool unlocked) { servoUnlocked = unlocked; servoWriteUs(unlocked ? SERVO_UNLOCK_US : SERVO_LOCK_US); }

void raiseFault(const char* reason, int c = -1, long id = -1) {
  for (int i = 0; i < 2; i++) {
    if (ch[i].state == MOVING || ch[i].state == HOMING) { ch[i].state = (i == c) ? MFAULT : IDLE; ch[i].homed = false; }
    coilsOff(i);
  }
  fault = reason;
  JsonDocument d; d["event"] = "FAULT"; d["reason"] = reason;
  if (c >= 0) d["c"] = c == 0 ? "A" : "B";
  if (id >= 0) d["id"] = id;
  send(d);
}

// ---------------------------------------------------------------- commands
void cmdDispense(long id, JsonDocument& m) {
  const char* cs = m["c"] | "";
  int c = strcmp(cs, "A") == 0 ? 0 : strcmp(cs, "B") == 0 ? 1 : -1;
  int n = m["n"] | 0;
  if (c < 0) return nack(id, "UNKNOWN_CHANNEL");
  if (n < 1 || n > 3) return nack(id, "BAD_QUANTITY");
  if (fault.length()) return nack(id, "FAULT");
  if (ch[c].state != IDLE || !ch[c].homed) return nack(id, "NOT_READY");
  txn = String((const char*)(m["t"] | ""));
  event("ACK", id);
  ch[c].state = MOVING; ch[c].cmdId = id;
  ch[c].stepsLeft = STEPS_PER_POCKET[c] * n; ch[c].stepsInPocket = 0;
  ch[c].pocketStartMs = millis();
}

void cmdRelease(long id, JsonDocument& m) {
  String t = String((const char*)(m["t"] | ""));
  long r = m["rev"] | -1L;
  if (fault.length()) return nack(id, "FAULT");
  if (ch[0].state != IDLE || ch[1].state != IDLE) return nack(id, "MOTOR_BUSY");
  if (!doorClosed) return nack(id, "DOOR_OPEN");
  if (r != rev) return nack(id, "BAD_REV");
  for (auto& x : releasedTxns) if (x.length() && x == t) return nack(id, "ALREADY_RELEASED");
  if (millis() - lastPingMs > WATCHDOG_MS) return nack(id, "HEARTBEAT_LOST");
  event("ACK", id);
  releasedTxns[releasedIdx] = t; releasedIdx = (releasedIdx + 1) % 8;
  setServo(true);
  JsonDocument d; d["event"] = "RELEASED"; d["id"] = id; d["t"] = t; d["rev"] = rev; d["servo"] = "U"; send(d);
}

void cmdLock(long id) {
  if (!doorClosed) return nack(id, "DOOR_OPEN");
  event("ACK", id);
  setServo(false); txn = "";
  JsonDocument d; d["event"] = "LOCKED"; d["id"] = id; d["servo"] = "L"; send(d);
}

void cmdStop(long id) {
  for (int i = 0; i < 2; i++) {
    if (ch[i].state == MOVING || ch[i].state == HOMING) ch[i].homed = false;  // stopped mid-motion: position unknown
    ch[i].state = IDLE; ch[i].stepsLeft = 0; coilsOff(i);
  }
  fault = "STOPPED";
  if (id >= 0) event("ACK", id);
  JsonDocument d; d["event"] = "STOPPED"; if (id >= 0) d["id"] = id; else d["id"] = nullptr; addStatus(d); send(d);
}

void handleLine(const char* line) {
  JsonDocument m;
  if (deserializeJson(m, line)) return;
  if ((m["v"] | 0) != 3) { nack(m["id"] | -1L, "BAD_VERSION"); return; }
  const char* cmd = m["cmd"] | "";
  if (strcmp(cmd, "PING") == 0) {
    lastPingMs = millis();
    JsonDocument d; d["event"] = "PONG"; addStatus(d); send(d);
    return;
  }
  if (strcmp(m["s"] | "", session) != 0) { nack(m["id"] | -1L, "BAD_SESSION"); return; }
  if (strcmp(cmd, "STOP") == 0) { cmdStop(m["id"] | -1L); return; }
  if (strcmp(cmd, "STATUS") == 0) { JsonDocument d; d["event"] = "STATUS"; addStatus(d); d["fw"] = FW_VERSION; send(d); return; }
  if (!m["id"].is<long>()) { nack(-1, "MISSING_ID"); return; }
  long id = m["id"];
  // Duplicate IDs: same payload -> ACK dup (no repeat); different payload -> NACK.
  String key = String(cmd) + "|" + String((const char*)(m["t"] | "")) + "|" + String((const char*)(m["c"] | "")) +
               "|" + String((int)(m["n"] | 0)) + "|" + String((long)(m["rev"] | -1L));
  uint32_t h = fnv(key);
  for (auto& s : seen) if (s.id == id) {
    if (s.hash == h) { JsonDocument d; d["event"] = "ACK"; d["id"] = id; d["dup"] = 1; send(d); }
    else nack(id, "ID_REUSED");
    return;
  }
  seen[seenIdx] = {id, h}; seenIdx = (seenIdx + 1) % 16;
  if (strcmp(cmd, "DISPENSE") == 0) cmdDispense(id, m);
  else if (strcmp(cmd, "RELEASE") == 0) cmdRelease(id, m);
  else if (strcmp(cmd, "LOCK") == 0) cmdLock(id);
  else if (strcmp(cmd, "RESET") == 0) { event("ACK", id); Serial.flush(); delay(50); ESP.restart(); }
  else nack(id, "UNKNOWN_CMD");
}

// ---------------------------------------------------------------- periodic work
void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') { lineBuf[lineLen] = 0; if (lineLen) handleLine(lineBuf); lineLen = 0; }
    else if (c != '\r') { if (lineLen < 256) lineBuf[lineLen++] = c; else lineLen = 0; /* drop oversized */ }
  }
}

void readSwitches() {
  for (int i = 0; i < 2; i++) ch[i].home = digitalRead(PIN_HOME[i]) == LOW;
  bool raw = digitalRead(PIN_DOOR) == LOW;
  if (raw != doorRaw) { doorRaw = raw; doorChangedMs = millis(); }
  if (raw != doorClosed && millis() - doorChangedMs > DEBOUNCE_MS) {
    doorClosed = raw; rev++;
    JsonDocument d; d["event"] = "SWITCH"; d["door"] = doorClosed ? 1 : 0;
    d["hA"] = ch[0].homed ? 1 : 0; d["hB"] = ch[1].homed ? 1 : 0; d["rev"] = rev; send(d);
  }
}

void runMotors() {
  uint32_t nowUs = micros();
  for (int c = 0; c < 2; c++) {
    Channel& k = ch[c];
    if (k.state != MOVING && k.state != HOMING) continue;
    if (nowUs - k.lastStepUs < STEP_INTERVAL_US) continue;
    k.lastStepUs = nowUs;
    if (k.state == HOMING) {
      if (k.home) { k.state = IDLE; k.homed = true; k.pocketIdx = 0; coilsOff(c); continue; }
      if (--k.stepsLeft <= 0) { raiseFault("HOMING_TIMEOUT", c); continue; }
      stepOnce(c);
      continue;
    }
    stepOnce(c);
    k.stepsLeft--; k.stepsInPocket++;
    if (k.stepsInPocket >= STEPS_PER_POCKET[c]) {  // one pocket delivered (motion only)
      k.stepsInPocket = 0; rev++; k.pocketStartMs = millis();
      if (POCKETS_PER_REV[c] > 0) {
        k.pocketIdx = (k.pocketIdx + 1) % POCKETS_PER_REV[c];
        // A full revolution must land on the home switch again; if not, steps were skipped or the rotor slipped.
        if (k.pocketIdx == 0 && !k.home) { raiseFault("HOME_MISMATCH", c, k.cmdId); continue; }
      }
    } else if (millis() - k.pocketStartMs > POCKET_TIMEOUT_MS) {
      raiseFault("MOTION_TIMEOUT", c, k.cmdId); continue;
    }
    if (k.stepsLeft <= 0) {
      k.state = IDLE; coilsOff(c);
      JsonDocument d; d["event"] = "CYCLE_DONE"; d["id"] = k.cmdId; d["c"] = c == 0 ? "A" : "B";
      addStatus(d); d["t"] = txn;  // status fields ride along so the host sees idle at once
      send(d);
    }
  }
}

void watchdog() {
  bool moving = ch[0].state == MOVING || ch[1].state == MOVING || ch[0].state == HOMING || ch[1].state == HOMING;
  if (moving && millis() - lastPingMs > WATCHDOG_MS) raiseFault("HEARTBEAT_LOST");
  digitalWrite(PIN_LED_FAULT, fault.length() ? HIGH : LOW);
  digitalWrite(PIN_LED_OK, (!fault.length() && millis() - lastPingMs < WATCHDOG_MS) ? HIGH : LOW);
}

// ---------------------------------------------------------------- setup / loop
void setup() {
  Serial.begin(115200);
  for (int c = 0; c < 2; c++) { for (int i = 0; i < 4; i++) pinMode(MOTOR_PINS[c][i], OUTPUT); coilsOff(c); pinMode(PIN_HOME[c], INPUT); }
  pinMode(PIN_DOOR, INPUT);
  pinMode(PIN_LED_OK, OUTPUT); pinMode(PIN_LED_FAULT, OUTPUT);
  ledcAttach(PIN_SERVO, 50, 16);  // core 3.x API; on core 2.x use ledcSetup + ledcAttachPin
  setServo(false);
  snprintf(session, sizeof(session), "%04lx", (unsigned long)(esp_random() & 0xFFFF));
  doorRaw = doorClosed = digitalRead(PIN_DOOR) == LOW;
  lastPingMs = millis();
  for (int c = 0; c < 2; c++) {  // rehome each rotor separately at boot
    ch[c].home = digitalRead(PIN_HOME[c]) == LOW;
    if (ch[c].home) { ch[c].homed = true; ch[c].pocketIdx = 0; }
    else { ch[c].state = HOMING; ch[c].stepsLeft = HOMING_MAX_STEPS; }
  }
  JsonDocument d; d["event"] = "HELLO"; d["fw"] = FW_VERSION; JsonArray a = d["ch"].to<JsonArray>(); a.add("A"); a.add("B"); send(d);
}

void loop() {
  readSerial();     // STOP is handled here, between steps
  readSwitches();
  runMotors();
  watchdog();
}
