# ============================================
# SEATs — Flask API Server
# Production-ready version
# ============================================
# Setup:
#   pip install flask psycopg2-binary python-dotenv
#   cp .env.example .env   (then fill in your values)
#   python app.py
# ============================================

import os
import uuid
import logging
from datetime import datetime, date
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv

# ── Load .env ──────────────────────────────
load_dotenv()

# ── Logging ────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("seats.log"),
    ]
)
log = logging.getLogger(__name__)

# ── App ────────────────────────────────────
app = Flask(__name__)

# ── Config from .env ───────────────────────
DB_CONFIG = {
    "dbname":   os.getenv("DB_NAME",     "seats_db"),
    "user":     os.getenv("DB_USER",     "ruthwikt"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
}

API_KEY = os.getenv("API_KEY", "")
PORT    = int(os.getenv("FLASK_PORT", 3000))

if not API_KEY:
    log.warning("API_KEY is not set — all requests will be accepted. Set it in .env.")


# ── Helpers ────────────────────────────────

def get_db():
    return psycopg2.connect(**DB_CONFIG)


def generate_record_id():
    today = date.today().strftime("%Y%m%d")
    short = str(uuid.uuid4())[:6]
    return f"ATT-{today}-{short}"


def serialize(row):
    """Convert a RealDictRow to a plain dict, serializing dates/datetimes."""
    if row is None:
        return None
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, date):
            d[k] = v.isoformat()
    return d


def serialize_all(rows):
    return [serialize(r) for r in rows]


