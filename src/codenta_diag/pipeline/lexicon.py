"""Phrase-level note analysis with character spans (provenance).

Shared by the simulated extraction stage, QA v2's note-evidence checks, and the agent's
deterministic evidence locator. It is a heuristic tuned to synthetic notes, NOT a clinical NLP model.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import cached_property
from typing import Any

TOOTH_RE = re.compile(r"#(\d{1,2})\b")
FILLING_RE = re.compile(r"#(\d{1,2})\s+([MODBL]{1,5})\b[^.]*?\bcomposite\b")
PA_COUNT_RE = re.compile(r"\b(\d+)\s+PAs?\b")


@dataclass(frozen=True)
class Span:
    text: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Sentence:
    text: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(phrase) + r"(?![\w-])", re.IGNORECASE)


class NoteAnalyzer:
    def __init__(self, text: str, lexicon: dict[str, Any]) -> None:
        self.text = text
        self.lex = lexicon

    # ---------- generic helpers ----------
    @cached_property
    def sentences(self) -> list[Sentence]:
        out = []
        for m in re.finditer(r"[^.]+\.?", self.text):
            raw = m.group(0)
            stripped = raw.strip()
            if stripped:
                out.append(Sentence(stripped, m.start() + raw.index(stripped)))
        return out

    def _find(self, phrases: list[str], sentences: list[Sentence]) -> list[Span]:
        spans: list[Span] = []
        for s in sentences:
            for phrase in phrases:
                for m in _phrase_regex(phrase).finditer(s.text):
                    spans.append(Span(m.group(0), s.start + m.start(), s.start + m.end()))
        return sorted(spans, key=lambda sp: sp.start)

    def _find_regex(self, patterns: list[str], sentences: list[Sentence]) -> list[Span]:
        spans: list[Span] = []
        for s in sentences:
            for pat in patterns:
                for m in re.finditer(pat, s.text, re.IGNORECASE):
                    spans.append(Span(m.group(0), s.start + m.start(), s.start + m.end()))
        return sorted(spans, key=lambda sp: sp.start)

    # ---------- x-ray / prophylaxis ----------
    def xray(self) -> tuple[int, list[Span]] | None:
        spans = self._find_regex(self.lex["xray_cues"]["PA"], self.sentences)
        if not spans:
            return None
        counts = [(int(m.group(1)), m) for m in PA_COUNT_RE.finditer(self.text)]
        # Each image is counted once: repeated mentions of "PA" without a count are the same image.
        count = max((c for c, _ in counts), default=1)
        return count, spans

    def prophylaxis(self) -> list[Span]:
        return self._find(self.lex["prophylaxis_cues"], self.sentences)

    # ---------- fillings ----------
    def fillings(self) -> list[dict[str, Any]]:
        out = []
        for s in self.sentences:
            for m in FILLING_RE.finditer(s.text):
                planned = any(_phrase_regex(c).search(s.text) for c in self.lex["planned_cues"])
                out.append(
                    {
                        "tooth": int(m.group(1)),
                        "surfaces": list(m.group(2)),
                        "material": "composite",
                        "status": "planned" if planned else "done_today",
                        "span": Span(m.group(0), s.start + m.start(), s.start + m.end()),
                    }
                )
        return out

    # ---------- crowns ----------
    @cached_property
    def crown_tooth(self) -> tuple[int, Span] | None:
        triggers = re.compile(r"\b(crown|prepped|prep|seat|seated)\b", re.IGNORECASE)
        for s in self.sentences:
            if "composite" in s.text.lower() or not triggers.search(s.text):
                continue
            m = TOOTH_RE.search(s.text)
            if m:
                return int(m.group(1)), Span(m.group(0), s.start + m.start(), s.start + m.end())
        return None

    @cached_property
    def crown_context(self) -> list[Sentence]:
        if self.crown_tooth is None:
            return []
        tooth_tag = f"#{self.crown_tooth[0]}"
        material_words = [p for phrases in self.lex["materials"].values() for p in phrases]
        ctx = []
        for s in self.sentences:
            low = s.text.lower()
            if "composite" in low:
                continue
            mentions_tooth = re.search(re.escape(tooth_tag) + r"\b", s.text) is not None
            other_tooth = any(int(t) != self.crown_tooth[0] for t in TOOTH_RE.findall(s.text))
            if mentions_tooth or "crown" in low or (
                not other_tooth and any(_phrase_regex(w).search(s.text) for w in material_words)
            ):
                ctx.append(s)
        return ctx

    def material_mentions(self) -> list[tuple[str, Span]]:
        found: list[tuple[str, Span]] = []
        for material, phrases in self.lex["materials"].items():
            for sp in self._find(phrases, self.crown_context):
                found.append((material, sp))
        # Longest match wins on overlap ("full gold" vs "gold").
        found.sort(key=lambda x: (x[1].start, -(x[1].end - x[1].start)))
        result: list[tuple[str, Span]] = []
        last_end = -1
        for material, sp in found:
            if sp.start >= last_end:
                result.append((material, sp))
                last_end = sp.end
        return result

    def selected_material(self) -> tuple[str, Span, Span] | None:
        """Material named after a selection cue ("pt chose X [over Y]") in the same sentence."""
        mentions = self.material_mentions()
        for cue in self._find(self.lex["selection_cues"], self.crown_context):
            sentence = next(s for s in self.crown_context if s.start <= cue.start < s.end)
            for material, sp in mentions:
                if cue.end <= sp.start < sentence.end:
                    return material, cue, sp
        return None

    def undecided(self) -> Span | None:
        spans = self._find(self.lex["undecided_cues"], self.crown_context)
        return spans[0] if spans else None

    def determinable_material(self) -> tuple[str | None, list[Span], str]:
        """Best material supported by the note, with evidence and a reason code."""
        sel = self.selected_material()
        if sel:
            return sel[0], [sel[1], sel[2]], "selection_cue"
        und = self.undecided()
        if und:
            return None, [und], "undecided"
        distinct = {m for m, _ in self.material_mentions()}
        if len(distinct) == 1:
            m = next(iter(distinct))
            return m, [sp for mat, sp in self.material_mentions() if mat == m], "single_mention"
        if len(distinct) > 1:
            return None, [sp for _, sp in self.material_mentions()], "multiple_without_selection"
        return None, [], "no_mention"

    def crown_status_evidence(self) -> dict[str, list[Span]]:
        cues = self.lex["crown_status_cues"]
        ev = {status: self._find(phrases, self.crown_context) for status, phrases in cues.items()}
        ev["future_seat"] = self._find_regex(self.lex["future_seat_patterns"], self.crown_context)
        return ev

    def clinical_crown_status(self) -> tuple[str | None, list[Span]]:
        """Clinical stage supported by the note; None when absent or self-contradictory."""
        ev = self.crown_status_evidence()
        if ev["seated"] and ev["future_seat"]:
            return None, ev["seated"] + ev["future_seat"]
        if ev["seated"]:
            return "seated", ev["seated"]
        if ev["prepared"] or ev["future_seat"]:
            return "prepared", ev["prepared"] + ev["future_seat"]
        if ev["planned"]:
            return "planned", ev["planned"]
        return None, []
