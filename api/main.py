"""
api/main.py — PRADO Passport Verification API v0.4.0
"""

import base64, json, os, uuid
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
from verification.biometric_checker import BiometricChecker

app = FastAPI(
    title="PRADO Passport Verification API",
    description="Automated passport authenticity verification using PRADO security feature data",
    version="0.4.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_ocr_extractor    = None
_prado_matcher    = None
_doc_detector     = None
_biometric_checker = None


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


def get_biometric_checker() -> BiometricChecker:
    global _biometric_checker
    if _biometric_checker is None:
        threshold = float(os.environ.get("FACE_MATCH_THRESHOLD", "0.75"))
        _biometric_checker = BiometricChecker(
            project_id=os.environ.get("GCP_PROJECT_ID"),
            location=os.environ.get("GCP_LOCATION", "us-central1"),
            match_threshold=threshold,
        )
    return _biometric_checker


# ---------------------------------------------------------------------------
# Risk scorer
# ---------------------------------------------------------------------------

def compute_risk_score(mrz_result, ocr_result, prado_result, biometric_result):
    score = 0.0
    reasons = []

    # MRZ checks (max 0.50)
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

    # OCR consistency (max 0.35)
    if ocr_result:
        if not ocr_result.consistency_pass:
            score += 0.25
            reasons.append("MRZ / VIZ field mismatch")
        if not ocr_result.viz.photo_present:
            score += 0.10
            reasons.append("No photo detected in bio page")

    # Biometric face match (max 0.30)
    if biometric_result:
        if biometric_result.error:
            pass  # don't penalise if biometric check errored
        elif not biometric_result.face_detected_selfie:
            score += 0.10
            reasons.append("No face detected in selfie")
        elif not biometric_result.face_match_pass:
            score += 0.30
            reasons.append(
                f"Face match failed (score={biometric_result.face_match_score:.2f}, "
                f"threshold={biometric_result.face_match_threshold})"
            )

    # PRADO digital checks (max 0.20)
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
        "version": "0.4.0",
        "status": "ok",
        "docs": "/docs",
        "endpoints": {
            "POST /verify":        "Full verification — scan + optional selfie + optional document_id",
            "POST /verify/mrz":    "MRZ-only check — no image or BQ needed",
            "POST /verify/biometric": "Face match only — passport scan + selfie",
            "POST /detect":        "Detect document_id from scan alone",
            "GET  /document/{id}": "Fetch PRADO features for a document",
            "GET  /health":        "Liveness check",
        }
    }


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/document/{document_id}")
async def get_document_features(document_id: str):
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
    """Detect the PRADO document_id from a passport scan alone."""
    image_bytes = await scan.read()
    mime_type = "image/jpeg" if "jpeg" in (scan.content_type or "") else "image/png"

    ocr = get_ocr_extractor().extract_from_bytes(image_bytes, mime_type)
    if ocr.error:
        raise HTTPException(status_code=422, detail=f"OCR failed: {ocr.error}")

    doc_id, confidence = get_doc_detector().detect_from_ocr(ocr)

    return {
        "detected_document_id": doc_id,
        "confidence":           confidence,
        "country_code":         ocr.mrz.line1[2:5].replace("<","").strip() if ocr.mrz.line1 else "",
        "detection_signals": {
            "photo_integration_technique": ocr.viz.photo_integration_technique,
            "document_title":              ocr.viz.document_title,
            "date_of_issue":               ocr.viz.date_of_issue,
        },
        "ocr_summary": {
            "surname":      ocr.viz.surname,
            "doc_number":   ocr.viz.doc_number,
            "photo_type":   ocr.viz.photo_type,
            "mrz_complete": ocr.mrz.complete,
        },
    }


@app.post("/verify/mrz")
async def verify_mrz_only(request: dict):
    """MRZ intrinsic checks only — no image or BigQuery needed."""
    result = check_mrz(request.get("line1", ""), request.get("line2", ""))
    return {
        "verification_id": str(uuid.uuid4()),
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "mrz_checks":      result.to_dict(),
        "overall_verdict": "PASS" if result.overall_pass else "FAIL",
    }


@app.post("/verify/biometric")
async def verify_biometric_only(
    scan:   UploadFile = File(..., description="Passport bio page scan (JPEG/PNG)"),
    selfie: UploadFile = File(..., description="Live photo or selfie (JPEG/PNG)"),
):
    """Face match only — no BQ or MRZ checks."""
    scan_bytes   = await scan.read()
    selfie_bytes = await selfie.read()

    scan_mime   = "image/jpeg" if "jpeg" in (scan.content_type   or "") else "image/png"
    selfie_mime = "image/jpeg" if "jpeg" in (selfie.content_type or "") else "image/png"

    result = get_biometric_checker().check_from_bytes(
        passport_bytes=scan_bytes, passport_mime=scan_mime,
        selfie_bytes=selfie_bytes, selfie_mime=selfie_mime,
    )

    return {
        "verification_id": str(uuid.uuid4()),
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "biometric":       result.to_dict(),
        "overall_verdict": "PASS" if result.overall_pass else "FAIL",
    }


