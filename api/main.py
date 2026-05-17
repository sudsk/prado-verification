"""
api/main.py
============
FastAPI verification API.

Endpoints:
  POST /verify          — upload bio page scan → full verification verdict
  POST /verify/mrz      — check MRZ string only (no image needed)
  GET  /document/{id}   — fetch PRADO features for a document from BQ
  GET  /health          — liveness check

Usage:
  uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

  # Verify a passport scan
  curl -X POST http://localhost:8000/verify \\
    -F "scan=@passport_scan.jpg" \\
    -F "document_id=GBR-AO-06001" \\
    -F "mrz_line1=P<GBRSPECIMEN<<ANGELA<ZOE<<<<<<<<<<<<<<<<<<<<<<" \\
    -F "mrz_line2=9250647140GBR8812049F2010855<<<<<<<<<<<<<<<6"

  # MRZ-only check
  curl -X POST http://localhost:8000/verify/mrz \\
    -H "Content-Type: application/json" \\
    -d '{"line1": "P<GBR...", "line2": "925..."}'
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add parent to path when running directly
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from verification.mrz_checker import check_mrz, MRZCheckResult
from verification.ocr_extractor import PassportOCRExtractor
from verification.prado_matcher import PRADOMatcher


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PRADO Passport Verification API",
    description="Automated passport authenticity verification using PRADO security feature data",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Singletons — initialised on first use
_ocr_extractor: Optional[PassportOCRExtractor] = None
_prado_matcher: Optional[PRADOMatcher] = None


def get_ocr_extractor() -> PassportOCRExtractor:
    global _ocr_extractor
    if _ocr_extractor is None:
        _ocr_extractor = PassportOCRExtractor(
            project_id=os.environ.get("GCP_PROJECT_ID"),
            location=os.environ.get("GCP_LOCATION", "europe-west2"),
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


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class VerificationVerdict(BaseModel):
    verification_id: str
    timestamp: str
    document_id: str
    country_code: str
    overall_verdict: str       # PASS / REFER / FAIL
    risk_score: float          # 0.0 (clean) to 1.0 (high risk)
    mrz_checks: dict
    ocr_checks: dict
    prado_checks: dict
    manual_review_items: list
    summary: str


class MRZOnlyRequest(BaseModel):
    line1: str
    line2: str


# ---------------------------------------------------------------------------
# Risk scorer
# ---------------------------------------------------------------------------

def compute_risk_score(
    mrz_result: MRZCheckResult,
    ocr_result,
    prado_result,
) -> tuple[float, str]:
    """
    Compute a 0.0–1.0 risk score and overall verdict.

    Weights:
      MRZ check digits  0.35 — highest weight: cryptographic
      MRZ expiry        0.10
      OCR consistency   0.25 — cross-source agreement
      PRADO digital     0.20
      Photo present     0.10
    """
    score = 0.0
    reasons = []

    # MRZ check digits (0.35)
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

    # OCR consistency (0.25)
    if ocr_result:
        if not ocr_result.consistency_pass:
            score += 0.25
            reasons.append("MRZ / VIZ field mismatch")
        if not ocr_result.viz.photo_present:
            score += 0.10
            reasons.append("No photo detected")

    # PRADO digital checks (0.20)
    if prado_result and prado_result.digital_pass_count + prado_result.digital_fail_count > 0:
        fail_rate = prado_result.digital_fail_count / (
            prado_result.digital_pass_count + prado_result.digital_fail_count
        )
        score += fail_rate * 0.20
        if fail_rate > 0:
            reasons.append(f"PRADO digital checks: {prado_result.digital_fail_count} failed")

    score = min(score, 1.0)

    if score < 0.15:
        verdict = "PASS"
    elif score < 0.45:
        verdict = "REFER"
    else:
        verdict = "FAIL"

    summary = f"{verdict} (risk={score:.2f})"
    if reasons:
        summary += f" — {'; '.join(reasons)}"

    return round(score, 3), verdict, summary


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/document/{document_id}")
async def get_document_features(document_id: str):
    """Fetch all PRADO features for a document from BigQuery."""
    try:
        matcher = get_prado_matcher()
        features = matcher.fetch_features(document_id)
        return {
            "document_id": document_id,
            "feature_count": len(features),
            "features": features,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/verify/mrz", response_model=dict)
async def verify_mrz_only(request: MRZOnlyRequest):
    """Run MRZ intrinsic checks only — no image required."""
    result = check_mrz(request.line1, request.line2)
    return {
        "verification_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mrz_checks": result.to_dict(),
        "overall_verdict": "PASS" if result.overall_pass else "FAIL",
    }


@app.post("/verify", response_model=VerificationVerdict)
async def verify_passport(
    scan: UploadFile = File(..., description="Bio page scan (JPEG/PNG)"),
    document_id: str = Form(..., description="PRADO document ID e.g. GBR-AO-06001"),
    mrz_line1: str = Form("", description="MRZ line 1 (44 chars) — optional if extractable from scan"),
    mrz_line2: str = Form("", description="MRZ line 2 (44 chars) — optional if extractable from scan"),
    nfc_data_json: str = Form("", description="NFC chip data as JSON string — optional"),
):
    """
    Full passport verification pipeline:
    1. OCR the bio page scan → extract MRZ + VIZ fields
    2. Run MRZ intrinsic checks
    3. Cross-check MRZ vs VIZ
    4. Fetch PRADO features from BigQuery
    5. Run all digital PRADO checks
    6. Flag visual checks for manual review
    7. Compute risk score and verdict
    """
    verification_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    # Infer country code from document_id
    country_code = document_id.split("-")[0] if document_id else ""

    # --- Step 1: OCR ---
    image_bytes = await scan.read()
    mime_type = "image/jpeg" if scan.content_type == "image/jpeg" else "image/png"

    ocr_result = None
    try:
        extractor = get_ocr_extractor()
        ocr_result = extractor.extract_from_bytes(image_bytes, mime_type)
        # If MRZ lines weren't provided, use OCR-extracted ones
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

    # --- Step 3: NFC data (optional) ---
    nfc_data = None
    if nfc_data_json:
        try:
            nfc_data = json.loads(nfc_data_json)
        except json.JSONDecodeError:
            pass

    # --- Step 4 + 5: PRADO checks ---
    prado_result = None
    if document_id:
        try:
            matcher = get_prado_matcher()
            prado_result = matcher.run_checks(
                document_id=document_id,
                country_code=country_code,
                mrz_result=mrz_result,
                ocr_result=ocr_result,
                nfc_data=nfc_data,
            )
        except Exception as e:
            prado_result = None

    # --- Step 6: Risk score ---
    risk_score, verdict, summary = compute_risk_score(mrz_result, ocr_result, prado_result)

    # --- Build response ---
    return VerificationVerdict(
        verification_id=verification_id,
        timestamp=timestamp,
        document_id=document_id,
        country_code=country_code,
        overall_verdict=verdict,
        risk_score=risk_score,
        mrz_checks=mrz_result.to_dict() if mrz_result else {"error": "MRZ not available"},
        ocr_checks=ocr_result.to_dict() if ocr_result else {"error": "OCR not performed"},
        prado_checks=prado_result.to_dict() if prado_result else {"error": "PRADO check not performed"},
        manual_review_items=[
            {
                "feature_id":         v.feature_id,
                "category":           v.feature_category,
                "location":           v.page_location,
                "requires_equipment": v.requires_equipment,
            }
            for v in (prado_result.visual_flags if prado_result else [])
        ],
        summary=summary,
    )
