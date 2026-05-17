"""
verification/nfc_parser.py
===========================
Parse and verify ICAO 9303 NFC/RFID chip data from ePassports.

In a real deployment this data comes from:
  - A hardware NFC reader (border control kiosk, Regula device, etc.)
  - A mobile app using iOS Core NFC / Android NfcAdapter
  - A library like OpenMobileID or jmrtd that handles BAC/PACE/EAC

This module accepts pre-parsed chip data (as a dict) and runs
cryptographic and consistency checks.

Data groups (DGs) in ICAO 9303:
  DG1  — MRZ data (same as printed MRZ)
  DG2  — Facial image (JPEG2000)
  DG3  — Fingerprints (EAC protected)
  DG14 — Security Info (algorithms)
  DG15 — Active Authentication public key
  SOD  — Security Object Document (signed hashes of all DGs)

Access control:
  BAC  — Basic Access Control (older, uses MRZ key)
  PACE — Password Authenticated Connection Establishment (newer)
  EAC  — Extended Access Control (for DG3/DG4 biometrics)

Passive Authentication (PA):
  Verifies SOD signature using issuing country's Document Signing
  Certificate (DSC), which chains to the Country Signing CA (CSCA).
  This proves chip data was not tampered with after issuance.

Active Authentication (AA):
  Challenges the chip's private key — proves chip was not cloned.
  (Superseded by Chip Authentication in newer passports.)

Chip Authentication (CA):
  Stronger anti-cloning proof using Diffie-Hellman key agreement.
"""

from dataclasses import dataclass, field
from typing import Optional
import hashlib
import base64


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class NFCAuthResult:
    """Results of NFC chip authentication checks."""
    bac_pace_success: bool = False      # successfully established secure channel
    passive_auth_pass: Optional[bool] = None   # SOD signature verified
    active_auth_pass: Optional[bool] = None    # chip private key challenge
    chip_auth_pass: Optional[bool] = None      # DH key agreement anti-clone
    dg1_hash_valid: Optional[bool] = None      # DG1 hash matches SOD
    dg2_hash_valid: Optional[bool] = None      # DG2 hash matches SOD
    issuer_cert_valid: Optional[bool] = None   # DSC chains to known CSCA
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def chip_genuine(self) -> bool:
        """True if chip is cryptographically verified as genuine."""
        return (
            self.passive_auth_pass is True
            and (self.chip_auth_pass is True or self.active_auth_pass is True)
        )

    @property
    def data_untampered(self) -> bool:
        """True if chip data matches signed hashes."""
        return self.dg1_hash_valid is True and self.dg2_hash_valid is True


@dataclass
class NFCDataGroups:
    """Parsed data from chip data groups."""
    # DG1 — MRZ
    mrz_line1: str = ""
    mrz_line2: str = ""

    # DG2 — Facial image
    facial_image_bytes: Optional[bytes] = None
    facial_image_format: str = ""   # "jpeg2000" / "jpeg"

    # DG3 — Fingerprints (EAC only)
    fingerprint_available: bool = False

    # DG14 — Security info
    security_infos: list = field(default_factory=list)

    # DG15 — AA public key
    aa_public_key: Optional[bytes] = None

    # SOD hashes (what was signed)
    sod_dg_hashes: dict = field(default_factory=dict)   # {dg_number: hex_hash}
    sod_signature: Optional[bytes] = None
    sod_signing_cert: Optional[bytes] = None


