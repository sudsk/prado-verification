"""
ingestion/bq_schema.py
=======================
BigQuery table schema definitions for the prado-verification dataset.

Tables:
  prado_documents  — one row per PRADO passport document version
  prado_features   — one row per security feature
  verification_log — audit log of every verification run
"""

from google.cloud import bigquery

# ---------------------------------------------------------------------------
# prado_documents
# ---------------------------------------------------------------------------

DOCUMENTS_SCHEMA = [
    bigquery.SchemaField("document_id",         "STRING",  mode="REQUIRED",
        description="PRADO document ID e.g. GBR-AO-06001"),
    bigquery.SchemaField("country_code",         "STRING",  mode="REQUIRED",
        description="ISO 3166-1 alpha-3 country code"),
    bigquery.SchemaField("country_name",         "STRING",
        description="Full country name from PRADO"),
    bigquery.SchemaField("doc_type",             "STRING",
        description="Ordinary Passport / Emergency Travel Document / etc."),
    bigquery.SchemaField("version",              "STRING",
        description="Version number e.g. 06001"),
    bigquery.SchemaField("title",                "STRING",
        description="Document title e.g. BRITISH PASSPORT"),
    bigquery.SchemaField("first_issued",         "STRING",
        description="Date first issued DD/MM/YYYY"),
    bigquery.SchemaField("valid",                "BOOL",
        description="Whether this document version is still valid"),
    bigquery.SchemaField("max_validity_years",   "STRING",
        description="Maximum validity in years"),
    bigquery.SchemaField("num_pages",            "STRING",
        description="Number of pages"),
    bigquery.SchemaField("format_width_mm",      "STRING",
        description="Document width in mm (TD3 = 88mm)"),
    bigquery.SchemaField("format_height_mm",     "STRING",
        description="Document height in mm (TD3 = 125mm)"),
    bigquery.SchemaField("cover_material",       "STRING",
        description="Cover material e.g. plastic"),
    bigquery.SchemaField("cover_colour",         "STRING",
        description="Cover colour e.g. burgundy / blue"),
    bigquery.SchemaField("cover_construction",   "STRING",
        description="Cover construction e.g. flexible"),
    bigquery.SchemaField("cover_embossing",      "STRING",
        description="Cover embossing type e.g. hot foil stamping"),
    bigquery.SchemaField("has_electronic_chip",  "BOOL",
        description="Whether this passport has an NFC/RFID chip"),
    bigquery.SchemaField("biodata_substrate",    "STRING",
        description="Biodata page substrate: paper or PC (polycarbonate)"),
    bigquery.SchemaField("photo_type",           "STRING",
        description="Photo type: colour / black & white"),
    bigquery.SchemaField("photo_integration",    "STRING",
        description="Photo integration: inkjet printing / laser engraving"),
    bigquery.SchemaField("series",               "STRING",
        description="Series identifier e.g. B (burgundy) / C (blue post-Brexit)"),
    bigquery.SchemaField("external_ref",         "STRING",
        description="External reference number e.g. Home Office NDF 01/20"),
    bigquery.SchemaField("prado_source_file",    "STRING",
        description="Source PDF filename"),
]


# ---------------------------------------------------------------------------
# prado_features
# ---------------------------------------------------------------------------

FEATURES_SCHEMA = [
    bigquery.SchemaField("feature_id",           "STRING",  mode="REQUIRED",
        description="Unique feature ID e.g. GBR-AO-06001-F001"),
    bigquery.SchemaField("document_id",          "STRING",  mode="REQUIRED",
        description="Parent document ID e.g. GBR-AO-06001"),
    bigquery.SchemaField("country_code",         "STRING",  mode="REQUIRED"),
    bigquery.SchemaField("country_name",         "STRING"),
    bigquery.SchemaField("page_location",        "STRING",
        description="Where on the document: Biodata page / Cover / Inner page(s) / etc."),
    bigquery.SchemaField("feature_category",     "STRING",
        description="Feature type: UV feature / OVD / Facial image / Electronic data / etc."),
    bigquery.SchemaField("feature_detail",       "STRING",
        description="Sub-detail: fluorescent overprint / hologram / colour photo / etc."),
    bigquery.SchemaField("light_condition",      "STRING",
        description="Inspection light: NORMAL / UV LIGHT (365nm) / TRANSMITTED / OBLIQUE"),
    bigquery.SchemaField("description",          "STRING",
        description="Full PRADO description text"),
    bigquery.SchemaField("colours",              "STRING",
        description="Feature colours if specified"),
    bigquery.SchemaField("technique",            "STRING",
        description="Printing/integration technique"),
    bigquery.SchemaField("security_element",     "STRING",
        description="Security element type e.g. latent image / rainbow colouring"),
    bigquery.SchemaField("verification_method",  "STRING",  mode="REQUIRED",
        description="digital | visual | both"),
    bigquery.SchemaField("requires_equipment",   "STRING",
        description="Equipment needed: UV lamp / NFC reader / loupe / none / etc."),
    bigquery.SchemaField("bio_page_scannable",   "BOOL",
        description="True = checkable from a standard VFS bio page scan alone"),
]


