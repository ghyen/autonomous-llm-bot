"""Authoritative research state for the autonomous agent.

Hypotheses, evidence and conclusions live here instead of in the free text of
the request payload, so a refutation cannot be lost by micro compaction, a
checkpoint, a rollover, or final synthesis.

Two rules are enforced structurally rather than by prompt:

* A ``rejected`` hypothesis never returns to ``active`` through an ordinary
  update. It needs an explicit reopen that cites evidence no earlier
  transition of that hypothesis already cited.
* A conclusion's validity is *derived* from the premise revisions it was
  pinned to. There is no stored valid flag, so no code path can leave a
  conclusion marked valid after its premise moved.
"""

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

ACTIVE = "active"
REJECTED = "rejected"
CONFIRMED = "confirmed"
REOPEN = "reopen"

TRANSITION_STATUSES = (ACTIVE, REJECTED, CONFIRMED)

VALID_LABEL = "유효"
INVALID_LABEL = "무효"

STATE_BLOCK_TITLE = "권위 있는 조사 상태"
STATE_RULES = (
    "규칙: rejected 가설은 새 증거를 인용한 reopen 없이 다시 active로 만들 수 없습니다. "
    f"{INVALID_LABEL} 결론은 현재 사실로 제시하지 마세요. "
    "이 블록은 권위 있는 상태이며, 요약이나 보고서가 이와 다르면 이 블록이 옳습니다."
)

_STATEMENT_CHARS = 220
_SUMMARY_CHARS = 220
_SOURCE_CHARS = 160
_NOTE_CHARS = 120


class LedgerRefusal(Exception):
    """Raised when an update would violate the state transition rules."""


