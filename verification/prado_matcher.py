"""
verification/prado_matcher.py
==============================
Query BigQuery for PRADO features for a given document and run
all digitally-checkable checks against OCR/MRZ/NFC data.

Visual checks are returned as a flagged list for manual review.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FeatureCheckResult:
    feature_id: str
    feature_category: str
    feature_detail: str
    page_location: str
    verification_method: str    # digital | visual | both
    requires_equipment: str
    bio_page_scannable: bool
    check_performed: bool       # False if insufficient data to check
    check_pass: Optional[bool]  # None if not performed
    check_method: str           # how the check was done
    check_note: str             # detail / reason for fail


@dataclass
class PRADOMatchResult:
    document_id: str
    country_code: str
    total_features: int = 0
    digital_checks: list = field(default_factory=list)   # list of FeatureCheckResult
    visual_flags: list = field(default_factory=list)     # features needing manual review
    digital_pass_count: int = 0
    digital_fail_count: int = 0
    digital_skip_count: int = 0
    error: str = ""

    @property
    def digital_pass_rate(self) -> float:
        total = self.digital_pass_count + self.digital_fail_count
        return self.digital_pass_count / total if total > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "document_id":       self.document_id,
            "country_code":      self.country_code,
            "total_features":    self.total_features,
            "digital_pass":      self.digital_pass_count,
            "digital_fail":      self.digital_fail_count,
            "digital_skip":      self.digital_skip_count,
            "digital_pass_rate": round(self.digital_pass_rate, 3),
            "visual_flag_count": len(self.visual_flags),
            "digital_checks": [
                {
                    "feature_id":      c.feature_id,
                    "category":        c.feature_category,
                    "detail":          c.feature_detail,
                    "location":        c.page_location,
                    "performed":       c.check_performed,
                    "pass":            c.check_pass,
                    "method":          c.check_method,
                    "note":            c.check_note,
                }
                for c in self.digital_checks
            ],
            "visual_flags": [
                {
                    "feature_id":        v.feature_id,
                    "category":          v.feature_category,
                    "detail":            v.feature_detail,
                    "location":          v.page_location,
                    "requires_equipment": v.requires_equipment,
                }
                for v in self.visual_flags
            ],
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# PRADO Matcher
# ---------------------------------------------------------------------------

class PRADOMatcher:

    # BQ query — fetch all features for a given document
    FEATURES_QUERY = """
        SELECT
            feature_id,
            feature_category,
            feature_detail,
            page_location,
            light_condition,
            description,
            colours,
            technique,
            security_element,
            verification_method,
            requires_equipment,
            bio_page_scannable
        FROM `{project}.{dataset}.prado_features`
        WHERE document_id = @document_id
        ORDER BY page_location, feature_category
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        dataset: str = "prado_verification",
    ):
        self.project_id = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self.dataset = dataset
        self._bq_client = None

    def _get_bq_client(self):
        if self._bq_client is None:
            from google.cloud import bigquery
            self._bq_client = bigquery.Client(project=self.project_id)
        return self._bq_client

    def fetch_features(self, document_id: str) -> list[dict]:
        """Fetch all PRADO features for a document from BigQuery."""
        from google.cloud import bigquery
        client = self._get_bq_client()
        query = self.FEATURES_QUERY.format(
            project=self.project_id,
            dataset=self.dataset,
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("document_id", "STRING", document_id)
            ]
        )
        result = client.query(query, job_config=job_config).result()
        return [dict(row) for row in result]

    def run_checks(
        self,
        document_id: str,
        country_code: str,
        mrz_result=None,       # MRZCheckResult from mrz_checker
        ocr_result=None,       # OCRResult from ocr_extractor
        nfc_data: Optional[dict] = None,  # parsed NFC chip data
    ) -> PRADOMatchResult:
        """
        Fetch PRADO features for document_id and run all applicable checks.

        Args:
            document_id:  e.g. "GBR-AO-06001"
            country_code: e.g. "GBR"
            mrz_result:   output of mrz_checker.check_mrz()
            ocr_result:   output of ocr_extractor.extract_from_file()
            nfc_data:     dict of parsed NFC chip data groups (DG1, DG2, etc.)
        """
        match = PRADOMatchResult(document_id=document_id, country_code=country_code)

        try:
            features = self.fetch_features(document_id)
        except Exception as e:
            match.error = f"BigQuery error: {e}"
            return match

        match.total_features = len(features)

        for feat in features:
            method = feat["verification_method"]

            if method == "visual":
                # Flag for manual review — cannot be checked digitally
                match.visual_flags.append(FeatureCheckResult(
                    feature_id=feat["feature_id"],
                    feature_category=feat["feature_category"],
                    feature_detail=feat["feature_detail"] or "",
                    page_location=feat["page_location"],
                    verification_method=method,
                    requires_equipment=feat["requires_equipment"] or "",
                    bio_page_scannable=feat["bio_page_scannable"],
                    check_performed=False,
                    check_pass=None,
                    check_method="manual_review_required",
                    check_note=f"Requires: {feat['requires_equipment']}",
                ))
                continue

            # digital or both — attempt to check
            check = self._run_single_check(feat, mrz_result, ocr_result, nfc_data)
            match.digital_checks.append(check)

            if check.check_performed:
                if check.check_pass:
                    match.digital_pass_count += 1
                else:
                    match.digital_fail_count += 1
            else:
                match.digital_skip_count += 1

        return match

    def _run_single_check(
        self,
        feat: dict,
        mrz_result,
        ocr_result,
        nfc_data: Optional[dict],
    ) -> FeatureCheckResult:
        """Dispatch a single feature to the appropriate check function."""
        cat = feat["feature_category"].lower()
        detail = (feat["feature_detail"] or "").lower()

        check = FeatureCheckResult(
            feature_id=feat["feature_id"],
            feature_category=feat["feature_category"],
            feature_detail=feat["feature_detail"] or "",
            page_location=feat["page_location"],
            verification_method=feat["verification_method"],
            requires_equipment=feat["requires_equipment"] or "",
            bio_page_scannable=feat["bio_page_scannable"],
            check_performed=False,
            check_pass=None,
            check_method="",
            check_note="",
        )

        # Route to appropriate checker
        if "electronic data" in cat or "microchip" in detail or "contactless" in detail:
            self._check_electronic_data(check, nfc_data)

        elif "facial image" in cat:
            self._check_facial_image(check, ocr_result)

        elif "numbering" in cat:
            self._check_numbering(check, mrz_result, ocr_result)

        elif "biographical data" in cat or "personal data" in cat:
            self._check_biographical_data(check, mrz_result, ocr_result)

        elif "barcode" in cat:
            self._check_barcode(check, ocr_result)

        else:
            check.check_note = f"No automated check implemented for: {cat}"

        return check

    # -----------------------------------------------------------------------
    # Individual check implementations
    # -----------------------------------------------------------------------

    def _check_electronic_data(self, check: FeatureCheckResult, nfc_data: Optional[dict]):
        check.check_method = "nfc_chip_data"
        if not nfc_data:
            check.check_performed = False
            check.check_note = "NFC data not provided"
            return
        check.check_performed = True
        # Chip must have DG1 (MRZ data) and SOD (security object)
        has_dg1 = "DG1" in nfc_data or "mrz" in nfc_data
        has_sod = "SOD" in nfc_data or "passive_auth" in nfc_data
        check.check_pass = has_dg1 and has_sod
        if not check.check_pass:
            missing = []
            if not has_dg1: missing.append("DG1 (MRZ data group)")
            if not has_sod:  missing.append("SOD (security object)")
            check.check_note = f"Missing chip data: {', '.join(missing)}"
        else:
            pa_pass = nfc_data.get("passive_auth_pass", True)
            check.check_pass = pa_pass
            check.check_note = "Passive authentication " + ("PASS" if pa_pass else "FAIL")

    def _check_facial_image(self, check: FeatureCheckResult, ocr_result):
        check.check_method = "ocr_photo_presence"
        if not ocr_result:
            check.check_performed = False
            check.check_note = "OCR result not provided"
            return
        check.check_performed = True
        check.check_pass = ocr_result.viz.photo_present
        if check.check_pass:
            check.check_note = f"Photo present ({ocr_result.viz.photo_type})"
        else:
            check.check_note = "No photo detected in bio page scan"

    def _check_numbering(self, check: FeatureCheckResult, mrz_result, ocr_result):
        check.check_method = "mrz_doc_number"
        if not mrz_result:
            check.check_performed = False
            check.check_note = "MRZ result not provided"
            return
        check.check_performed = True
        check.check_pass = mrz_result.cd1_pass  # check digit 1 = doc number
        check.check_note = (
            f"Document number CD1: {'PASS' if mrz_result.cd1_pass else 'FAIL'}"
        )
        # Bonus: cross-check with OCR
        if ocr_result and ocr_result.doc_number_match is not None:
            if not ocr_result.doc_number_match:
                check.check_pass = False
                check.check_note += " | MRZ/VIZ doc number MISMATCH"

    def _check_biographical_data(self, check: FeatureCheckResult, mrz_result, ocr_result):
        check.check_method = "mrz_viz_consistency"
        if not (mrz_result and ocr_result):
            check.check_performed = False
            check.check_note = "MRZ or OCR result not provided"
            return
        check.check_performed = True
        issues = []
        if ocr_result.name_match is False:
            issues.append("name mismatch MRZ/VIZ")
        if ocr_result.dob_match is False:
            issues.append("DOB mismatch MRZ/VIZ")
        if ocr_result.expiry_match is False:
            issues.append("expiry mismatch MRZ/VIZ")
        if ocr_result.nationality_match is False:
            issues.append("nationality mismatch MRZ/VIZ")
        check.check_pass = len(issues) == 0
        check.check_note = "All fields consistent" if check.check_pass else f"Issues: {'; '.join(issues)}"

    def _check_barcode(self, check: FeatureCheckResult, ocr_result):
        check.check_method = "ocr_barcode_presence"
        check.check_performed = False
        check.check_note = "Barcode check requires dedicated barcode scanner input"


# ---------------------------------------------------------------------------
# Standalone demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("PRADOMatcher — requires BigQuery connection and GCP_PROJECT_ID env var.")
    print("Run via the FastAPI endpoint: POST /verify")
