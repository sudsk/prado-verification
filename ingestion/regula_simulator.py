"""
ingestion/regula_simulator.py
==============================
Simulates a Regula Document Reader Web API /process response
using real passport data already extracted by our OCR pipeline.

This allows end-to-end testing of the Regula adapter without
physical Regula hardware.

Usage:
    python ingestion/regula_simulator.py \
        --scan passport_scan.jpg \
        --ocr-json ocr_result.json \
        --output regula_simulated.json

    # Or use known data directly
    python ingestion/regula_simulator.py \
        --scan passport_scan.jpg \
        --surname KUMAR \
        --given-names SUDHANSHU \
        --doc-number 152962909 \
        --dob 19/06/1978 \
        --expiry 03/09/2034 \
        --nationality GBR \
        --mrz1 "P<GBRKUMAR<<SUDHANSHU<<<<<<<<<<<<<<<<<<<<<<<" \
        --mrz2 "1529629099GBR7806197M3409039<<<<<<<<<<<<<<02" \
        --output regula_simulated.json
"""

import argparse
import base64
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Regula constants (from Regula Web API OpenAPI spec)
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
    BARCODE = 4
    RFID   = 8

class TextFieldType:
    DOCUMENT_CLASS_CODE     = 0
    ISSUING_STATE_CODE      = 8
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
    MRZ_LINE_1              = 48
    MRZ_LINE_2              = 49

class GraphicFieldType:
    DOCUMENT_FRONT  = 0
    DOCUMENT_REAR   = 1
    PORTRAIT        = 15
    SIGNATURE       = 14
    GHOST_PORTRAIT  = 16
    UV_FRONT        = 30
    IR_FRONT        = 31
    TRANSMITTED     = 32

