"""
verification/document_detector.py
===================================
Automatically detect the PRADO document_id from OCR output.
Queries BigQuery prado_documents to find the best matching version.

Detection signals (weighted):
  Weight  Signal                  Source
  0.40    Cover colour            OCR viz.cover_colour
  0.25    Photo integration       OCR viz.photo_integration_technique
  0.20    Document title          OCR viz.document_title
  0.15    Issue date window       OCR viz.date_of_issue vs BQ first_issued

All signals are optional — the detector degrades gracefully when
fewer signals are available.
"""

import os
from typing import Optional


class DocumentDetector:

    CANDIDATES_QUERY = """
        SELECT
            document_id,
            version,
            first_issued,
            cover_colour,
            title,
            photo_integration,
            series,
            has_electronic_chip
        FROM `{project}.{dataset}.prado_documents`
        WHERE country_code = @country_code
          AND valid = TRUE
        ORDER BY first_issued DESC
    """

    def __init__(self, project_id: Optional[str] = None,
                 dataset: str = "prado_verification"):
        self.project_id = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self.dataset = dataset

    def detect_from_ocr(self, ocr_result) -> tuple[str, float]:
        """
        Detect document_id directly from an OCRResult object.
        Returns (document_id, confidence_score 0.0-1.0).
        """
        if not ocr_result or not ocr_result.mrz.line1:
            return ("", 0.0)

        country_code = ocr_result.mrz.line1[2:5].replace("<", "").strip()
        if not country_code:
            return ("", 0.0)

        return self.detect(
            country_code=country_code,
            cover_colour=ocr_result.viz.cover_colour,
            photo_integration=ocr_result.viz.photo_integration_technique,
            document_title=ocr_result.viz.document_title,
            issue_date=ocr_result.viz.date_of_issue,
        )

    def detect(
        self,
        country_code: str,
        cover_colour: str = "",
        photo_integration: str = "",
        document_title: str = "",
        issue_date: str = "",
    ) -> tuple[str, float]:
        """
        Returns (document_id, confidence_score).

        Args:
            country_code:      ISO-3 e.g. "GBR"
            cover_colour:      "blue" / "burgundy" / "" from OCR
            photo_integration: "inkjet printing" / "laser engraving" / "" from OCR
            document_title:    "BRITISH PASSPORT" / "PASSPORT" / "" from OCR
            issue_date:        "27 NOV 2019" / "DD/MM/YYYY" / "" from VIZ
        """
        try:
            candidates = self._fetch_candidates(country_code)
        except Exception as e:
            return ("", 0.0)

        if not candidates:
            return ("", 0.0)

        if len(candidates) == 1:
            return (candidates[0]["document_id"], 0.85)

        return self._score_and_rank(
            candidates, cover_colour, photo_integration,
            document_title, issue_date
        )

    def _fetch_candidates(self, country_code: str) -> list[dict]:
        from google.cloud import bigquery
        client = bigquery.Client(project=self.project_id)
        query = self.CANDIDATES_QUERY.format(
            project=self.project_id, dataset=self.dataset
        )
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("country_code", "STRING", country_code)
        ])
        return [dict(r) for r in
                client.query(query, job_config=job_config).result()]

    def _score_and_rank(
        self, candidates: list[dict],
        cover_colour: str, photo_integration: str,
        document_title: str, issue_date: str,
    ) -> tuple[str, float]:

        scored = []
        for c in candidates:
            score = 0.0
            signals_used = 0
            max_possible = 0.0

            # --- Photo integration technique (weight 0.45) ---
            # cover_colour removed — VFS bio-page-only scans never show the cover
            max_possible += 0.45
            if photo_integration and c.get("photo_integration"):
                signals_used += 1
                ocr_pi = photo_integration.lower()
                bq_pi  = c["photo_integration"].lower()
                if ("laser" in ocr_pi and "laser" in bq_pi) or \
                   ("inkjet" in ocr_pi and "inkjet" in bq_pi):
                    score += 0.45

            # --- Document title (weight 0.35) ---
            max_possible += 0.35
            if document_title and c.get("title"):
                signals_used += 1
                if document_title.upper().strip() == c["title"].upper().strip():
                    score += 0.35

            # --- Issue date window (weight 0.20) ---
            # If VIZ shows an issue date, verify it falls AFTER this version's
            # first_issued date (i.e. this version was available when issued)
            max_possible += 0.15
            if issue_date and c.get("first_issued"):
                parsed_issue   = self._parse_date(issue_date)
                parsed_release = self._parse_date(c["first_issued"])
                if parsed_issue and parsed_release:
                    signals_used += 1
                    if parsed_issue >= parsed_release:
                        score += 0.20

            # Normalise: if signals available > 0, scale confidence
            # by how much of the max_possible we actually scored
            if signals_used == 0:
                confidence = 0.0
            else:
                confidence = round(score / max_possible, 2) if max_possible > 0 else 0.0

            scored.append((c["document_id"], score, confidence))

        # Sort by raw score descending
        scored.sort(key=lambda x: x[1], reverse=True)

        best_id, best_score, best_conf = scored[0]

        # Penalise if top two candidates are too close
        if len(scored) > 1:
            _, second_score, _ = scored[1]
            if best_score > 0 and abs(best_score - second_score) < 0.10:
                best_conf = round(best_conf * 0.6, 2)  # ambiguous

        # If no signals fired at all, return lowest-confidence best guess
        # (most recently issued version for that country)
        if best_score == 0:
            best_id   = scored[0][0]
            best_conf = 0.10  # very low confidence — just defaulting to latest

        return (best_id, best_conf)

    @staticmethod
    def _parse_date(date_str: str):
        from datetime import datetime
        MONTHS = {
            "JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
            "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12",
        }
        import re
        s = str(date_str).strip()
        # DD/MM/YYYY
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
        if m:
            try: return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except: pass
        # DD MMM YYYY
        m = re.match(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", s.upper())
        if m:
            mm = MONTHS.get(m.group(2), "00")
            try: return datetime(int(m.group(3)), int(mm), int(m.group(1)))
            except: pass
        # YYYY-MM-DD
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            try: return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except: pass
        return None
