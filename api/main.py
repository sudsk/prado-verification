"""
api/main.py — PRADO Passport Verification API v0.3.0
"""

import json, os, uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from verification.mrz_checker import check_mrz
from verification.ocr_extractor import PassportOCRExtractor
from verification.prado_matcher import PRADOMatcher
from verification.document_detector import DocumentDetector

app = FastAPI(
    title="PRADO Passport Verification API",
    description="Automated passport authenticity verification using PRADO security feature data",
    version="0.3.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_ocr_extractor = None
_prado_matcher  = None
_doc_detector   = None


def get_ocr_extractor() -> PassportOCRExtractor:
    global _ocr_extractor
    if _ocr_extractor is None:
        _ocr_extractor = PassportOCRExtractor(
            project_id=os.environ.get("GCP_PROJECT_ID"),
            location=os.environ.get("GCP_LOCATION", "us-central1"),
        )
    return _ocr_extractor


def get_prado_matcher() -> PRADOMatcher:
    global _prado_matcher
    if _prado_matcher is None:
        _prado_matcher = PRADOMatcher(
            project_id=os.environ.get("GCP_PROJECT_ID"),
            dataset=os.environ.get("BQ_DATASET", "prado_verification"),
        )
    return _prado_matcher


def get_doc_detector() -> DocumentDetector:
    global _doc_detector
    if _doc_detector is None:
        _doc_detector = DocumentDetector(
            project_id=os.environ.get("GCP_PROJECT_ID"),
            dataset=os.environ.get("BQ_DATASET", "prado_verification"),
        )
    return _doc_detector


# ---------------------------------------------------------------------------
# Risk scorer
# ---------------------------------------------------------------------------

def compute_risk_score(mrz_result, ocr_result, prado_result):
    score = 0.0
    reasons = []

    if mrz_result:
        if not mrz_result.all_checkdigits_pass:
            score += 0.35
            reasons.append("MRZ check digit failure")
        if not mrz_result.expiry_future_pass:
            score += 0.10
            reasons.append("Passport expired")
        if not mrz_result.doc_type_pass:
            score += 0.05
            reasons.append("Invalid document type in MRZ")

    if ocr_result:
        if not ocr_result.consistency_pass:
            score += 0.25
            reasons.append("MRZ / VIZ field mismatch")
        if not ocr_result.viz.photo_present:
            score += 0.10
            reasons.append("No photo detected")

    if prado_result:
        total = prado_result.digital_pass_count + prado_result.digital_fail_count
        if total > 0:
            fail_rate = prado_result.digital_fail_count / total
            score += fail_rate * 0.20
            if prado_result.digital_fail_count > 0:
                reasons.append(
                    f"PRADO digital checks: {prado_result.digital_fail_count} failed"
                )

    score = min(score, 1.0)
    verdict = "PASS" if score < 0.15 else ("REFER" if score < 0.45 else "FAIL")
    summary = f"{verdict} (risk={score:.2f})"
    if reasons:
        summary += " — " + "; ".join(reasons)

    return round(score, 3), verdict, summary


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "service": "prado-verification",
        "version": "0.3.0",
        "status": "ok",
        "docs": "/docs",
        "endpoints": {
            "POST /verify":     "Full verification — scan + optional document_id",
            "POST /verify/mrz": "MRZ-only check — no image needed",
            "POST /detect":     "Detect document_id from scan only",
            "GET  /document/{id}": "Fetch PRADO features for a document",
            "GET  /health":     "Liveness check",
        }
    }


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/document/{document_id}")
async def get_document_features(document_id: str):
    """Fetch all PRADO features for a document from BigQuery."""
    try:
        features = get_prado_matcher().fetch_features(document_id)
        return {
            "document_id":   document_id,
            "feature_count": len(features),
            "features":      features,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/detect")
async def detect_document(
    scan: UploadFile = File(..., description="Bio page scan (JPEG/PNG)"),
):
    """
    Detect the PRADO document_id from a passport scan alone.
    No document_id needed — derived from OCR signals.
    """
    image_bytes = await scan.read()
    mime_type = "image/jpeg" if "jpeg" in (scan.content_type or "") else "image/png"

    ocr = get_ocr_extractor().extract_from_bytes(image_bytes, mime_type)
    if ocr.error:
        raise HTTPException(status_code=422, detail=f"OCR failed: {ocr.error}")

    doc_id, confidence = get_doc_detector().detect_from_ocr(ocr)

    return {
        "detected_document_id":       doc_id,
        "confidence":                 confidence,
        "country_code":               ocr.mrz.line1[2:5].replace("<","").strip() if ocr.mrz.line1 else "",
        "detection_signals_used": {
            "cover_colour":            ocr.viz.cover_colour,
            "photo_integration":       ocr.viz.photo_integration_technique,
            "document_title":          ocr.viz.document_title,
            "date_of_issue":           ocr.viz.date_of_issue,
        },
        "ocr_summary": {
            "surname":    ocr.viz.surname,
            "doc_number": ocr.viz.doc_number,
            "photo_type": ocr.viz.photo_type,
            "mrz_complete": ocr.mrz.complete,
        }
    }


@app.post("/verify/mrz")
async def verify_mrz_only(request: dict):
    """MRZ intrinsic checks only — no image or BigQuery needed."""
    line1 = request.get("line1", "")
    line2 = request.get("line2", "")
    result = check_mrz(line1, line2)
    return {
        "verification_id": str(uuid.uuid4()),
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "mrz_checks":      result.to_dict(),
        "overall_verdict": "PASS" if result.overall_pass else "FAIL",
    }


@app.post("/verify")
async def verify_passport(
    scan: UploadFile = File(..., description="Bio page scan (JPEG/PNG)"),
    document_id: str = Form(
        "", description="PRADO document ID e.g. GBR-AO-06001 — auto-detected if blank"
    ),
    mrz_line1: str = Form(
        "", description="MRZ line 1 (44 chars) — extracted from scan if blank"
    ),
    mrz_line2: str = Form(
        "", description="MRZ line 2 (44 chars) — extracted from scan if blank"
    ),
    nfc_data_json: str = Form(
        "", description="NFC chip data as JSON string — optional"
    ),
):
    """
    Full verification pipeline:
      1. OCR bio page scan → MRZ + VIZ fields + photo signals
      2. MRZ intrinsic checks (ICAO 9303 check digits, expiry, format)
      3. MRZ vs VIZ consistency
      4. Auto-detect document_id if not provided
      5. PRADO feature lookup from BigQuery
      6. Run all digital checks; flag visual checks for manual review
      7. Compute risk score and overall verdict

    document_id is optional — auto-detected from OCR signals if omitted.
    """
    verification_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    image_bytes = await scan.read()
    mime_type = "image/jpeg" if "jpeg" in (scan.content_type or "") else "image/png"

    # --- Step 1: OCR ---
    ocr_result = None
    try:
        ocr_result = get_ocr_extractor().extract_from_bytes(image_bytes, mime_type)
        # Use OCR MRZ if caller didn't provide
        if not mrz_line1 and ocr_result.mrz.line1:
            mrz_line1 = ocr_result.mrz.line1
        if not mrz_line2 and ocr_result.mrz.line2:
            mrz_line2 = ocr_result.mrz.line2
    except Exception as e:
        ocr_result = None

    # --- Step 2: MRZ checks ---
    mrz_result = None
    if mrz_line1 and mrz_line2:
        mrz_result = check_mrz(mrz_line1, mrz_line2)

    # --- Step 3: NFC (optional) ---
    nfc_data = None
    if nfc_data_json:
        try:
            nfc_data = json.loads(nfc_data_json)
        except json.JSONDecodeError:
            pass

    # --- Step 4: Auto-detect document_id ---
    detection_note = "provided_by_caller"
    detection_confidence = 1.0

    if not document_id and ocr_result:
        document_id, detection_confidence = get_doc_detector().detect_from_ocr(ocr_result)
        if document_id:
            detection_note = f"auto_detected (confidence={detection_confidence})"
        else:
            detection_note = "detection_failed — PRADO checks skipped"

    # Derive country code
    country_code = ""
    if document_id:
        country_code = document_id.split("-")[0]
    elif mrz_line1 and len(mrz_line1) >= 5:
        country_code = mrz_line1[2:5].replace("<", "").strip()

    # --- Steps 5+6: PRADO checks ---
    prado_result = None
    if document_id:
        try:
            prado_result = get_prado_matcher().run_checks(
                document_id=document_id,
                country_code=country_code,
                mrz_result=mrz_result,
                ocr_result=ocr_result,
                nfc_data=nfc_data,
            )
        except Exception:
            pass

    # --- Step 7: Risk score ---
    risk_score, verdict, summary = compute_risk_score(mrz_result, ocr_result, prado_result)

    return {
        "verification_id":      verification_id,
        "timestamp":            timestamp,
        "document_id":          document_id,
        "document_id_source":   detection_note,
        "detection_confidence": detection_confidence,
        "country_code":         country_code,
        "overall_verdict":      verdict,
        "risk_score":           risk_score,
        "mrz_checks":   mrz_result.to_dict() if mrz_result else {"error": "MRZ not available"},
        "ocr_checks":   ocr_result.to_dict() if ocr_result else {"error": "OCR not performed"},
        "prado_checks": prado_result.to_dict() if prado_result else {"error": "PRADO checks not performed"},
        "manual_review_items": [
            {
                "feature_id":         v.feature_id,
                "category":           v.feature_category,
                "location":           v.page_location,
                "requires_equipment": v.requires_equipment,
            }
            for v in (prado_result.visual_flags if prado_result else [])
        ],
        "summary": summary,
    }
