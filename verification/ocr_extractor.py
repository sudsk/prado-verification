"""
verification/ocr_extractor.py
==============================
Extract passport bio page fields using Gemini Vision (gemini-3-flash-preview).

Extracts:
  - MRZ lines (2 x 44 chars)
  - VIZ fields: surname, given names, nationality, DOB, sex, expiry, doc number, place of birth
  - Photo presence and type
  - Document number from top of page
  - Issuing authority

Then cross-checks MRZ vs VIZ for consistency.
"""

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class VIZFields:
    """Visual Inspection Zone fields extracted by OCR."""
    surname: str = ""
    given_names: str = ""
    nationality: str = ""
    date_of_birth: str = ""
    sex: str = ""
    expiry_date: str = ""
    doc_number: str = ""
    place_of_birth: str = ""
    issuing_authority: str = ""
    photo_present: bool = False
    photo_type: str = ""       # colour / monochrome / black & white


@dataclass
class MRZLines:
    line1: str = ""
    line2: str = ""

    @property
    def complete(self) -> bool:
        return len(self.line1) == 44 and len(self.line2) == 44


@dataclass
class OCRResult:
    viz: VIZFields = field(default_factory=VIZFields)
    mrz: MRZLines = field(default_factory=MRZLines)

    # Consistency checks (MRZ vs VIZ)
    name_match: Optional[bool] = None
    dob_match: Optional[bool] = None
    doc_number_match: Optional[bool] = None
    expiry_match: Optional[bool] = None
    nationality_match: Optional[bool] = None

    ocr_confidence: str = ""   # high / medium / low
    error: str = ""

    @property
    def consistency_pass(self) -> bool:
        checks = [self.name_match, self.dob_match, self.doc_number_match, self.expiry_match]
        non_null = [c for c in checks if c is not None]
        return all(non_null) if non_null else False

    def to_dict(self) -> dict:
        return {
            "viz": {
                "surname":           self.viz.surname,
                "given_names":       self.viz.given_names,
                "nationality":       self.viz.nationality,
                "date_of_birth":     self.viz.date_of_birth,
                "sex":               self.viz.sex,
                "expiry_date":       self.viz.expiry_date,
                "doc_number":        self.viz.doc_number,
                "place_of_birth":    self.viz.place_of_birth,
                "issuing_authority": self.viz.issuing_authority,
                "photo_present":     self.viz.photo_present,
                "photo_type":        self.viz.photo_type,
            },
            "mrz": {
                "line1":    self.mrz.line1,
                "line2":    self.mrz.line2,
                "complete": self.mrz.complete,
            },
            "consistency": {
                "name_match":        self.name_match,
                "dob_match":         self.dob_match,
                "doc_number_match":  self.doc_number_match,
                "expiry_match":      self.expiry_match,
                "nationality_match": self.nationality_match,
                "overall_pass":      self.consistency_pass,
            },
            "ocr_confidence": self.ocr_confidence,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Gemini Vision extractor
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You are a passport OCR system. Extract all fields from this passport bio page image.

Return ONLY valid JSON with this exact structure:
{
  "mrz_line1": "<44-character MRZ line 1 or empty string>",
  "mrz_line2": "<44-character MRZ line 2 or empty string>",
  "viz_surname": "<surname as printed in VIZ>",
  "viz_given_names": "<given names as printed in VIZ>",
  "viz_nationality": "<nationality as printed>",
  "viz_date_of_birth": "<DOB as DD MMM YYYY or YYYY-MM-DD>",
  "viz_sex": "<M or F or X>",
  "viz_expiry_date": "<expiry as DD MMM YYYY or YYYY-MM-DD>",
  "viz_doc_number": "<document/passport number as printed>",
  "viz_place_of_birth": "<place of birth if visible, else empty string>",
  "viz_issuing_authority": "<issuing authority if visible, else empty string>",
  "photo_present": true,
  "photo_type": "<colour or monochrome or black and white>",
  "confidence": "<high or medium or low>"
}

Rules:
- MRZ lines must be exactly 44 characters using only A-Z, 0-9 and < (filler)
- If a field is not visible or legible, use empty string
- Do not include any text outside the JSON object
- Preserve exact MRZ characters — do not correct or interpret them
"""


class PassportOCRExtractor:

    def __init__(self, project_id: Optional[str] = None, location: str = "europe-west2"):
        self.project_id = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self.location = location
        self._client = None

    def _get_client(self):
        if self._client is None:
            import vertexai
            from vertexai.generative_models import GenerativeModel
            vertexai.init(project=self.project_id, location=self.location)
            self._client = GenerativeModel("gemini-3-flash-preview")
        return self._client

    def extract_from_file(self, image_path: str) -> OCRResult:
        """Extract passport fields from an image file (JPEG/PNG)."""
        path = Path(image_path)
        if not path.exists():
            result = OCRResult()
            result.error = f"File not found: {image_path}"
            return result

        with open(image_path, "rb") as f:
            image_bytes = f.read()

        suffix = path.suffix.lower()
        mime_type = "image/jpeg" if suffix in (".jpg", ".jpeg") else "image/png"
        return self.extract_from_bytes(image_bytes, mime_type)

    def extract_from_bytes(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> OCRResult:
        """Extract passport fields from raw image bytes."""
        try:
            from vertexai.generative_models import GenerativeModel, Part, Image
            import vertexai

            vertexai.init(project=self.project_id, location=self.location)
            model = GenerativeModel("gemini-3-flash-preview")

            image_part = Part.from_data(data=image_bytes, mime_type=mime_type)
            response = model.generate_content([EXTRACTION_PROMPT, image_part])
            raw = response.text.strip()

            return self._parse_response(raw)

        except Exception as e:
            result = OCRResult()
            result.error = str(e)
            return result

    def extract_from_base64(self, b64_string: str, mime_type: str = "image/jpeg") -> OCRResult:
        """Extract from a base64-encoded image."""
        image_bytes = base64.b64decode(b64_string)
        return self.extract_from_bytes(image_bytes, mime_type)

    def _parse_response(self, raw: str) -> OCRResult:
        """Parse Gemini JSON response into OCRResult."""
        result = OCRResult()

        # Strip markdown code fences if present
        raw = re.sub(r"```(?:json)?", "", raw).strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            result.error = f"JSON parse error: {e} — raw: {raw[:200]}"
            return result

        # MRZ
        result.mrz.line1 = data.get("mrz_line1", "").upper().strip()
        result.mrz.line2 = data.get("mrz_line2", "").upper().strip()

        # VIZ
        viz = result.viz
        viz.surname           = data.get("viz_surname", "")
        viz.given_names       = data.get("viz_given_names", "")
        viz.nationality       = data.get("viz_nationality", "")
        viz.date_of_birth     = data.get("viz_date_of_birth", "")
        viz.sex               = data.get("viz_sex", "")
        viz.expiry_date       = data.get("viz_expiry_date", "")
        viz.doc_number        = data.get("viz_doc_number", "")
        viz.place_of_birth    = data.get("viz_place_of_birth", "")
        viz.issuing_authority = data.get("viz_issuing_authority", "")
        viz.photo_present     = bool(data.get("photo_present", False))
        viz.photo_type        = data.get("photo_type", "")
        result.ocr_confidence = data.get("confidence", "")

        # Run consistency checks
        self._check_consistency(result)
        return result

    def _check_consistency(self, result: OCRResult):
        """Cross-check MRZ fields against VIZ fields."""
        mrz = result.mrz
        viz = result.viz

        if not mrz.complete:
            return  # can't cross-check without complete MRZ

        # --- Document number (MRZ positions 0-8 of line2, strip trailing <) ---
        mrz_docnum = mrz.line2[0:9].rstrip("<").strip()
        viz_docnum = re.sub(r"[^A-Z0-9]", "", viz.doc_number.upper())
        if mrz_docnum and viz_docnum:
            result.doc_number_match = mrz_docnum == viz_docnum

        # --- Date of birth (MRZ YYMMDD) ---
        mrz_dob = mrz.line2[13:19]
        viz_dob_normalised = self._normalise_date(viz.date_of_birth)
        if mrz_dob and viz_dob_normalised:
            result.dob_match = mrz_dob == viz_dob_normalised

        # --- Expiry date ---
        mrz_exp = mrz.line2[21:27]
        viz_exp_normalised = self._normalise_date(viz.expiry_date)
        if mrz_exp and viz_exp_normalised:
            result.expiry_match = mrz_exp == viz_exp_normalised

        # --- Name (surname from MRZ line1, positions 5-43) ---
        mrz_name_field = mrz.line1[5:44]
        mrz_surname = mrz_name_field.split("<<")[0].replace("<", " ").strip().upper()
        viz_surname = viz.surname.upper().strip()
        if mrz_surname and viz_surname:
            # Fuzzy: VIZ surname should start with MRZ surname (truncation possible)
            result.name_match = (
                viz_surname.startswith(mrz_surname)
                or mrz_surname.startswith(viz_surname)
                or mrz_surname == viz_surname
            )

        # --- Nationality ---
        mrz_nat = mrz.line2[10:13].replace("<", "").strip()
        viz_nat = viz.nationality.upper()[:3].strip()
        if mrz_nat and viz_nat:
            result.nationality_match = mrz_nat == viz_nat

    @staticmethod
    def _normalise_date(date_str: str) -> str:
        """Convert various date formats to MRZ YYMMDD."""
        if not date_str:
            return ""

        # Already YYMMDD
        if re.match(r"^\d{6}$", date_str):
            return date_str

        # YYYY-MM-DD or YYYY/MM/DD
        m = re.match(r"(\d{4})[-/](\d{2})[-/](\d{2})", date_str)
        if m:
            yy = m.group(1)[2:]
            return f"{yy}{m.group(2)}{m.group(3)}"

        # DD MMM YYYY or D MMM YYYY
        months = {
            "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
            "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
            "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
        }
        m = re.match(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", date_str.upper())
        if m:
            dd  = m.group(1).zfill(2)
            mm  = months.get(m.group(2), "00")
            yy  = m.group(3)[2:]
            return f"{yy}{mm}{dd}"

        return ""


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python ocr_extractor.py <passport_scan.jpg>")
        print("\nSet GCP_PROJECT_ID env var before running.")
        sys.exit(1)

    extractor = PassportOCRExtractor()
    result = extractor.extract_from_file(sys.argv[1])
    print(json.dumps(result.to_dict(), indent=2))
    print(f"\nConsistency: {'PASS' if result.consistency_pass else 'FAIL'}")
