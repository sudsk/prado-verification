"""
verification/document_detector.py
===================================
Automatically detect the PRADO document_id from OCR + VIZ data.
Queries BigQuery prado_documents to find the best matching version.

Detection signals (in priority order):
  1. Country code        — from MRZ (definitive)
  2. Issue date          — from VIZ, matched against first_issued ranges in BQ
  3. Title               — "BRITISH PASSPORT" vs "PASSPORT"
  4. Cover colour        — blue vs burgundy (from vision model)
  5. Photo integration   — laser engraving vs inkjet (from vision model)
"""

import os
from typing import Optional


class DocumentDetector:

    DETECT_QUERY = """
        SELECT
            document_id,
            version,
            first_issued,
            cover_colour,
            title,
            photo_integration,
            series
        FROM `{project}.{dataset}.prado_documents`
        WHERE country_code = @country_code
          AND valid = TRUE
        ORDER BY first_issued DESC
    """

    def __init__(self, project_id: Optional[str] = None,
                 dataset: str = "prado_verification"):
        self.project_id = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self.dataset = dataset

    def detect(
        self,
        country_code: str,
        issue_date: str = "",        # DD/MM/YYYY or DD MMM YYYY from VIZ
        cover_colour: str = "",      # blue / burgundy
        title: str = "",             # BRITISH PASSPORT / PASSPORT
        photo_integration: str = "", # inkjet printing / laser engraving
    ) -> tuple[str, float]:
        """
        Returns (document_id, confidence_score).
        confidence_score: 0.0-1.0 — how certain the match is.
        """
        try:
            candidates = self._fetch_candidates(country_code)
        except Exception as e:
            return ("", 0.0)

        if not candidates:
            return ("", 0.0)

        if len(candidates) == 1:
            return (candidates[0]["document_id"], 0.9)

        return self._score_candidates(
            candidates, issue_date, cover_colour, title, photo_integration
        )

    def _fetch_candidates(self, country_code: str) -> list[dict]:
        from google.cloud import bigquery
        client = bigquery.Client(project=self.project_id)
        query = self.DETECT_QUERY.format(
            project=self.project_id, dataset=self.dataset
        )
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("country_code", "STRING", country_code)
        ])
        return [dict(r) for r in client.query(query, job_config=job_config).result()]

    def _score_candidates(
        self, candidates: list[dict],
        issue_date: str, cover_colour: str,
        title: str, photo_integration: str,
    ) -> tuple[str, float]:
        """Score each candidate and return the best match."""
        scored = []
        for c in candidates:
            score = 0.0
            signals = 0

            # Cover colour — strong signal
            if cover_colour and c.get("cover_colour"):
                signals += 1
                if cover_colour.lower() == c["cover_colour"].lower():
                    score += 0.40

            # Title — strong signal (BRITISH PASSPORT only on 06001+)
            if title and c.get("title"):
                signals += 1
                if title.upper().strip() == c["title"].upper().strip():
                    score += 0.30

            # Photo integration — strong signal
            if photo_integration and c.get("photo_integration"):
                signals += 1
                if photo_integration.lower() in c["photo_integration"].lower():
                    score += 0.20

            # Issue date — if VIZ has date of issue, match to version window
            if issue_date and c.get("first_issued"):
                signals += 1
                parsed = self._parse_date(issue_date)
                issued = self._parse_date(c["first_issued"])
                if parsed and issued and parsed >= issued:
                    score += 0.10

            # Normalise score by signals used
            final = score if signals > 0 else 0.0
            scored.append((c["document_id"], final))

        scored.sort(key=lambda x: x[1], reverse=True)
        best_id, best_score = scored[0]

        # If top two are tied, confidence is lower
        if len(scored) > 1 and abs(scored[0][1] - scored[1][1]) < 0.05:
            best_score = best_score * 0.7  # penalise ambiguity

        return (best_id, round(best_score, 2))

    @staticmethod
    def _parse_date(date_str: str):
        """Parse DD/MM/YYYY or DD MMM YYYY into a comparable value."""
        import re
        from datetime import datetime
        MONTHS = {
            "JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
            "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"
        }
        date_str = date_str.strip()
        # DD/MM/YYYY
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", date_str)
        if m:
            try: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except: pass
        # DD MMM YYYY
        m = re.match(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", date_str.upper())
        if m:
            mm = MONTHS.get(m.group(2), "00")
            try: return datetime(int(m.group(3)), int(mm), int(m.group(1)))
            except: pass
        return None
