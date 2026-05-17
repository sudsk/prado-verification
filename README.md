# prado-verification

Automated passport authenticity verification using PRADO security feature data.

Parses EU Council PRADO passport specifications into BigQuery, then verifies
passport scans against those specifications using OCR, MRZ check digit validation,
NFC chip checks, and biometric cross-referencing.

## Architecture

```
prado-verification/
├── ingestion/
│   ├── prado_pdf_parser.py     # Parse PRADO PDFs → structured CSV/JSON
│   ├── bq_schema.py            # BigQuery table definitions (Python)
│   ├── bq_schema.sql           # BigQuery DDL — run directly in BQ console
│   └── bq_loader.py            # Load CSVs into BigQuery
├── verification/
│   ├── mrz_checker.py          # ICAO 9303 MRZ intrinsic checks
│   ├── ocr_extractor.py        # Gemini Vision — extract fields from bio page scan
│   ├── nfc_parser.py           # ICAO 9303 NFC chip data parsing + verification
│   ├── document_detector.py    # Auto-detect PRADO document_id from OCR signals
│   └── prado_matcher.py        # BQ lookup + digital/visual check routing
├── api/
│   └── main.py                 # FastAPI — POST scan → verification verdict
├── tests/
│   └── test_mrz.py             # 40 unit tests (all passing)
└── data/                       # Generated — gitignored
    ├── prado_documents.csv     # 5 UK passport versions
    ├── prado_features.csv      # 77 security features
    └── prado_features.json
```

## Setup

```bash
pip install -r requirements.txt

export GCP_PROJECT_ID=your-project-id
export GCP_LOCATION=us-central1       # gemini-2.5-flash availability
export BQ_DATASET=prado_verification
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/serviceAccount.json
```

## Step 1 — Parse PRADO PDFs

Download PDFs from https://www.consilium.europa.eu/prado/en/ (print to PDF from browser)
and run the parser:

```bash
python ingestion/prado_pdf_parser.py \
  --input-dir ./prado_pdfs/ \
  --output-dir ./data/

# Or parse specific files
python ingestion/prado_pdf_parser.py \
  --files prado_pdfs/GBR-AO-05001.pdf prado_pdfs/GBR-AO-06001.pdf \
  --output-dir ./data/
```

Outputs:
- `data/prado_documents.csv` — one row per passport version
- `data/prado_features.csv`  — one row per security feature
- `data/prado_features.json` — nested JSON

## Step 2 — Create BigQuery tables

Option A — run SQL directly in BQ console (replace `YOUR_PROJECT`):
```
ingestion/bq_schema.sql
```

Option B — Python:
```bash
python ingestion/bq_schema.py \
  --project $GCP_PROJECT_ID \
  --dataset $BQ_DATASET \
  --location europe-west2
```

## Step 3 — Load data into BigQuery

```bash
python ingestion/bq_loader.py \
  --project $GCP_PROJECT_ID \
  --dataset $BQ_DATASET \
  --documents data/prado_documents.csv \
  --features  data/prado_features.csv
```

## Step 4 — Run the API

```bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Service info and endpoint listing |
| GET | `/health` | Liveness check |
| POST | `/verify` | Full verification — scan + optional document_id |
| POST | `/verify/mrz` | MRZ-only check — no image or BQ needed |
| POST | `/detect` | Detect document_id from scan alone |
| GET | `/document/{id}` | Fetch PRADO features for a document |

### POST /verify

`document_id` is **optional** — auto-detected from scan if not provided.

```bash
# With explicit document_id
curl -X POST http://localhost:8000/verify \
  -F "scan=@passport_scan.jpg" \
  -F "document_id=GBR-AO-06001"

# Fully automatic — no document_id needed
curl -X POST http://localhost:8000/verify \
  -F "scan=@passport_scan.jpg"

# With MRZ override (bypasses OCR for MRZ)
curl -X POST http://localhost:8000/verify \
  -F "scan=@passport_scan.jpg" \
  -F "mrz_line1=P<GBRSPECIMEN<<ANGELA<ZOE<<<<<<<<<<<<<<<<<<" \
  -F "mrz_line2=9250647142GBR8812049F2512314<<<<<<<<<<<<<<<6"

