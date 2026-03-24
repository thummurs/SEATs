# SEATs — Smart Electronic Attendance Tracking System

An IoT-based attendance tracking system using NFC cards, an ESP32-C6 microcontroller, a Flask REST API, and a PostgreSQL database. Built for CS7NS2 (IoT) at Trinity College Dublin.

---

## Architecture

```
┌─────────────────┐        WiFi / HTTP         ┌──────────────────┐        ┌─────────────────┐
│   ESP32-C6      │  ──── POST /api/attendance ──▶  Flask API      │ ──────▶  PostgreSQL DB   │
│   + RC522 NFC   │                             │  (Python)        │        │  (Supabase)      │
└─────────────────┘                             └──────────────────┘        └─────────────────┘
                                                         │
                                                         ▼
                                                ┌──────────────────┐
                                                │  Web Dashboard   │
                                                │  (HTML/JS)       │
                                                └──────────────────┘
```

### Flow
1. Student taps NFC card on RC522 reader
2. ESP32-C6 reads the card UID and POSTs it to the Flask API
3. API checks the UID against the students table in PostgreSQL
4. Attendance is recorded; LED + buzzer give instant feedback
5. Live dashboard updates every 5 seconds

---

## Hardware

| Component     | Details                        |
|---------------|--------------------------------|
| Microcontroller | ESP32-C6 Dev Module          |
| NFC Reader    | MFRC522 (RC522)                |
| Feedback      | RGB LED (NeoPixel), buzzer     |
| Power         | USB from laptop / power bank   |

### Wiring

| RC522 Pin | ESP32-C6 Pin |
|-----------|--------------|
| SDA (SS)  | GPIO 10      |
| SCK       | GPIO 6       |
| MOSI      | GPIO 7       |
| MISO      | GPIO 2       |
| RST       | GPIO 5       |
| GND       | GND          |
| 3.3V      | 3V3          |

| Buzzer | ESP32-C6 |
|--------|----------|
| +      | GPIO 4   |
| -      | GND      |

> ⚠️ RC522 runs on **3.3V only** — connecting to 5V will damage the module.

---

## Database Schema

5 tables: `students`, `sessions`, `session_students`, `attendance`, `pending_registrations`

See [`schema.sql`](schema.sql) for full definitions.

---

## API Endpoints

All endpoints (except `/` and `/dashboard`) require an `X-API-Key` header.

| Method | Route | Description |
|--------|-------|-------------|
| GET  | `/` | Health check |
| GET  | `/dashboard` | Web dashboard |
| POST | `/api/attendance` | Record a card tap (called by ESP32) |
| GET  | `/api/sessions` | List all sessions |
| POST | `/api/sessions` | Create a session |
| PUT  | `/api/sessions/<id>/start` | Start a session |
| PUT  | `/api/sessions/<id>/end` | End a session |
| GET  | `/api/sessions/<id>/attendance` | Get attendance for a session |
| GET  | `/api/students` | List students |
| POST | `/api/students` | Register a student |
| POST | `/api/register` | Create pending registration |
| POST | `/api/register/tap` | Link card to pending registration |
| GET  | `/api/register/pending` | List pending registrations |

---

## Setup

### Requirements
- Python 3.10+
- PostgreSQL 16
- Arduino IDE 2.x with ESP32 board support

### 1. Clone the repo
```bash
git clone https://github.com/ruthwikt/SEATs.git
cd SEATs
```

### 2. Python environment
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment variables
```bash
cp .env.example .env
```
Edit `.env` and fill in your database credentials and API key.

Generate a secure API key:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 4. Database
```bash
createdb seats_db
psql seats_db -f schema.sql
```

### 5. Run the API
```bash
python app.py
```

Dashboard available at `http://localhost:3000/dashboard`

### 6. Flash the ESP32
- Open `arduino/seats_esp32/seats_esp32.ino` in Arduino IDE
- Set board to **ESP32C6 Dev Module**
- Update `WIFI_SSID`, `WIFI_PASS`, `API_URL`, and `API_KEY` at the top of the sketch
- Upload

---

## Security

- All API routes protected by `X-API-Key` header authentication
- Secrets managed via `.env` (never committed to version control)
- Input validation on all endpoints
- UID format validation before database writes
- Error responses never expose internal details

---

## Project Structure

```
SEATs/
├── app.py              # Flask API server
├── dashboard.html      # Web dashboard
├── schema.sql          # PostgreSQL schema
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── .gitignore
└── arduino/
    └── seats_esp32/
        └── seats_esp32.ino   # ESP32 firmware
```
