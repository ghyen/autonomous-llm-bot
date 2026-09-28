"""Record-time grounding warnings for new evidence.

A new evidence claim is checked against recent trajectory observations of the
same target. Only machine-readable contradictions produce a warning, and a
warning never blocks: the record still succeeds. False positives cost one
warning line; false negatives are the status quo.

The check is deliberately narrow (v1): a bare positive phone-pattern claim
about a single target while recent observations of it are all clean-negative.
PII exposure verdicts ("phone FOUND on this page") must be right, and the one
observed live failure was exactly this shape: steps 23-24 measured 404 + 0
patterns, step 28 recorded "010-3183-3933 발견".
"""

import re

import observation

GROUNDING_WINDOW_STEPS = 10
GROUNDING_MIN_OBSERVATIONS = 2

_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+82[\s.-]?0?10|\(010\)|010|011|016|017|018|019)[\s.-]?\d{3,4}[\s.-]?\d{4}"
)
_NEGATION_RE = re.compile(
    r"0건|없음|없습|없다|없었|없는|없어|없고|미발견|미검출|못\s*찾|못찾"
    r"|아님|아닙니다|실패|발견되지 않음|not found",
    re.IGNORECASE,
)
# 결과 본문의 부재 신호. 단독 ": 0"은 오탐이 많아("age: 0" 등) 세지 않는다.
# 개수 맥락(count/patterns/HTTP 등)과 붙은 0만 본다. [exit code] 꼬리는
# 호출 전에 뗀다.
_NEG_RESULT_RE = re.compile(
    r"0건|없음|없습|없다|없었|없는|없어|없고|미발견|미검출|못\s*찾|못찾"
    r"|아님|아닙니다|실패|not found|page not found"
    r"|0 found|no match|no matches|\bempty\b"
    r"|(?:count|matches|matched|found|total|patterns|results|http|결과|건수|개수)\s*:\s*0\b"
    r"|=\s*0\b",
    re.IGNORECASE,
)
_EXIT_TRAILER_RE = re.compile(r"\[exit code: -?\d+\]\s*\Z")


def _has_positive_phone_claim(summary: str) -> bool:
    """A bare phone-pattern assertion, not a negated one."""
    text = str(summary or "")
    if not _PHONE_RE.search(text):
        return False
    # "010 패턴 0건"은 부재 보고이지 발견 주장이 아니다.
    return not _NEGATION_RE.search(text)


def _observation_is_clean_negative(record) -> bool:
    """True when the observation unambiguously found nothing."""
    result = str(record.get("result") or "")
    outcome = observation.classify_observation_outcome(
        result, bool(record.get("failed"))
    )
    if outcome is None:
        return False
    if outcome in observation.DEAD_FAMILY_OUTCOMES:
        return True
    # [exit code: 0] 꼬리의 ": 0"이 카운트 패턴에 걸리지 않게 먼저 뗀다.
    body = _EXIT_TRAILER_RE.sub("", result)
    if outcome in ("exit0", "other") and _NEG_RESULT_RE.search(body):
        return True
    return False


def may_need_check(item) -> bool:
    """Cheap pre-filter: phone claim about exactly one target.

    Lets callers skip the trajectory read entirely when no new item can
    possibly trigger a warning.
    """
    if not isinstance(item, dict):
        return False
    summary = str(item.get("summary") or "")
    if not _has_positive_phone_claim(summary):
        return False
    text = "{0} {1}".format(item.get("source", ""), summary)
    return len(observation.extract_observation_targets(text)) == 1


def check_evidence_grounding(records, new_evidence, current_step=None) -> list:
    """Warn for new phone claims contradicting recent measurements.

    `new_evidence` holds {"id", "summary", "source"} dicts that the ledger
    actually accepted as new. Only single-target items are checked: with
    several targets the claim cannot be attributed to one measurement.
    Returns warning strings (possibly empty). Never raises.
    """
    warnings = []
    try:
        items = [
            item for item in (new_evidence or [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
        if not items:
            return warnings
        steps = [
            record.get("step") for record in (records or [])
            if isinstance(record.get("step"), int)
        ]
        if not steps:
            return warnings
        if isinstance(current_step, int) and not isinstance(current_step, bool):
            anchor = current_step
        else:
            anchor = max(steps)
        floor = anchor - GROUNDING_WINDOW_STEPS
        recent = [
            record for record in records
            if isinstance(record.get("step"), int)
            and floor < record["step"] <= anchor
            and record.get("tool") == "bash_exec"
            and record.get("executed")
        ]
        for item in items:
            if len(warnings) >= 3:
                break
            summary = str(item.get("summary") or "")
            if not _has_positive_phone_claim(summary):
                continue
            text = "{0} {1}".format(item.get("source", ""), summary)
            targets = observation.extract_observation_targets(text)
            if len(targets) != 1:
                continue
            target = targets[0]
            observations = [
                record for record in recent
                if target
                in observation.extract_observation_targets(
                    str((record.get("arguments") or {}).get("command", ""))
                )
            ]
            if (
                len(observations) >= GROUNDING_MIN_OBSERVATIONS
                and all(
                    _observation_is_clean_negative(record)
                    for record in observations
                )
            ):
                steps = sorted({
                    record["step"] for record in observations
                    if isinstance(record.get("step"), int)
                })
                warnings.append(
                    "[grounding 주의]: 새 증거 {0}의 주장(전화번호 패턴)이 "
                    "직전 측정과 모순될 수 있습니다. {1}의 최근 관측"
                    "(step {2}): 전화번호 0건·데드엔드. 측정 원문을 "
                    "lookup_trajectory로 확인하고, 주장이 틀렸으면 해당 "
                    "증거를 retracts로 철회하세요.".format(
                        item.get("id"),
                        target[:80],
                        ",".join(str(step) for step in steps[:6]),
                    )
                )
    except Exception:
        return warnings
    return warnings
