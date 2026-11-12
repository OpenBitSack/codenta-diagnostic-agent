"""DETERMINISTIC DEMO REASONER — not an LLM.

A transparent, rule-based investigation policy used so the full workflow runs (and is testable)
without an API key. It makes the same kinds of decisions an LLM reasoner makes through the same
tools, but its "reasoning" is the explicit code below. Investigations it produces are labelled
`reasoner=deterministic-demo` everywhere (API, dashboard, reports).

Policy: cheapest informative experiment first —
  reproduce baseline -> single-field corrections -> combined correction -> policy-input injection
  -> leave-one-out / counterfactual policy (to separate necessary vs latent causes)
  -> candidate QA on the failing output -> candidate extraction components -> conclude.
"""
from __future__ import annotations

from typing import Any, Iterator

from ..pipeline.coding import run_coding
from ..pipeline.runner import set_field
from .observations import Observation, causes_from_observations
from .reasoner_base import ReasonerOutput, ToolCall
from .state import Hypothesis, InvestigationState
from .tools import ToolContext

STATEMENTS = {
    "value_incorrect": "The extraction stage recorded an incorrect value for {field}.",
    "unrepresentable_value": "Extraction schema cannot represent the clinical value of {field}; "
                             "the stage was forced into a wrong enum value.",
    "coding.policy.not_applied": "The coding stage did not correctly apply the practice billing policy.",
    "input.policy.missing_at_runtime": "The practice billing policy was not supplied to the pipeline run.",
    "coding.policy.silent_default_when_missing": "Coding silently defaulted the billing policy instead of holding.",
    "qa.missed_error": "QA did not independently detect the incorrect / policy-violating prediction.",
    "model.capability": "The extraction model lacks capability for this note (requires live model replay).",
}


def _statement(cause: str) -> str:
    if cause in STATEMENTS:
        return STATEMENTS[cause]
    parts = cause.split(".")
    field = ".".join(parts[-3:-1]) if parts[0] == "extraction" else cause
    return STATEMENTS.get(parts[-1], cause).format(field=field)


def _stage(cause: str) -> str:
    return {"extraction": "extraction", "coding": "coding", "input": "coding", "qa": "qa",
            "model": "extraction"}[cause.split(".")[0]]


def generate_hypotheses(obs: list[Observation], ctx: ToolContext) -> list[dict[str, Any]]:
    causes = causes_from_observations(obs)
    card = ctx.registry.rule_card()
    policy_procs = {p for p, spec in card["procedures"].items() if spec["billing"]["mode"] == "policy_event"}
    mismatch_on_policy_proc = any(
        o.kind in ("code_false_positive", "code_false_negative") and o.field in policy_procs for o in obs)
    value = [c for c in causes if c.endswith("value_incorrect")]
    policy_in = [c for c in causes if c.startswith(("input.", "coding."))]
    if mismatch_on_policy_proc:
        policy_in.append("coding.policy.not_applied")
    qa = [c for c in causes if c.startswith("qa.")]
    schema = [c for c in causes if ".schema." in c]
    ordered = value + policy_in + qa + schema + (["model.capability"] if value else [])
    return [{"cause_id": c, "stage": _stage(c), "statement": _statement(c)} for c in ordered]


def _field_of(cause: str) -> str:
    return ".".join(cause.split(".")[1:3])