# ---------------------------------------------------------------------------
# verification_log
# ---------------------------------------------------------------------------

VERIFICATION_LOG_SCHEMA = [
    bigquery.SchemaField("verification_id",      "STRING",  mode="REQUIRED",
        description="UUID for this verification run"),
    bigquery.SchemaField("timestamp",            "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("document_id",          "STRING",
        description="PRADO document ID matched e.g. GBR-AO-06001"),
    bigquery.SchemaField("country_code",         "STRING"),
    bigquery.SchemaField("mrz_line1",            "STRING",
        description="MRZ line 1 from scan (PII — consider masking)"),
    bigquery.SchemaField("mrz_line2",            "STRING",
        description="MRZ line 2 from scan (PII — consider masking)"),
    bigquery.SchemaField("overall_verdict",      "STRING",
        description="PASS / REFER / FAIL"),
    bigquery.SchemaField("risk_score",           "FLOAT64",
        description="0.0 (clean) to 1.0 (high risk)"),

    # Intrinsic check results
    bigquery.SchemaField("mrz_checkdigits_pass", "BOOL"),
    bigquery.SchemaField("mrz_format_pass",      "BOOL"),
    bigquery.SchemaField("mrz_expiry_pass",      "BOOL"),
    bigquery.SchemaField("mrz_country_pass",     "BOOL"),
    bigquery.SchemaField("nfc_passive_auth",     "BOOL"),
    bigquery.SchemaField("nfc_active_auth",      "BOOL"),
    bigquery.SchemaField("nfc_chip_auth",        "BOOL"),
    bigquery.SchemaField("ocr_mrz_viz_match",    "BOOL"),
    bigquery.SchemaField("ocr_date_consistent",  "BOOL"),
    bigquery.SchemaField("face_match_score",     "FLOAT64"),
    bigquery.SchemaField("liveness_pass",        "BOOL"),

    # PRADO check results
    bigquery.SchemaField("prado_digital_checks_total",  "INT64"),
    bigquery.SchemaField("prado_digital_checks_pass",   "INT64"),
    bigquery.SchemaField("prado_visual_checks_flagged", "INT64",
        description="Number of visual checks requiring manual review"),

    # Cross-source check
    bigquery.SchemaField("cross_source_pass",    "BOOL",
        description="True = MRZ / OCR / NFC all agree on all fields"),

    bigquery.SchemaField("manual_review_reasons", "STRING",
        description="JSON array of reasons flagged for manual review"),
    bigquery.SchemaField("raw_result_json",       "STRING",
        description="Full JSON result payload for audit"),
]


# ---------------------------------------------------------------------------
# Helper — create dataset + tables
# ---------------------------------------------------------------------------

def create_dataset_and_tables(project: str, dataset_id: str, location: str = "europe-west2"):
    """Create the BQ dataset and all tables if they don't already exist."""
    client = bigquery.Client(project=project)

    dataset_ref = bigquery.Dataset(f"{project}.{dataset_id}")
    dataset_ref.location = location
    dataset = client.create_dataset(dataset_ref, exists_ok=True)
    print(f"Dataset: {project}.{dataset_id} ({location})")

    tables = {
        "prado_documents":  DOCUMENTS_SCHEMA,
        "prado_features":   FEATURES_SCHEMA,
        "verification_log": VERIFICATION_LOG_SCHEMA,
    }

    for table_name, schema in tables.items():
        table_ref = dataset.table(table_name)
        table = bigquery.Table(table_ref, schema=schema)
        client.create_table(table, exists_ok=True)
        print(f"  Table: {table_name} ({len(schema)} fields)")

    print("Done.")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--project",  required=True)
    ap.add_argument("--dataset",  default="prado_verification")
    ap.add_argument("--location", default="europe-west2")
    args = ap.parse_args()
    create_dataset_and_tables(args.project, args.dataset, args.location)
