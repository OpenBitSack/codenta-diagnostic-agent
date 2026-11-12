"""Production-style note + trace store.

`ReadOnlyTraceStore` is the ONLY interface the diagnostic agent gets. It exposes getters only,
enforces practice scoping, and returns immutable/deep-copied models. In AWS this maps to a read
replica / read-only DB role on the prediction pipeline's trace store.

`PipelineRecorder` simulates the production pipeline writing its own runs (seed data and the dev-only
"run pipeline" endpoint). The agent never receives a reference to it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..config_registry import ConfigRegistry
from ..db import Database, PipelineRunRow, SourceNoteRow
from ..domain import PipelineRun, SourceNote
from ..pipeline import production_config, run_pipeline
from ..settings import PROJECT_ROOT


class NotFound(LookupError):
    pass


class PracticeScopeError(PermissionError):
    pass


class ReadOnlyTraceStore:
    def __init__(self, db: Database, practice_scope: str) -> None:
        self._db = db
        self.practice_scope = practice_scope

    def get_note(self, note_id: str) -> SourceNote:
        with self._db.session() as s:
            row = s.get(SourceNoteRow, note_id)
            if row is None:
                raise NotFound(f"note {note_id}")
            if row.practice_id != self.practice_scope:
                raise PracticeScopeError(f"note {note_id} is outside practice scope")
            return SourceNote(note_id=row.note_id, practice_id=row.practice_id, text=row.text,
                              synthetic=row.synthetic)

    def get_run(self, run_id: str) -> PipelineRun:
        with self._db.session() as s:
            row = s.get(PipelineRunRow, run_id)
            if row is None:
                raise NotFound(f"run {run_id}")
            if row.practice_id != self.practice_scope:
                raise PracticeScopeError(f"run {run_id} is outside practice scope")
            return PipelineRun(**row.payload)


class PipelineRecorder:
    def __init__(self, db: Database, registry: ConfigRegistry) -> None:
        self.db = db
        self.registry = registry

    def add_note(self, note: SourceNote) -> None:
        with self.db.session() as s:
            if s.get(SourceNoteRow, note.note_id) is None:
                s.add(SourceNoteRow(note_id=note.note_id, practice_id=note.practice_id, text=note.text,
                                    synthetic=True))

    def record_run(self, run_id: str, note: SourceNote, policy_ref: str | None,
                   pipeline_version: str = "v1.3") -> PipelineRun:
        self.add_note(note)
        with self.db.session() as s:
            existing = s.get(PipelineRunRow, run_id)
            if existing is not None:
                return PipelineRun(**existing.payload)
        run = run_pipeline(note, policy_ref, production_config(self.registry, pipeline_version), self.registry,
                           run_id=run_id)
        with self.db.session() as s:
            s.add(PipelineRunRow(run_id=run_id, practice_id=note.practice_id, note_id=note.note_id,
                                 pipeline_version=pipeline_version, policy_ref=policy_ref,
                                 payload=run.model_dump(mode="json")))
        return run


def seed_production_store(db: Database, registry: ConfigRegistry,
                          path: Path = PROJECT_ROOT / "data" / "production_store" / "store.yaml") -> list[dict[str, Any]]:
    with path.open() as fh:
        spec = yaml.safe_load(fh)
    recorder = PipelineRecorder(db, registry)
    out = []
    for note in spec["notes"]:
        n = SourceNote(note_id=note["note_id"], practice_id=note["practice_id"], text=note["text"])
        run = recorder.record_run(note["run_id"], n, note.get("policy_ref"), note.get("pipeline_version", "v1.3"))
        out.append({"note_id": n.note_id, "run_id": run.run_id, "predicted": run.predicted_codes})
    return out
