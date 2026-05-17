"""
ingestion/prado_pdf_parser.py
==============================
Parse PRADO passport PDF exports into structured CSV/JSON for BigQuery.

Usage:
    # Parse specific files
    python ingestion/prado_pdf_parser.py \
        --files prado_pdfs/GBR-AO-04001.pdf prado_pdfs/GBR-AO-06001.pdf \
        --output-dir data/

    # Parse all PDFs in a directory
    python ingestion/prado_pdf_parser.py \
        --input-dir prado_pdfs/ \
        --output-dir data/

Outputs:
    data/prado_documents.csv   — one row per passport version
    data/prado_features.csv    — one row per security feature
    data/prado_features.json   — nested JSON (BQ-importable)
"""

import argparse
import csv
import json
import re
import sys
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

warnings.filterwarnings("ignore")  # suppress pdfplumber font warnings

import pdfplumber


# ---------------------------------------------------------------------------
# Feature classification
# ---------------------------------------------------------------------------

# Maps PRADO security feature category keywords → verification_method
# digital  = checkable from bio page scan + NFC data alone
# visual   = requires physical inspection (UV lamp, hologram, etc.)
# both     = partially checkable digitally

VERIFICATION_MAP = {
    # --- Digital ---
    "biographical data":    ("digital", "none"),
    "personal data":        ("digital", "none"),
    "facial image":         ("digital", "none"),
    "numbering":            ("digital", "none"),
    "electronic data":      ("digital", "NFC reader"),
    "mrz":                  ("digital", "none"),
    "barcode":              ("digital", "none"),
    "digital seal":         ("digital", "none"),
    # --- Visual ---
    "uv feature":           ("visual",  "UV lamp (365nm)"),
    "watermark":            ("visual",  "light box (transmitted)"),
    "ovd":                  ("visual",  "tilt/oblique light"),
    "optically variable":   ("visual",  "tilt/oblique light"),
    "printing technique":   ("visual",  "loupe/microscope"),
    "substrate":            ("visual",  "physical feel"),
    "binding":              ("visual",  "physical inspection"),
    "perforation":          ("visual",  "physical inspection"),
    "laminate":             ("visual",  "UV lamp + tilt"),
    "embossing":            ("visual",  "physical feel"),
    "fluorescent":          ("visual",  "UV lamp (365nm)"),
    "additional safeguard": ("visual",  "physical inspection"),
    # --- Both ---
    "cover":                ("both",    "visual + colour analysis"),
    "signature":            ("both",    "none"),
}


def classify_feature(category: str) -> tuple[str, str]:
    """Return (verification_method, requires_equipment) for a feature category."""
    lower = category.lower()
    for keyword, result in VERIFICATION_MAP.items():
        if keyword in lower:
            return result
    return ("visual", "physical inspection")


def bio_page_checkable(category: str, page_location: str) -> bool:
    """
    True if this feature can be checked from a standard bio page scan.
    Only features ON the biodata page with digital verification_method.
    """
    method, _ = classify_feature(category)
    if method != "digital":
        return False
    bio_locations = {"biodata page", "integrated biodata card", "page 2"}
    loc_lower = page_location.lower()
    return any(b in loc_lower for b in bio_locations)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SecurityFeature:
    feature_id: str            # GBR-AO-06001-F001
    document_id: str           # GBR-AO-06001
    country_code: str          # GBR
    country_name: str
    page_location: str         # Outside front cover / Biodata page / Inner page(s)
    feature_category: str      # UV feature / OVD / Facial image / etc.
    feature_detail: str        # sub-detail e.g. "fluorescent overprint"
    light_condition: str       # NORMAL / UV LIGHT (365nm) / TRANSMITTED / OBLIQUE
    description: str           # full description text
    colours: str               # e.g. "green, red"
    technique: str             # e.g. "intaglio printing"
    security_element: str      # e.g. "latent image", "rainbow colouring"
    verification_method: str   # digital | visual | both
    requires_equipment: str    # UV lamp / NFC reader / none / etc.
    bio_page_scannable: bool   # True = checkable from standard VFS bio page scan


