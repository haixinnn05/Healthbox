/*
  Healthbox stepper bench test (ESP32 + ULN2003 + 28BYJ-48). Standalone: no app, no switches needed.
  Same pins as the controller sketch: motor A IN1..IN4 = GPIO 18, 19, 21, 22; motor B = 25, 26, 27, 32.

  Wiring:  ULN2003 IN1-IN4 -> the GPIOs above.  ULN2003 "+" -> 5 V supply for the motor (not the ESP32 3.3 V pin).
           ULN2003 "-" -> GND, and that GND must also connect to an ESP32 GND pin.

  Serial Monitor at 115200, line ending "Newline". Type:
    a / b      pick motor A or B
    f          one full turn forward (4096 half-steps)
    r          one full turn backward
    p          one pocket forward (POCKET steps, default 683; calibrate it)
    s 500      step 500 half-steps (negative = backward)
    v 1200     step interval in microseconds (smaller = faster). Measured: this motor buzzes at 800 on 5 V.
               Below ~600 only makes sense on a 12 V motor; a 5 V motor must never get 12 V.
    x          stop and release the coils (motor goes cool, can be turned by hand)
    ?          show settings
*/
#include <Arduino.h>

const uint8_t PINS[2][4] = {{18, 19, 21, 22}, {25, 26, 27, 32}};
const int8_t HALF_STEP[8][4] = {{1,0,0,0},{1,1,0,0},{0,1,0,0},{0,1,1,0},{0,0,1,0},{0,0,1,1},{0,0,0,1},{1,0,0,1}};
const long STEPS_PER_REV = 4096;  // 28BYJ-48 in half-step mode (about 4076 measured on many units)

int motor = 0;
int phase[2] = {0, 0};
long position[2] = {0, 0};
uint32_t intervalUs = 820;
long pocketSteps = 683;

void release(int m) { for (int i = 0; i < 4; i++) digitalWrite(PINS[m][i], LOW); }

void stepOnce(int m, int dir) {
  phase[m] = (phase[m] + (dir > 0 ? 1 : 7)) & 7;
  for (int i = 0; i < 4; i++) digitalWrite(PINS[m][i], HALF_STEP[phase[m]][i]);
  position[m] += dir;
}

// Blocking is fine for a bench test; any key typed during a move stops it.
void move(long steps) {
  int dir = steps >= 0 ? 1 : -1;
  long n = labs(steps);
  Serial.printf("Motor %c: %ld half-steps %s at %lu us/step...\n", 'A' + motor, n, dir > 0 ? "forward" : "backward", (unsigned long)intervalUs);
  uint32_t t0 = millis();
  for (long i = 0; i < n; i++) {
    if (Serial.available()) { Serial.println("Stopped by keypress."); break; }
    stepOnce(motor, dir);
    delayMicroseconds(intervalUs);
  }
  release(motor);
  Serial.printf("Done in %lu ms. Motor %c position: %ld\n", (unsigned long)(millis() - t0), 'A' + motor, position[motor]);
}

void help() {
  Serial.printf("Motor %c selected | interval %lu us | pocket %ld steps | positions A=%ld B=%ld\n",
                'A' + motor, (unsigned long)intervalUs, pocketSteps, position[0], position[1]);
  Serial.println("Commands: a, b, f, r, p, s <steps>, v <us>, k <pocket steps>, x, ?");
}

void setup() {
  Serial.begin(115200);
  for (int m = 0; m < 2; m++) { for (int i = 0; i < 4; i++) pinMode(PINS[m][i], OUTPUT); release(m); }
  delay(300);
  Serial.println("\nHealthbox stepper test ready.");
  help();
}

void loop() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (!line.length()) return;
  char c = line.charAt(0);
  long arg = line.length() > 1 ? line.substring(1).toInt() : 0;
  switch (c) {
    case 'a': case 'A': motor = 0; help(); break;
    case 'b': case 'B': motor = 1; help(); break;
    case 'f': move(STEPS_PER_REV); break;
    case 'r': move(-STEPS_PER_REV); break;
    case 'p': move(pocketSteps); break;
    case 's': if (arg) move(arg); else Serial.println("Give a step count, e.g. s 500"); break;
    case 'v': if (arg >= 300 && arg <= 20000) { intervalUs = arg; help(); } else Serial.println("Use 300..20000 us"); break;
    case 'k': if (arg > 0 && arg < 20000) { pocketSteps = arg; help(); } else Serial.println("Give pocket steps, e.g. k 683"); break;
    case 'x': release(0); release(1); Serial.println("Coils released."); break;
    default: help();
  }
}
