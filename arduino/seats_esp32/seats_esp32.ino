// ============================================
// SEATs — ESP32-C6 NFC Controller
// NFC tap → wait for face verification result
// ============================================

#include <SPI.h>
#include <MFRC522.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// --- Pins ---
#define SS_PIN   10
#define RST_PIN   5
#define LED_PIN   8
#define BUZZER    4

// --- Config ---
const char* WIFI_SSID = "Cumberland";
const char* WIFI_PASS = "Cumberland7";
const char* API_URL   = "https://seats-production-4c03.up.railway.app";
const char* API_KEY   = "c32d2eb7db57fb3cf743ca72c53cd8971579cbba99c53e77f80ea282405170d3";

// --- LED colours (R, G, B) ---
#define LED_OFF     0,   0,   0
#define LED_BLUE    0,   0,  10   // idle/waiting
#define LED_ORANGE 50,  20,   0   // no WiFi
#define LED_GREEN   0,  50,   0   // success
#define LED_RED    50,   0,   0   // denied
#define LED_YELLOW 50,  50,   0   // waiting for face
#define LED_PURPLE 30,   0,  50   // saved to DB

// --- Timeouts ---
#define FACE_POLL_INTERVAL_MS  1000   // poll for result every 1s
#define FACE_TIMEOUT_MS       35000   // give up after 35s

MFRC522 rfid(SS_PIN, RST_PIN);

// --- Local student fallback ---
const String allowedUIDs[] = {
  "33:8c:f2:12",
  "f3:fc:9b:0f",
  "33:fc:8c:0f",
};
const String studentNames[] = {
  "Pratik",
  "Tanmay",
  "Student3",
};
const int NUM_STUDENTS = 3;


void setup() {
  Serial.begin(115200);
  delay(2000);
  pinMode(BUZZER, OUTPUT);

  for (int i = 0; i < 3; i++) {
    neopixelWrite(LED_PIN, LED_BLUE);
    delay(200);
    neopixelWrite(LED_PIN, LED_OFF);
    delay(200);
  }

  connectWiFi();

  SPI.begin(6, 2, 7, 10);
  rfid.PCD_Init();
  delay(500);
  rfid.PCD_SetAntennaGain(rfid.RxGain_max);

  Serial.println("================================");
  Serial.println("  SEATs NFC + Face Verification");
  Serial.println("  Tap card to begin");
  Serial.println("================================");

  tone(BUZZER, 2000, 200);
  delay(300);
  neopixelWrite(LED_PIN, LED_BLUE);
}


void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    neopixelWrite(LED_PIN, LED_ORANGE);
    connectWiFi();
  }

  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) {
    return;
  }

  String uid = getUID();
  Serial.println("\n[NFC] Card: " + uid);

  // Local check first
  int idx  = findStudent(uid);
  String name = (idx >= 0) ? studentNames[idx] : "Unknown";

  // POST to Flask API
  String response = postAttendance(uid, name);
  if (response == "") {
    Serial.println("[ERR] No response from API");
    failureSignal();
    neopixelWrite(LED_PIN, LED_BLUE);
    waitForCardRemoval();
    return;
  }

  // Parse response
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, response);
  if (err) {
    Serial.println("[ERR] JSON parse failed");
    failureSignal();
    neopixelWrite(LED_PIN, LED_BLUE);
    waitForCardRemoval();
    return;
  }

  String status = doc["status"].as<String>();
  Serial.println("[API] Status: " + status);

  if (status == "denied") {
    Serial.println("[RESULT] Access denied — unknown card");
    failureSignal();

  } else if (status == "duplicate") {
    Serial.println("[RESULT] Already checked in");
    // One short beep
    tone(BUZZER, 1500, 150);
    neopixelWrite(LED_PIN, LED_PURPLE);
    delay(1500);

  } else if (status == "face_required") {
    // NFC passed — now wait for face verification result
    Serial.println("[NFC] OK: " + String(doc["name"].as<String>()));
    Serial.println("[FACE] Look at the camera...");

    // Yellow = waiting for face
    neopixelWrite(LED_PIN, LED_YELLOW);
    tone(BUZZER, 1800, 100);
    delay(200);
    tone(BUZZER, 2200, 100);

    // Poll for result
    String finalStatus = pollForResult(uid);

    if (finalStatus == "present") {
      Serial.println("[RESULT] Present — NFC + Face verified");
      successSignal();
    } else if (finalStatus == "denied") {
      Serial.println("[RESULT] Denied — face did not match");
      failureSignal();
    } else {
      Serial.println("[RESULT] Timeout — no face detected in time");
      // Two short beeps = timeout
      tone(BUZZER, 800, 200);
      delay(300);
      tone(BUZZER, 800, 200);
      neopixelWrite(LED_PIN, LED_RED);
      delay(1000);
    }
  }

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  neopixelWrite(LED_PIN, LED_BLUE);
  waitForCardRemoval();
  delay(1000);
}


