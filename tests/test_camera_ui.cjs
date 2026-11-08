const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");

// Exercise the actual plain-JS camera lifecycle without requiring a DOM framework.
const app = readFileSync(require("node:path").join(__dirname, "../static/app.js"), "utf8");
const source = app.slice(app.indexOf("function cameraStream("), app.indexOf("/* ---------------------------------------------------------------- wiring */"));
function fixture() {
  const makeImage = () => ({
    dataset: {}, isConnected: true, assignments: 0,
    classList: {
      classes: new Set(),
      toggle(name, enabled) { if (enabled) this.classes.add(name); else this.classes.delete(name); },
      contains(name) { return this.classes.has(name); },
    },
    set src(value) { this.url = value; this.assignments++; },
    hasAttribute() { return !!this.url; },
    removeAttribute() { this.url = null; },
  });
  const S = { view: "setup", status: { camera: { simulated: false, mode: "dual_camera" }, workflow: { txn: null } },
    cameraImg: makeImage(), labelImg: makeImage(), capturingFor: null };
  const document = { hidden: false, addEventListener() {} };
  const context = { S, document, window: { addEventListener() {} }, setTimeout() {} };
  runInNewContext(source, context);
  return { S, document, ...context };
}

test("a visible preview maintains one stream and reconnects only after failure", () => {
  const { S, cameraLoop } = fixture();
  for (let i = 0; i < 20; i++) cameraLoop();
  assert.equal(S.cameraImg.assignments, 1);
  assert.equal(S.cameraImg.url, "/api/camera/stream?role=face");
  assert.equal(S.cameraImg.classList.contains("camera-mirrored"), true);
  S.cameraImg.onerror();
  cameraLoop();
  assert.equal(S.cameraImg.assignments, 2);
});

test("streams stop when hidden, detached or away from camera screens", () => {
  const { S, document, cameraLoop } = fixture();
  cameraLoop();
  document.hidden = true;
  cameraLoop();
  assert.equal(S.cameraImg.url, null);
  document.hidden = false;
  S.cameraImg.isConnected = false;
  cameraLoop();
  assert.equal(S.cameraImg.url, null);
  S.cameraImg.isConnected = true;
  cameraLoop();
  S.view = "today";
  cameraLoop();
  assert.equal(S.cameraImg.url, null);
});

test("verification switches to tray, while setup and label capture always use face", () => {
  const { S, cameraLoop } = fixture();
  S.status.workflow.txn = { pose: "TRAY" };
  S.view = "verify";
  cameraLoop();
  assert.equal(S.cameraImg.url, "/api/camera/stream?role=tray");
  assert.equal(S.cameraImg.classList.contains("camera-mirrored"), true);
  S.view = "setup";
  S.capturingFor = 0;
  cameraLoop();
  assert.equal(S.cameraImg.url, "/api/camera/stream?role=face");
  assert.equal(S.labelImg.url, "/api/camera/stream?role=face");
  assert.equal(S.cameraImg.classList.contains("camera-mirrored"), true);
  assert.equal(S.labelImg.classList.contains("camera-mirrored"), false);
});

test("tray and face views both mirror without restarting the stream", () => {
  const { S, cameraLoop } = fixture();
  S.status.camera.mode = "single_laptop";
  S.view = "verify";
  S.status.workflow.txn = { pose: "FACE" };
  cameraLoop();
  assert.equal(S.cameraImg.classList.contains("camera-mirrored"), true);
  S.status.workflow.txn.pose = "TRAY";
  cameraLoop();
  assert.equal(S.cameraImg.classList.contains("camera-mirrored"), true);
  assert.equal(S.cameraImg.assignments, 1);
});
