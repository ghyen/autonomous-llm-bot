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

from dataclasses import asdict, dataclass, field
import difflib
import re
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
    "철회된 증거는 새 전이의 근거로 쓸 수 없습니다. "
    "이 블록은 권위 있는 상태이며, 요약이나 보고서가 이와 다르면 이 블록이 옳습니다."
)
DEFAULT_MAX_RENDERED_EVIDENCE = 6

_STATEMENT_CHARS = 220
_SUMMARY_CHARS = 220
_SOURCE_CHARS = 160
_NOTE_CHARS = 120
# 상태 블록은 매 요청마다 다시 들어가는 고정 비용이다. 저장은 길게 하되
# 렌더는 짧게 하고, 상세는 원장과 findings.md에 남긴다.
_RENDERED_SUMMARY_CHARS = 100
_RENDERED_STATEMENT_CHARS = 96
# 반증·확정된 가설은 판단 대상이 아니라 이력이다. 마커와 짧은 진술만 남긴다.
_RENDERED_STATEMENT_CHARS_INACTIVE = 64
_RENDERED_NOTE_CHARS = 60


class LedgerRefusal(Exception):
    """Raised when an update would violate the state transition rules."""


@dataclass
class LedgerDelta:
    """Classified changes produced by a batch update."""
    new_evidence: List[str] = field(default_factory=list)
    duplicate_evidence: List[str] = field(default_factory=list)
    retracted_evidence: List[str] = field(default_factory=list)
    hypotheses_changed: List[str] = field(default_factory=list)
    conclusions_changed: List[str] = field(default_factory=list)
    goal_changed: bool = False

    @property
    def substantive(self) -> bool:
        """True if the update introduced meaningful progress, refutation, or goal shift."""
        return bool(
            self.new_evidence
            or self.retracted_evidence
            or self.hypotheses_changed
            or self.conclusions_changed
            or self.goal_changed
        )


class ApplyStatusResult(tuple):
    """Result of apply_updates_with_status.

    Unpacks as (report, had_refusal) for 100% backward compatibility,
    while exposing .delta as an attribute.
    """
    report: str
    had_refusal: bool
    delta: LedgerDelta

    def __new__(cls, report: str, had_refusal: bool, delta: Optional[LedgerDelta] = None):
        return super().__new__(cls, (report, had_refusal))

    def __init__(self, report: str, had_refusal: bool, delta: Optional[LedgerDelta] = None):
        self.report = report
        self.had_refusal = had_refusal
        self.delta = delta if delta is not None else LedgerDelta()