# ── API Key auth decorator ──────────────────
def require_api_key(f):
    """
    Checks for X-API-Key header on every decorated route.
    The ESP32 sends this header with every request.
    Skip check if API_KEY is not configured (dev mode).
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_KEY:
            return f(*args, **kwargs)
        key = request.headers.get("X-API-Key", "")
        if key != API_KEY:
            log.warning(f"Unauthorised request to {request.path} from {request.remote_addr}")
            return jsonify({"error": "Unauthorised"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Routes ─────────────────────────────────

@app.route("/")
def health():
    return jsonify({"status": "ok", "message": "SEATs API running"}), 200


@app.route("/dashboard")
def dashboard():
    return send_file("dashboard.html")


# ── Attendance ──────────────────────────────

@app.route("/api/attendance", methods=["POST"])
@require_api_key
def record_attendance():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON body"}), 400

    uid    = data.get("uid", "").strip().lower()
    name   = data.get("name", "Unknown").strip()
    device = data.get("device", "ESP32-C6").strip()

    if not uid:
        return jsonify({"error": "Missing uid"}), 400

    # Basic UID format sanity check (xx:xx:xx:xx)
    parts = uid.split(":")
    if not (3 <= len(parts) <= 7):
        return jsonify({"error": "Invalid UID format"}), 400

    record_id = generate_record_id()

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # 1. Look up student
        cur.execute("SELECT name, active FROM students WHERE uid = %s", (uid,))
        student = cur.fetchone()

        if student:
            tap_status   = "present" if student["active"] else "denied"
            student_name = student["name"]
        else:
            tap_status   = "denied"
            student_name = "Unknown"

        # 2. Find active session
        cur.execute("""
            SELECT id, occurrence FROM sessions
            WHERE status = 'active'
            ORDER BY start_time DESC LIMIT 1
        """)
        session    = cur.fetchone()
        session_id = session["id"]         if session else None
        occurrence = session["occurrence"] if session else None

        # 3. Prevent duplicate tap in same session
        if session_id and tap_status == "present":
            cur.execute("""
                SELECT id FROM attendance
                WHERE uid = %s AND session_id = %s AND status = 'present'
            """, (uid, session_id))
            if cur.fetchone():
                conn.close()
                return jsonify({
                    "message": "Already marked present",
                    "uid": uid,
                    "name": student_name,
                    "status": "duplicate"
                }), 200

        # 4. Insert attendance record
        cur.execute("""
            INSERT INTO attendance
                (record_id, uid, student_name, session_id, occurrence, status, device)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (record_id, uid, student_name, session_id, occurrence, tap_status, device))

        # 5. Increment session present count
        if session_id and tap_status == "present":
            cur.execute("""
                UPDATE sessions SET present_count = present_count + 1
                WHERE id = %s
            """, (session_id,))

        conn.commit()
        log.info(f"Attendance: {student_name} ({uid}) — {tap_status}")

        return jsonify({
            "message":    "Attendance recorded",
            "record_id":  record_id,
            "uid":        uid,
            "name":       student_name,
            "status":     tap_status,
            "session_id": session_id
        }), 201

    except Exception as e:
        conn.rollback()
        log.error(f"POST /api/attendance error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close()
        conn.close()


# ── Sessions ────────────────────────────────

@app.route("/api/sessions", methods=["GET"])
@require_api_key
def list_sessions():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM sessions ORDER BY created_at DESC")
        return jsonify(serialize_all(cur.fetchall())), 200
    finally:
        cur.close(); conn.close()


@app.route("/api/sessions", methods=["POST"])
@require_api_key
def create_session():
    data = request.get_json(silent=True) or {}
    for field in ["session_code", "course_name", "session_date", "occurrence"]:
        if not data.get(field):
            return jsonify({"error": f"Missing field: {field}"}), 400

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO sessions (session_code, course_name, session_date, occurrence)
            VALUES (%s, %s, %s, %s)
            RETURNING id, session_code, status
        """, (data["session_code"], data["course_name"],
              data["session_date"], data["occurrence"]))
        row = cur.fetchone()
        conn.commit()
        return jsonify({"message": "Session created", "session": serialize(row)}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "session_code already exists"}), 409
    except Exception as e:
        conn.rollback()
        log.error(f"POST /api/sessions error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close(); conn.close()


@app.route("/api/sessions/<int:session_id>/start", methods=["PUT"])
@require_api_key
def start_session(session_id):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT id FROM sessions WHERE status = 'active'")
        active = cur.fetchone()
        if active:
            return jsonify({"error": f"Session {active['id']} is already active. End it first."}), 409

        cur.execute("""
            UPDATE sessions SET status = 'active', start_time = NOW()
            WHERE id = %s AND status = 'created'
            RETURNING id, session_code, status
        """, (session_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Session not found or already started"}), 404
        conn.commit()
        log.info(f"Session {session_id} started")
        return jsonify({"message": "Session started", "session": serialize(row)}), 200
    except Exception as e:
        conn.rollback()
        log.error(f"PUT /api/sessions/{session_id}/start error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close(); conn.close()


@app.route("/api/sessions/<int:session_id>/end", methods=["PUT"])
@require_api_key
def end_session(session_id):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            UPDATE sessions SET status = 'ended', end_time = NOW()
            WHERE id = %s AND status = 'active'
            RETURNING id, session_code, present_count, total_students
        """, (session_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Session not found or not active"}), 404
        conn.commit()
        log.info(f"Session {session_id} ended — {row['present_count']} present")
        return jsonify({"message": "Session ended", "session": serialize(row)}), 200
    except Exception as e:
        conn.rollback()
        log.error(f"PUT /api/sessions/{session_id}/end error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close(); conn.close()


@app.route("/api/sessions/<int:session_id>/attendance", methods=["GET"])
@require_api_key
def session_attendance(session_id):
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT record_id, uid, student_name, status, nfc_timestamp, device
            FROM attendance WHERE session_id = %s
            ORDER BY nfc_timestamp ASC
        """, (session_id,))
        return jsonify(serialize_all(cur.fetchall())), 200
    finally:
        cur.close(); conn.close()


# ── Students ────────────────────────────────

@app.route("/api/students", methods=["GET"])
@require_api_key
def list_students():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT * FROM students ORDER BY name")
        return jsonify(serialize_all(cur.fetchall())), 200
    finally:
        cur.close(); conn.close()


@app.route("/api/students", methods=["POST"])
@require_api_key
def register_student():
    data = request.get_json(silent=True) or {}
    for field in ["uid", "name", "student_id"]:
        if not data.get(field):
            return jsonify({"error": f"Missing field: {field}"}), 400

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO students (uid, name, student_id, email)
            VALUES (%s, %s, %s, %s)
            RETURNING id, uid, name, student_id
        """, (
            data["uid"].strip().lower(),
            data["name"].strip(),
            data["student_id"].strip(),
            data.get("email", "").strip()
        ))
        row = cur.fetchone()
        conn.commit()
        return jsonify({"message": "Student registered", "student": serialize(row)}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "uid or student_id already exists"}), 409
    except Exception as e:
        conn.rollback()
        log.error(f"POST /api/students error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close(); conn.close()


# ── Pending Registrations ───────────────────

@app.route("/api/register", methods=["POST"])
@require_api_key
def create_pending():
    data = request.get_json(silent=True) or {}
    for field in ["name", "student_id"]:
        if not data.get(field):
            return jsonify({"error": f"Missing field: {field}"}), 400

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO pending_registrations (name, student_id, email)
            VALUES (%s, %s, %s)
            RETURNING id, name, student_id, status
        """, (data["name"].strip(), data["student_id"].strip(), data.get("email", "").strip()))
        row = cur.fetchone()
        conn.commit()
        return jsonify({"message": "Pending registration created", "registration": serialize(row)}), 201
    except Exception as e:
        conn.rollback()
        log.error(f"POST /api/register error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close(); conn.close()


@app.route("/api/register/tap", methods=["POST"])
@require_api_key
def link_card():
    data = request.get_json(silent=True) or {}
    uid  = data.get("uid", "").strip().lower()
    if not uid:
        return jsonify({"error": "Missing uid"}), 400

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("SELECT id FROM students WHERE uid = %s", (uid,))
        if cur.fetchone():
            return jsonify({"error": "Card already registered"}), 409

        cur.execute("""
            SELECT * FROM pending_registrations
            WHERE status = 'waiting' ORDER BY created_at ASC LIMIT 1
        """)
        pending = cur.fetchone()
        if not pending:
            return jsonify({"error": "No pending registrations"}), 404

        cur.execute("""
            UPDATE pending_registrations
            SET uid = %s, status = 'completed', completed_at = NOW()
            WHERE id = %s
        """, (uid, pending["id"]))

        cur.execute("""
            INSERT INTO students (uid, name, student_id, email)
            VALUES (%s, %s, %s, %s)
            RETURNING id, uid, name, student_id
        """, (uid, pending["name"], pending["student_id"], pending.get("email", "")))
        student = cur.fetchone()
        conn.commit()
        return jsonify({"message": "Card linked", "student": serialize(student)}), 201
    except Exception as e:
        conn.rollback()
        log.error(f"POST /api/register/tap error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close(); conn.close()


@app.route("/api/register/pending", methods=["GET"])
@require_api_key
def list_pending():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT * FROM pending_registrations
            WHERE status = 'waiting' ORDER BY created_at ASC
        """)
        return jsonify(serialize_all(cur.fetchall())), 200
    finally:
        cur.close(); conn.close()


# ── Run ─────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print(f"  SEATs API  →  http://0.0.0.0:{PORT}")
    print(f"  Dashboard  →  http://localhost:{PORT}/dashboard")
    print(f"  API Key    →  {'SET ✓' if API_KEY else 'NOT SET — open access'}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=PORT, debug=os.getenv("FLASK_ENV") == "development")