# With NFC chip data
curl -X POST http://localhost:8000/verify \
  -F "scan=@passport_scan.jpg" \
  -F "nfc_data_json={\"bac_pace_success\":true,\"passive_auth_pass\":true,\"DG1\":{\"mrz_line1\":\"P<GBR...\",\"mrz_line2\":\"925...\"}}"
```

### POST /verify/mrz

No image needed — MRZ string only:

```bash
curl -X POST http://localhost:8000/verify/mrz \
  -H "Content-Type: application/json" \
  -d '{"line1": "P<GBRSPECIMEN<<ANGELA<ZOE<<<<<<<<<<<<<<<<<<",
       "line2": "9250647142GBR8812049F2512314<<<<<<<<<<<<<<<6"}'
```

### POST /detect

Identify document version from scan alone:

```bash
curl -X POST http://localhost:8000/detect \
  -F "scan=@passport_scan.jpg"
```

## Verification layers

| Layer | Checks | Data source | DB needed |
|-------|--------|-------------|-----------|
| MRZ intrinsic | Check digits CD1-CD5, format, expiry, country code | MRZ from scan | No |
| NFC intrinsic | Passive Auth, Active Auth, Chip Auth (ICAO 9303) | NFC chip | No |
| OCR consistency | MRZ vs VIZ field match, date sanity | Bio page scan | No |
| Biometric | Photo presence, type, integration technique | Bio page scan | No |
| PRADO digital | Numbering, facial image, electronic data | BQ lookup | Yes |
| PRADO visual | UV, watermark, OVD, laminate → manual review | BQ lookup | Yes |
| Cross-source | MRZ = OCR = NFC on all fields | All sources | No |

## Document auto-detection

When `document_id` is not provided to `/verify`, the system detects it automatically
using four weighted signals extracted by OCR:

| Signal | Weight | Example values |
|--------|--------|----------------|
| Cover colour | 0.40 | `blue` → GBR-AO-06001; `burgundy` → GBR-AO-04001 to 05002 |
| Photo integration | 0.25 | `laser engraving` → 06001; `inkjet printing` → earlier |
| Document title | 0.20 | `BRITISH PASSPORT` → 06001+; `PASSPORT` → earlier |
| Issue date window | 0.15 | Issue date ≥ version first_issued date |

Detection confidence is returned in every `/verify` response as `detection_confidence`.
A value below 0.5 means the caller should consider passing `document_id` explicitly.

## verification_method field

Every PRADO feature in BigQuery carries a `verification_method` tag:
- `digital` — checkable from bio page scan + NFC data alone
- `visual`  — requires physical inspection (UV lamp, hologram tilt, etc.)
- `both`    — partially digital, partially physical

Only `bio_page_scannable = TRUE` features can be checked from a standard
VFS-style remote submission (bio page scan only — no cover, no inner pages).

## Verdict scoring

| Risk score | Verdict | Meaning |
|------------|---------|---------|
| 0.00–0.14 | PASS | All digital checks clean |
| 0.15–0.44 | REFER | Minor issues — manual review recommended |
| 0.45–1.00 | FAIL | Significant failures — reject |

Risk weights:
- MRZ check digit failure: +0.35
- Passport expired: +0.10
- MRZ/VIZ field mismatch: +0.25
- No photo detected: +0.10
- PRADO digital check failures: up to +0.20

## UK passport versions in BQ

| document_id | Issued | Cover | Biodata substrate | Photo method |
|-------------|--------|-------|-------------------|--------------|
| GBR-AO-04001 | Oct 2010 | Burgundy | Paper | Inkjet |
| GBR-AO-04002 | Jun 2011 | Burgundy | Paper | Inkjet (BN(O)) |
| GBR-AO-05001 | Dec 2015 | Burgundy | Paper | Inkjet |
| GBR-AO-05002 | Jul 2019 | Burgundy | Paper | Inkjet (post-Brexit) |
| GBR-AO-06001 | Mar 2020 | **Blue** | **Polycarbonate** | **Laser engraving** |

## Running tests

```bash
pytest tests/ -v
# 40 tests, all passing
```

## GitHub

```bash
git init
git add .
git commit -m "Initial build: PDF parser, MRZ checker, OCR extractor, PRADO matcher, FastAPI"
gh repo create prado-verification --public --source=. --push
```
