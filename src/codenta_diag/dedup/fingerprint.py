from __future__ import annotations

import hashlib


def exact_fingerprint(practice_id: str, source_note_id: str, pipeline_run_id: str,
                      predicted_codes: list[str]) -> str:
    """Identity of a failure: same practice + note + pipeline run + predicted output.

    Different notes that share an error category deliberately get DIFFERENT fingerprints; relating
    them is the job of similarity retrieval + evidence-based grouping, not exact dedup.
    """
    material = "|".join([practice_id, source_note_id, pipeline_run_id, ",".join(sorted(predicted_codes))])
    return hashlib.sha256(material.encode()).hexdigest()