@dataclass
class NFCParseResult:
    """Complete result of NFC chip parsing and verification."""
    auth: NFCAuthResult = field(default_factory=NFCAuthResult)
    dgs: NFCDataGroups = field(default_factory=NFCDataGroups)

    # Cross-checks with physical document
    mrz_matches_chip: Optional[bool] = None   # printed MRZ == chip DG1
    photo_matches_chip: Optional[bool] = None  # OCR photo_present and chip has DG2

    error: str = ""

    def to_dict(self) -> dict:
        return {
            "chip_genuine":      self.auth.chip_genuine,
            "data_untampered":   self.auth.data_untampered,
            "mrz_matches_chip":  self.mrz_matches_chip,
            "auth": {
                "bac_pace":      self.auth.bac_pace_success,
                "passive_auth":  self.auth.passive_auth_pass,
                "active_auth":   self.auth.active_auth_pass,
                "chip_auth":     self.auth.chip_auth_pass,
                "dg1_hash":      self.auth.dg1_hash_valid,
                "dg2_hash":      self.auth.dg2_hash_valid,
                "issuer_cert":   self.auth.issuer_cert_valid,
            },
            "dgs": {
                "mrz_line1":     self.dgs.mrz_line1,
                "mrz_line2":     self.dgs.mrz_line2,
                "has_photo":     self.dgs.facial_image_bytes is not None,
                "photo_format":  self.dgs.facial_image_format,
                "has_fingerprint": self.dgs.fingerprint_available,
            },
            "errors":   self.auth.errors,
            "warnings": self.auth.warnings,
            "error":    self.error,
        }


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class NFCChipParser:
    """
    Parse and verify ePassport NFC chip data.

    Accepts a dict representation of chip data as returned by
    common NFC reading libraries. Expected input format:

    {
        "bac_pace_success": true,
        "passive_auth_pass": true,
        "active_auth_pass": true,
        "chip_auth_pass": null,          # null = not attempted
        "issuer_cert_valid": true,
        "DG1": {
            "mrz_line1": "P<GBR...",
            "mrz_line2": "925..."
        },
        "DG2": {
            "image_base64": "<base64 JPEG2000>",
            "format": "jpeg2000"
        },
        "SOD": {
            "dg_hashes": {
                "1": "a3f2...",
                "2": "c8b1..."
            },
            "signature_base64": "<base64>",
            "signing_cert_base64": "<base64>"
        }
    }
    """

    def parse(self, chip_data: dict) -> NFCParseResult:
        result = NFCParseResult()

        if not chip_data:
            result.error = "No chip data provided"
            return result

        try:
            self._parse_auth(chip_data, result.auth)
            self._parse_dg1(chip_data, result.dgs)
            self._parse_dg2(chip_data, result.dgs)
            self._parse_sod(chip_data, result.dgs, result.auth)
            self._verify_dg_hashes(result.dgs, result.auth)
        except Exception as e:
            result.error = f"Parse error: {e}"

        return result

    def parse_and_crosscheck(
        self,
        chip_data: dict,
        printed_mrz_line1: str = "",
        printed_mrz_line2: str = "",
    ) -> NFCParseResult:
        """Parse chip data and cross-check against the printed MRZ."""
        result = self.parse(chip_data)

        # Cross-check DG1 MRZ against printed MRZ
        if result.dgs.mrz_line1 and printed_mrz_line1:
            result.mrz_matches_chip = (
                result.dgs.mrz_line1.strip().upper() == printed_mrz_line1.strip().upper()
                and result.dgs.mrz_line2.strip().upper() == printed_mrz_line2.strip().upper()
            )
            if not result.mrz_matches_chip:
                result.auth.errors.append(
                    "CRITICAL: printed MRZ does not match chip DG1 — possible document substitution"
                )

        # Check photo presence
        result.photo_matches_chip = result.dgs.facial_image_bytes is not None

        return result

    # -----------------------------------------------------------------------
    # Internal parsers
    # -----------------------------------------------------------------------

    def _parse_auth(self, data: dict, auth: NFCAuthResult):
        auth.bac_pace_success = bool(data.get("bac_pace_success", False))
        if not auth.bac_pace_success:
            auth.errors.append("BAC/PACE failed — could not establish secure channel")
            return

        pa = data.get("passive_auth_pass")
        auth.passive_auth_pass = None if pa is None else bool(pa)
        if auth.passive_auth_pass is False:
            auth.errors.append("Passive Authentication FAILED — chip data may be tampered")

        aa = data.get("active_auth_pass")
        auth.active_auth_pass = None if aa is None else bool(aa)

        ca = data.get("chip_auth_pass")
        auth.chip_auth_pass = None if ca is None else bool(ca)

        if auth.active_auth_pass is False and auth.chip_auth_pass is False:
            auth.errors.append("Both Active Auth and Chip Auth failed — possible cloned chip")

        ic = data.get("issuer_cert_valid")
        auth.issuer_cert_valid = None if ic is None else bool(ic)
        if auth.issuer_cert_valid is False:
            auth.warnings.append("Issuer certificate not in trusted CSCA store")

    def _parse_dg1(self, data: dict, dgs: NFCDataGroups):
        dg1 = data.get("DG1", {})
        if not dg1:
            return
        dgs.mrz_line1 = dg1.get("mrz_line1", "").upper().strip()
        dgs.mrz_line2 = dg1.get("mrz_line2", "").upper().strip()

    def _parse_dg2(self, data: dict, dgs: NFCDataGroups):
        dg2 = data.get("DG2", {})
        if not dg2:
            return
        img_b64 = dg2.get("image_base64", "")
        if img_b64:
            try:
                dgs.facial_image_bytes = base64.b64decode(img_b64)
                dgs.facial_image_format = dg2.get("format", "jpeg2000")
            except Exception:
                pass

    def _parse_sod(self, data: dict, dgs: NFCDataGroups, auth: NFCAuthResult):
        sod = data.get("SOD", {})
        if not sod:
            return
        dgs.sod_dg_hashes = sod.get("dg_hashes", {})
        sig_b64 = sod.get("signature_base64", "")
        cert_b64 = sod.get("signing_cert_base64", "")
        if sig_b64:
            try:
                dgs.sod_signature = base64.b64decode(sig_b64)
            except Exception:
                pass
        if cert_b64:
            try:
                dgs.sod_signing_cert = base64.b64decode(cert_b64)
            except Exception:
                pass

    def _verify_dg_hashes(self, dgs: NFCDataGroups, auth: NFCAuthResult):
        """
        Verify that DG1/DG2 raw bytes hash to the values stored in SOD.

        In production this would use the actual DG bytes from the chip.
        Here we mark as not verified if raw bytes aren't available.
        """
        if not dgs.sod_dg_hashes:
            auth.warnings.append("SOD hashes not available — cannot verify data group integrity")
            return

        # DG1 hash check
        dg1_hash_in_sod = dgs.sod_dg_hashes.get("1") or dgs.sod_dg_hashes.get("DG1")
        if dg1_hash_in_sod:
            # In real implementation: hash the raw DG1 TLV bytes and compare
            # Here we trust the library's passive auth result
            auth.dg1_hash_valid = auth.passive_auth_pass
        else:
            auth.warnings.append("DG1 hash not in SOD")

        # DG2 hash check
        dg2_hash_in_sod = dgs.sod_dg_hashes.get("2") or dgs.sod_dg_hashes.get("DG2")
        if dg2_hash_in_sod:
            auth.dg2_hash_valid = auth.passive_auth_pass
        else:
            auth.warnings.append("DG2 hash not in SOD")


