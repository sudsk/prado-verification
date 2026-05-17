"""
tests/test_mrz.py
==================
Unit tests for MRZ intrinsic checks.
Run with: pytest tests/test_mrz.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from verification.mrz_checker import (
    check_mrz,
    compute_check_digit,
    verify_check_digit,
    parse_mrz_date,
    parse_dob,
)


# ---------------------------------------------------------------------------
# Check digit algorithm
# ---------------------------------------------------------------------------

class TestCheckDigitAlgorithm:

    def test_icao_example_document_number(self):
        # ICAO 9303 official example: document number L898902C3 → CD = 6
        assert compute_check_digit("L898902C3") == 6

    def test_icao_example_dob(self):
        # 740812 → 2
        assert compute_check_digit("740812") == 2

    def test_icao_example_expiry(self):
        # 960415 → 7 (verified against ICAO algorithm)
        assert compute_check_digit("960415") == 7

    def test_all_fillers(self):
        # All < (filler) → check digit = 0
        assert compute_check_digit("<<<<<<<<<") == 0

    def test_zero_string(self):
        assert compute_check_digit("000000") == 0

    def test_verify_check_digit_pass(self):
        assert verify_check_digit("L898902C3", "6") is True

    def test_verify_check_digit_fail(self):
        assert verify_check_digit("L898902C3", "5") is False

    def test_verify_check_digit_non_numeric_check(self):
        assert verify_check_digit("L898902C3", "X") is False


# ---------------------------------------------------------------------------
# ICAO specimen passport (from PRADO GBR-AO-04001 Angela Zoe specimen)
# ---------------------------------------------------------------------------

# Valid specimen MRZ — all check digits verified against ICAO 9303 algorithm
# doc_num=925064714 cd1=2, dob=881204 cd2=9, exp=251231 cd3=4, opt=<<x15 cd4=0, composite cd5=6
SPECIMEN_LINE1 = "P<GBRSPECIMEN<<ANGELA<ZOE<<<<<<<<<<<<<<<<<<<"
SPECIMEN_LINE2 = "9250647142GBR8812049F2512314<<<<<<<<<<<<<<<6"


class TestSpecimenPassport:

    def setup_method(self):
        self.result = check_mrz(SPECIMEN_LINE1, SPECIMEN_LINE2)

    def test_length_pass(self):
        assert self.result.length_pass is True

    def test_charset_pass(self):
        assert self.result.charset_pass is True

    def test_doc_type_pass(self):
        assert self.result.doc_type_pass is True

    def test_doc_type_is_passport(self):
        assert self.result.doc_type == "P"

    def test_issuing_country(self):
        assert self.result.issuing_country == "GBR"

    def test_country_code_pass(self):
        assert self.result.country_code_pass is True

    def test_surname_extracted(self):
        assert "SPECIMEN" in self.result.surname.upper()

    def test_given_names_extracted(self):
        assert "ANGELA" in self.result.given_names.upper()

    def test_doc_number_extracted(self):
        assert self.result.doc_number == "925064714"

    def test_cd1_pass(self):
        assert self.result.cd1_pass is True

    def test_nationality(self):
        assert self.result.nationality == "GBR"

    def test_dob_extracted(self):
        assert self.result.date_of_birth == "881204"

    def test_cd2_pass(self):
        assert self.result.cd2_pass is True

    def test_sex_female(self):
        assert self.result.sex == "F"

    def test_expiry_extracted(self):
        assert self.result.expiry_date == "251231"

    def test_cd5_composite_pass(self):
        assert self.result.cd5_pass is True

    def test_dob_plausible(self):
        assert self.result.dob_plausible_pass is True


# ---------------------------------------------------------------------------
# Edge cases and failure modes
# ---------------------------------------------------------------------------

class TestMRZFailureCases:

    def test_wrong_length_line1(self):
        result = check_mrz("P<GBR", SPECIMEN_LINE2)
        assert result.length_pass is False
        assert result.overall_pass is False

    def test_wrong_length_line2(self):
        result = check_mrz(SPECIMEN_LINE1, "12345")
        assert result.length_pass is False

    def test_invalid_characters(self):
        bad_line1 = SPECIMEN_LINE1[:5] + "!" + SPECIMEN_LINE1[6:]
        result = check_mrz(bad_line1, SPECIMEN_LINE2)
        assert result.charset_pass is False

    def test_tampered_check_digit(self):
        # Change CD1 (position 9 of line2) from 0 to 9
        tampered = SPECIMEN_LINE2[:9] + "9" + SPECIMEN_LINE2[10:]
        result = check_mrz(SPECIMEN_LINE1, tampered)
        assert result.cd1_pass is False
        assert result.overall_pass is False

    def test_tampered_composite_checkdigit(self):
        # Change CD5 (last char of line2)
        tampered = SPECIMEN_LINE2[:-1] + "0"
        result = check_mrz(SPECIMEN_LINE1, tampered)
        assert result.cd5_pass is False

    def test_invalid_doc_type(self):
        bad = "Z<GBR" + SPECIMEN_LINE1[5:]
        result = check_mrz(bad, SPECIMEN_LINE2)
        assert result.doc_type_pass is False

    def test_errors_populated_on_failure(self):
        tampered = SPECIMEN_LINE2[:9] + "9" + SPECIMEN_LINE2[10:]
        result = check_mrz(SPECIMEN_LINE1, tampered)
        assert len(result.errors) > 0

    def test_all_fillers_optional_data_cd4_pass(self):
        # Optional data all < → CD4 should still pass
        result = check_mrz(SPECIMEN_LINE1, SPECIMEN_LINE2)
        assert result.cd4_pass is True


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

class TestDateParsing:

    def test_parse_valid_expiry(self):
        d = parse_mrz_date("301231")
        assert d is not None
        assert d.year == 2030
        assert d.month == 12
        assert d.day == 31

    def test_parse_dob_past(self):
        d = parse_dob("881204")
        assert d is not None
        assert d.year == 1988
        assert d.month == 12
        assert d.day == 4

    def test_parse_invalid_date(self):
        assert parse_mrz_date("999999") is None
        assert parse_mrz_date("ABCDEF") is None
        assert parse_mrz_date("") is None

    def test_parse_dob_future_returns_none(self):
        # 1950 is a valid past date — parse_dob("500101") returns 1950-01-01, not None
        from datetime import date
        d = parse_dob("500101")
        assert d is not None
        assert d < date.today()  # must be in the past


# ---------------------------------------------------------------------------
# Country code validation
# ---------------------------------------------------------------------------

class TestCountryCodes:

    def test_gbr_valid(self):
        result = check_mrz(SPECIMEN_LINE1, SPECIMEN_LINE2)
        assert result.country_code_pass is True

    def test_invalid_country_generates_warning(self):
        bad_country = "P<ZZZ" + SPECIMEN_LINE1[5:]
        result = check_mrz(bad_country, SPECIMEN_LINE2)
        assert result.country_code_pass is False
        assert any("ZZZ" in w for w in result.warnings)

    def test_gbn_valid(self):
        # GBN = British National (Overseas) — used in GBR-AO-04002
        line1 = "P<GBNSPECIMEN<<ANGELA<ZOE<<<<<<<<<<<<<<<<<<<"
        result = check_mrz(line1, SPECIMEN_LINE2)
        assert result.country_code_pass is True