class AuthenticityCheckType:
    UV_LUMINESCENCE     = 0x0002
    IR_B900             = 0x0004
    IMAGE_PATTERN       = 0x0008
    AXIAL_PROTECTION    = 0x0010
    PHOTO_EMBED_TYPE    = 0x0200
    HOLOGRAM            = 0x1000
    FIBERS              = 0x0001
    PORTRAIT_COMPARISON = 0x8000


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class RegulaSimulator:

    def __init__(self, scan_path: str):
        self.scan_path = scan_path
        self.scan_bytes = Path(scan_path).read_bytes()
        self.scan_b64 = base64.b64encode(self.scan_bytes).decode()

    def generate(
        self,
        # Text fields
        surname: str,
        given_names: str,
        doc_number: str,
        nationality: str,
        dob: str,               # DD/MM/YYYY
        expiry: str,            # DD/MM/YYYY
        sex: str = "M",
        place_of_birth: str = "",
        date_of_issue: str = "",
        issuing_authority: str = "",
        mrz_line1: str = "",
        mrz_line2: str = "",
        # Auth results (simulated as passing for genuine passport)
        uv_check_pass: bool = True,
        ir_check_pass: bool = True,
        hologram_pass: bool = True,
        fibers_pass: bool = True,
        portrait_comparison_pass: bool = True,
        nfc_passive_auth_pass: bool = True,
        # Document metadata
        document_id: str = "GBR-AO-05002",
        issuing_country: str = "GBR",
    ) -> dict:
        """
        Generate a simulated Regula /process response.
        Structure mirrors actual Regula DocumentReaderResults JSON.
        """

        def field(field_type: int, value: str, source: int = Source.VISUAL,
                  validity: int = CheckResult.OK) -> dict:
            return {
                "fieldType": field_type,
                "fieldName": self._field_name(field_type),
                "value": value,
                "values": [
                    {
                        "source": source,
                        "value": value,
                        "originalValue": value,
                        "validity": validity,
                        "probability": 100,
                    }
                ],
                "sourceValidity": {
                    str(source): validity
                },
                "crossSourceComparison": {},
            }

        def mrz_field(field_type: int, viz_value: str, mrz_value: str,
                      match: int = CheckResult.OK) -> dict:
            return {
                "fieldType": field_type,
                "fieldName": self._field_name(field_type),
                "value": viz_value,
                "values": [
                    {"source": Source.VISUAL, "value": viz_value,
                     "validity": CheckResult.OK, "probability": 100},
                    {"source": Source.MRZ, "value": mrz_value,
                     "validity": CheckResult.OK, "probability": 100},
                ],
                "sourceValidity": {
                    str(Source.VISUAL): CheckResult.OK,
                    str(Source.MRZ):    CheckResult.OK,
                },
                "crossSourceComparison": {
                    f"{Source.MRZ}_{Source.VISUAL}": match,
                },
            }

        def auth_check(check_type: int, passed: bool, element: str) -> dict:
            return {
                "type": check_type,
                "typeName": element,
                "result": CheckResult.OK if passed else CheckResult.ERROR,
                "elements": [
                    {
                        "elementType": check_type,
                        "elementDiagnose": 0 if passed else 1,
                        "status": CheckResult.OK if passed else CheckResult.ERROR,
                    }
                ],
            }

        # Parse dates for MRZ format
        dob_mrz    = self._to_mrz_date(dob)
        expiry_mrz = self._to_mrz_date(expiry)
        doc_no_clean = doc_number.replace(" ", "")

        # Cross-source match status
        name_match = CheckResult.OK
        dob_match  = CheckResult.OK
        num_match  = CheckResult.OK
        exp_match  = CheckResult.OK

        response = {
            "TransactionInfo": {
                "TransactionId": str(uuid.uuid4()),
                "ComputerName": "REGULA-SIMULATOR",
                "UserName":     "simulator",
                "DateTime":     datetime.now(timezone.utc).isoformat(),
                "DeviceId":     "SIMULATED-7027M",
                "DeviceType":   "Regula 7027M (simulated)",
            },

            # --- Status ---
            "Status": {
                "OverallStatus":    CheckResult.OK,
                "PortraitComparison": CheckResult.OK if portrait_comparison_pass else CheckResult.ERROR,
                "DetailsOptical": {
                    "OverallStatus": CheckResult.OK,
                    "Text":          CheckResult.OK,
                    "DocType":       CheckResult.OK,
                    "Security":      CheckResult.OK if all([
                        uv_check_pass, ir_check_pass, hologram_pass, fibers_pass
                    ]) else CheckResult.ERROR,
                    "ImageQA":       CheckResult.OK,
                    "Expiry":        CheckResult.OK,
                    "VDSNCMRZ":      CheckResult.OK,
                },
                "DetailsRFID": {
                    "OverallStatus": CheckResult.OK if nfc_passive_auth_pass else CheckResult.ERROR,
                    "PA": CheckResult.OK if nfc_passive_auth_pass else CheckResult.ERROR,
                    "AA": CheckResult.UNKNOWN,  # not attempted in simulation
                    "CA": CheckResult.UNKNOWN,
                },
            },

            # --- Document type ---
            "DocType": [
                {
                    "DocumentType":   1,   # 1 = Passport
                    "DocumentTypeDescription": "PASSPORT",
                    "IssuingCountry": issuing_country,
                    "IssuingState":   issuing_country,
                    "DocumentID":     document_id,
                    "Year":           date_of_issue[:4] if date_of_issue else "",
                }
            ],

            # --- Text fields ---
            "Text": {
                "fieldList": [
                    mrz_field(TextFieldType.DOCUMENT_NUMBER,
                              doc_no_clean, doc_no_clean, num_match),
                    mrz_field(TextFieldType.SURNAME,
                              surname, surname, name_match),
                    mrz_field(TextFieldType.GIVEN_NAMES,
                              given_names, given_names, name_match),
                    mrz_field(TextFieldType.SURNAME_AND_GIVEN_NAMES,
                              f"{surname} {given_names}",
                              f"{surname}<<{given_names.replace(' ', '<')}",
                              name_match),
                    mrz_field(TextFieldType.DATE_OF_BIRTH,
                              dob, dob_mrz, dob_match),
                    mrz_field(TextFieldType.EXPIRY_DATE,
                              expiry, expiry_mrz, exp_match),
                    mrz_field(TextFieldType.NATIONALITY,
                              "BRITISH CITIZEN", nationality, CheckResult.OK),
                    field(TextFieldType.SEX,             sex),
                    field(TextFieldType.PLACE_OF_BIRTH,  place_of_birth),
                    field(TextFieldType.DATE_OF_ISSUE,   date_of_issue),
                    field(TextFieldType.ISSUING_AUTHORITY, issuing_authority),
                    field(TextFieldType.ISSUING_STATE_CODE, issuing_country),
                    field(TextFieldType.DOCUMENT_CLASS_CODE, "P"),
                    field(TextFieldType.MRZ_LINE_1, mrz_line1, Source.MRZ),
                    field(TextFieldType.MRZ_LINE_2, mrz_line2, Source.MRZ),
                ]
            },

            # --- Images ---
            # In real Regula output, order is: white first, then IR, then UV
            "Images": {
                "fieldList": [
                    {
                        "fieldType": GraphicFieldType.DOCUMENT_FRONT,
                        "fieldName": "DOCUMENT_FRONT",
                        "valueList": [
                            {"source": Source.VISUAL, "value": self.scan_b64,
                             "lightType": Light.WHITE}
                        ],
                    },
                    {
                        "fieldType": GraphicFieldType.PORTRAIT,
                        "fieldName": "PORTRAIT",
                        "valueList": [
                            # In simulation we use the full scan — real Regula crops the portrait
                            {"source": Source.VISUAL, "value": self.scan_b64,
                             "lightType": Light.WHITE}
                        ],
                    },
                    {
                        "fieldType": GraphicFieldType.UV_FRONT,
                        "fieldName": "UV_FRONT",
                        "valueList": [
                            # Simulation: same image as UV channel stand-in
                            {"source": Source.VISUAL, "value": self.scan_b64,
                             "lightType": Light.UV}
                        ],
                    },
                    {
                        "fieldType": GraphicFieldType.IR_FRONT,
                        "fieldName": "IR_FRONT",
                        "valueList": [
                            {"source": Source.VISUAL, "value": self.scan_b64,
                             "lightType": Light.IR}
                        ],
                    },
                    {
                        "fieldType": GraphicFieldType.TRANSMITTED,
                        "fieldName": "TRANSMITTED",
                        "valueList": [
                            {"source": Source.VISUAL, "value": self.scan_b64,
                             "lightType": Light.TRANSMITTED}
                        ],
                    },
                ]
            },

            # --- Authenticity checks ---
            "Authenticity": {
                "checks": [
                    auth_check(AuthenticityCheckType.UV_LUMINESCENCE,
                               uv_check_pass, "UV_LUMINESCENCE"),
                    auth_check(AuthenticityCheckType.IR_B900,
                               ir_check_pass, "IR_B900"),
                    auth_check(AuthenticityCheckType.IMAGE_PATTERN,
                               uv_check_pass, "IMAGE_PATTERN"),
                    auth_check(AuthenticityCheckType.HOLOGRAM,
                               hologram_pass, "HOLOGRAM"),
                    auth_check(AuthenticityCheckType.FIBERS,
                               fibers_pass, "FIBERS"),
                    auth_check(AuthenticityCheckType.PHOTO_EMBED_TYPE,
                               True, "PHOTO_EMBED_TYPE"),
                    auth_check(AuthenticityCheckType.PORTRAIT_COMPARISON,
                               portrait_comparison_pass, "PORTRAIT_COMPARISON"),
                ],
            },

            # --- RFID / NFC ---
            "RFID": {
                "sessionData": {
                    "Status": CheckResult.OK if nfc_passive_auth_pass else CheckResult.ERROR,
                    "PA": CheckResult.OK if nfc_passive_auth_pass else CheckResult.ERROR,
                    "AA": CheckResult.UNKNOWN,
                    "CA": CheckResult.UNKNOWN,
                    "BAC": CheckResult.OK,
                    "PACE": CheckResult.UNKNOWN,
                },
                "DG1": {
                    "MRZLine1": mrz_line1,
                    "MRZLine2": mrz_line2,
                },
                "DG2": {
                    "portrait_b64": self.scan_b64,  # stand-in for chip photo
                },
                "_simulated": True,
                "_note": "RFID data simulated from MRZ. PA result not cryptographically verified.",
            },

            "_simulated": True,
            "_simulation_note": (
                "Generated by regula_simulator.py using real OCR data. "
                "UV/IR/transmitted images use white scan as stand-in. "
                "NFC/RFID is not cryptographically verified."
            ),
        }

        return response

    @staticmethod
    def _to_mrz_date(date_str: str) -> str:
        """Convert DD/MM/YYYY to YYMMDD."""
        import re
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", date_str)
        if m:
            return f"{m.group(3)[2:]}{m.group(2)}{m.group(1)}"
        return date_str

    @staticmethod
    def _field_name(field_type: int) -> str:
        names = {
            0: "DOCUMENT_CLASS_CODE", 8: "ISSUING_STATE_CODE",
            25: "DOCUMENT_NUMBER", 33: "EXPIRY_DATE", 30: "DATE_OF_BIRTH",
            31: "SEX", 32: "NATIONALITY", 16: "SURNAME", 17: "GIVEN_NAMES",
            1023: "SURNAME_AND_GIVEN_NAMES", 35: "PLACE_OF_BIRTH",
            36: "DATE_OF_ISSUE", 37: "ISSUING_AUTHORITY",
            48: "MRZ_LINE_1", 49: "MRZ_LINE_2",
        }
        return names.get(field_type, f"FIELD_{field_type}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Simulate Regula Document Reader output from passport scan + data"
    )
    ap.add_argument("--scan",          required=True, help="Bio page scan image")
    ap.add_argument("--surname",       required=True)
    ap.add_argument("--given-names",   required=True)
    ap.add_argument("--doc-number",    required=True)
    ap.add_argument("--dob",           required=True, help="DD/MM/YYYY")
    ap.add_argument("--expiry",        required=True, help="DD/MM/YYYY")
    ap.add_argument("--nationality",   default="GBR")
    ap.add_argument("--sex",           default="M")
    ap.add_argument("--place-of-birth", default="")
    ap.add_argument("--date-of-issue", default="")
    ap.add_argument("--issuing-authority", default="")
    ap.add_argument("--mrz1",          default="")
    ap.add_argument("--mrz2",          default="")
    ap.add_argument("--document-id",   default="GBR-AO-05002")
    ap.add_argument("--output",        default="regula_simulated.json")
    # Failure flags for testing
    ap.add_argument("--fail-uv",       action="store_true")
    ap.add_argument("--fail-hologram", action="store_true")
    ap.add_argument("--fail-nfc",      action="store_true")
    args = ap.parse_args()

    sim = RegulaSimulator(args.scan)
    result = sim.generate(
        surname=args.surname,
        given_names=args.given_names,
        doc_number=args.doc_number,
        nationality=args.nationality,
        dob=args.dob,
        expiry=args.expiry,
        sex=args.sex,
        place_of_birth=args.place_of_birth,
        date_of_issue=args.date_of_issue,
        issuing_authority=args.issuing_authority,
        mrz_line1=args.mrz1,
        mrz_line2=args.mrz2,
        document_id=args.document_id,
        issuing_country=args.nationality,
        uv_check_pass=not args.fail_uv,
        hologram_pass=not args.fail_hologram,
        nfc_passive_auth_pass=not args.fail_nfc,
    )

    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Written: {args.output}")
    print(f"  Text fields:       {len(result['Text']['fieldList'])}")
    print(f"  Image channels:    {len(result['Images']['fieldList'])}")
    print(f"  Auth checks:       {len(result['Authenticity']['checks'])}")
    print(f"  Overall status:    {'OK' if result['Status']['OverallStatus'] == 1 else 'ERROR'}")


if __name__ == "__main__":
    main()
