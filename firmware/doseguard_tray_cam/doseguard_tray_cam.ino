/*
  DoseGuard tray camera firmware (ESP32-CAM, AI Thinker board), for CAMERA_MODE=dual_camera.
  Board: "AI Thinker ESP32-CAM" (esp32:esp32:esp32cam), Arduino-ESP32 core 3.x. No extra libraries.

  What the app needs (docs/serial-protocol.md, "Tray camera"):
    GET /snapshot  ->  200 image/jpeg captured AFTER the request arrived, with headers
      X-Boot-Id         random per boot
      X-Frame-Seq       increases with every frame served
      X-Capture-Age-Ms  milliseconds between capture and sending the response
    The app rejects frames older than 500 ms, repeated sequence numbers and replies slower than 1.5 s.
    Receiving a JPEG now does not prove it was captured now, so this firmware never serves a
    frame that was already buffered when the request came in.

  The camera supplies pictures only. It has no link to the motors or the latch.

  Setup: copy wifi_secrets.example.h to wifi_secrets.h (gitignored) and fill in the network.
  Then in the app:  CAMERA_MODE=dual_camera TRAY_CAMERA_URL=http://doseguard-tray.local/snapshot ./run.sh
  Check it first:   .venv/bin/python scripts/tray_cam_check.py --url http://doseguard-tray.local/snapshot

  STATUS: compiles with arduino-cli (core 3.x); NOT tested on a board yet.
*/
#include <Arduino.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include "esp_camera.h"
#include "esp_http_server.h"
#include "esp_timer.h"

#if __has_include("wifi_secrets.h")
#include "wifi_secrets.h"
#else
#error "Copy wifi_secrets.example.h to wifi_secrets.h and set WIFI_SSID and WIFI_PASSWORD."
#endif

// ---------------------------------------------------------------- settings
const char* HOSTNAME = "doseguard-tray";      // http://doseguard-tray.local/snapshot
const framesize_t FRAME_SIZE = FRAMESIZE_VGA; // 640x480: enough for the corner markers and pill outlines
const int JPEG_QUALITY = 12;                  // 10-63, lower = better quality and bigger files
const int MAX_STALE_DISCARDS = 4;             // buffered frames thrown away per request at most
const char* FW_VERSION = "doseguard-tray-0.1";

// ---------------------------------------------------------------- AI Thinker pin map
#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y9_GPIO_NUM 35
#define Y8_GPIO_NUM 34
#define Y7_GPIO_NUM 39
#define Y6_GPIO_NUM 36
#define Y5_GPIO_NUM 21
#define Y4_GPIO_NUM 19
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 5
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

// ---------------------------------------------------------------- state
char bootId[9];
uint32_t frameSeq = 0;
uint32_t served = 0, rejectedStale = 0, captureFailures = 0;
httpd_handle_t server = nullptr;

static int64_t captureUs(const camera_fb_t* fb) {
  // The camera driver stamps each frame with esp_timer time when it was taken.
  return (int64_t)fb->timestamp.tv_sec * 1000000LL + fb->timestamp.tv_usec;
}

