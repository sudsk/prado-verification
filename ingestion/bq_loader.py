"""
ingestion/bq_loader.py
=======================
Load prado_documents.csv and prado_features.csv into BigQuery.

Usage:
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/serviceAccount.json

    python ingestion/bq_loader.py \
        --project my-gcp-project \
        --dataset prado_verification \
        --documents data/prado_documents.csv \
        --features  data/prado_features.csv
"""

import argparse
from pathlib import Path

from google.cloud import bigquery


def load_csv(client: bigquery.Client, project: str, dataset: str,
             table_name: str, csv_path: str):
    table_ref = f"{project}.{dataset}.{table_name}"
    uri = csv_path  # local file path

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=1,        # skip header
        autodetect=False,           # use existing schema
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    print(f"Loading {Path(csv_path).name} → {table_ref} ...", end=" ", flush=True)
    with open(csv_path, "rb") as f:
        job = client.load_table_from_file(f, table_ref, job_config=job_config)
    job.result()  # wait

    table = client.get_table(table_ref)
    print(f"{table.num_rows} rows loaded")


def main():
    ap = argparse.ArgumentParser(description="Load PRADO CSVs into BigQuery")
    ap.add_argument("--project",   required=True, help="GCP project ID")
    ap.add_argument("--dataset",   default="prado_verification")
    ap.add_argument("--documents", required=True, help="Path to prado_documents.csv")
    ap.add_argument("--features",  required=True, help="Path to prado_features.csv")
    args = ap.parse_args()

    client = bigquery.Client(project=args.project)

    load_csv(client, args.project, args.dataset, "prado_documents", args.documents)
    load_csv(client, args.project, args.dataset, "prado_features",  args.features)

    print("\nAll done. Example queries:")
    print(f"""
  -- All digital checks for GBR-AO-06001
  SELECT feature_id, feature_category, feature_detail, requires_equipment
  FROM `{args.project}.{args.dataset}.prado_features`
  WHERE document_id = 'GBR-AO-06001'
    AND verification_method = 'digital'
  ORDER BY page_location;

  -- Bio page scannable checks only
  SELECT document_id, feature_category, feature_detail
  FROM `{args.project}.{args.dataset}.prado_features`
  WHERE bio_page_scannable = TRUE
  ORDER BY document_id;

  -- Visual checks requiring UV lamp
  SELECT document_id, page_location, feature_detail
  FROM `{args.project}.{args.dataset}.prado_features`
  WHERE requires_equipment LIKE '%UV%'
  ORDER BY document_id, page_location;
    """)


if __name__ == "__main__":
    main()