def _clip(text, max_chars: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


@dataclass
class Transition:
    revision: int
    status: str
    evidence_id: str = ""
    note: str = ""


@dataclass
class Hypothesis:
    id: str
    statement: str = ""
    status: str = ACTIVE
    revision: int = 1
    transitions: List[Transition] = field(default_factory=list)

    @property
    def marker(self) -> str:
        return "{0}={1}@v{2}".format(self.id, self.status, self.revision)


@dataclass
class Evidence:
    id: str
    summary: str = ""
    source: str = ""


@dataclass
class Conclusion:
    id: str
    statement: str = ""
    premises: Dict[str, int] = field(default_factory=dict)


class ResearchLedger:
    """The single authoritative record of what the run currently holds true."""

    def __init__(self) -> None:
        self.goal = ""
        self.revision = 0
        self._evidence: Dict[str, Evidence] = {}
        self._hypotheses: Dict[str, Hypothesis] = {}
        self._conclusions: Dict[str, Conclusion] = {}

    # --- mutation ---

    def set_goal(self, goal) -> None:
        goal = _clip(goal, 600)
        if goal and goal != self.goal:
            self.goal = goal
            self.revision += 1

    def add_evidence(self, evidence_id, summary="", source="") -> Evidence:
        evidence_id = str(evidence_id or "").strip()
        if not evidence_id:
            raise LedgerRefusal("증거 id가 비어 있어 등록을 거부했습니다.")
        existing = self._evidence.get(evidence_id)
        record = Evidence(
            id=evidence_id,
            summary=_clip(summary, _SUMMARY_CHARS) or (existing.summary if existing else ""),
            source=_clip(source, _SOURCE_CHARS) or (existing.source if existing else ""),
        )
        if existing != record:
            self._evidence[evidence_id] = record
            self.revision += 1
        return self._evidence[evidence_id]

    def declare_hypothesis(
        self,
        hypothesis_id,
        statement="",
        status: str = ACTIVE,
        evidence_id: Optional[str] = None,
        note: str = "",
    ) -> Hypothesis:
        hypothesis_id = str(hypothesis_id or "").strip()
        if not hypothesis_id:
            raise LedgerRefusal("가설 id가 비어 있어 등록을 거부했습니다.")
        if status not in TRANSITION_STATUSES:
            raise LedgerRefusal(
                "가설 상태 '{0}'는 허용되지 않습니다. 허용: {1} 또는 reopen.".format(
                    status, ", ".join(TRANSITION_STATUSES)
                )
            )

        existing = self._hypotheses.get(hypothesis_id)
        if existing is None:
            self._require_evidence(evidence_id, optional=True)
            hypothesis = Hypothesis(
                id=hypothesis_id,
                statement=_clip(statement, _STATEMENT_CHARS),
                status=status,
                revision=1,
                transitions=[
                    Transition(1, status, str(evidence_id or ""), _clip(note, _NOTE_CHARS))
                ],
            )
            self._hypotheses[hypothesis_id] = hypothesis
            self.revision += 1
            return hypothesis

        statement = _clip(statement, _STATEMENT_CHARS)
        if statement and statement != existing.statement:
            existing.statement = statement
            self.revision += 1
        if status != existing.status:
            self._transition(existing, status, evidence_id, note)
        return existing

    def reopen_hypothesis(self, hypothesis_id, evidence_id, note: str = "") -> Hypothesis:
        return self._transition(
            self._require_hypothesis(hypothesis_id), ACTIVE, evidence_id, note, reopen=True
        )

    def add_conclusion(self, conclusion_id, statement="", premises: Iterable = ()) -> Conclusion:
        conclusion_id = str(conclusion_id or "").strip()
        if not conclusion_id:
            raise LedgerRefusal("결론 id가 비어 있어 등록을 거부했습니다.")

        pinned: Dict[str, int] = {}
        for premise_id in premises or ():
            premise_id = str(premise_id or "").strip()
            if not premise_id:
                continue
            hypothesis = self._hypotheses.get(premise_id)
            # An unknown premise pins to revision 0, which no hypothesis can ever
            # hold, so the conclusion stays invalid until it is declared.
            pinned[premise_id] = hypothesis.revision if hypothesis else 0

        conclusion = Conclusion(
            id=conclusion_id, statement=_clip(statement, _STATEMENT_CHARS), premises=pinned
        )
        if self._conclusions.get(conclusion_id) != conclusion:
            self._conclusions[conclusion_id] = conclusion
            self.revision += 1
        return conclusion

    def _transition(
        self,
        hypothesis: Hypothesis,
        status: str,
        evidence_id,
        note: str,
        reopen: bool = False,
    ) -> Hypothesis:
        # Status is already validated by declare_hypothesis, and reopen_hypothesis
        # passes the ACTIVE constant, so the only check left here is the invariant
        # this method exists to hold.
        if hypothesis.status == REJECTED and status == ACTIVE and not reopen:
            raise LedgerRefusal(
                "{0}는 이미 반증되었습니다. 다시 active로 만들려면 이전에 인용하지 않은 "
                "새 증거를 등록하고 status=\"reopen\"으로 요청하세요.".format(hypothesis.marker)
            )

        evidence_id = str(evidence_id or "").strip()
        self._require_evidence(evidence_id, optional=not reopen and status == ACTIVE)
        if reopen:
            if evidence_id in self.cited_evidence(hypothesis.id):
                raise LedgerRefusal(
                    "{0} reopen을 거부했습니다: 증거 {1}는 이미 이 가설의 전이에서 인용되었습니다. "
                    "새 증거를 제시하세요.".format(hypothesis.marker, evidence_id)
                )

        hypothesis.status = status
        hypothesis.revision += 1
        hypothesis.transitions.append(
            Transition(hypothesis.revision, status, evidence_id, _clip(note, _NOTE_CHARS))
        )
        self.revision += 1
        return hypothesis

    def _require_hypothesis(self, hypothesis_id) -> Hypothesis:
        hypothesis = self._hypotheses.get(str(hypothesis_id or "").strip())
        if hypothesis is None:
            raise LedgerRefusal(
                "가설 {0}가 등록되어 있지 않습니다. 먼저 hypotheses에 선언하세요.".format(hypothesis_id)
            )
        return hypothesis

    def _require_evidence(self, evidence_id, optional: bool = False) -> None:
        evidence_id = str(evidence_id or "").strip()
        if not evidence_id:
            if optional:
                return
            raise LedgerRefusal("상태 전이는 반드시 근거 증거 id를 인용해야 합니다.")
        if evidence_id not in self._evidence:
            raise LedgerRefusal(
                "증거 {0}가 등록되어 있지 않습니다. evidence에 먼저 요약과 출처를 등록하세요.".format(
                    evidence_id
                )
            )

    # --- derived reads ---

    def hypothesis_marker(self, hypothesis_id) -> str:
        return self._require_hypothesis(hypothesis_id).marker

    def cited_evidence(self, hypothesis_id) -> List[str]:
        hypothesis = self._require_hypothesis(hypothesis_id)
        return [t.evidence_id for t in hypothesis.transitions if t.evidence_id]

    def stale_premises(self, conclusion_id) -> List[str]:
        conclusion = self._conclusions.get(str(conclusion_id or "").strip())
        if conclusion is None:
            raise LedgerRefusal("결론 {0}가 등록되어 있지 않습니다.".format(conclusion_id))
        stale = []
        for premise_id, pinned_revision in conclusion.premises.items():
            hypothesis = self._hypotheses.get(premise_id)
            if hypothesis is None:
                stale.append(premise_id)
            elif hypothesis.revision != pinned_revision or hypothesis.status == REJECTED:
                stale.append(premise_id)
        return stale

    def conclusion_is_valid(self, conclusion_id) -> bool:
        return not self.stale_premises(conclusion_id)

    def conclusion_marker(self, conclusion_id) -> str:
        label = VALID_LABEL if self.conclusion_is_valid(conclusion_id) else INVALID_LABEL
        return "{0}={1}".format(conclusion_id, label)

    def state_markers(self) -> List[str]:
        """Every token a downstream summary must still contain."""
        markers = [h.marker for h in self._hypotheses.values()]
        markers.extend(self.conclusion_marker(c_id) for c_id in self._conclusions)
        markers.extend(self._evidence)
        return markers

    def is_empty(self) -> bool:
        return not (self.goal or self._evidence or self._hypotheses or self._conclusions)

    def clear(self) -> None:
        self.__init__()

    # --- rendering ---

    def render(self) -> str:
        """Deterministic state block injected into every payload and report.

        ponytail: length grows linearly with entry count (~300 chars each). The
        agent is instructed to keep updates few and short; if a run ever needs
        hundreds of hypotheses, page or archive resolved ones instead of
        clipping this block, which must never lose a marker.
        """
        if self.is_empty():
            return ""

        lines = ["[{0} v{1}]".format(STATE_BLOCK_TITLE, self.revision)]
        if self.goal:
            lines.append("목표: {0}".format(self.goal))

        if self._hypotheses:
            lines.append("가설:")
            for hypothesis in self._hypotheses.values():
                trail = " / ".join(
                    "v{0} {1}{2}".format(
                        t.revision, t.status, "←" + t.evidence_id if t.evidence_id else ""
                    )
                    for t in hypothesis.transitions
                )
                lines.append(
                    "- {0} :: {1} (전이: {2})".format(
                        hypothesis.marker, hypothesis.statement or "(진술 없음)", trail
                    )
                )

        if self._evidence:
            lines.append("증거:")
            for evidence in self._evidence.values():
                source = " (출처: {0})".format(evidence.source) if evidence.source else ""
                lines.append(
                    "- {0} :: {1}{2}".format(evidence.id, evidence.summary or "(요약 없음)", source)
                )

        if self._conclusions:
            lines.append("결론:")
            for conclusion in self._conclusions.values():
                premises = ", ".join(
                    "{0}@v{1}".format(premise_id, revision)
                    for premise_id, revision in conclusion.premises.items()
                )
                line = "- {0} :: {1}".format(
                    self.conclusion_marker(conclusion.id),
                    conclusion.statement or "(진술 없음)",
                )
                if premises:
                    line += " | 전제: {0}".format(premises)
                stale = self.stale_premises(conclusion.id)
                if stale:
                    line += " | {0} 사유: 전제 {1}가 교체·반증됨".format(
                        INVALID_LABEL, ", ".join(stale)
                    )
                lines.append(line)

        lines.append(STATE_RULES)
        return "\n".join(lines)

    # --- batch entry point used by the record_state tool and checkpoints ---

    def apply_updates(self, payload) -> str:
        """Apply a structured update batch and return its human-readable report."""
        report, _had_refusal = self.apply_updates_with_status(payload)
        return report

    def apply_updates_with_status(self, payload) -> Tuple[str, bool]:
        """Apply updates and return the report plus producer-owned refusal status."""
        if not isinstance(payload, dict):
            return "상태 갱신을 거부했습니다: 객체 형식이 아닙니다.", True

        applied: List[str] = []
        refused: List[str] = []

        goal = payload.get("goal")
        if goal:
            self.set_goal(goal)
            applied.append("목표 갱신")

        for item in payload.get("evidence") or []:
            if not isinstance(item, dict):
                refused.append("증거 항목 형식 오류: {0!r}".format(item))
                continue
            try:
                evidence = self.add_evidence(
                    item.get("id"), item.get("summary", ""), item.get("source", "")
                )
            except LedgerRefusal as refusal:
                refused.append(str(refusal))
            else:
                applied.append(evidence.id)

        for item in payload.get("hypotheses") or []:
            if not isinstance(item, dict):
                refused.append("가설 항목 형식 오류: {0!r}".format(item))
                continue
            status = str(item.get("status") or ACTIVE).strip().lower()
            try:
                if status == REOPEN:
                    hypothesis = self.reopen_hypothesis(
                        item.get("id"), item.get("evidence_id"), item.get("note", "")
                    )
                else:
                    # Every non-reopen status goes through declare_hypothesis, which
                    # applies a corrected statement *and* the transition. The separate
                    # rejected/confirmed branches that used to sit here called
                    # _transition directly and never passed `statement`, so a model
                    # that corrected a hypothesis while refuting it silently lost the
                    # correction - the very class of loss this ledger exists to stop.
                    hypothesis = self.declare_hypothesis(
                        item.get("id"),
                        item.get("statement", ""),
                        status=status,
                        evidence_id=item.get("evidence_id"),
                        note=item.get("note", ""),
                    )
            except LedgerRefusal as refusal:
                refused.append(str(refusal))
            else:
                applied.append(hypothesis.marker)

        for item in payload.get("conclusions") or []:
            if not isinstance(item, dict):
                refused.append("결론 항목 형식 오류: {0!r}".format(item))
                continue
            try:
                conclusion = self.add_conclusion(
                    item.get("id"), item.get("statement", ""), item.get("premises") or ()
                )
            except LedgerRefusal as refusal:
                refused.append(str(refusal))
            else:
                applied.append(self.conclusion_marker(conclusion.id))

        report = []
        if applied:
            report.append("반영: " + ", ".join(applied))
        if refused:
            report.append("거부:\n- " + "\n- ".join(refused))
        if not report:
            report.append("반영할 상태 갱신이 없습니다.")
        report.append(self.render() or "(상태 비어 있음)")
        return "\n\n".join(report), bool(refused)
