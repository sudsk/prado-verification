# prado-verification

Automated passport authenticity verification using PRADO security feature data.

## Architecture

```
prado-verification/
├── ingestion/
│   ├── prado_pdf_parser.py     # Parse PRADO PDFs → structured data
│   ├── bq_schema.py            # BigQuery table definitions
│   └── bq_loader.py            # Load parsed data into BigQuery
├── verification/
│   ├── mrz_checker.py          # Intrinsic MRZ checks (checkdigits, format)
│   ├── ocr_extractor.py        # Gemini Vision — extract fields from bio page scan
│   ├── nfc_parser.py           # Parse NFC/RFID chip data (ICAO 9303)
│   ├── biometric_checker.py    # Face match, liveness, ghost image checks
│   └── prado_matcher.py        # Query BQ PRADO features, run digital checks
├── api/
│   └── main.py                 # FastAPI — upload scan → verification verdict
├── tests/
│   └── test_mrz.py             # MRZ check digit unit tests
└── data/                       # Output CSVs from parser (gitignored)
```

## Setup

```bash
pip install -r requirements.txt

# Set GCP credentials
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/serviceAccount.json
export GCP_PROJECT_ID=your-project-id
export BQ_DATASET=prado_verification
```

## Step 1 — Parse PRADO PDFs

Download PDFs from https://www.consilium.europa.eu/prado/en/ and run:

```bash
python ingestion/prado_pdf_parser.py \
  --input-dir ./prado_pdfs/ \
  --output-dir ./data/
```

Outputs:
- `data/prado_documents.csv` — one row per passport version
- `data/prado_features.csv`  — one row per security feature
- `data/prado_features.json` — nested JSON version

## Step 2 — Load to BigQuery

```bash
python ingestion/bq_loader.py \
  --project $GCP_PROJECT_ID \
  --dataset $BQ_DATASET \
  --documents data/prado_documents.csv \
  --features  data/prado_features.csv
```

## Step 3 — Run the API

```bash
uvicorn api.main:app --reload
```

POST a passport scan to `/verify`:
```bash
curl -X POST http://localhost:8000/verify \
  -F "scan=@passport_scan.jpg" \
  -F "country_code=GBR" \
  -F "document_version=GBR-AO-06001"
```

## Verification layers

| Layer | Checks | Data source |
|-------|--------|-------------|
| MRZ intrinsic | Check digits CD1-CD5, format, expiry, country code | MRZ lines from scan |
| NFC intrinsic | Passive Auth, Active Auth, Chip Auth (ICAO 9303) | NFC chip |
| OCR consistency | MRZ vs VIZ field match, date sanity, font anomaly | Bio page scan |
| Biometric | Face match, liveness, ghost image, photo substitution | Photo + chip DG2 |
| PRADO digital | Numbering format, facial image type, electronic data | BQ lookup |
| PRADO visual | UV, watermark, OVD, laminate → manual review queue | BQ lookup |
| Cross-source | MRZ = OCR = NFC = chip DG1 on all fields | All sources |

## verification_method field

Every PRADO feature in BigQuery has a `verification_method` tag:
- `digital` — checkable from bio page scan + NFC alone
- `visual` — requires physical inspection (UV lamp, hologram tilt, etc.)
- `both` — partially digital, partially physical

## Notes on GBR passport versions

| Version | Issued | Key difference |
|---------|--------|---------------|
| GBR-AO-04001 | Oct 2010 | Burgundy, paper bio page, inkjet photo |
| GBR-AO-04002 | Jun 2011 | British National (Overseas) variant |
| GBR-AO-05001 | Dec 2015 | Burgundy, inkjet photo, new watermarks |
| GBR-AO-05002 | Jul 2019 | Post-Brexit — no "European Union" on cover |
| GBR-AO-06001 | Mar 2020 | **Blue**, polycarbonate biodata card, laser engraving, CLI/MLI OVD |
