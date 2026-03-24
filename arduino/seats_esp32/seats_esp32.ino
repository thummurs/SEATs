// ============================================
// SEATs — NFC Attendance System
// ESP32-C6 + RC522 + PostgreSQL API
// ============================================

#include <SPI.h>
#include <MFRC522.h>
#include <WiFi.h>
#include <HTTPClient.h>

// --- Pin Definitions ---
#define SS_PIN    10
#define RST_PIN   5
#define LED_PIN   8
#define BUZZER    4

// --- WiFi Credentials ---
const char* WIFI_SSID = "Cumberland";
const char* WIFI_PASS = "Cumberland7";

const char* API_URL = "https://seats-production.up.railway.app";
const char* API_KEY = "c32d2eb7db57fb3cf743ca72c53cd8971579cbba99c53e77f80ea282405170d3";

MFRC522 rfid(SS_PIN, RST_PIN);

// --- Registered Students (local fallback if WiFi is down) ---
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

  pinMode(LED_PIN, OUTPUT);
  pinMode(BUZZER, OUTPUT);

  // Blue blink = starting up
  for (int i = 0; i < 3; i++) {
    neopixelWrite(LED_PIN, 0, 0, 50);
    delay(200);
    neopixelWrite(LED_PIN, 0, 0, 0);
    delay(200);
  }

  // Connect to WiFi
  connectWiFi();

  // Start NFC reader
  SPI.begin(6, 2, 7, 10);
  rfid.PCD_Init();
  delay(500);
  rfid.PCD_SetAntennaGain(rfid.RxGain_max);

  Serial.println("================================");
  Serial.println("  NFC Attendance System Ready");
  Serial.println("  Tap your card to check in");
  Serial.println("================================");

  // Startup beep
  tone(BUZZER, 2000, 200);
  delay(300);

  // Dim blue = waiting for card
  neopixelWrite(LED_PIN, 0, 0, 10);
}

void loop() {
  // Check WiFi, reconnect if dropped
  if (WiFi.status() != WL_CONNECTED) {
    neopixelWrite(LED_PIN, 50, 20, 0); // Orange = no WiFi
    connectWiFi();
  }

  // Wait for NFC card
  if (!rfid.PICC_IsNewCardPresent() ||
      !rfid.PICC_ReadCardSerial()) {
    return;
  }

  String uid = getUID();
  Serial.println("\nCard detected! UID: " + uid);

  // Check locally first
  int studentIndex = findStudent(uid);
  String name = (studentIndex >= 0) ? studentNames[studentIndex] : "Unknown";

  if (studentIndex >= 0) {
    Serial.println("ACCESS GRANTED: " + name);
    successSignal();
    sendToAPI(uid, name);
  } else {
    Serial.println("ACCESS DENIED: Unknown card");
    failureSignal();
    sendToAPI(uid, "Unknown");
  }

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  neopixelWrite(LED_PIN, 0, 0, 10);

  // Wait for card removal to prevent double-read
  delay(500);
  while (rfid.PICC_IsNewCardPresent()) {
    rfid.PICC_ReadCardSerial();
    rfid.PICC_HaltA();
    delay(200);
  }
  delay(1000);
}

// ============================================
// WiFi
// ============================================
void connectWiFi() {
  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);

  WiFi.disconnect(true);
  delay(1000);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    Serial.print(".");
    neopixelWrite(LED_PIN, 50, 20, 0);
    delay(200);
    neopixelWrite(LED_PIN, 0, 0, 0);
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi Connected!");
    Serial.print("IP Address: ");
    Serial.println(WiFi.localIP());
    neopixelWrite(LED_PIN, 0, 50, 0); // Green = connected
    tone(BUZZER, 2000, 100);
    delay(500);
  } else {
    Serial.println("\nWiFi FAILED — running in offline mode.");
    neopixelWrite(LED_PIN, 50, 0, 0); // Red = failed
    tone(BUZZER, 500, 500);
    delay(1000);
  }
}

// ============================================
// API
// ============================================
void sendToAPI(String uid, String name) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("No WiFi — tap not sent to server.");
    return;
  }

  HTTPClient http;
  String url = String(API_URL) + "/api/attendance";

  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);
  http.setTimeout(5000);

  String json = "{";
  json += "\"uid\":\"" + uid + "\",";
  json += "\"name\":\"" + name + "\",";
  json += "\"device\":\"ESP32-C6\"";
  json += "}";

  Serial.println("Sending: " + json);

  int responseCode = http.POST(json);

  if (responseCode > 0) {
    String response = http.getString();
    Serial.println("Response (" + String(responseCode) + "): " + response);
    if (responseCode == 201) {
      neopixelWrite(LED_PIN, 30, 0, 50); // Purple = saved to DB
      delay(300);
    }
  } else {
    Serial.println("API Error: " + String(responseCode));
    neopixelWrite(LED_PIN, 50, 50, 0); // Yellow = NFC ok but API failed
    delay(300);
  }

  http.end();
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

// ============================================
// Feedback
// ============================================
void successSignal() {
  neopixelWrite(LED_PIN, 0, 50, 0); // Green
  tone(BUZZER, 2000, 200);
  delay(1000);
}

void failureSignal() {
  for (int i = 0; i < 3; i++) {
    neopixelWrite(LED_PIN, 50, 0, 0); // Red
    tone(BUZZER, 1000, 150);
    delay(300);
    neopixelWrite(LED_PIN, 0, 0, 0);
  }
}
