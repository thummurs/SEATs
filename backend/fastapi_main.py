# ============================================
# SEATs — FastAPI Face Recognition Backend
# Receives JPEG from ESP32-S3-EYE,
# calls AWS Rekognition, reports back to Flask
# ============================================
# .env additions needed:
#   SEATS_API_URL=https://your-railway-url.up.railway.app
#   SEATS_API_KEY=your_api_key_here

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import boto3
import httpx
import logging
from config import settings

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="SEATs Face Recognition")

rekognition = boto3.client(
    "rekognition",
    region_name=settings.aws_region,
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
)

COLLECTION_ID   = settings.collection_id
MATCH_THRESHOLD = 80.0


# ── Health ──────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok", "message": "Face recognition backend running"}


# ── Recognize ───────────────────────────────

@app.post("/recognize")
async def recognize(request: Request):
    """
    Called by ESP32-S3-EYE with:
      - Raw JPEG bytes as request body
      - Header: X-Verification-Id: <id from Flask>
      - Header: X-API-Key: <api key>
    """
    # Validate API key
    api_key = request.headers.get("X-API-Key", "")
    if settings.seats_api_key and api_key != settings.seats_api_key:
        raise HTTPException(status_code=401, detail="Unauthorised")

    verification_id = request.headers.get("X-Verification-Id")
    if not verification_id:
        raise HTTPException(status_code=400, detail="Missing X-Verification-Id header")

    image_bytes = await request.body()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image body")

    log.info(f"Received image: {len(image_bytes)} bytes, verification_id={verification_id}")

    # Call Rekognition
    matched       = False
    similarity    = 0.0
    rekognition_id = ""

    try:
        response = rekognition.search_faces_by_image(
            CollectionId=COLLECTION_ID,
            Image={"Bytes": image_bytes},
            MaxFaces=1,
            FaceMatchThreshold=MATCH_THRESHOLD,
        )
        matches = response.get("FaceMatches", [])
        if matches:
            best           = matches[0]
            similarity     = best["Similarity"]
            rekognition_id = best["Face"]["ExternalImageId"]  # the person_id you used when indexing
            matched        = True
            log.info(f"Face matched: {rekognition_id} ({similarity:.1f}%)")
        else:
            log.info("No face match found")

    except rekognition.exceptions.InvalidParameterException:
        log.warning("Rekognition: no face detected in image")
        matched = False
    except Exception as e:
        log.error(f"Rekognition error: {e}")
        # Still report back to Flask so ESP32-C6 doesn't hang
        matched = False

    # Report result back to Flask API
    await report_to_flask(
        verification_id=int(verification_id),
        matched=matched,
        similarity=similarity,
        rekognition_id=rekognition_id,
    )

    return JSONResponse({
        "matched":        matched,
        "similarity":     similarity,
        "rekognition_id": rekognition_id,
    })


async def report_to_flask(verification_id: int, matched: bool,
                           similarity: float, rekognition_id: str):
    """POST result to Flask API so it can finalise the attendance record."""
    url = f"{settings.seats_api_url}/api/face/result"
    payload = {
        "verification_id": verification_id,
        "matched":         matched,
        "similarity":      similarity,
        "rekognition_id":  rekognition_id,
    }
    headers = {"X-API-Key": settings.seats_api_key}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=headers)
            log.info(f"Flask callback: {r.status_code} {r.text}")
    except Exception as e:
        log.error(f"Failed to report to Flask: {e}")


# ── Face Management ─────────────────────────

@app.post("/faces/add")
async def add_face(request: Request):
    """Index a new face into Rekognition. Header: X-Person-Id: <id>"""
    person_id   = request.headers.get("X-Person-Id")
    image_bytes = await request.body()
    if not person_id or not image_bytes:
        raise HTTPException(status_code=400, detail="Missing person_id or image")
    try:
        response = rekognition.index_faces(
            CollectionId=COLLECTION_ID,
            Image={"Bytes": image_bytes},
            ExternalImageId=person_id,
            DetectionAttributes=[],
        )
        face_id = response["FaceRecords"][0]["Face"]["FaceId"]
        return {"message": "Face indexed", "face_id": face_id, "person_id": person_id}
    except Exception as e:
        log.error(f"index_faces error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/faces/list")
def list_faces():
    try:
        response = rekognition.list_faces(CollectionId=COLLECTION_ID)
        return {"faces": response.get("Faces", [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/faces/{face_id}")
def delete_face(face_id: str):
    try:
        rekognition.delete_faces(
            CollectionId=COLLECTION_ID,
            FaceIds=[face_id]
        )
        return {"message": f"Face {face_id} deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