@app.post("/verify")
async def verify_passport(
    scan:          UploadFile  = File(...,  description="Passport bio page scan (JPEG/PNG)"),
    selfie:        Optional[UploadFile] = File(None, description="Live photo or selfie — optional but recommended"),
    document_id:   str = Form("",  description="PRADO document ID — auto-detected if blank"),
    mrz_line1:     str = Form("",  description="MRZ line 1 (44 chars) — extracted from scan if blank"),
    mrz_line2:     str = Form("",  description="MRZ line 2 (44 chars) — extracted from scan if blank"),
    nfc_data_json: str = Form("",  description="NFC chip data as JSON — optional"),
):
    """
    Full verification pipeline:
      1. OCR bio page → MRZ + VIZ + photo signals
      2. MRZ intrinsic checks (ICAO 9303 check digits, expiry, format)
      3. MRZ vs VIZ consistency
      4. Auto-detect document_id if not provided
      5. PRADO feature lookup + digital checks + visual flags
      6. Biometric face match (if selfie provided)
      7. Risk score + overall verdict

    selfie is optional — if not provided, biometric check is skipped.
    document_id is optional — auto-detected from OCR signals.
    """
    verification_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    scan_bytes = await scan.read()
    scan_mime  = "image/jpeg" if "jpeg" in (scan.content_type or "") else "image/png"

    # --- Step 1: OCR ---
    ocr_result = None
    try:
        ocr_result = get_ocr_extractor().extract_from_bytes(scan_bytes, scan_mime)
        if not mrz_line1 and ocr_result.mrz.line1:
            mrz_line1 = ocr_result.mrz.line1
        if not mrz_line2 and ocr_result.mrz.line2:
            mrz_line2 = ocr_result.mrz.line2
    except Exception:
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
        detection_note = (
            f"auto_detected (confidence={detection_confidence})"
            if document_id else "detection_failed — PRADO checks skipped"
        )

    country_code = ""
    if document_id:
        country_code = document_id.split("-")[0]
    elif mrz_line1 and len(mrz_line1) >= 5:
        country_code = mrz_line1[2:5].replace("<", "").strip()

    # --- Steps 5: PRADO checks ---
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

    # --- Step 6: Biometric face match ---
    biometric_result = None
    if selfie:
        try:
            selfie_bytes = await selfie.read()
            selfie_mime  = "image/jpeg" if "jpeg" in (selfie.content_type or "") else "image/png"
            biometric_result = get_biometric_checker().check_from_bytes(
                passport_bytes=scan_bytes, passport_mime=scan_mime,
                selfie_bytes=selfie_bytes,  selfie_mime=selfie_mime,
            )
        except Exception as e:
            pass

    # --- Step 7: Risk score ---
    risk_score, verdict, summary = compute_risk_score(
        mrz_result, ocr_result, prado_result, biometric_result
    )

    return {
        "verification_id":      verification_id,
        "timestamp":            timestamp,
        "document_id":          document_id,
        "document_id_source":   detection_note,
        "detection_confidence": detection_confidence,
        "country_code":         country_code,
        "overall_verdict":      verdict,
        "risk_score":           risk_score,
        "mrz_checks":           mrz_result.to_dict() if mrz_result
                                else {"error": "MRZ not available"},
        "ocr_checks":           ocr_result.to_dict() if ocr_result
                                else {"error": "OCR not performed"},
        "biometric_checks":     biometric_result.to_dict() if biometric_result
                                else {"skipped": "No selfie provided"},
        "prado_checks":         prado_result.to_dict() if prado_result
                                else {"error": "PRADO checks not performed"},
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


# ---------------------------------------------------------------------------
# Regula integration endpoint
# ---------------------------------------------------------------------------

@app.post("/verify/from-regula")
async def verify_from_regula(
    regula_json: UploadFile = File(..., description="Regula /process response JSON"),
    selfie: Optional[UploadFile] = File(None, description="Selfie for biometric check"),
):
    """
    Accept Regula Document Reader /process output directly.
    Adapts it to our schema then runs full PRADO + biometric verification.

    Works with both real Regula hardware output and simulated output
    from regula_simulator.py.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from ingestion.regula_adapter import RegulaAdapter

    verification_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    # Parse Regula JSON
    try:
        regula_data = json.loads(await regula_json.read())
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid Regula JSON: {e}")

    # Read selfie if provided
    selfie_b64 = ""
    selfie_bytes = None
    selfie_mime  = "image/jpeg"
    if selfie:
        selfie_bytes = await selfie.read()
        selfie_mime  = "image/jpeg" if "jpeg" in (selfie.content_type or "") else "image/png"
        selfie_b64   = base64.b64encode(selfie_bytes).decode()

    # Adapt Regula output
    adapter = RegulaAdapter()
    adapted = adapter.adapt(regula_data, selfie_b64)

    # Run MRZ checks
    mrz_result = None
    if adapted.mrz_line1 and adapted.mrz_line2:
        mrz_result = check_mrz(adapted.mrz_line1, adapted.mrz_line2)

    # Run PRADO checks
    prado_result = None
    if adapted.document_id:
        try:
            # Build a minimal OCR result from adapted data for PRADO matcher
            from verification.ocr_extractor import OCRResult, VIZFields, MRZLines
            mock_ocr = OCRResult()
            mock_ocr.viz.surname     = adapted.surname
            mock_ocr.viz.given_names = adapted.given_names
            mock_ocr.viz.doc_number  = adapted.doc_number
            mock_ocr.viz.date_of_birth = adapted.date_of_birth
            mock_ocr.viz.expiry_date = adapted.expiry_date
            mock_ocr.viz.nationality = adapted.nationality
            mock_ocr.viz.photo_present = bool(adapted.portrait_b64)
            mock_ocr.viz.photo_type  = "colour"
            mock_ocr.mrz.line1 = adapted.mrz_line1
            mock_ocr.mrz.line2 = adapted.mrz_line2
            mock_ocr.name_match       = adapted.regula_mrz_viz_match == 1
            mock_ocr.dob_match        = adapted.regula_mrz_viz_match == 1
            mock_ocr.doc_number_match = adapted.regula_mrz_viz_match == 1
            mock_ocr.expiry_match     = adapted.regula_mrz_viz_match == 1

            # Build NFC data from Regula RFID output
            nfc_data = {
                "bac_pace_success":  True,
                "passive_auth_pass": adapted.regula_nfc_pa_pass,
                "active_auth_pass":  adapted.regula_nfc_aa_pass,
                "chip_auth_pass":    None,
                "DG1": {
                    "mrz_line1": adapted.mrz_line1,
                    "mrz_line2": adapted.mrz_line2,
                },
                "DG2": {"portrait_b64": adapted.chip_portrait_b64} if adapted.chip_portrait_b64 else None,
                "SOD": {"dg_hashes": {}, "signature_base64": None},
            } if adapted.regula_nfc_overall != 2 else None

            prado_result = get_prado_matcher().run_checks(
                document_id=adapted.document_id,
                country_code=adapted.country_code,
                mrz_result=mrz_result,
                ocr_result=mock_ocr,
                nfc_data=nfc_data,
            )
        except Exception as e:
            pass

    # Biometric check using white image vs selfie
    biometric_result = None
    if selfie_bytes and adapted.white_image_b64:
        try:
            passport_bytes = base64.b64decode(adapted.white_image_b64)
            biometric_result = get_biometric_checker().check_from_bytes(
                passport_bytes=passport_bytes, passport_mime="image/jpeg",
                selfie_bytes=selfie_bytes,     selfie_mime=selfie_mime,
            )
        except Exception:
            pass

    # Risk score
    risk_score, verdict, summary = compute_risk_score(
        mrz_result, None, prado_result, biometric_result
    )

    # Add Regula authenticity check results to summary
    regula_auth_summary = {
        "overall":  "OK" if adapted.regula_overall_status == 1 else "ERROR",
        "uv":       adapted.regula_uv_pass,
        "ir":       adapted.regula_ir_pass,
        "hologram": adapted.regula_hologram_pass,
        "fibers":   adapted.regula_fibers_pass,
        "nfc_pa":   adapted.regula_nfc_pa_pass,
    }

    return {
        "verification_id":    verification_id,
        "timestamp":          timestamp,
        "document_id":        adapted.document_id,
        "document_id_source": "regula_output",
        "country_code":       adapted.country_code,
        "simulated_input":    adapted.simulated,
        "overall_verdict":    verdict,
        "risk_score":         risk_score,
        "mrz_checks":         mrz_result.to_dict() if mrz_result else {"error": "MRZ not available"},
        "regula_auth_checks": regula_auth_summary,
        "prado_checks":       prado_result.to_dict() if prado_result else {"error": "PRADO not run"},
        "biometric_checks":   biometric_result.to_dict() if biometric_result else {"skipped": "No selfie"},
        "prado_digital_inputs": adapted.prado_digital_inputs,
        "manual_review_items": [
            {"feature_id": v.feature_id, "category": v.feature_category,
             "location": v.page_location, "requires_equipment": v.requires_equipment}
            for v in (prado_result.visual_flags if prado_result else [])
        ],
        "summary": summary,
    }