// ============================================
// Poll Flask API for final attendance result
// Returns: "present", "denied", or "timeout"
// ============================================
String pollForResult(String uid) {
  unsigned long start = millis();

  while (millis() - start < FACE_TIMEOUT_MS) {
    delay(FACE_POLL_INTERVAL_MS);

    HTTPClient http;
    String url = String(API_URL) + "/api/attendance/status/" + uid;
    http.begin(url);
    http.addHeader("X-API-Key", API_KEY);
    http.setTimeout(5000);

    int code = http.GET();
    if (code == 200) {
      String body = http.getString();
      http.end();

      StaticJsonDocument<128> doc;
      if (deserializeJson(doc, body) == DeserializationError::Ok) {
        String s = doc["status"].as<String>();
        if (s == "verified") return "present";
        if (s == "failed")   return "denied";
        if (s == "timeout")  return "timeout";
        // s == "pending" → keep polling
        Serial.println("[FACE] Still pending...");

        // Pulse yellow while waiting
        neopixelWrite(LED_PIN, LED_OFF);
        delay(100);
        neopixelWrite(LED_PIN, LED_YELLOW);
      }
    } else {
      Serial.println("[POLL] HTTP error: " + String(code));
      http.end();
    }
  }

  return "timeout";
}


// ============================================
// POST /api/attendance
// ============================================
String postAttendance(String uid, String name) {
  if (WiFi.status() != WL_CONNECTED) return "";

  HTTPClient http;
  String url = String(API_URL) + "/api/attendance";
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);
  http.setTimeout(5000);

  String json = "{\"uid\":\"" + uid + "\",\"name\":\"" + name + "\",\"device\":\"ESP32-C6\"}";
  Serial.println("[POST] " + json);

  int code = http.POST(json);
  String body = "";
  if (code > 0) {
    body = http.getString();
    Serial.println("[RESP] (" + String(code) + ") " + body);
  } else {
    Serial.println("[ERR] POST failed: " + String(code));
  }
  http.end();
  return body;
}


// ============================================
// WiFi
// ============================================
void connectWiFi() {
  Serial.print("[WiFi] Connecting to ");
  Serial.println(WIFI_SSID);
  WiFi.disconnect(true);
  delay(500);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    Serial.print(".");
    neopixelWrite(LED_PIN, LED_ORANGE);
    delay(200);
    neopixelWrite(LED_PIN, LED_OFF);
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n[WiFi] Connected: " + WiFi.localIP().toString());
    neopixelWrite(LED_PIN, LED_GREEN);
    tone(BUZZER, 2000, 100);
    delay(500);
  } else {
    Serial.println("\n[WiFi] FAILED — offline mode");
    neopixelWrite(LED_PIN, LED_RED);
    tone(BUZZER, 500, 500);
    delay(1000);
  }
}


// ============================================
// NFC Helpers
// ============================================
String getUID() {
  String uid = "";
  for (int i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
    if (i < rfid.uid.size - 1) uid += ":";
  }
  uid.toLowerCase();
  return uid;
}

int findStudent(String uid) {
  for (int i = 0; i < NUM_STUDENTS; i++) {
    if (uid == allowedUIDs[i]) return i;
  }
  return -1;
}

void waitForCardRemoval() {
  delay(500);
  while (rfid.PICC_IsNewCardPresent()) {
    rfid.PICC_ReadCardSerial();
    rfid.PICC_HaltA();
    delay(200);
  }
}


// ============================================
// Feedback
// ============================================
void successSignal() {
  neopixelWrite(LED_PIN, LED_GREEN);
  tone(BUZZER, 2000, 200);
  delay(1000);
}

void failureSignal() {
  for (int i = 0; i < 3; i++) {
    neopixelWrite(LED_PIN, LED_RED);
    tone(BUZZER, 1000, 150);
    delay(300);
    neopixelWrite(LED_PIN, LED_OFF);
    delay(100);
  }
}