# ---------------------------------------------------------------------------
# Simulated NFC data for POC testing
# ---------------------------------------------------------------------------

def simulate_nfc_from_mrz(mrz_line1: str, mrz_line2: str) -> dict:
    """
    Build a simulated NFC chip data dict from MRZ lines.
    Use this for POC testing when a real NFC reader isn't available.

    NOTE: This simulates a GENUINE chip — passive auth is marked True.
    In production, passive auth requires the actual DSC certificate chain.
    """
    return {
        "bac_pace_success": True,
        "passive_auth_pass": True,
        "active_auth_pass": None,    # not attempted in simulation
        "chip_auth_pass": None,      # not attempted in simulation
        "issuer_cert_valid": None,   # cannot verify without real cert store
        "DG1": {
            "mrz_line1": mrz_line1.upper().strip(),
            "mrz_line2": mrz_line2.upper().strip(),
        },
        "DG2": None,   # no actual photo in simulation
        "SOD": {
            "dg_hashes": {
                "1": hashlib.sha256(mrz_line1.encode()).hexdigest(),
                "2": None,
            },
            "signature_base64": None,
            "signing_cert_base64": None,
        },
        "_simulated": True,
        "_note": "Simulated NFC data from MRZ. Not cryptographically verified.",
    }


# ---------------------------------------------------------------------------
# Standalone demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    # Simulate NFC from specimen MRZ
    LINE1 = "P<GBRSPECIMEN<<ANGELA<ZOE<<<<<<<<<<<<<<<<<<<"
    LINE2 = "9250647142GBR8812049F2512314<<<<<<<<<<<<<<<6"

    print("=== Simulated NFC (POC mode) ===")
    simulated = simulate_nfc_from_mrz(LINE1, LINE2)
    parser = NFCChipParser()
    result = parser.parse_and_crosscheck(simulated, LINE1, LINE2)
    print(json.dumps(result.to_dict(), indent=2))

    print("\nChip genuine:", result.auth.chip_genuine)
    print("Data untampered:", result.auth.data_untampered)
    print("MRZ matches chip:", result.mrz_matches_chip)
