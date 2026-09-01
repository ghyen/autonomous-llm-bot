"""Per-run steering mailbox: bounded, FIFO, and honest about what happened.

One mailbox belongs to exactly one run, so a run's cleanup can only ever settle
its own items. Every accepted item ends in exactly one terminal state - applied,
superseded, rejected, or cancelled - and the state the caller is told is the
state the item actually reached.

The mailbox is only open while the run has a next step to apply an item to. A
direct-answer run never opens one, and the terminal report phase closes it, so
"reflected on the next step" is never promised when there is no next step.

Observability here is content-free by construction: depth, counters, and
timestamps only. Instruction text lives in the items and is never returned by
`stats()` or `observability_line()`.
"""

import dataclasses
import json
import time

# 종결 상태. 접수된 항목은 정확히 하나로 끝난다.
APPLIED = "applied"
SUPERSEDED = "superseded"
REJECTED = "rejected"
CANCELLED = "cancelled"
TERMINAL_STATES = (APPLIED, SUPERSEDED, REJECTED, CANCELLED)

# 접수 결과. 사용자 안내 문구를 고르는 기준이다.
QUEUED = "queued"
COALESCED = "coalesced"

# 거절 사유. 사용자에게 "왜 반영되지 않았는지"를 말하기 위한 것.
REASON_QUEUE_FULL = "queue_full"
REASON_NO_LOOP = "no_loop"
REASON_TERMINAL = "terminal"


@dataclasses.dataclass
class SteeringItem:
    author: str
    text: str
    received_at: float
    applied_at: float = None
    state: str = QUEUED


@dataclasses.dataclass
class SteeringReceipt:
    """What the caller is told at arrival time, and nothing more."""

    state: str
    depth: int
    received_at: float
    reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.state in (QUEUED, COALESCED)


class SteeringMailbox:
    """FIFO steering queue for one run.

    Bound and dedup are what keep a single channel from inflating every later
    prompt: each pending item is expanded into its own steering block, so an
    unbounded queue multiplies the prompt by its depth.
    """

    def __init__(self, run_id, max_depth=8, clock=time.time):
        self.run_id = run_id
        self.max_depth = max(1, int(max_depth))
        self._clock = clock
        self._pending = []
        self._open = False
        self._closed_reason = REASON_NO_LOOP
        self._counts = {state: 0 for state in TERMINAL_STATES}
        self._received = 0
        self._last_received_at = None
        self._last_applied_at = None

    def __len__(self) -> int:
        return len(self._pending)

    @property
    def depth(self) -> int:
        return len(self._pending)

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        """Open only where a next step is guaranteed to consume the queue."""
        self._open = True

    def offer(self, author, text) -> SteeringReceipt:
        """Take one instruction. The receipt is the truth told to the caller."""
        now = self._clock()
        self._received += 1
        self._last_received_at = now
        if not self._open:
            self._counts[REJECTED] += 1
            return SteeringReceipt(REJECTED, len(self._pending), now, self._closed_reason)
        for pending in self._pending:
            if pending.author == author and pending.text == text:
                # 같은 지시를 다시 넣어도 프롬프트에 두 번 실리지 않는다.
                self._counts[SUPERSEDED] += 1
                return SteeringReceipt(COALESCED, len(self._pending), now)
        if len(self._pending) >= self.max_depth:
            # ponytail: 상한 초과는 최신 항목을 거절한다. 이미 접수를 약속한
            # 항목을 조용히 버리지 않는 대신 새 항목의 반영이 늦어진다.
            # 넘친 지시까지 보존해야 하면 영속 스풀(이슈 #6) 위에서 확장한다.
            self._counts[REJECTED] += 1
            return SteeringReceipt(REJECTED, len(self._pending), now, REASON_QUEUE_FULL)
        self._pending.append(SteeringItem(author=author, text=text, received_at=now))
        return SteeringReceipt(QUEUED, len(self._pending), now)

    def drain(self):
        """Hand over every pending item in arrival order, settled as applied."""
        if not self._pending:
            return []
        now = self._clock()
        taken = self._pending[:]
        self._pending.clear()
        for item in taken:
            item.state = APPLIED
            item.applied_at = now
        self._counts[APPLIED] += len(taken)
        self._last_applied_at = now
        return taken

    def close(self, state=CANCELLED, reason=REASON_TERMINAL):
        """Shut the queue and settle what is left. Idempotent.

        Whatever is still pending will never be applied, so it is settled here
        instead of being dropped - the caller has to be told the real outcome.
        """
        self._open = False
        self._closed_reason = reason
        unapplied = self._pending[:]
        self._pending.clear()
        for item in unapplied:
            item.state = state
        self._counts[state] = self._counts.get(state, 0) + len(unapplied)
        return unapplied

    def stats(self) -> dict:
        """Content-free view: depth, bound, counters, and timestamps."""
        stats = {
            "run_id": self.run_id,
            "depth": len(self._pending),
            "max_depth": self.max_depth,
            "open": self._open,
            "received": self._received,
            "last_received_at": self._last_received_at,
            "last_applied_at": self._last_applied_at,
        }
        stats.update(self._counts)
        return stats

    def observability_line(self) -> str:
        """One log line. Instruction text is never part of it."""
        return json.dumps(
            self.stats(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
