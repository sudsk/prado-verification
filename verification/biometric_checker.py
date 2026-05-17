"""
verification/biometric_checker.py
===================================
Face match between passport photo (from bio page scan) and a live selfie.

Uses Gemini Vision for POC-level face similarity comparison.
For production, replace with a dedicated face recognition model
(Vertex AI Vision Face Detection, FaceNet, DeepFace, AWS Rekognition etc.)

Two checks performed:
  1. Face match    — same person in passport vs selfie?
  2. Photo quality — is the passport photo clearly visible and unobstructed?
"""

import base64
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class BiometricResult:
    face_match_score: float = 0.0       # 0.0 (different) to 1.0 (same person)
    face_match_pass: Optional[bool] = None  # True if score >= threshold
    face_match_threshold: float = 0.75  # configurable
    face_detected_passport: bool = False
    face_detected_selfie: bool = False
    photo_quality: str = ""             # good / acceptable / poor
    liveness_note: str = ""             # note on liveness (Gemini can't do real liveness)
    reasoning: str = ""                 # Gemini's explanation
    error: str = ""

    @property
    def overall_pass(self) -> bool:
        return (
            self.face_match_pass is True
            and self.face_detected_passport
            and self.face_detected_selfie
        )

    def to_dict(self) -> dict:
        return {
            "overall_pass":            self.overall_pass,
            "face_match_score":        self.face_match_score,
            "face_match_pass":         self.face_match_pass,
            "face_match_threshold":    self.face_match_threshold,
            "face_detected_passport":  self.face_detected_passport,
            "face_detected_selfie":    self.face_detected_selfie,
            "photo_quality":           self.photo_quality,
            "liveness_note":           self.liveness_note,
            "reasoning":               self.reasoning,
            "error":                   self.error,
            "production_note": (
                "POC: Gemini Vision used for face similarity. "
                "Replace with dedicated face recognition model for production."
            ),
        }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

FACE_MATCH_PROMPT = """You are a biometric verification assistant.
You are given two images:
  Image 1: a passport bio page scan (contains a passport photo)
  Image 2: a selfie or live photo of a person

Analyse both images and return ONLY valid JSON with this exact structure:
{
  "face_detected_passport": true,
  "face_detected_selfie": true,
  "face_match_score": 0.85,
  "same_person": true,
  "photo_quality_passport": "<good or acceptable or poor>",
  "reasoning": "<brief explanation of key facial features compared>",
  "confidence": "<high or medium or low>"
}

Scoring rules for face_match_score (0.0 to 1.0):
  0.0 - 0.3  Clearly different people
  0.3 - 0.6  Uncertain / insufficient detail
  0.6 - 0.8  Likely same person
  0.8 - 1.0  Very likely same person

Important:
- Focus on facial geometry, not photo style or lighting
- Passport photos are often older, different lighting, different expression
- Do not penalise for age difference unless extreme (>20 years apparent difference)
- If either image has no detectable face, set face_match_score to 0.0
- Return ONLY the JSON — no preamble, no markdown
"""


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

class BiometricChecker:

    def __init__(self, project_id: Optional[str] = None,
                 location: str = "us-central1",
                 match_threshold: float = 0.75):
        self.project_id = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self.location = location
        self.match_threshold = match_threshold

    def check_from_files(
        self,
        passport_scan_path: str,
        selfie_path: str,
    ) -> BiometricResult:
        """Check face match from two file paths."""
        from pathlib import Path

        def read(path):
            with open(path, "rb") as f:
                return f.read()

        def mime(path):
            return "image/jpeg" if Path(path).suffix.lower() in (".jpg", ".jpeg") \
                   else "image/png"

        return self.check_from_bytes(
            passport_bytes=read(passport_scan_path),
            passport_mime=mime(passport_scan_path),
            selfie_bytes=read(selfie_path),
            selfie_mime=mime(selfie_path),
        )

    def check_from_bytes(
        self,
        passport_bytes: bytes,
        selfie_bytes: bytes,
        passport_mime: str = "image/jpeg",
        selfie_mime: str = "image/jpeg",
    ) -> BiometricResult:
        """Check face match from raw image bytes."""
        result = BiometricResult(face_match_threshold=self.match_threshold)

        try:
            import vertexai
            from vertexai.generative_models import GenerativeModel, Part

            vertexai.init(project=self.project_id, location=self.location)
            model = GenerativeModel("gemini-2.5-flash")

            passport_part = Part.from_data(data=passport_bytes, mime_type=passport_mime)
            selfie_part   = Part.from_data(data=selfie_bytes,   mime_type=selfie_mime)

            response = model.generate_content([
                FACE_MATCH_PROMPT,
                "Image 1 (passport bio page):",
                passport_part,
                "Image 2 (selfie/live photo):",
                selfie_part,
            ])

            self._parse_response(response.text.strip(), result)

        except Exception as e:
            result.error = str(e)

        return result

    def check_from_base64(
        self,
        passport_b64: str,
        selfie_b64: str,
        passport_mime: str = "image/jpeg",
        selfie_mime: str = "image/jpeg",
    ) -> BiometricResult:
        """Check face match from base64-encoded images."""
        return self.check_from_bytes(
            passport_bytes=base64.b64decode(passport_b64),
            selfie_bytes=base64.b64decode(selfie_b64),
            passport_mime=passport_mime,
            selfie_mime=selfie_mime,
        )

    def _parse_response(self, raw: str, result: BiometricResult):
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            result.error = f"JSON parse error: {e} — raw: {raw[:200]}"
            return

        result.face_detected_passport = bool(data.get("face_detected_passport", False))
        result.face_detected_selfie   = bool(data.get("face_detected_selfie", False))
        result.face_match_score       = float(data.get("face_match_score", 0.0))
        result.photo_quality          = data.get("photo_quality_passport", "")
        result.reasoning              = data.get("reasoning", "")
        result.liveness_note = (
            "NOTE: Gemini Vision cannot perform true liveness detection. "
            "For production use a dedicated anti-spoofing model."
        )

        result.face_match_pass = (
            result.face_match_score >= self.match_threshold
            and result.face_detected_passport
            and result.face_detected_selfie
        )


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python biometric_checker.py <passport_scan.jpg> <selfie.jpg>")
        sys.exit(1)

    checker = BiometricChecker()
    result = checker.check_from_files(sys.argv[1], sys.argv[2])
    print(json.dumps(result.to_dict(), indent=2))
    print(f"\nBiometric: {'PASS' if result.overall_pass else 'FAIL'}")