// ---------------------------------------------------------------- handlers
static esp_err_t snapshotHandler(httpd_req_t* req) {
  const int64_t requestUs = esp_timer_get_time();
  camera_fb_t* fb = nullptr;
  // Throw away anything captured before the request arrived; only a newer frame is proof of "now".
  for (int i = 0; i <= MAX_STALE_DISCARDS; i++) {
    fb = esp_camera_fb_get();
    if (!fb) break;
    if (captureUs(fb) >= requestUs) break;
    esp_camera_fb_return(fb);
    fb = nullptr;
    rejectedStale++;
  }
  if (!fb) {
    captureFailures++;
    httpd_resp_set_status(req, "503 Service Unavailable");
    httpd_resp_set_type(req, "text/plain");
    httpd_resp_set_hdr(req, "Cache-Control", "no-store");
    return httpd_resp_send(req, "No fresh frame. Check the camera ribbon and power.", HTTPD_RESP_USE_STRLEN);
  }
  frameSeq++;
  char seq[12], age[12], w[8], h[8];
  snprintf(seq, sizeof(seq), "%lu", (unsigned long)frameSeq);
  snprintf(age, sizeof(age), "%lld", (long long)((esp_timer_get_time() - captureUs(fb)) / 1000));
  snprintf(w, sizeof(w), "%u", (unsigned)fb->width);
  snprintf(h, sizeof(h), "%u", (unsigned)fb->height);
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  httpd_resp_set_hdr(req, "X-Boot-Id", bootId);
  httpd_resp_set_hdr(req, "X-Frame-Seq", seq);
  httpd_resp_set_hdr(req, "X-Capture-Age-Ms", age);
  httpd_resp_set_hdr(req, "X-Frame-Width", w);
  httpd_resp_set_hdr(req, "X-Frame-Height", h);
  esp_err_t res = httpd_resp_send(req, (const char*)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  if (res == ESP_OK) served++;
  return res;
}

static esp_err_t statusHandler(httpd_req_t* req) {
  char body[320];
  snprintf(body, sizeof(body),
           "{\"fw\":\"%s\",\"boot\":\"%s\",\"seq\":%lu,\"served\":%lu,\"stale_discarded\":%lu,"
           "\"capture_failures\":%lu,\"rssi\":%d,\"heap\":%u,\"psram\":%u,\"uptime_ms\":%llu}",
           FW_VERSION, bootId, (unsigned long)frameSeq, (unsigned long)served, (unsigned long)rejectedStale,
           (unsigned long)captureFailures, (int)WiFi.RSSI(), (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getFreePsram(),
           (unsigned long long)(esp_timer_get_time() / 1000));
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  return httpd_resp_send(req, body, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t rootHandler(httpd_req_t* req) {
  httpd_resp_set_type(req, "text/plain");
  return httpd_resp_send(req, "DoseGuard tray camera. GET /snapshot for a fresh JPEG, /status for health.\n",
                         HTTPD_RESP_USE_STRLEN);
}

// ---------------------------------------------------------------- setup
static bool startCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM; c.pin_d2 = Y4_GPIO_NUM; c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM; c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM; c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAME_SIZE;
  c.jpeg_quality = JPEG_QUALITY;
  c.grab_mode = CAMERA_GRAB_LATEST;  // newest frame first; stale ones are still discarded above
  if (psramFound()) { c.fb_count = 2; c.fb_location = CAMERA_FB_IN_PSRAM; }
  else { c.fb_count = 1; c.fb_location = CAMERA_FB_IN_DRAM; c.frame_size = FRAMESIZE_QVGA; }
  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) { Serial.printf("Camera init failed: 0x%x\n", err); return false; }
  sensor_t* s = esp_camera_sensor_get();
  s->set_whitebal(s, 1);
  s->set_exposure_ctrl(s, 1);
  // Let auto exposure and white balance settle before anyone asks for a frame.
  for (int i = 0; i < 8; i++) { camera_fb_t* fb = esp_camera_fb_get(); if (fb) esp_camera_fb_return(fb); delay(60); }
  return true;
}

static void startServer() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  cfg.lru_purge_enable = true;
  if (httpd_start(&server, &cfg) != ESP_OK) { Serial.println("HTTP server failed to start"); return; }
  httpd_uri_t snap = {.uri = "/snapshot", .method = HTTP_GET, .handler = snapshotHandler, .user_ctx = nullptr};
  httpd_uri_t stat = {.uri = "/status", .method = HTTP_GET, .handler = statusHandler, .user_ctx = nullptr};
  httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = rootHandler, .user_ctx = nullptr};
  httpd_register_uri_handler(server, &snap);
  httpd_register_uri_handler(server, &stat);
  httpd_register_uri_handler(server, &root);
}

void setup() {
  Serial.begin(115200);
  snprintf(bootId, sizeof(bootId), "%08lx", (unsigned long)esp_random());
  Serial.printf("\nDoseGuard tray camera %s, boot %s\n", FW_VERSION, bootId);
  if (!startCamera()) {
    Serial.println("Restarting in 5 s.");
    delay(5000);
    ESP.restart();
  }
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setSleep(false);  // power save adds latency to every snapshot
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Joining WiFi");
  for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\nWiFi failed. Check wifi_secrets.h. Restarting.");
    delay(2000);
    ESP.restart();
  }
  if (MDNS.begin(HOSTNAME)) MDNS.addService("http", "tcp", 80);
  startServer();
  Serial.printf("\nReady: http://%s/snapshot  (or http://%s.local/snapshot)\n",
                WiFi.localIP().toString().c_str(), HOSTNAME);
}

void loop() {
  // The HTTP server runs in its own task. Report WiFi drops so a bench watcher can see them;
  // the app blocks physical verification on its own when snapshots fail, and reminders continue.
  static bool wasUp = true;
  bool up = WiFi.status() == WL_CONNECTED;
  if (up != wasUp) { Serial.println(up ? "WiFi back" : "WiFi lost"); wasUp = up; }
  delay(500);
}
