# ============================================
# SEATs — Flask API Server
# NFC + Face Recognition dual verification
# ============================================

import os
import uuid
import logging
import requests
from datetime import datetime, date, timedelta
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv

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

# ── Config ─────────────────────────────────
DB_CONFIG = {
    "dbname":   os.getenv("DB_NAME",     "seats_db"),
    "user":     os.getenv("DB_USER",     "ruthwikt"),
    "password": os.getenv("DB_PASSWORD", ""),
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
}

API_KEY  = os.getenv("API_KEY", "")
PORT     = int(os.getenv("FLASK_PORT", 3000))

# How long (seconds) to wait for face before timing out
FACE_TIMEOUT_SECONDS = 30

if not API_KEY:
    log.warning("API_KEY not set — open access mode.")

FASTAPI_URL = os.getenv("FASTAPI_URL", "http://localhost:8000")

_scan_state = {"active": False, "uid": None}

# ── Helpers ────────────────────────────────

def get_db():
    return psycopg2.connect(**DB_CONFIG)


def generate_record_id():
    today = date.today().strftime("%Y%m%d")
    return f"ATT-{today}-{str(uuid.uuid4())[:6]}"


def serialize(row):
    if row is None:
        return None
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, (datetime, date)):
            d[k] = v.isoformat()
    return d


