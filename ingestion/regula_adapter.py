"""
ingestion/regula_adapter.py
============================
Normalises a Regula Document Reader Web API /process response
into our prado-verification input schema.

Handles both:
  - Real Regula output (from hardware SDK)
  - Simulated output (from regula_simulator.py)

Usage:
    # Standalone — translate Regula JSON → our verify input
    python ingestion/regula_adapter.py \
        --regula-json regula_simulated.json \
        --selfie selfie.jpg \
        --output verify_input.json

    # Then POST to API:
    curl -X POST http://localhost:8000/verify/from-regula \
        -H "Content-Type: application/json" \
        -d @verify_input.json
"""

import argparse
import base64
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Regula constants
# ---------------------------------------------------------------------------

class Light:
    WHITE       = 6
    UV          = 128
    IR          = 32
    TRANSMITTED = 16
    OBLIQUE     = 64

class CheckResult:
    OK      = 1
    ERROR   = 0
    UNKNOWN = 2

class Source:
    VISUAL = 1
    MRZ    = 2
    RFID   = 8

class TextFieldType:
    DOCUMENT_NUMBER         = 25
    EXPIRY_DATE             = 33
    DATE_OF_BIRTH           = 30
    SEX                     = 31
    NATIONALITY             = 32
    SURNAME                 = 16
    GIVEN_NAMES             = 17
    SURNAME_AND_GIVEN_NAMES = 1023
    PLACE_OF_BIRTH          = 35
    DATE_OF_ISSUE           = 36
    ISSUING_AUTHORITY       = 37
    ISSUING_STATE_CODE      = 8
    MRZ_LINE_1              = 48
    MRZ_LINE_2              = 49

class GraphicFieldType:
    DOCUMENT_FRONT  = 0
    PORTRAIT        = 15
    UV_FRONT        = 30
    IR_FRONT        = 31
    TRANSMITTED     = 32

class AuthCheckType:
    UV_LUMINESCENCE  = 0x0002
    IR_B900          = 0x0004
    IMAGE_PATTERN    = 0x0008
    HOLOGRAM         = 0x1000
    FIBERS           = 0x0001
    PHOTO_EMBED_TYPE = 0x0200


# ---------------------------------------------------------------------------
# Adapted schema
# ---------------------------------------------------------------------------

