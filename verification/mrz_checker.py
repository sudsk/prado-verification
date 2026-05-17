"""
verification/mrz_checker.py
============================
ICAO 9303 MRZ intrinsic checks — no external DB needed.

Handles TD3 (passport) MRZ format:
  Line 1: 44 chars — document type, country, name
  Line 2: 44 chars — document number, nationality, DOB, sex, expiry, optional, composite check digit

Check digits use the weighted modulo-10 algorithm (ICAO 9303 Part 3).
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

# ICAO valid character set for MRZ
MRZ_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789<")

# Character values for check digit computation
CHAR_VALUES = {c: i for i, c in enumerate("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
CHAR_VALUES["<"] = 0

# Weights cycle: 7, 3, 1
WEIGHTS = [7, 3, 1]

# ISO 3166-1 alpha-3 codes (subset of commonly checked)
VALID_COUNTRY_CODES = {
    "AFG","ALB","DZA","AND","AGO","ARG","ARM","AUS","AUT","AZE",
    "BHS","BHR","BGD","BRB","BLR","BEL","BLZ","BEN","BTN","BOL",
    "BIH","BWA","BRA","BRN","BGR","BFA","BDI","CPV","KHM","CMR",
    "CAN","CAF","TCD","CHL","CHN","COL","COM","COD","COG","CRI",
    "CIV","HRV","CUB","CYP","CZE","DNK","DJI","DOM","ECU","EGY",
    "SLV","GNQ","ERI","EST","SWZ","ETH","FJI","FIN","FRA","GAB",
    "GMB","GEO","DEU","GHA","GRC","GRD","GTM","GIN","GNB","GUY",
    "HTI","HND","HUN","ISL","IND","IDN","IRN","IRQ","IRL","ISR",
    "ITA","JAM","JPN","JOR","KAZ","KEN","PRK","KOR","KWT","KGZ",
    "LAO","LVA","LBN","LSO","LBR","LBY","LIE","LTU","LUX","MDG",
    "MWI","MYS","MDV","MLI","MLT","MRT","MUS","MEX","MDA","MCO",
    "MNG","MNE","MAR","MOZ","MMR","NAM","NPL","NLD","NZL","NIC",
    "NER","NGA","MKD","NOR","OMN","PAK","PAN","PNG","PRY","PER",
    "PHL","POL","PRT","QAT","ROU","RUS","RWA","SAU","SEN","SRB",
    "SYC","SLE","SGP","SVK","SVN","SOM","ZAF","SSD","ESP","LKA",
    "SDN","SUR","SWE","CHE","SYR","TWN","TJK","TZA","THA","TLS",
    "TGO","TON","TTO","TUN","TUR","TKM","TUV","UGA","UKR","ARE",
    "GBR","USA","URY","UZB","VUT","VEN","VNM","YEM","ZMB","ZWE",
    # Special issuing authorities
    "GBN",  # British National (Overseas)
    "UNO",  # United Nations
    "XOM",  # Micronation
    "D<<",  # Germany legacy
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MRZCheckResult:
    raw_line1: str
    raw_line2: str

    # Parsed fields
    doc_type: str = ""
    issuing_country: str = ""
    surname: str = ""
    given_names: str = ""
    doc_number: str = ""
    nationality: str = ""
    date_of_birth: str = ""
    sex: str = ""
    expiry_date: str = ""
    optional_data: str = ""

    # Check digit results
    cd1_pass: bool = False   # document number check digit
    cd2_pass: bool = False   # DOB check digit
    cd3_pass: bool = False   # expiry check digit
    cd4_pass: bool = False   # optional data check digit (if present)
    cd5_pass: bool = False   # composite check digit

    # Format checks
    length_pass: bool = False
    charset_pass: bool = False
    doc_type_pass: bool = False
    country_code_pass: bool = False
    expiry_future_pass: bool = False   # expiry date is in the future
    dob_plausible_pass: bool = False   # DOB is a plausible date

    # Cross-field checks
    dob_before_expiry: bool = False

    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def all_checkdigits_pass(self) -> bool:
        return self.cd1_pass and self.cd2_pass and self.cd3_pass and self.cd5_pass

    @property
    def overall_pass(self) -> bool:
        return (
            self.length_pass
            and self.charset_pass
            and self.doc_type_pass
            and self.all_checkdigits_pass
            and self.expiry_future_pass
            and self.dob_plausible_pass
        )

    def to_dict(self) -> dict:
        return {
            "overall_pass": self.overall_pass,
            "doc_number": self.doc_number,
            "issuing_country": self.issuing_country,
            "nationality": self.nationality,
            "date_of_birth": self.date_of_birth,
            "expiry_date": self.expiry_date,
            "surname": self.surname,
            "given_names": self.given_names,
            "sex": self.sex,
            "checks": {
                "length_pass":           self.length_pass,
                "charset_pass":          self.charset_pass,
                "doc_type_pass":         self.doc_type_pass,
                "country_code_pass":     self.country_code_pass,
                "cd1_doc_number":        self.cd1_pass,
                "cd2_dob":               self.cd2_pass,
                "cd3_expiry":            self.cd3_pass,
                "cd4_optional":          self.cd4_pass,
                "cd5_composite":         self.cd5_pass,
                "expiry_future":         self.expiry_future_pass,
                "dob_plausible":         self.dob_plausible_pass,
                "dob_before_expiry":     self.dob_before_expiry,
            },
            "errors":   self.errors,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Core check digit algorithm
# ---------------------------------------------------------------------------

def compute_check_digit(data: str) -> int:
    """ICAO 9303 weighted modulo-10 check digit."""
    total = 0
    for i, char in enumerate(data.upper()):
        val = CHAR_VALUES.get(char, 0)
        total += val * WEIGHTS[i % 3]
    return total % 10


def verify_check_digit(data: str, check_char: str) -> bool:
    """Return True if check_char matches the computed check digit for data."""
    try:
        expected = compute_check_digit(data)
        actual = int(check_char)
        return expected == actual
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_mrz_date(yymmdd: str, future_cutoff_years: int = 15) -> Optional[date]:
    """
    Parse YYMMDD MRZ date. Years 00-YY are assumed future; rest are past.
    For expiry dates, we want future interpretation.
    For DOB, we need the opposite.
    This function returns the raw parsed date — callers decide future/past.
    """
    if not yymmdd or len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    try:
        year  = int(yymmdd[:2])
        month = int(yymmdd[2:4])
        day   = int(yymmdd[4:6])
        current_year = date.today().year % 100
        # If YY <= current year + cutoff → 2000s; else 1900s
        full_year = 2000 + year if year <= (current_year + future_cutoff_years) else 1900 + year
        return date(full_year, month, day)
    except ValueError:
        return None


def parse_dob(yymmdd: str) -> Optional[date]:
    """Parse DOB — assumes 1900s for far-future years."""
    if not yymmdd or len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    try:
        year  = int(yymmdd[:2])
        month = int(yymmdd[2:4])
        day   = int(yymmdd[4:6])
        # DOB: if YY > current year → 1900s
        current_year = date.today().year % 100
        full_year = 1900 + year if year > current_year else 2000 + year
        d = date(full_year, month, day)
        # Sanity: DOB must be in the past and person must be < 130 years old
        today = date.today()
        if d >= today:
            return None
        if (today - d).days > 130 * 365:
            return None
        return d
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Main checker
# ---------------------------------------------------------------------------

def check_mrz(line1: str, line2: str) -> MRZCheckResult:
    """
    Run all ICAO 9303 TD3 intrinsic checks on a two-line MRZ.

    Args:
        line1: First MRZ line (44 chars)
        line2: Second MRZ line (44 chars)

    Returns:
        MRZCheckResult with all check outcomes and parsed fields.
    """
    result = MRZCheckResult(raw_line1=line1, raw_line2=line2)

    # Normalise
    l1 = line1.strip().upper()
    l2 = line2.strip().upper()

    # --- Length check ---
    if len(l1) == 44 and len(l2) == 44:
        result.length_pass = True
    else:
        result.errors.append(
            f"MRZ length invalid: line1={len(l1)} line2={len(l2)} (expected 44 each)"
        )

    # --- Character set check ---
    all_chars = set(l1 + l2)
    invalid = all_chars - MRZ_CHARS
    if not invalid:
        result.charset_pass = True
    else:
        result.errors.append(f"Invalid MRZ characters: {sorted(invalid)}")

    if not result.length_pass:
        # Can't do positional parsing without correct length
        return result

    # --- Parse line 1 ---
    result.doc_type       = l1[0:2].replace("<", "").strip()
    result.issuing_country = l1[2:5]
    name_field             = l1[5:44]
    name_parts             = name_field.split("<<", 1)
    result.surname         = name_parts[0].replace("<", " ").strip()
    result.given_names     = name_parts[1].replace("<", " ").strip() if len(name_parts) > 1 else ""

    # --- Document type check ---
    if l1[0] in ("P", "V", "I", "A", "C", "N"):
        result.doc_type_pass = True
    else:
        result.errors.append(f"Invalid document type: '{l1[0]}'")

    # --- Country code check ---
    result.issuing_country = l1[2:5]
    clean_cc = result.issuing_country.replace("<", "").strip()
    if clean_cc in VALID_COUNTRY_CODES:
        result.country_code_pass = True
    else:
        result.warnings.append(f"Unrecognised issuing country: '{result.issuing_country}'")
        result.country_code_pass = False

    # --- Parse line 2 ---
    result.doc_number   = l2[0:9]
    cd1_char            = l2[9]
    result.nationality  = l2[10:13]
    result.date_of_birth = l2[13:19]
    cd2_char            = l2[19]
    result.sex          = l2[20]
    result.expiry_date  = l2[21:27]
    cd3_char            = l2[27]
    result.optional_data = l2[28:42]
    cd4_char            = l2[42]
    cd5_char            = l2[43]

    # --- Check digit 1: document number (positions 0-8) ---
    result.cd1_pass = verify_check_digit(l2[0:9], cd1_char)
    if not result.cd1_pass:
        result.errors.append(
            f"CD1 (document number) failed: "
            f"expected {compute_check_digit(l2[0:9])}, got '{cd1_char}'"
        )

    # --- Check digit 2: DOB (positions 13-18) ---
    result.cd2_pass = verify_check_digit(l2[13:19], cd2_char)
    if not result.cd2_pass:
        result.errors.append(
            f"CD2 (DOB) failed: "
            f"expected {compute_check_digit(l2[13:19])}, got '{cd2_char}'"
        )

    # --- Check digit 3: expiry (positions 21-26) ---
    result.cd3_pass = verify_check_digit(l2[21:27], cd3_char)
    if not result.cd3_pass:
        result.errors.append(
            f"CD3 (expiry) failed: "
            f"expected {compute_check_digit(l2[21:27])}, got '{cd3_char}'"
        )

    # --- Check digit 4: optional data (if non-filler) ---
    if result.optional_data.replace("<", ""):
        result.cd4_pass = verify_check_digit(result.optional_data, cd4_char)
    else:
        result.cd4_pass = True  # all-filler optional data: CD4 = 0 or any digit is ok

    # --- Check digit 5: composite (doc number+CD1 + DOB+CD2 + expiry+CD3 + optional+CD4) ---
    composite = l2[0:10] + l2[13:20] + l2[21:28] + l2[28:43]
    result.cd5_pass = verify_check_digit(composite, cd5_char)
    if not result.cd5_pass:
        result.errors.append(
            f"CD5 (composite) failed: "
            f"expected {compute_check_digit(composite)}, got '{cd5_char}'"
        )

    # --- Expiry date check ---
    expiry = parse_mrz_date(result.expiry_date, future_cutoff_years=25)
    if expiry:
        result.expiry_future_pass = expiry >= date.today()
        if not result.expiry_future_pass:
            result.errors.append(f"Passport expired: {expiry.isoformat()}")
    else:
        result.warnings.append(f"Could not parse expiry date: '{result.expiry_date}'")

    # --- DOB plausibility check ---
    dob = parse_dob(result.date_of_birth)
    if dob:
        result.dob_plausible_pass = True
        # DOB must be before expiry
        if expiry:
            result.dob_before_expiry = dob < expiry
            if not result.dob_before_expiry:
                result.errors.append("DOB is on or after expiry date — impossible")
    else:
        result.warnings.append(f"Could not parse DOB: '{result.date_of_birth}'")

    # --- Sex field ---
    if result.sex not in ("M", "F", "<"):
        result.warnings.append(f"Unusual sex field value: '{result.sex}'")

    return result


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------

def check_mrz_from_string(mrz_string: str) -> MRZCheckResult:
    """
    Parse a combined MRZ string (88 chars total or two lines separated by newline).
    """
    mrz_string = mrz_string.strip()
    if "\n" in mrz_string:
        parts = mrz_string.split("\n")
        line1, line2 = parts[0], parts[1]
    elif len(mrz_string) == 88:
        line1, line2 = mrz_string[:44], mrz_string[44:]
    else:
        raise ValueError(f"Expected 88-char MRZ or two lines, got {len(mrz_string)} chars")

    return check_mrz(line1, line2)


# ---------------------------------------------------------------------------
# Quick test / demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # ICAO specimen passport MRZ (Angela Zoe, UK, from PRADO examples)
    LINE1 = "P<GBRSPECIMEN<<ANGELA<ZOE<<<<<<<<<<<<<<<<<<<<<"
    LINE2 = "9250647140GBR8812049F2010855<<<<<<<<<<<<<<<6"

    result = check_mrz(LINE1, LINE2)
    import json
    print(json.dumps(result.to_dict(), indent=2))
    print(f"\nOverall: {'PASS' if result.overall_pass else 'FAIL'}")