def serialize_all(rows):
    return [serialize(r) for r in rows]


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_KEY:
            return f(*args, **kwargs)
        if request.headers.get("X-API-Key", "") != API_KEY:
            log.warning(f"Unauthorised: {request.path} from {request.remote_addr}")
            return jsonify({"error": "Unauthorised"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Health ──────────────────────────────────

@app.route("/")
def health():
    return jsonify({"status": "ok", "message": "SEATs API running"}), 200


@app.route("/dashboard")
def dashboard():
    with open("dashboard.html", "r") as f:
        html = f.read()
    # Inject the API key at serve time so it never sits in the file
    html = html.replace("__API_KEY_PLACEHOLDER__", API_KEY)
    return html, 200, {"Content-Type": "text/html"}


# ── Attendance (NFC tap) ────────────────────
#
# This is what the ESP32-C6 calls on every card tap.
# Now returns "face_required" instead of immediately
# marking present — the face step must complete first.
#
@app.route("/api/attendance", methods=["POST"])
@require_api_key
def record_attendance():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    uid    = data.get("uid", "").strip().lower()
    device = data.get("device", "ESP32-C6").strip()

    if not uid:
        return jsonify({"error": "Missing uid"}), 400

    parts = uid.split(":")
    if not (3 <= len(parts) <= 7):
        return jsonify({"error": "Invalid UID format"}), 400
    
    # If scan mode is active, capture this UID for enrollment
    if _scan_state["active"]:
        _scan_state["uid"] = uid
        _scan_state["active"] = False
        log.info(f"Scan captured: {uid}")
        return jsonify({"status": "scan_captured", "uid": uid}), 200

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # 1. Check student exists
        cur.execute("SELECT name, active FROM students WHERE uid = %s", (uid,))
        student = cur.fetchone()

        if not student or not student["active"]:
            # Unknown or deactivated card — deny immediately, no face check
            record_id = generate_record_id()
            cur.execute("""
                SELECT id, occurrence FROM sessions
                WHERE status = 'active' ORDER BY start_time DESC LIMIT 1
            """)
            session    = cur.fetchone()
            session_id = session["id"]         if session else None
            occurrence = session["occurrence"] if session else None

            cur.execute("""
                INSERT INTO attendance
                    (record_id, uid, student_name, session_id, occurrence, status, device)
                VALUES (%s, %s, %s, %s, %s, 'denied', %s)
            """, (record_id, uid, "Unknown", session_id, occurrence, device))
            conn.commit()
            log.info(f"NFC DENIED (unknown): {uid}")
            return jsonify({"status": "denied", "reason": "unknown_card"}), 200

        student_name = student["name"]

        # 2. Find active session
        cur.execute("""
            SELECT id, occurrence FROM sessions
            WHERE status = 'active' ORDER BY start_time DESC LIMIT 1
        """)
        session    = cur.fetchone()
        session_id = session["id"]         if session else None
        occurrence = session["occurrence"] if session else None

        # 3. Check for duplicate tap in same session
        if session_id:
            cur.execute("""
                SELECT id FROM attendance
                WHERE uid = %s AND session_id = %s AND status = 'present'
            """, (uid, session_id))
            if cur.fetchone():
                conn.close()
                return jsonify({
                    "status":  "duplicate",
                    "message": "Already marked present",
                    "name":    student_name
                }), 200

        # 4. Cancel any stale pending verifications for this UID
        cur.execute("""
            UPDATE face_verifications
            SET status = 'timeout'
            WHERE uid = %s AND status = 'pending'
              AND created_at < NOW() - INTERVAL '%s seconds'
        """, (uid, FACE_TIMEOUT_SECONDS))

        # 5. Create a new pending face verification
        cur.execute("""
            INSERT INTO face_verifications (uid, student_name, session_id)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (uid, student_name, session_id))
        verification_id = cur.fetchone()["id"]

        conn.commit()
        log.info(f"NFC OK: {student_name} ({uid}) — awaiting face verification #{verification_id}")

        return jsonify({
            "status":          "face_required",
            "verification_id": verification_id,
            "name":            student_name,
            "message":         "NFC verified. Look at the camera."
        }), 200

    except Exception as e:
        conn.rollback()
        log.error(f"POST /api/attendance error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close()
        conn.close()


# ── Face Verification ───────────────────────
#
# GET  /api/face/pending
#   Called by ESP32-S3-EYE every 2 seconds.
#   Returns the oldest pending verification if any.
#
# POST /api/face/result
#   Called by FastAPI backend after Rekognition returns.
#   Finalises the attendance record.
#
# GET  /api/attendance/status/<uid>
#   Called by ESP32-C6 to poll for final result
#   (so it can show green/red LED).
#

@app.route("/api/face/pending", methods=["GET"])
@require_api_key
def get_pending_face():
    """ESP32-S3-EYE polls this to know when to capture."""
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # First, time out stale verifications
        cur.execute("""
            UPDATE face_verifications
            SET status = 'timeout'
            WHERE status = 'pending'
              AND created_at < NOW() - INTERVAL '%s seconds'
        """, (FACE_TIMEOUT_SECONDS,))
        conn.commit()

        # Get oldest pending
        cur.execute("""
            SELECT id, uid, student_name, session_id, created_at
            FROM face_verifications
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return jsonify({"pending": False}), 200

        return jsonify({
            "pending":         True,
            "verification_id": row["id"],
            "uid":             row["uid"],
            "student_name":    row["student_name"],
        }), 200

    except Exception as e:
        log.error(f"GET /api/face/pending error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/face/result", methods=["POST"])
@require_api_key
def face_result():
    """
    FastAPI calls this after Rekognition responds.
    Body: { verification_id, matched, similarity, rekognition_id }
    """
    data = request.get_json(silent=True)
    if not data or "verification_id" not in data:
        return jsonify({"error": "Missing verification_id"}), 400

    verification_id = data["verification_id"]
    matched         = data.get("matched", False)
    similarity      = data.get("similarity", 0.0)
    rekognition_id  = data.get("rekognition_id", "")

    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        # Load the pending verification
        cur.execute("""
            SELECT * FROM face_verifications
            WHERE id = %s AND status = 'pending'
        """, (verification_id,))
        verification = cur.fetchone()

        if not verification:
            conn.close()
            return jsonify({"error": "Verification not found or already resolved"}), 404

        face_status = "verified" if matched else "failed"

        # Update face_verifications
        cur.execute("""
            UPDATE face_verifications
            SET status = %s, similarity = %s, rekognition_id = %s, resolved_at = NOW()
            WHERE id = %s
        """, (face_status, similarity, rekognition_id, verification_id))

        # Write final attendance record
        record_id    = generate_record_id()
        att_status   = "present" if matched else "denied"
        uid          = verification["uid"]
        student_name = verification["student_name"]
        session_id   = verification["session_id"]

        cur.execute("""
            SELECT occurrence FROM sessions WHERE id = %s
        """, (session_id,)) if session_id else None
        session    = cur.fetchone() if session_id else None
        occurrence = session["occurrence"] if session else None

        cur.execute("""
            INSERT INTO attendance
                (record_id, uid, student_name, session_id, occurrence, status, device)
            VALUES (%s, %s, %s, %s, %s, %s, 'ESP32-S3-EYE+NFC')
        """, (record_id, uid, student_name, session_id, occurrence, att_status))

        # Increment session count if present
        if session_id and matched:
            cur.execute("""
                UPDATE sessions SET present_count = present_count + 1
                WHERE id = %s
            """, (session_id,))

        conn.commit()
        log.info(f"Face {'VERIFIED' if matched else 'FAILED'}: {student_name} ({uid}) similarity={similarity:.1f}%")

        return jsonify({
            "status":    att_status,
            "name":      student_name,
            "record_id": record_id,
            "similarity": similarity
        }), 201

    except Exception as e:
        conn.rollback()
        log.error(f"POST /api/face/result error: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/attendance/status/<uid>", methods=["GET"])
@require_api_key
def attendance_status(uid):
    """
    ESP32-C6 polls this after NFC tap to get the final result.
    Returns the most recent face verification for this UID.
    """
    uid = uid.strip().lower()
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            SELECT status, similarity, created_at, resolved_at
            FROM face_verifications
            WHERE uid = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (uid,))
        row = cur.fetchone()
        if not row:
            return jsonify({"status": "not_found"}), 404
        return jsonify(serialize(row)), 200
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
            VALUES (%s, %s, %s, %s) RETURNING id, session_code, status
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
        log.error(f"POST /api/sessions: {e}")
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
            return jsonify({"error": f"Session {active['id']} already active"}), 409
        cur.execute("""
            UPDATE sessions SET status = 'active', start_time = NOW()
            WHERE id = %s AND status = 'created'
            RETURNING id, session_code, status
        """, (session_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Session not found or already started"}), 404
        conn.commit()
        return jsonify({"message": "Session started", "session": serialize(row)}), 200
    except Exception as e:
        conn.rollback()
        log.error(f"start_session: {e}")
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
        return jsonify({"message": "Session ended", "session": serialize(row)}), 200
    except Exception as e:
        conn.rollback()
        log.error(f"end_session: {e}")
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
        """, (data["uid"].strip().lower(), data["name"].strip(),
              data["student_id"].strip(), data.get("email", "").strip()))
        row = cur.fetchone()
        conn.commit()
        return jsonify({"message": "Student registered", "student": serialize(row)}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "uid or student_id already exists"}), 409
    except Exception as e:
        conn.rollback()
        log.error(f"register_student: {e}")
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
            VALUES (%s, %s, %s) RETURNING id, name, student_id, status
        """, (data["name"].strip(), data["student_id"].strip(),
              data.get("email", "").strip()))
        row = cur.fetchone()
        conn.commit()
        return jsonify({"message": "Pending registration created", "registration": serialize(row)}), 201
    except Exception as e:
        conn.rollback()
        log.error(f"create_pending: {e}")
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
            VALUES (%s, %s, %s, %s) RETURNING id, uid, name, student_id
        """, (uid, pending["name"], pending["student_id"], pending.get("email", "")))
        student = cur.fetchone()
        conn.commit()
        return jsonify({"message": "Card linked", "student": serialize(student)}), 201
    except Exception as e:
        conn.rollback()
        log.error(f"link_card: {e}")
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

@app.route("/api/enroll", methods=["POST"])
@require_api_key
def enroll_student():
    """
    Single endpoint that:
    1. Registers student in PostgreSQL
    2. Indexes their face in Rekognition via FastAPI
    Accepts multipart/form-data:
      - name, student_id, uid, email (form fields)
      - photo (JPEG file)
    """
    import requests as req
 
    # --- Parse form fields ---
    name       = request.form.get("name", "").strip()
    student_id = request.form.get("student_id", "").strip()
    uid        = request.form.get("uid", "").strip().lower()
    email      = request.form.get("email", "").strip()
 
    if not name or not student_id or not uid:
        return jsonify({"error": "name, student_id and uid are required"}), 400
 
    parts = uid.split(":")
    if not (3 <= len(parts) <= 7):
        return jsonify({"error": "Invalid UID format"}), 400
 
    # --- Get photo ---
    photo = request.files.get("photo")
    if not photo:
        return jsonify({"error": "photo is required"}), 400
 
    photo_bytes = photo.read()
    if len(photo_bytes) < 1000:
        return jsonify({"error": "Photo too small — make sure it's a valid JPEG"}), 400
 
    # --- Register in PostgreSQL ---
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO students (uid, name, student_id, email)
            VALUES (%s, %s, %s, %s)
            RETURNING id, uid, name, student_id
        """, (uid, name, student_id, email))
        student = cur.fetchone()
        conn.commit()
        log.info(f"Enrolled student in DB: {name} ({uid})")
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close(); conn.close()
        return jsonify({"error": "uid or student_id already exists"}), 409
    except Exception as e:
        conn.rollback()
        cur.close(); conn.close()
        log.error(f"enroll DB error: {e}")
        return jsonify({"error": "Database error"}), 500
    finally:
        cur.close()
        conn.close()
 
    # --- Index face in Rekognition via FastAPI ---
    fastapi_url = os.getenv("FASTAPI_URL", "http://localhost:8000")
    try:
        face_resp = req.post(
            f"{fastapi_url}/faces/add",
            data=photo_bytes,
            headers={
                "Content-Type":  "image/jpeg",
                "X-Person-Id":   student_id,   # used as ExternalImageId in Rekognition
                "X-API-Key":     API_KEY,
            },
            timeout=15
        )
        if face_resp.status_code == 200:
            face_data = face_resp.json()
            log.info(f"Face indexed: {student_id} → {face_data.get('face_id')}")
            return jsonify({
                "message":    "Student enrolled successfully",
                "student":    serialize(student),
                "face_id":    face_data.get("face_id"),
                "person_id":  student_id,
            }), 201
        else:
            # DB registration succeeded but face indexing failed
            # Student is registered but won't pass face check until re-indexed
            log.warning(f"Face indexing failed: {face_resp.status_code} {face_resp.text}")
            return jsonify({
                "message":  "Student registered in DB but face indexing failed",
                "student":  serialize(student),
                "face_error": face_resp.text,
                "warning":  "Re-upload photo to complete enrollment"
            }), 207  # 207 = partial success
 
    except req.exceptions.ConnectionError:
        log.warning("FastAPI not reachable — student registered in DB only")
        return jsonify({
            "message": "Student registered in DB but face backend is offline",
            "student": serialize(student),
            "warning": "Start FastAPI backend and re-index face"
        }), 207
    except Exception as e:
        log.error(f"Face indexing error: {e}")
        return jsonify({
            "message": "Student registered but face indexing errored",
            "student": serialize(student),
            "warning": str(e)
        }), 207
    
@app.route("/api/scan/start", methods=["POST"])
@require_api_key
def scan_start():
    _scan_state["active"] = True
    _scan_state["uid"] = None
    log.info("Card scan mode activated")
    return jsonify({"message": "Scan mode active — tap card now"}), 200

@app.route("/api/scan/result", methods=["GET"])
@require_api_key
def scan_result():
    if _scan_state["uid"]:
        uid = _scan_state["uid"]
        _scan_state["active"] = False
        _scan_state["uid"] = None
        return jsonify({"found": True, "uid": uid}), 200
    return jsonify({"found": False}), 200


# ── Run ─────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print(f"  SEATs API  →  http://0.0.0.0:{PORT}")
    print(f"  Dashboard  →  http://localhost:{PORT}/dashboard")
    print(f"  API Key    →  {'SET ✓' if API_KEY else 'NOT SET'}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=PORT,
            debug=os.getenv("FLASK_ENV") == "development")