@dataclass
class RegulaAdapterResult:
    """Our normalised representation of Regula output — input to /verify."""

    # Document identity
    document_id: str = ""           # from DocType.DocumentID
    country_code: str = ""          # from DocType.IssuingCountry

    # MRZ lines
    mrz_line1: str = ""
    mrz_line2: str = ""

    # VIZ text fields
    surname: str = ""
    given_names: str = ""
    doc_number: str = ""
    nationality: str = ""
    date_of_birth: str = ""
    expiry_date: str = ""
    sex: str = ""
    place_of_birth: str = ""
    date_of_issue: str = ""
    issuing_authority: str = ""

    # Cross-source validation from Regula
    regula_overall_status: int = CheckResult.UNKNOWN
    regula_text_valid: int = CheckResult.UNKNOWN
    regula_doc_type_valid: int = CheckResult.UNKNOWN
    regula_mrz_viz_match: int = CheckResult.UNKNOWN   # cross-source comparison

    # Authenticity check results from Regula
    regula_uv_pass: Optional[bool] = None
    regula_ir_pass: Optional[bool] = None
    regula_hologram_pass: Optional[bool] = None
    regula_fibers_pass: Optional[bool] = None
    regula_photo_embed_pass: Optional[bool] = None

    # NFC/RFID
    regula_nfc_pa_pass: Optional[bool] = None
    regula_nfc_aa_pass: Optional[bool] = None
    regula_nfc_overall: int = CheckResult.UNKNOWN

    # Images (base64)
    white_image_b64: str = ""       # normal light — bio page
    uv_image_b64: str = ""          # UV 365nm
    ir_image_b64: str = ""          # IR
    transmitted_image_b64: str = "" # transmitted light
    portrait_b64: str = ""          # cropped portrait
    chip_portrait_b64: str = ""     # DG2 chip photo

    # Selfie (added by adapter if provided)
    selfie_b64: str = ""

    # PRADO feature check inputs (derived)
    prado_digital_inputs: dict = field(default_factory=dict)

    # Metadata
    simulated: bool = False
    transaction_id: str = ""
    device_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_verify_payload(self) -> dict:
        """
        Build the payload dict for our /verify endpoint.
        Suitable for direct programmatic use — not multipart form.
        """
        return {
            "document_id":   self.document_id,
            "mrz_line1":     self.mrz_line1,
            "mrz_line2":     self.mrz_line2,
            "regula_output": self.to_dict(),
        }


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class RegulaAdapter:

    def adapt(self, regula_json: dict, selfie_b64: str = "") -> RegulaAdapterResult:
        result = RegulaAdapterResult()
        result.simulated = regula_json.get("_simulated", False)

        self._extract_transaction(regula_json, result)
        self._extract_doc_type(regula_json, result)
        self._extract_text_fields(regula_json, result)
        self._extract_images(regula_json, result)
        self._extract_status(regula_json, result)
        self._extract_authenticity(regula_json, result)
        self._extract_rfid(regula_json, result)
        self._build_prado_inputs(result)

        result.selfie_b64 = selfie_b64
        return result

    def adapt_from_file(self, json_path: str,
                        selfie_path: str = "") -> RegulaAdapterResult:
        with open(json_path) as f:
            regula_json = json.load(f)
        selfie_b64 = ""
        if selfie_path and Path(selfie_path).exists():
            selfie_b64 = base64.b64encode(
                Path(selfie_path).read_bytes()
            ).decode()
        return self.adapt(regula_json, selfie_b64)

    # -----------------------------------------------------------------------
    # Extraction helpers
    # -----------------------------------------------------------------------

    def _extract_transaction(self, data: dict, r: RegulaAdapterResult):
        ti = data.get("TransactionInfo", {})
        r.transaction_id = ti.get("TransactionId", "")
        r.device_id      = ti.get("DeviceId", "")

    def _extract_doc_type(self, data: dict, r: RegulaAdapterResult):
        doc_types = data.get("DocType", [])
        if doc_types:
            dt = doc_types[0]
            r.document_id  = dt.get("DocumentID", "")
            r.country_code = dt.get("IssuingCountry", "")

    def _extract_text_fields(self, data: dict, r: RegulaAdapterResult):
        fields = data.get("Text", {}).get("fieldList", [])

        def get_value(field_type: int, source: int = Source.VISUAL) -> str:
            for f in fields:
                if f.get("fieldType") == field_type:
                    # Try source-specific value first
                    for v in f.get("values", []):
                        if v.get("source") == source:
                            return v.get("value", "").strip()
                    # Fall back to top-level value
                    return f.get("value", "").strip()
            return ""

        def get_cross_source(field_type: int) -> int:
            for f in fields:
                if f.get("fieldType") == field_type:
                    cs = f.get("crossSourceComparison", {})
                    key = f"{Source.MRZ}_{Source.VISUAL}"
                    return cs.get(key, CheckResult.UNKNOWN)
            return CheckResult.UNKNOWN

        r.doc_number       = get_value(TextFieldType.DOCUMENT_NUMBER)
        r.surname          = get_value(TextFieldType.SURNAME)
        r.given_names      = get_value(TextFieldType.GIVEN_NAMES)
        r.nationality      = get_value(TextFieldType.NATIONALITY)
        r.date_of_birth    = get_value(TextFieldType.DATE_OF_BIRTH)
        r.expiry_date      = get_value(TextFieldType.EXPIRY_DATE)
        r.sex              = get_value(TextFieldType.SEX)
        r.place_of_birth   = get_value(TextFieldType.PLACE_OF_BIRTH)
        r.date_of_issue    = get_value(TextFieldType.DATE_OF_ISSUE)
        r.issuing_authority = get_value(TextFieldType.ISSUING_AUTHORITY)
        r.mrz_line1        = get_value(TextFieldType.MRZ_LINE_1, Source.MRZ)
        r.mrz_line2        = get_value(TextFieldType.MRZ_LINE_2, Source.MRZ)

        # MRZ vs VIZ cross-source match
        r.regula_mrz_viz_match = get_cross_source(TextFieldType.DOCUMENT_NUMBER)

        # If MRZ lines not in text fields, try RFID DG1
        if not r.mrz_line1:
            rfid = data.get("RFID", {})
            dg1  = rfid.get("DG1", {})
            r.mrz_line1 = dg1.get("MRZLine1", "")
            r.mrz_line2 = dg1.get("MRZLine2", "")

    def _extract_images(self, data: dict, r: RegulaAdapterResult):
        fields = data.get("Images", {}).get("fieldList", [])

        def get_image(field_type: int, light: Optional[int] = None) -> str:
            for f in fields:
                if f.get("fieldType") == field_type:
                    for v in f.get("valueList", []):
                        if light is None or v.get("lightType") == light:
                            return v.get("value", "")
            return ""

        r.white_image_b64       = get_image(GraphicFieldType.DOCUMENT_FRONT, Light.WHITE)
        r.uv_image_b64          = get_image(GraphicFieldType.UV_FRONT, Light.UV)
        r.ir_image_b64          = get_image(GraphicFieldType.IR_FRONT, Light.IR)
        r.transmitted_image_b64 = get_image(GraphicFieldType.TRANSMITTED, Light.TRANSMITTED)
        r.portrait_b64          = get_image(GraphicFieldType.PORTRAIT, Light.WHITE)

        # Chip portrait from RFID DG2
        rfid = data.get("RFID", {})
        dg2  = rfid.get("DG2", {})
        r.chip_portrait_b64 = dg2.get("portrait_b64", "")

    def _extract_status(self, data: dict, r: RegulaAdapterResult):
        status = data.get("Status", {})
        r.regula_overall_status = status.get("OverallStatus", CheckResult.UNKNOWN)
        optical = status.get("DetailsOptical", {})
        r.regula_text_valid     = optical.get("Text",    CheckResult.UNKNOWN)
        r.regula_doc_type_valid = optical.get("DocType", CheckResult.UNKNOWN)

    def _extract_authenticity(self, data: dict, r: RegulaAdapterResult):
        checks = data.get("Authenticity", {}).get("checks", [])

        def get_check(check_type: int) -> Optional[bool]:
            for c in checks:
                if c.get("type") == check_type:
                    return c.get("result") == CheckResult.OK
            return None

        r.regula_uv_pass         = get_check(AuthCheckType.UV_LUMINESCENCE)
        r.regula_ir_pass         = get_check(AuthCheckType.IR_B900)
        r.regula_hologram_pass   = get_check(AuthCheckType.HOLOGRAM)
        r.regula_fibers_pass     = get_check(AuthCheckType.FIBERS)
        r.regula_photo_embed_pass = get_check(AuthCheckType.PHOTO_EMBED_TYPE)

    def _extract_rfid(self, data: dict, r: RegulaAdapterResult):
        rfid   = data.get("RFID", {})
        session = rfid.get("sessionData", {})
        r.regula_nfc_overall = session.get("Status", CheckResult.UNKNOWN)
        pa = session.get("PA", CheckResult.UNKNOWN)
        aa = session.get("AA", CheckResult.UNKNOWN)
        r.regula_nfc_pa_pass = (pa == CheckResult.OK) if pa != CheckResult.UNKNOWN else None
        r.regula_nfc_aa_pass = (aa == CheckResult.OK) if aa != CheckResult.UNKNOWN else None

    def _build_prado_inputs(self, r: RegulaAdapterResult):
        """
        Build the prado_digital_inputs dict — maps PRADO feature categories
        to Regula check results. This is what feeds our PRADO matcher.
        """
        r.prado_digital_inputs = {
            # Biodata page features
            "facial_image_present":       bool(r.portrait_b64 or r.white_image_b64),
            "photo_type":                 "colour",  # determined from photo_embed check
            "biographical_data_valid":    r.regula_text_valid == CheckResult.OK,
            "mrz_viz_consistent":         r.regula_mrz_viz_match == CheckResult.OK,
            "doc_number_valid":           bool(r.doc_number),
            "electronic_data_present":    r.regula_nfc_overall != CheckResult.UNKNOWN,
            "nfc_passive_auth_pass":      r.regula_nfc_pa_pass,
            # Multi-spectral checks (from specialist hardware)
            "uv_features_pass":           r.regula_uv_pass,
            "ir_features_pass":           r.regula_ir_pass,
            "hologram_pass":              r.regula_hologram_pass,
            "fibers_pass":                r.regula_fibers_pass,
            "transmitted_watermark_available": bool(r.transmitted_image_b64),
            # Images available for further AI checks
            "has_white_image":            bool(r.white_image_b64),
            "has_uv_image":               bool(r.uv_image_b64),
            "has_ir_image":               bool(r.ir_image_b64),
            "has_transmitted_image":      bool(r.transmitted_image_b64),
            "has_chip_portrait":          bool(r.chip_portrait_b64),
        }


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def print_summary(result: RegulaAdapterResult):
    print("\n=== Regula Adapter Summary ===")
    print(f"Transaction:   {result.transaction_id}")
    print(f"Device:        {result.device_id}")
    print(f"Document ID:   {result.document_id}")
    print(f"Country:       {result.country_code}")
    print(f"Simulated:     {result.simulated}")
    print(f"\nPerson:        {result.surname}, {result.given_names}")
    print(f"Doc number:    {result.doc_number}")
    print(f"DOB:           {result.date_of_birth}")
    print(f"Expiry:        {result.expiry_date}")
    print(f"\nMRZ line 1:    {result.mrz_line1}")
    print(f"MRZ line 2:    {result.mrz_line2}")
    print(f"\nRegula status: {'OK' if result.regula_overall_status == 1 else 'ERROR/UNKNOWN'}")
    print(f"MRZ/VIZ match: {'OK' if result.regula_mrz_viz_match == 1 else 'ERROR/UNKNOWN'}")
    print(f"\nAuthenticity checks:")
    print(f"  UV:          {result.regula_uv_pass}")
    print(f"  IR:          {result.regula_ir_pass}")
    print(f"  Hologram:    {result.regula_hologram_pass}")
    print(f"  Fibres:      {result.regula_fibers_pass}")
    print(f"  NFC PA:      {result.regula_nfc_pa_pass}")
    print(f"\nImages captured:")
    print(f"  White:       {'YES' if result.white_image_b64 else 'NO'}")
    print(f"  UV:          {'YES' if result.uv_image_b64 else 'NO'}")
    print(f"  IR:          {'YES' if result.ir_image_b64 else 'NO'}")
    print(f"  Transmitted: {'YES' if result.transmitted_image_b64 else 'NO'}")
    print(f"  Portrait:    {'YES' if result.portrait_b64 else 'NO'}")
    print(f"  Chip DG2:    {'YES' if result.chip_portrait_b64 else 'NO'}")
    print(f"  Selfie:      {'YES' if result.selfie_b64 else 'NO'}")
    print(f"\nPRADO digital inputs:")
    for k, v in result.prado_digital_inputs.items():
        print(f"  {k:<35} {v}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Adapt Regula /process output to prado-verification input schema"
    )
    ap.add_argument("--regula-json", required=True,
                    help="Path to Regula /process response JSON")
    ap.add_argument("--selfie",      default="",
                    help="Optional selfie image path for biometric check")
    ap.add_argument("--output",      default="verify_input.json",
                    help="Output file for adapted schema")
    ap.add_argument("--summary",     action="store_true",
                    help="Print human-readable summary")
    args = ap.parse_args()

    adapter = RegulaAdapter()
    result  = adapter.adapt_from_file(args.regula_json, args.selfie)

    if args.summary:
        print_summary(result)

    # Write adapted result
    with open(args.output, "w") as f:
        json.dump(result.to_dict(), f, indent=2)
    print(f"\nWritten: {args.output}")


if __name__ == "__main__":
    main()
