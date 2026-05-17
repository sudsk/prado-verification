"""
verification/ocr_extractor.py
==============================
Extract passport bio page fields using Gemini Vision (gemini-2.5-flash).

Extracts:
  - MRZ lines (2 x 44 chars)
  - VIZ fields: surname, given names, nationality, DOB, sex, expiry,
    doc number, place of birth, issue date, issuing authority
  - Photo: presence, type (colour/B&W/monochrome), integration technique
  - Cover colour (if visible)
  - Document title

Then cross-checks MRZ vs VIZ for consistency.
"""

import base64
import json
import os
import re
from dataclasses import dataclass, field
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
    date_of_issue: str = ""             # NEW — used for document version detection
    issuing_authority: str = ""
    photo_present: bool = False
    photo_type: str = ""                # colour / monochrome / black and white
    photo_integration_technique: str = "" # NEW — inkjet printing / laser engraving
    cover_colour: str = ""              # NEW — blue / burgundy (if cover visible)
    document_title: str = ""            # NEW — BRITISH PASSPORT / PASSPORT


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

    ocr_confidence: str = ""
    error: str = ""

    @property
    def consistency_pass(self) -> bool:
        checks = [self.name_match, self.dob_match,
                  self.doc_number_match, self.expiry_match]
        non_null = [c for c in checks if c is not None]
        return all(non_null) if non_null else False

    def to_dict(self) -> dict:
        return {
            "viz": {
                "surname":                    self.viz.surname,
                "given_names":                self.viz.given_names,
                "nationality":                self.viz.nationality,
                "date_of_birth":              self.viz.date_of_birth,
                "sex":                        self.viz.sex,
                "expiry_date":                self.viz.expiry_date,
                "doc_number":                 self.viz.doc_number,
                "place_of_birth":             self.viz.place_of_birth,
                "date_of_issue":              self.viz.date_of_issue,
                "issuing_authority":          self.viz.issuing_authority,
                "photo_present":              self.viz.photo_present,
                "photo_type":                 self.viz.photo_type,
                "photo_integration_technique": self.viz.photo_integration_technique,
                "cover_colour":               self.viz.cover_colour,
                "document_title":             self.viz.document_title,
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
  "mrz_line1": "<MRZ line 1 — EXACTLY 44 characters, only A-Z 0-9 and <, pad with < if needed>",
  "mrz_line2": "<MRZ line 2 — EXACTLY 44 characters, only A-Z 0-9 and <, pad with < if needed>",
  "viz_surname": "<surname as printed in VIZ>",
  "viz_given_names": "<given names as printed in VIZ>",
  "viz_nationality": "<nationality as printed>",
  "viz_date_of_birth": "<DOB as DD MMM YYYY>",
  "viz_sex": "<M or F or X>",
  "viz_expiry_date": "<expiry date as DD MMM YYYY>",
  "viz_date_of_issue": "<date of issue as DD MMM YYYY if visible, else empty string>",
  "viz_doc_number": "<document/passport number as printed>",
  "viz_place_of_birth": "<place of birth if visible, else empty string>",
  "viz_issuing_authority": "<issuing authority if visible, else empty string>",
  "photo_present": true,
  "photo_type": "<colour or monochrome or black and white>",
  "photo_integration_technique": "<inkjet printing or laser engraving or unknown>",
  "cover_colour": "<blue or burgundy or red or green or black or unknown>",
  "document_title": "<exact title printed on document e.g. BRITISH PASSPORT or PASSPORT>",
  "confidence": "<high or medium or low>"
}

Critical rules for MRZ:
- MRZ lines must be EXACTLY 44 characters — count every character carefully
- Only valid characters: A-Z, 0-9, and < (filler/separator)
- Pad with < to reach exactly 44 if the line appears shorter
- The last character of line 2 is the composite check digit — never truncate it
- Do not correct, interpret or guess MRZ characters — transcribe exactly as printed

Other rules:
- If a field is not visible or legible, use empty string
- Return ONLY the JSON object — no preamble, no markdown, no explanation
- photo_integration_technique: look for fine dot patterns (inkjet) vs engraved/etched appearance (laser)
- cover_colour: only fill if the cover is visible in the image, else empty string
"""


class PassportOCRExtractor:

    def __init__(self, project_id: Optional[str] = None,
                 location: str = "us-central1"):
        self.project_id = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self.location = location

    def extract_from_file(self, image_path: str) -> OCRResult:
        from pathlib import Path
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

    def extract_from_bytes(self, image_bytes: bytes,
                           mime_type: str = "image/jpeg") -> OCRResult:
        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel, Part
            vertexai.init(project=self.project_id, location=self.location)
            model = GenerativeModel("gemini-2.5-flash")
            image_part = Part.from_data(data=image_bytes, mime_type=mime_type)
            response = model.generate_content([EXTRACTION_PROMPT, image_part])
            return self._parse_response(response.text.strip())
        except Exception as e:
            result = OCRResult()
            result.error = str(e)
            return result

    def extract_from_base64(self, b64_string: str,
                             mime_type: str = "image/jpeg") -> OCRResult:
        return self.extract_from_bytes(base64.b64decode(b64_string), mime_type)

    def _parse_response(self, raw: str) -> OCRResult:
        result = OCRResult()
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            result.error = f"JSON parse error: {e} — raw: {raw[:200]}"
            return result

        # MRZ — truncate strictly to 44
        result.mrz.line1 = data.get("mrz_line1", "").upper().strip()[:44]
        result.mrz.line2 = data.get("mrz_line2", "").upper().strip()[:44]

        # VIZ
        v = result.viz
        v.surname                    = data.get("viz_surname", "")
        v.given_names                = data.get("viz_given_names", "")
        v.nationality                = data.get("viz_nationality", "")
        v.date_of_birth              = data.get("viz_date_of_birth", "")
        v.sex                        = data.get("viz_sex", "")
        v.expiry_date                = data.get("viz_expiry_date", "")
        v.date_of_issue              = data.get("viz_date_of_issue", "")
        v.doc_number                 = data.get("viz_doc_number", "")
        v.place_of_birth             = data.get("viz_place_of_birth", "")
        v.issuing_authority          = data.get("viz_issuing_authority", "")
        v.photo_present              = bool(data.get("photo_present", False))
        v.photo_type                 = data.get("photo_type", "")
        v.photo_integration_technique = data.get("photo_integration_technique", "")
        v.cover_colour               = data.get("cover_colour", "")
        v.document_title             = data.get("document_title", "")
        result.ocr_confidence        = data.get("confidence", "")

        self._check_consistency(result)
        return result

    def _check_consistency(self, result: OCRResult):
        mrz = result.mrz
        viz = result.viz
        if not mrz.complete:
            return

        # Document number
        mrz_docnum = mrz.line2[0:9].rstrip("<").strip()
        viz_docnum = re.sub(r"[^A-Z0-9]", "", viz.doc_number.upper())
        if mrz_docnum and viz_docnum:
            result.doc_number_match = mrz_docnum == viz_docnum

        # DOB
        mrz_dob = mrz.line2[13:19]
        viz_dob = self._normalise_date(viz.date_of_birth)
        if mrz_dob and viz_dob:
            result.dob_match = mrz_dob == viz_dob

        # Expiry
        mrz_exp = mrz.line2[21:27]
        viz_exp = self._normalise_date(viz.expiry_date)
        if mrz_exp and viz_exp:
            result.expiry_match = mrz_exp == viz_exp

        # Name
        mrz_surname = mrz.line1[5:44].split("<<")[0].replace("<", " ").strip().upper()
        viz_surname = viz.surname.upper().strip()
        if mrz_surname and viz_surname:
            result.name_match = (
                viz_surname.startswith(mrz_surname)
                or mrz_surname.startswith(viz_surname)
                or mrz_surname == viz_surname
            )

        # Nationality
        mrz_nat = mrz.line2[10:13].replace("<", "").strip()
        viz_nat = viz.nationality.upper()[:3].strip()
        if mrz_nat and viz_nat:
            result.nationality_match = mrz_nat == viz_nat

    @staticmethod
    def _normalise_date(date_str: str) -> str:
        if not date_str:
            return ""
        if re.match(r"^\d{6}$", date_str):
            return date_str
        # YYYY-MM-DD
        m = re.match(r"(\d{4})[-/](\d{2})[-/](\d{2})", date_str)
        if m:
            return f"{m.group(1)[2:]}{m.group(2)}{m.group(3)}"
        # DD MMM YYYY
        months = {
            "JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
            "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12",
        }
        m = re.match(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", date_str.upper())
        if m:
            mm = months.get(m.group(2), "00")
            return f"{m.group(3)[2:]}{mm}{m.group(1).zfill(2)}"
        return ""


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python ocr_extractor.py <passport_scan.jpg>")
        sys.exit(1)
    extractor = PassportOCRExtractor()
    result = extractor.extract_from_file(sys.argv[1])
    print(json.dumps(result.to_dict(), indent=2))
    print(f"\nConsistency: {'PASS' if result.consistency_pass else 'FAIL'}")