def _normalize_text(text: str) -> str:
    s = str(text or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_id(eid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", str(eid or "")).lower()


def _is_near_duplicate_text(left, right, threshold=0.85) -> bool:
    """정규화한 두 문장이 사실상 같은 내용인지 본다.

    같은 사실을 어순만 바꾸거나 표현을 다듬어 다시 등록하는 기록을 중복으로
    잡기 위한 판정이다. 한쪽이 비어 있으면 중복으로 보지 않는다.
    """
    norm_left = _normalize_text(left)
    norm_right = _normalize_text(right)
    if not norm_left or not norm_right:
        return False
    if norm_left == norm_right:
        return True
    return difflib.SequenceMatcher(None, norm_left, norm_right).ratio() >= threshold


def _token_containment(left, right) -> float:
    """짧은 쪽 어휘가 긴 쪽 어휘에 얼마나 포함되는지 본다.

    요약이 짧으면 문자열 유사도가 낮게 나와 같은 사실을 놓친다. 어휘 집합으로
    보면 "컨텍스트 플러시 후 재개"처럼 긴 설명의 일부만 다시 쓴 경우를 잡는다.
    """
    left_tokens = set(_normalize_text(left).split())
    right_tokens = set(_normalize_text(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / float(min(len(left_tokens), len(right_tokens)))


_ID_PREFIX_CHARS = 8
# 라이브 런(2e2190c5) 원장 105건으로 실측한 값이다. 0.5에서 중복으로 묶이는
# 쌍은 3건이고 전부 같은 사실을 다른 id로 다시 등록한 기록이었다.
_ID_TOKEN_CONTAINMENT = 0.5
# 같은 계획을 재배열하거나 항목만 덧붙인 goal 변경을 걸러내는 기준이다.
_GOAL_CONTAINMENT = 0.8


def _is_duplicate_evidence(
    new_id: str, new_summary: str, existing_id: str, existing_summary: str
) -> bool:
    """같은 사실을 다른 이름으로 다시 등록한 기록인지 판정한다."""
    if _is_near_duplicate_text(new_summary, existing_summary):
        return True
    norm_new_id = _normalize_id(new_id)
    norm_ext_id = _normalize_id(existing_id)
    if not norm_new_id or not norm_ext_id:
        return False
    if norm_new_id == norm_ext_id:
        return True
    if norm_new_id in norm_ext_id or norm_ext_id in norm_new_id:
        return _is_near_duplicate_text(new_summary, existing_summary, threshold=0.70)
    # 같은 주제를 다른 이름으로 반복 등록하는 경우(E_CONTEXT_FLUSH /
    # E_CONTEXT_RESTART)는 id 앞머리가 같다. 앞머리가 같고 어휘마저
    # 겹치면 같은 사실의 재등록으로 본다.
    if (
        len(norm_new_id) >= _ID_PREFIX_CHARS
        and len(norm_ext_id) >= _ID_PREFIX_CHARS
        and norm_new_id[:_ID_PREFIX_CHARS] == norm_ext_id[:_ID_PREFIX_CHARS]
    ):
        return _token_containment(new_summary, existing_summary) >= _ID_TOKEN_CONTAINMENT
    return False


def _required_list(payload, key):
    """A missing or non-list section is a shape mismatch, not an empty one."""
    value = payload[key]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypeError("{0} is not a list of objects".format(key))
    return value


def _render_clip(text, limit: int) -> str:
    """상태 블록에 들어갈 자유 서술을 렌더 상한까지 줄인다."""
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def _evidence_line(evidence: "Evidence") -> str:
    """One rendered evidence item.

    A retracted item stays visible but cannot be mistaken for a fact: the
    marker and the reason travel with it into every payload.
    """
    source = " (출처: {0})".format(_render_clip(evidence.source, _RENDERED_NOTE_CHARS)) if evidence.source else ""
    summary = _render_clip(evidence.summary, _RENDERED_SUMMARY_CHARS) or "(요약 없음)"
    if not evidence.retracted:
        return "- {0} :: {1}{2}".format(evidence.id, summary, source)
    reason = (
        " 사유: {0}".format(_render_clip(evidence.note, _RENDERED_NOTE_CHARS))
        if evidence.note
        else ""
    )
    return "- {0} [철회된 증거 - 사실로 인용 금지]{1} :: {2}{3}".format(
        evidence.id, reason, summary, source
    )


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
    # 반증된 증거는 지우지 않고 철회로 표시한다. 지우면 그 증거를 인용한
    # 전이가 남은 채 근거만 사라져 이력이 거짓말을 하고, 남겨두면 다음 스텝이
    # 여전히 사실로 인용한다. 철회만이 둘 다 막는다.
    retracted: bool = False
    note: str = ""


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
        self.last_delta: Optional[LedgerDelta] = None

    # --- mutation ---

    def set_goal(self, goal) -> None:
        goal = _clip(goal, 600)
        if goal and goal != self.goal:
            self.goal = goal
            self.revision += 1

    def add_evidence(
        self,
        evidence_id,
        summary="",
        source="",
        retracted=None,
        note="",
    ) -> Evidence:
        """Register or correct one evidence item.

        ``retracted`` is tri-state on purpose: ``None`` preserves the current
        flag so an ordinary re-registration cannot silently un-retract a
        disproven finding, ``True`` retracts it, and an explicit ``False`` is the
        deliberate act of putting it back. Retraction keeps summary and source,
        because the record of what was believed is as useful as the correction.
        """
        evidence_id = str(evidence_id or "").strip()
        if not evidence_id:
            raise LedgerRefusal("증거 id가 비어 있어 등록을 거부했습니다.")
        existing = self._evidence.get(evidence_id)
        record = Evidence(
            id=evidence_id,
            summary=_clip(summary, _SUMMARY_CHARS) or (existing.summary if existing else ""),
            source=_clip(source, _SOURCE_CHARS) or (existing.source if existing else ""),
            retracted=(
                existing.retracted if retracted is None and existing else bool(retracted)
            ),
            note=_clip(note, _NOTE_CHARS) or (existing.note if existing else ""),
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
        evidence = self._evidence.get(evidence_id)
        if evidence is None:
            raise LedgerRefusal(
                "증거 {0}가 등록되어 있지 않습니다. evidence에 먼저 요약과 출처를 등록하세요.".format(
                    evidence_id
                )
            )
        if evidence.retracted:
            raise LedgerRefusal(
                "증거 {0}는 철회되었습니다. 새 전이의 근거로 쓸 수 없습니다. "
                "다시 쓰려면 evidence에 retracted=false로 명시해 되살릴 수 있는지 먼저 판단하세요.".format(
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

    # --- durable state (issue #6) ---

    def to_dict(self) -> dict:
        """Serializable form of the whole ledger.

        Conclusion validity is *derived*, so nothing about it is written: the
        pinned premise revisions plus the hypotheses' own revisions reproduce it
        exactly, and no restored ledger can claim a validity it cannot derive.
        """
        return {
            "goal": self.goal,
            "revision": self.revision,
            "evidence": [asdict(item) for item in self._evidence.values()],
            "hypotheses": [asdict(item) for item in self._hypotheses.values()],
            "conclusions": [asdict(item) for item in self._conclusions.values()],
        }

    @classmethod
    def from_dict(cls, payload) -> "ResearchLedger":
        """Rebuild from `to_dict` output.

        A payload that does not match raises. The ledger decides which
        hypotheses are still true, so a half-read one is discarded by the
        caller, never migrated into a shape this code did not write.
        """
        if not isinstance(payload, dict):
            raise ValueError("원장 스냅샷이 객체가 아닙니다.")
        revision = payload.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("원장 리비전이 0 이상의 정수가 아닙니다.")

        ledger = cls()
        ledger.goal = str(payload.get("goal") or "")
        ledger.revision = revision
        try:
            for item in _required_list(payload, "evidence"):
                evidence = Evidence(**item)
                ledger._evidence[evidence.id] = evidence
            for item in _required_list(payload, "hypotheses"):
                item = dict(item)
                transitions = item.pop("transitions", [])
                if not isinstance(transitions, list):
                    raise TypeError("transitions is not a list")
                hypothesis = Hypothesis(
                    transitions=[Transition(**entry) for entry in transitions], **item
                )
                ledger._hypotheses[hypothesis.id] = hypothesis
            for item in _required_list(payload, "conclusions"):
                item = dict(item)
                premises = item.pop("premises", {})
                if not isinstance(premises, dict):
                    raise TypeError("premises is not an object")
                conclusion = Conclusion(
                    premises={str(key): int(value) for key, value in premises.items()},
                    **item,
                )
                ledger._conclusions[conclusion.id] = conclusion
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise ValueError("원장 스냅샷 형식이 맞지 않습니다: {0}".format(error))
        return ledger

    # --- rendering ---

    def render(self, max_evidence: Optional[int] = None) -> str:
        """Deterministic state block injected into every payload and report.

        ponytail: length grows linearly with entry count (~300 chars each). When
        evidence items accumulate over long runs, older items are summarized
        while retaining cited evidence and the most recent items.
        """
        if self.is_empty():
            return ""

        lines = ["[{0} v{1}]".format(STATE_BLOCK_TITLE, self.revision)]
        if self.goal:
            lines.append("목표: {0}".format(self.goal))

        if self._hypotheses:
            lines.append("가설:")
            # 전이 이력 전체는 길어지기만 한다. 반증·확정 이유를 담은 마지막
            # 전이만 남긴다. 문장은 유지하되 렌더 상한까지 줄인다.
            for hypothesis in self._hypotheses.values():
                latest = hypothesis.transitions[-1] if hypothesis.transitions else None
                trail = (
                    "v{0} {1}{2}".format(
                        latest.revision,
                        latest.status,
                        "←" + latest.evidence_id if latest.evidence_id else "",
                    )
                    if latest is not None
                    else "전이 없음"
                )
                statement = _render_clip(
                    hypothesis.statement,
                    (
                        _RENDERED_STATEMENT_CHARS
                        if hypothesis.status == ACTIVE
                        else _RENDERED_STATEMENT_CHARS_INACTIVE
                    ),
                )
                lines.append(
                    "- {0} :: {1} (전이: {2})".format(
                        hypothesis.marker, statement or "(진술 없음)", trail
                    )
                )

        if self._evidence:
            lines.append("증거:")
            all_evidence = list(self._evidence.values())
            if max_evidence is not None and len(all_evidence) > max_evidence:
                cited = set()
                for h in self._hypotheses.values():
                    for t in h.transitions:
                        if t.evidence_id:
                            cited.add(t.evidence_id)
                for c in self._conclusions.values():
                    for p in c.premises:
                        cited.add(p)

                # 지금 목표와 겹치는 증거를 먼저 남기고, 그다음 최근 순으로
                # 채운다. 인용된 증거는 항상 남긴다. 목표가 바뀌면 관련 없는
                # 옛 증거는 자동으로 빠지고 개수만 남는다.
                goal_tokens = set(_normalize_text(self.goal).split())
                ranked = []
                for index, evidence in enumerate(all_evidence):
                    tokens = set(
                        _normalize_text(
                            "{0} {1} {2}".format(
                                evidence.id, evidence.summary, evidence.source
                            )
                        ).split()
                    )
                    overlap = len(goal_tokens & tokens) if goal_tokens else 0
                    # 하네스가 자동 승격한 도구 출력은 같은 관련도면 뒤로 둔다.
                    harness = 0 if str(evidence.id).startswith("TRAJ_") else 1
                    ranked.append((overlap, harness, index, evidence))
                ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)

                selected = [e for e in all_evidence if e.id in cited]
                selected_ids = {e.id for e in selected}
                picked = 0
                for _overlap, _harness, _index, evidence in ranked:
                    if picked >= max_evidence:
                        break
                    if evidence.id in selected_ids:
                        continue
                    selected.append(evidence)
                    selected_ids.add(evidence.id)
                    picked += 1

                omitted_count = len(all_evidence) - len(selected_ids)
                if omitted_count > 0:
                    lines.append(
                        "- ... (이전 증거 {0}건 생략: 목표 무관·오래된 항목. 원문은 findings.md와 원장에 보존됨)".format(
                            omitted_count
                        )
                    )
                order = {evidence.id: index for index, evidence in enumerate(all_evidence)}
                for evidence in sorted(selected, key=lambda e: order[e.id]):
                    lines.append(_evidence_line(evidence))
            else:
                for evidence in all_evidence:
                    lines.append(_evidence_line(evidence))

        if self._conclusions:
            lines.append("결론:")
            for conclusion in self._conclusions.values():
                premises = ", ".join(
                    "{0}@v{1}".format(premise_id, revision)
                    for premise_id, revision in conclusion.premises.items()
                )
                line = "- {0} :: {1}".format(
                    self.conclusion_marker(conclusion.id),
                    _render_clip(conclusion.statement, _RENDERED_STATEMENT_CHARS)
                    or "(진술 없음)",
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
        report, _had_refusal = self.apply_updates_with_status(payload, include_render=True)
        return report

    def apply_updates_with_status(
        self, payload, include_render: bool = True
    ) -> Tuple[str, bool]:
        """Apply updates and return the report plus producer-owned refusal status and delta."""
        delta = LedgerDelta()
        if not isinstance(payload, dict):
            return ApplyStatusResult("상태 갱신을 거부했습니다: 객체 형식이 아닙니다.", True, delta)

        applied: List[str] = []
        refused: List[str] = []

        goal = payload.get("goal")
        if goal:
            old_goal = self.goal
            self.set_goal(goal)
            if self.goal != old_goal:
                # 같은 목표를 어순만 바꾸거나 항목을 덧붙여 다시 쓰는 것은
                # 실질 갱신이 아니다. 그대로 인정하면 문구만 손봐서 게이트를
                # 계속 열 수 있다.
                delta.goal_changed = not (
                    _is_near_duplicate_text(old_goal, self.goal)
                    or _token_containment(old_goal, self.goal) >= _GOAL_CONTAINMENT
                )
                applied.append("목표 갱신: " + self.goal)

        for item in payload.get("evidence") or []:
            if not isinstance(item, dict):
                refused.append("증거 항목 형식 오류: {0!r}".format(item))
                continue
            eid = str(item.get("id") or "").strip()
            summary = item.get("summary", "")
            is_retract = bool(item.get("retracted"))
            existing = self._evidence.get(eid)
            is_existing_identical = (
                existing is not None
                and existing.summary == summary
                and existing.source == item.get("source", "")
                and bool(existing.retracted) == is_retract
                and existing.note == item.get("note", "")
            )

            is_dup = False
            if not is_retract and not (existing and existing.retracted):
                for ext_id, ext_record in self._evidence.items():
                    if ext_record.retracted or ext_id == eid:
                        continue
                    if _is_duplicate_evidence(
                        eid, summary, ext_id, ext_record.summary
                    ):
                        is_dup = True
                        break

            try:
                evidence = self.add_evidence(
                    item.get("id"),
                    item.get("summary", ""),
                    item.get("source", ""),
                    retracted=item.get("retracted"),
                    note=item.get("note", ""),
                )
            except LedgerRefusal as refusal:
                refused.append(str(refusal))
            else:
                if evidence.retracted:
                    delta.retracted_evidence.append(evidence.id)
                    applied.append(
                        "{0}(철회)".format(evidence.id)
                    )
                elif is_dup:
                    delta.duplicate_evidence.append(evidence.id)
                    applied.append("{0}(중복)".format(evidence.id))
                elif is_existing_identical:
                    applied.append(evidence.id)
                else:
                    delta.new_evidence.append(evidence.id)
                    applied.append(evidence.id)

        for item in payload.get("hypotheses") or []:
            if not isinstance(item, dict):
                refused.append("가설 항목 형식 오류: {0!r}".format(item))
                continue
            hid = str(item.get("id") or "").strip()
            old_hypo = self._hypotheses.get(hid)
            old_status = old_hypo.status if old_hypo else None
            old_statement = old_hypo.statement if old_hypo else None
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
                # 가설 문장을 다듬은 것만으로는 실질 갱신이 아니다. 상태 전이,
                # 새 가설, 내용이 실제로 달라진 재정의만 인정한다.
                statement_changed = old_statement != hypothesis.statement
                if (
                    not old_hypo
                    or old_status != hypothesis.status
                    or (
                        statement_changed
                        and not _is_near_duplicate_text(
                            old_statement, hypothesis.statement
                        )
                    )
                ):
                    delta.hypotheses_changed.append(hypothesis.id)
                applied.append(hypothesis.marker)

        for item in payload.get("conclusions") or []:
            if not isinstance(item, dict):
                refused.append("결론 항목 형식 오류: {0!r}".format(item))
                continue
            cid = str(item.get("id") or "").strip()
            old_conc = self._conclusions.get(cid)
            old_statement = old_conc.statement if old_conc else None
            old_premises = old_conc.premises if old_conc else None
            try:
                conclusion = self.add_conclusion(
                    item.get("id"), item.get("statement", ""), item.get("premises") or ()
                )
            except LedgerRefusal as refusal:
                refused.append(str(refusal))
            else:
                # 결론 문장 다듬기도 실질 갱신이 아니다. 전제 교체는 근거가
                # 바뀐 것이므로 그대로 인정한다.
                statement_changed = old_statement != conclusion.statement
                if (
                    not old_conc
                    or old_premises != conclusion.premises
                    or (
                        statement_changed
                        and not _is_near_duplicate_text(
                            old_statement, conclusion.statement
                        )
                    )
                ):
                    delta.conclusions_changed.append(conclusion.id)
                applied.append(self.conclusion_marker(conclusion.id))

        report = []
        if applied:
            report.append("반영: " + ", ".join(applied))
        if refused:
            report.append("거부:\n- " + "\n- ".join(refused))
        if delta.duplicate_evidence and not delta.substantive:
            report.append(
                "[중복 증거 감지]: 새로 관측된 실질적 사실이 없어 게이트가 유지됩니다. "
                "새로운 명령 결과나 외부 사실을 기록하세요."
            )
        if not report:
            report.append("반영할 상태 갱신이 없습니다.")
        if include_render:
            report.append(self.render() or "(상태 비어 있음)")
        self.last_delta = delta
        return ApplyStatusResult("\n\n".join(report), bool(refused), delta)