@dataclass
class PassportDocument:
    document_id: str           # GBR-AO-06001
    country_code: str          # GBR
    country_name: str          # United Kingdom
    doc_type: str              # Ordinary Passport
    version: str               # 06001
    title: str                 # BRITISH PASSPORT / PASSPORT
    first_issued: str          # 01/03/2020
    valid: bool
    max_validity_years: str    # 10
    num_pages: str             # 32
    format_width_mm: str       # 88
    format_height_mm: str      # 125
    cover_material: str        # plastic
    cover_colour: str          # blue / burgundy
    cover_construction: str    # flexible
    cover_embossing: str       # hot foil stamping
    has_electronic_chip: bool
    biodata_substrate: str     # paper / PC (polycarbonate)
    photo_type: str            # colour / black & white
    photo_integration: str     # inkjet printing / laser engraving
    series: str                # Series B / Series C
    external_ref: str          # Home Office NDF 01/20
    prado_source_file: str
    features: list = field(default_factory=list)  # list of feature_ids


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class PRADOPdfParser:

    # Known page location headings in PRADO documents
    LOCATION_HEADINGS = [
        "Outside front cover",
        "Inside front cover",
        "Inside back cover",
        "Biodata page",
        "Integrated biodata card - recto (identity)",
        "Integrated biodata card - verso",
        "Inner page(s)",
        "Cover",
        "Title page",
    ]

    def __init__(self):
        self.documents: list[PassportDocument] = []
        self.features: list[SecurityFeature] = []

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def parse_file(self, pdf_path: str) -> Optional[PassportDocument]:
        path = Path(pdf_path)
        print(f"Parsing: {path.name}")

        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join(
                page.extract_text() or "" for page in pdf.pages
            )

        doc = self._parse_document_meta(full_text, path.name)
        if not doc:
            print(f"  WARNING: could not extract document metadata from {path.name}")
            return None

        features = self._parse_features(full_text, doc)
        doc.features = [f.feature_id for f in features]

        self.documents.append(doc)
        self.features.extend(features)
        print(f"  OK  {doc.document_id}: {len(features)} features | "
              f"cover={doc.cover_colour} | chip={doc.has_electronic_chip} | "
              f"photo={doc.photo_integration}")
        return doc

    def parse_directory(self, directory: str):
        for pdf in sorted(Path(directory).glob("*.pdf")):
            self.parse_file(str(pdf))

    def write_csv(self, output_dir: str):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        # --- prado_documents.csv ---
        doc_fields = [
            "document_id", "country_code", "country_name", "doc_type",
            "version", "title", "first_issued", "valid", "max_validity_years",
            "num_pages", "format_width_mm", "format_height_mm",
            "cover_material", "cover_colour", "cover_construction",
            "cover_embossing", "has_electronic_chip", "biodata_substrate",
            "photo_type", "photo_integration", "series", "external_ref",
            "prado_source_file",
        ]
        with open(out / "prado_documents.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=doc_fields)
            w.writeheader()
            for doc in self.documents:
                row = asdict(doc)
                row.pop("features", None)
                w.writerow({k: row[k] for k in doc_fields})
        print(f"Written: {out}/prado_documents.csv ({len(self.documents)} rows)")

        # --- prado_features.csv ---
        feat_fields = [
            "feature_id", "document_id", "country_code", "country_name",
            "page_location", "feature_category", "feature_detail",
            "light_condition", "description", "colours", "technique",
            "security_element", "verification_method", "requires_equipment",
            "bio_page_scannable",
        ]
        with open(out / "prado_features.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=feat_fields)
            w.writeheader()
            for feat in self.features:
                w.writerow(asdict(feat))
        print(f"Written: {out}/prado_features.csv ({len(self.features)} rows)")

    def write_json(self, output_dir: str):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        feat_by_doc: dict[str, list] = {}
        for f in self.features:
            feat_by_doc.setdefault(f.document_id, []).append(asdict(f))

        output = {}
        for doc in self.documents:
            cc = doc.country_code
            if cc not in output:
                output[cc] = {
                    "country_code": cc,
                    "country_name": doc.country_name,
                    "documents": {},
                }
            d = asdict(doc)
            d["features"] = feat_by_doc.get(doc.document_id, [])
            output[cc]["documents"][doc.document_id] = d

        with open(out / "prado_features.json", "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"Written: {out}/prado_features.json")

    # -----------------------------------------------------------------------
    # Document metadata
    # -----------------------------------------------------------------------

    def _parse_document_meta(self, text: str, source_file: str) -> Optional[PassportDocument]:
        def grab(pattern, default=""):
            m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            return m.group(1).strip() if m else default

        def grab_after_label(label, default=""):
            # Matches "Label:\nValue" or "Label: Value" patterns
            m = re.search(
                rf"^{re.escape(label)}[:\s]+(.+)$",
                text, re.IGNORECASE | re.MULTILINE
            )
            return m.group(1).strip() if m else default

        doc_id_m = re.search(r"Document:\s*(([A-Z]{3})-AO-(\d+))", text)
        if not doc_id_m:
            return None

        doc_id      = doc_id_m.group(1)
        country_code = doc_id_m.group(2)
        version     = doc_id_m.group(3)

        country_name = grab(
            r"Issuing Country:\s*[A-Z]{3}\s*-\s*([^•\n]+?)(?:\s*•|\s*$)"
        )
        title = grab(r"Title:\s*(.+)")
        first_issued = grab(r"First issued on:\s*(\d{2}/\d{2}/\d{4})")
        valid_str = grab(r"Valid:\s*(yes|no)", "yes")
        max_validity = grab(r"Maximum validity:\s*(\d+)")
        num_pages = grab(r"Number of pages:\s*(\d+)")
        fmt_w = grab(r"Format width \(mm\):\s*(\d+)")
        fmt_h = grab(r"Format height \(mm\):\s*(\d+)")
        cover_material = grab(r"Material:\s*(\w+)")
        cover_colour = grab(r"Overall colou?r:\s*(\w+)")
        cover_construction = grab(r"Construction:\s*(\w+)")
        cover_embossing = grab(r"Cover embossing:\s*([^\n]+)")
        external_ref = grab(r"External reference number:\s*([^\n]+)")

        has_chip = bool(re.search(
            r"electronic data|e-passport|microchip|contactless|NFC",
            text, re.IGNORECASE
        ))

        # Biodata substrate — key difference between Series B (paper) and C (polycarbonate)
        biodata_substrate = grab(r"Substrate:\s*(PC \(polycarbonate\)|polycarbonate|paper)", "paper")

        # Photo type and integration
        photo_type = grab(r"Photo type:\s*([^\n]+)")
        photo_integration = grab(r"Photo integration technique:\s*([^\n]+)")

        # Series detection (C = post-Brexit blue, B = burgundy)
        series = ""
        if "Series C" in text:
            series = "C"
        elif "Series B" in text:
            series = "B"

        return PassportDocument(
            document_id=doc_id,
            country_code=country_code,
            country_name=country_name,
            doc_type="Ordinary Passport",
            version=version,
            title=title,
            first_issued=first_issued,
            valid=(valid_str.lower() == "yes"),
            max_validity_years=max_validity,
            num_pages=num_pages,
            format_width_mm=fmt_w,
            format_height_mm=fmt_h,
            cover_material=cover_material,
            cover_colour=cover_colour,
            cover_construction=cover_construction,
            cover_embossing=cover_embossing.rstrip(",").strip(),
            has_electronic_chip=has_chip,
            biodata_substrate=biodata_substrate,
            photo_type=photo_type,
            photo_integration=photo_integration,
            series=series,
            external_ref=external_ref,
            prado_source_file=source_file,
        )

    # -----------------------------------------------------------------------
    # Feature extraction
    # -----------------------------------------------------------------------

    def _parse_features(self, text: str, doc: PassportDocument) -> list[SecurityFeature]:
        """
        Split the document text into page-location sections, then extract
        security feature blocks within each section.
        """
        features: list[SecurityFeature] = []
        feat_counter = 1
        seen: set[str] = set()

        # Split text into sections by location heading
        sections = self._split_into_sections(text)

        for location, section_text in sections:
            feat_blocks = self._extract_feature_blocks(section_text)
            for block in feat_blocks:
                category = block.get("category", "").strip()
                if not category or len(category) > 120:
                    continue

                detail      = block.get("detail", "")
                light       = block.get("light", "")
                description = block.get("description", "")[:300]
                colours     = block.get("colours", "")
                technique   = block.get("technique", "")
                sec_element = block.get("security_element", "")

                # Dedup: same category+light+detail in same location
                dedup_key = f"{location}|{category}|{light}|{detail[:40]}"
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)

                method, equipment = classify_feature(category)
                scannable = bio_page_checkable(category, location)

                feat = SecurityFeature(
                    feature_id=f"{doc.document_id}-F{feat_counter:03d}",
                    document_id=doc.document_id,
                    country_code=doc.country_code,
                    country_name=doc.country_name,
                    page_location=location,
                    feature_category=category,
                    feature_detail=detail[:200],
                    light_condition=light,
                    description=description,
                    colours=colours,
                    technique=technique,
                    security_element=sec_element,
                    verification_method=method,
                    requires_equipment=equipment,
                    bio_page_scannable=scannable,
                )
                features.append(feat)
                feat_counter += 1

        return features

    def _split_into_sections(self, text: str) -> list[tuple[str, str]]:
        """Split full document text into (location_heading, text_block) pairs."""
        # Build regex alternation from known headings
        headings_pattern = "|".join(re.escape(h) for h in self.LOCATION_HEADINGS)
        heading_re = re.compile(
            rf"^({headings_pattern})\s*$",
            re.IGNORECASE | re.MULTILINE
        )

        splits = list(heading_re.finditer(text))
        if not splits:
            # No headings found — treat entire text as "unknown"
            return [("Unknown", text)]

        sections = []
        for i, match in enumerate(splits):
            heading = match.group(1).strip()
            start = match.end()
            end = splits[i + 1].start() if i + 1 < len(splits) else len(text)
            section_text = text[start:end]
            sections.append((heading, section_text))

        return sections

    def _extract_feature_blocks(self, section_text: str) -> list[dict]:
        """
        Within a section, find all Security features: X blocks and
        extract their sub-fields.
        """
        blocks = []

        # Find each "Security features: <Category>" occurrence
        feat_re = re.compile(
            r"Security features:\s*(.+?)(?=Security features:|$)",
            re.DOTALL | re.IGNORECASE
        )

        for m in feat_re.finditer(section_text):
            block_text = m.group(0)
            category_line = m.group(1).split("\n")[0].strip()

            # Clean up category — remove trailing punctuation
            category = re.sub(r"[:\.\,]+$", "", category_line).strip()

            block = {"category": category}

            # Extract sub-fields
            def extract(pattern, key, text=block_text):
                sub = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
                if sub:
                    block[key] = sub.group(1).strip()

            extract(r"^Light:\s*(.+)$",              "light")
            extract(r"^UV feature:\s*(.+)$",          "detail")
            extract(r"^OVD \(optically variable device\):\s*(.+)$", "detail")
            extract(r"^Substrate:\s*(.+)$",           "detail")
            extract(r"^Description:\s*(.+)$",         "description")
            extract(r"^Colours?:\s*(.+)$",            "colours")
            extract(r"^Technique:\s*(.+)$",           "technique")
            extract(r"^Security Element:\s*(.+)$",    "security_element")
            extract(r"^Remark:\s*(.+)$",              "security_element")
            extract(r"^Photo type:\s*(.+)$",          "detail")
            extract(r"^Photo integration technique:\s*(.+)$", "technique")
            extract(r"^Identifiers:\s*(.+)$",         "detail")
            extract(r"^Biodata integration:\s*(.+)$", "detail")
            extract(r"^OVD description:\s*(.+)$",     "description")
            extract(r"^Additional safeguard:\s*(.+)$","detail")

            blocks.append(block)

        return blocks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Parse PRADO passport PDFs into BigQuery-ready CSV/JSON"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--files", nargs="+", metavar="PDF",
        help="Specific PDF files to parse"
    )
    group.add_argument(
        "--input-dir", metavar="DIR",
        help="Directory containing PRADO PDF files"
    )
    parser.add_argument(
        "--output-dir", default="data",
        help="Output directory for CSV/JSON files (default: data/)"
    )
    args = parser.parse_args()

    p = PRADOPdfParser()

    if args.files:
        for f in args.files:
            p.parse_file(f)
    else:
        p.parse_directory(args.input_dir)

    if not p.documents:
        print("ERROR: no documents parsed. Check your input files.")
        sys.exit(1)

    p.write_csv(args.output_dir)
    p.write_json(args.output_dir)

    print(f"\nDone: {len(p.documents)} documents, {len(p.features)} features")
    digital = sum(1 for f in p.features if f.verification_method == "digital")
    visual  = sum(1 for f in p.features if f.verification_method == "visual")
    both    = sum(1 for f in p.features if f.verification_method == "both")
    scannable = sum(1 for f in p.features if f.bio_page_scannable)
    print(f"  digital={digital}  visual={visual}  both={both}")
    print(f"  bio_page_scannable={scannable} (checkable from standard VFS scan)")


if __name__ == "__main__":
    main()
