"""Terminal state for a run: one transition decides everything after it.

Whether a run had finished, and whether it had succeeded, used to be inferred
from the *wording* of the last model response - it counted as final if the text
contained 최종/보고서/결론 and ran past 400 characters. So a short, correct
completion or correction was ignored and the loop was forced onward, while a run
that ended by user interrupt or by burning all 250 steps was dressed up with a
completion label and a 완료 시간 footer.

Completion intent is now a structured signal (`finish_task`) and nothing else.
`RunOutcome` records exactly one reason; later attempts are kept for the log but
change nothing. Everything downstream - nudges, tool dispatch, checkpoints,
rollover, the synthesis prompt, the user-facing label - reads that one value.
"""

from typing import List, Optional, Tuple

COMPLETED = "completed"
STOPPED = "stopped"
EXHAUSTED = "exhausted"
FAILED = "failed"

TERMINAL_REASONS = (COMPLETED, STOPPED, EXHAUSTED, FAILED)

# Headers are deliberately blunt. A run the user interrupted, or one that ran out
# of steps, is not a finished investigation and must not read like one.
LABELS = {
    COMPLETED: "✅ 조사 완료",
    STOPPED: "🛑 사용자 중단 — 미완료",
    EXHAUSTED: "⚠️ 스텝 소진 — 미완료",
    FAILED: "❌ 실패 — 미완료",
}

# Detail strings, so log readers can tell apart the ways a run reaches a reason.
DETAIL_FINISH_TASK = "finish_task 호출"
DETAIL_DIRECT_ANSWER = "도구 없이 직접 답변"
DETAIL_USER_STOP = "사용자 중단 요청"
DETAIL_STEP_BUDGET = "스텝 예산 소진"
DETAIL_NO_TOOL_STALL = "도구 호출 없이 연속 응답"


class RunOutcome:
    """Single-assignment terminal state."""

    def __init__(self) -> None:
        self._reason: Optional[str] = None
        self._detail: str = ""
        self.ignored_attempts: List[Tuple[str, str]] = []

    @property
    def settled(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> Optional[str]:
        return self._reason

    @property
    def detail(self) -> str:
        return self._detail

    def settle(self, reason: str, detail: str = "") -> bool:
        """Record the terminal reason. Only the first call takes effect."""
        if reason not in TERMINAL_REASONS:
            raise ValueError("알 수 없는 종료 사유: {0}".format(reason))
        if self._reason is not None:
            self.ignored_attempts.append((reason, detail))
            return False
        self._reason = reason
        self._detail = detail
        return True

    @property
    def is_completed(self) -> bool:
        return self._reason == COMPLETED

    @property
    def label(self) -> str:
        if self._reason is None:
            return "진행 중"
        return LABELS[self._reason]

    def describe(self) -> str:
        if self._reason is None:
            return "진행 중"
        if self._detail:
            return "{0} ({1})".format(self._reason, self._detail)
        return self._reason
