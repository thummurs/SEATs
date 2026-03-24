-- ============================================
-- SEATs Migration: Add face_verifications table
-- Run this in Supabase SQL Editor
-- ============================================

CREATE TABLE face_verifications (
    id              SERIAL PRIMARY KEY,
    uid             VARCHAR(20)  NOT NULL,
    student_name    VARCHAR(100) NOT NULL,
    session_id      INTEGER      REFERENCES sessions(id) ON DELETE SET NULL,
    status          VARCHAR(20)  NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'verified', 'failed', 'timeout')),
    similarity      FLOAT,                        -- Rekognition similarity score
    rekognition_id  VARCHAR(100),                 -- matched face ID from collection
    created_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TIMESTAMP                     -- when verification completed
);

CREATE INDEX idx_face_verifications_uid    ON face_verifications(uid);
CREATE INDEX idx_face_verifications_status ON face_verifications(status);

-- Auto-timeout: any pending verification older than 30 seconds is stale
-- This is handled in the API, not the DB, but good to document here.