class DeterministicReasoner:
    kind = "deterministic-demo"
    is_llm = False

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _obs(state: InvestigationState) -> list[Observation]:
        return [Observation(**o) for o in state.observations]

    @staticmethod
    def _corrections(state: InvestigationState) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for o in DeterministicReasoner._obs(state):
            if o.kind in ("field_contradicted_by_note", "field_unsupported_by_note"):
                out[o.field] = o.evidence_value
        return out

    @staticmethod
    def _value_hyps(state: InvestigationState) -> list[Hypothesis]:
        return [h for h in state.hypotheses if h.cause_id.endswith("value_incorrect")]

    @staticmethod
    def _exp(state: InvestigationState, key: str) -> dict[str, Any] | None:
        return state.experiment_by_label(key)

    def _correct_policy_event(self, ctx: ToolContext) -> str | None:
        if ctx.run_policy is not None:
            return ctx.run_policy.crown_billing_event
        return ctx.registry_policy.crown_billing_event if ctx.registry_policy else None

    def _derived_alt_expectation(self, ctx: ToolContext, corrections: dict[str, Any], alt: str) -> list[str]:
        """Rule card applied to evidence-corrected facts under the counterfactual policy (not a human label)."""
        facts = {k: dict(v) if isinstance(v, dict) else v for k, v in ctx.run.trace("extraction").output.items()}
        for path, value in corrections.items():
            set_field(facts, path, value)
        return run_coding(facts, alt, ctx.registry.rule_card())["predicted_codes"]

    def _replay(self, key: str, rationale: str, tests: list[str], **args: Any) -> tuple[str, ToolCall]:
        return key, ToolCall("run_sandbox_replay", {"label": key, "tests_hypotheses": tests, **args}, rationale)

    # ------------------------------------------------------------------ plan
    def _plan(self, state: InvestigationState, ctx: ToolContext) -> Iterator[tuple[str, ToolCall]]:
        corrections = self._corrections(state)
        value_hyps = self._value_hyps(state)
        hyp_by_cause = {h.cause_id: h for h in state.hypotheses}
        field_order = [_field_of(h.cause_id) for h in value_hyps]

        yield self._replay("baseline:recorded-extraction", "Reproduce the failure from saved stage outputs before "
                           "testing anything (cheap; confirms the trace is self-consistent).", [])
        yield self._replay("baseline:rerun-extraction", "Re-run the extraction stage with production versions to "
                           "check the failure is reproducible end-to-end.", [], from_stage="extraction")

        for i, f in enumerate(field_order):
            h = hyp_by_cause[f"extraction.{f}.value_incorrect"]
            prev = self._exp(state, f"single:{field_order[i - 1]}") if i else None
            if prev and prev["comparison"] and not prev["comparison"]["exact_match"]:
                why = (f"Previous correction left the error in place (predicted {prev['predicted_codes']}); "
                       f"continue with hypothesis {h.label}: correct only {f}.")
            else:
                why = f"Cheapest test of hypothesis {h.label}: inject the note-supported value of {f} only."
            yield self._replay(f"single:{f}", why, [h.id], field_overrides={f: corrections[f]})

        if len(field_order) >= 2:
            yield self._replay("combined:all-fields", "Correct all evidence-contradicted fields together to test "
                               "whether extraction errors fully explain the failure.",
                               [h.id for h in value_hyps], field_overrides=corrections)
        if len(field_order) >= 3:
            for f in field_order:
                rest = {k: v for k, v in corrections.items() if k != f}
                yield self._replay(f"loo:{f}", f"Leave-one-out: is {f} necessary?", [hyp_by_cause[
                    f"extraction.{f}.value_incorrect"].id], field_overrides=rest)

        if "input.policy.missing_at_runtime" in hyp_by_cause and ctx.registry_policy is not None:
            yield self._replay(
                "inject:registry-policy",
                f"No policy was attached to the run; inject the practice's registry policy "
                f"({ctx.registry_policy.ref} -> {ctx.registry_policy.crown_billing_event}) with corrected facts.",
                [hyp_by_cause["input.policy.missing_at_runtime"].id,
                 hyp_by_cause.get("coding.policy.silent_default_when_missing", hyp_by_cause[
                     "input.policy.missing_at_runtime"]).id],
                field_overrides=corrections, billing_event_override=ctx.registry_policy.crown_billing_event)

        corrected = self._corrected_experiment(state)
        coding_refuted = bool(corrected and corrected["comparison"] and corrected["comparison"]["exact_match"])
        event = self._correct_policy_event(ctx)

        # Label-implied facts: if evidence-supported corrections cannot reach the label, find out what the
        # label would require and whether the note supports it.
        if corrected and corrected["comparison"] and not corrected["comparison"]["exact_match"]:
            card = ctx.registry.rule_card()
            for code in corrected["comparison"]["false_negatives"]:
                spec = card["codes"].get(code)
                if spec and spec["procedure"] == "crown":
                    implied = {f"crown.{k}": v for k, v in spec["match"].items()}
                    yield self._replay(
                        f"label-implied:{code}",
                        f"Evidence-supported corrections do not reach the label; test what the label {code} "
                        f"implies ({implied}) to separate pipeline error from label/documentation conflict.",
                        [], field_overrides={**corrections, **implied},
                        **({"billing_event_override": event} if ctx.run_policy is None and event else {}))

        if coding_refuted and event and value_hyps and "crown" in ctx.run.trace("extraction").output:
            alt = "prep" if event == "seat" else "seat"
            derived = self._derived_alt_expectation(ctx, corrections, alt)
            yield self._replay(f"counterfactual:{alt}:all-fields",
                               f"Coding is verified correct on corrected facts; ask whether the conclusions depend "
                               f"on the policy by replaying under a {alt.upper()}-billing counterfactual.",
                               [h.id for h in value_hyps], field_overrides=corrections,
                               billing_event_override=alt, expected_override=derived)
            for f in field_order:
                rest = {k: v for k, v in corrections.items() if k != f}
                yield self._replay(f"counterfactual:{alt}:without:{f}",
                                   f"Under {alt.upper()} billing, is {f} necessary? (separates latent from "
                                   f"necessary causes)", [hyp_by_cause[f"extraction.{f}.value_incorrect"].id],
                                   field_overrides=rest, billing_event_override=alt, expected_override=derived)

        if "qa.missed_error" in hyp_by_cause:
            for change in ctx.registry.candidate_catalog():
                if "qa.missed_error" in change["addresses"]:
                    yield self._replay(f"qa-candidate:{change['id']}",
                                       "Run the candidate QA rules on the ORIGINAL failing output: would an "
                                       "independent check have caught it?", [hyp_by_cause["qa.missed_error"].id],
                                       candidate_change_ids=[change["id"]])

        extraction_causes = [c for c in hyp_by_cause if c.startswith("extraction.")]
        for change in ctx.registry.candidate_catalog():
            if change["component"] == "extraction" and set(change["addresses"]) & set(extraction_causes):
                tests = [hyp_by_cause[c].id for c in change["addresses"] if c in hyp_by_cause]
                yield self._replay(f"component:{change['id']}",
                                   f"Re-run the extraction stage with candidate '{change['id']}' alone to see which "
                                   f"fields a {change['overrides']} change can fix (prompt vs schema).", tests,
                                   from_stage="extraction", candidate_change_ids=[change["id"]])

    def _corrected_experiment(self, state: InvestigationState) -> dict[str, Any] | None:
        """Experiment where facts AND policy are corrected (the test of the coding-policy hypothesis)."""
        for key in ("inject:registry-policy", "combined:all-fields"):
            if (e := self._exp(state, key)) is not None:
                return e
        singles = [e for e in state.experiments if e["label"].startswith("single:")]
        if len(singles) == 1:
            return singles[0]
        if not self._value_hyps(state):
            return self._exp(state, "baseline:recorded-extraction")
        return None

    # ------------------------------------------------------------------ verdicts
    def verdicts(self, state: InvestigationState, ctx: ToolContext) -> dict[str, Any]:
        obs = self._obs(state)
        exp = {e["label"]: e for e in state.experiments}
        baseline = exp.get("baseline:recorded-extraction")
        base_codes = sorted(baseline["predicted_codes"]) if baseline else None

        def exact(label: str) -> bool | None:
            e = exp.get(label)
            return None if e is None or e["comparison"] is None else e["comparison"]["exact_match"]

        value_fields = [_field_of(h.cause_id) for h in self._value_hyps(state)]
        verdicts: list[dict[str, Any]] = []
        label_conflict = None
        for code_label, e in exp.items():
            if code_label.startswith("label-implied:") and e["comparison"] and e["comparison"]["exact_match"]:
                label_conflict = {"experiment_id": e["experiment_id"], "code": code_label.split(":")[1],
                                  "implied_facts": e["request"]["field_overrides"]}

        corrected = self._corrected_experiment(state)
        for h in state.hypotheses:
            v: dict[str, Any] = {"hypothesis_id": h.id, "roles": [], "supporting_experiments": [],
                                 "refuting_experiments": []}
            c = h.cause_id
            if c.endswith("value_incorrect"):
                f = _field_of(c)
                single = exp.get(f"single:{f}")
                changed = single is not None and sorted(single["predicted_codes"]) != base_codes
                if exact(f"single:{f}"):
                    v["roles"].append("sufficient")
                if len(value_fields) == 1:
                    necessary = not (baseline and baseline["comparison"]["exact_match"])
                    necessary_exp = baseline
                elif len(value_fields) == 2:
                    other = next(x for x in value_fields if x != f)
                    necessary_exp = exp.get(f"single:{other}")
                    necessary = necessary_exp is not None and not necessary_exp["comparison"]["exact_match"]
                else:
                    necessary_exp = exp.get(f"loo:{f}")
                    necessary = necessary_exp is not None and not necessary_exp["comparison"]["exact_match"]
                if necessary:
                    v["roles"].append("necessary")
                cf = next((e for k, e in exp.items() if k.startswith("counterfactual:") and k.endswith(f":without:{f}")), None)
                latent = cf is not None and not cf["comparison"]["exact_match"] and not necessary
                if latent:
                    v["roles"].append("latent")
                for e in (single, necessary_exp if necessary else None, cf if latent else None):
                    if e and e["experiment_id"] not in v["supporting_experiments"]:
                        v["supporting_experiments"].append(e["experiment_id"])
                if changed or necessary or latent:
                    v["status"] = "supported"
                    parts = []
                    if single:
                        parts.append(f"correcting only {f} -> {single['predicted_codes']} "
                                     f"({'matches label' if exact(f'single:{f}') else 'error remains'})")
                    if necessary:
                        parts.append(f"{f} is necessary under the actual policy")
                    if latent:
                        parts.append(f"{f} does not change today's codes but is necessary under the "
                                     f"counterfactual policy ({cf['predicted_codes']} vs derived "
                                     f"{cf['comparison']['expected']}) -> latent defect")
                    v["rationale"] = "; ".join(parts) + "."
                else:
                    v["status"] = "inconclusive"
                    v["rationale"] = f"note contradicts {f} but correcting it did not change outcomes in any replay."
            elif c == "coding.policy.not_applied":
                if corrected is None:
                    v.update(status="inconclusive", rationale="no experiment with corrected facts and policy")
                elif corrected["comparison"]["exact_match"]:
                    v.update(status="refuted", refuting_experiments=[corrected["experiment_id"]],
                             rationale=f"with corrected facts and the correct policy, the production coding stage "
                                       f"produces the expected codes {corrected['predicted_codes']} — it applies "
                                       f"the policy correctly when given correct facts; it propagated bad inputs.")
                elif label_conflict:
                    v.update(status="inconclusive", rationale="remaining mismatch is explained by a label/"
                             "documentation conflict, not by policy handling.")
                else:
                    v.update(status="supported", roles=["contributing"],
                             supporting_experiments=[corrected["experiment_id"]],
                             rationale=f"even with corrected facts/policy coding produced {corrected['predicted_codes']}.")
            elif c == "input.policy.missing_at_runtime":
                e = exp.get("inject:registry-policy")
                if e and (e["comparison"]["exact_match"] or sorted(e["predicted_codes"]) != base_codes):
                    roles = ["sufficient", "necessary"] if e["comparison"]["exact_match"] and not value_fields else ["necessary"]
                    v.update(status="supported", roles=roles, supporting_experiments=[e["experiment_id"]],
                             rationale=f"injecting the registry policy changes the output to {e['predicted_codes']}.")
                else:
                    v.update(status="inconclusive", rationale="injecting the registry policy did not change outcome")
            elif c == "coding.policy.silent_default_when_missing":
                e = exp.get("inject:registry-policy")
                obs_ids = [o.id for o in obs if o.kind == "policy_defaulted"]
                if e and obs_ids:
                    v.update(status="supported", roles=["contributing"], supporting_experiments=[e["experiment_id"]],
                             rationale="coding trace shows a silent default (no flag/hold); the defaulted event "
                                       "differs from the registry policy and changes the codes.")
                    v["supporting_observations"] = obs_ids
                else:
                    v.update(status="inconclusive", rationale="no evidence of a silent default")
            elif c == "qa.missed_error":
                obs_ids = [o.id for o in obs if o.kind == "qa_passed_with_mismatch"]
                cand = [e for k, e in exp.items() if k.startswith("qa-candidate:")]
                catching = [e for e in cand if not e["qa_passed"]]
                v.update(status="supported" if obs_ids else "inconclusive", roles=["contributing"],
                         supporting_experiments=[e["experiment_id"] for e in catching],
                         rationale="production QA passed the wrong prediction"
                                   + (f"; candidate QA flags it via {catching[0]['qa_failed_rules']}" if catching else
                                      "; no candidate QA rule catches it yet"))
                v["supporting_observations"] = obs_ids
            elif ".schema." in c:
                f = ".".join(c.split(".")[2:4])
                fixing = [e for k, e in exp.items() if k.startswith("component:")
                          and any("schema" in cid for cid in e["request"]["candidate_change_ids"])
                          and self._field_value(e["extraction"], f) == self._corrections(state).get(f)]
                obs_ids = [o.id for o in obs if o.kind == "schema_cannot_represent" and o.field == f]
                if obs_ids and fixing:
                    v.update(status="supported", roles=["structural"],
                             supporting_experiments=[e["experiment_id"] for e in fixing],
                             rationale=f"production schema has no enum value for the note's {f}; only a schema "
                                       f"change makes the stage emit the correct value (prompt-only changes cannot).")
                    v["supporting_observations"] = obs_ids
                else:
                    v.update(status="inconclusive", rationale="schema limitation not demonstrated by replay")
            elif c == "model.capability":
                v.update(status="untestable", rationale="requires replaying the real extraction model or an "
                         "alternative model; not possible with the deterministic simulated stage in this prototype.")
            else:
                v.update(status="inconclusive", rationale="no test defined")
            verdicts.append(v)

        return {"verdicts": verdicts, "label_conflict": label_conflict}

    @staticmethod
    def _field_value(extraction: dict[str, Any], path: str) -> Any:
        group, _, name = path.partition(".")
        return (extraction.get(group) or {}).get(name)

    # ------------------------------------------------------------------ main entry
    def next_actions(self, state: InvestigationState, ctx: ToolContext) -> ReasonerOutput:
        if not state.hypotheses:
            hyps = generate_hypotheses(self._obs(state), ctx)
            return ReasonerOutput([ToolCall("record_hypotheses", {"hypotheses": hyps},
                                            f"Generated {len(hyps)} hypotheses from deterministic observations "
                                            f"(observations are facts; hypotheses are untested).", "hypothesize")])
        for key, call in self._plan(state, ctx):
            if key not in state.plan_done:
                state.plan_done.append(key)
                return ReasonerOutput([call])

        result = self.verdicts(state, ctx)
        if ctx.expected is None:
            return ReasonerOutput([ToolCall("create_escalation_report", {
                "reason": "expected_codes_unknown",
                "questions_for_human": ["Provide the expected codes for this run so candidate fixes can be verified."],
            }, "Cannot verify any fix without a label.", "escalated")])
        if result["label_conflict"]:
            lc = result["label_conflict"]
            note_material = next((o for o in self._obs(state) if o.field == "crown.material"), None)
            return ReasonerOutput([ToolCall("create_escalation_report", {
                "reason": "label_documentation_conflict",
                "questions_for_human": [
                    f"The ticket expects {lc['code']}, which requires {lc['implied_facts']}. The note does not "
                    f"support this ({note_material.detail if note_material else 'no supporting evidence'}). Is there "
                    f"an authoritative source (e.g. lab Rx) for the selected material? If so, should the clinical "
                    f"note be amended rather than the pipeline changed?",
                    "Should notes with no documented material selection be HELD for review instead of auto-coded "
                    "(candidate prompt v2 + QA v2 behaviour)?",
                ],
                "verdicts": result["verdicts"],
            }, "Every evidence-supported correction fails to reach the label, but the label-implied facts reach it: "
               "this is a label/documentation conflict that requires human judgement, not a pipeline fix.",
                "escalated")])
        return ReasonerOutput([ToolCall("submit_diagnosis", {
            "earliest_incorrect_stage": state.earliest_incorrect_stage,
            "verdicts": result["verdicts"],
            "summary": "deterministic verdicts computed from cited sandbox experiments",
        }, "All testable hypotheses have been tested; evidence is sufficient to conclude.", "conclude")])

    def record_results(self, state: InvestigationState, results: list[tuple[ToolCall, dict[str, Any]]]) -> None:
        return None
