"""Observation-repeat guard for read-only bash observations.

The exact-command loop guard (bot._tool_fingerprint) only fires on
byte-identical repeats. Long runs keep re-observing the same target with
slightly rewritten commands (curl one step, a python one-liner the next,
reordered flags, a trailing slash) and collect the same dead-end answer
dozens of times: in one production run `/en/profile/55142` was fetched ~150
times across 600 steps, each spelling different enough to pass.

This module identifies the *target* of a bash observation and decides whether
that target has already been observed enough times. Everything here is pure
and side-effect free; bot.py wires the decision into tool dispatch.

The rule is deliberately simple: the same normalized target observed `limit`
or more times is stale, whatever the outcomes were. Judging whether an old
answer is "still valid" would require re-fetching it, which is exactly the
waste. Legitimate re-checks use the force=true escape hatch, and bulk sweeps
with at least one fresh target always proceed. read_file already has
known_bad_calls for failures and legitimate re-reads (findings.md after
milestones); this guard covers bash_exec URL targets.
"""

import re
from urllib.parse import urlsplit

_URL_RE = re.compile(r"https?://[^\s'\"`)\]]+")
_HTTP_ERROR_RE = re.compile(
    r"\b(400|401|403|404|405|406|409|410|429|500|502|503)\b"
)
# 성공 상태. 에러와 성공이 섞여 있으면 혼합 배치라 어느 대상의 결과인지
# 특정할 수 없다. CSS 수치(135deg 등)는 여기서 세지 않는다.
_HTTP_SUCCESS_RE = re.compile(r"\b(200|201|204|206|301|302|303|307|308)\b")
_EXIT_CODE_RE = re.compile(r"\[exit code: (-?\d+)\]")

# Too many distinct targets means a bulk sweep/dump, not one observation.
# Refusing it on a single stale target would be wrong.
MAX_TARGETS_PER_COMMAND = 32

# Outcome classes below exist so the stagnation index (a separate change)
# can tell dead ends from live answers. The repeat decision itself does not
# use them: judging whether an old answer is "still valid" would require
# re-fetching it, which is exactly the waste.
DEAD_FAMILY_OUTCOMES = frozenset(
    ["failed"]
    + [
        "http" + code
        for code in (
            "400", "401", "403", "404", "405", "406", "409", "410",
            "429", "500", "502", "503",
        )
    ]
)


def normalize_url(raw: str) -> str:
    """Canonicalize one URL so cosmetic spellings compare equal."""
    parts = urlsplit(str(raw or "").strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def extract_observation_targets(command) -> list:
    """Distinct normalized URLs in a bash command.

    Returns [] when there is no URL or when there are too many to attribute
    one outcome to (bulk sweep). Different IDs stay distinct on purpose:
    `/en/profile/55142` and `/en/profile/66` are different observations.
    """
    if not isinstance(command, str) or not command:
        return []
    seen = []
    for raw in _URL_RE.findall(command):
        try:
            norm = normalize_url(raw)
        except ValueError:
            continue
        if norm not in seen:
            seen.append(norm)
        if len(seen) > MAX_TARGETS_PER_COMMAND:
            return []
    return seen


def classify_observation_outcome(result, failed: bool):
    """Outcome class of one executed observation, or None when unusable.

    Harness refusals (`blocked`) are not observations, so they are skipped.
    A result with several different HTTP statuses, or an error status mixed
    with a success status, is ambiguous (a mixed batch) and is also skipped
    rather than attributed to every target in it.
    """
    text = str(result or "")
    if '"blocked":true' in text.replace(" ", ""):
        return None
    if failed:
        return "failed"
    statuses = sorted(set(_HTTP_ERROR_RE.findall(text)))
    if len(statuses) == 1:
        # 성공 상태가 함께 있으면 혼합 배치다. 어느 대상의 결과인지 특정할
        # 수 없으므로 학습하지 않는다. CSS 수치 같은 비상태 숫자는 무시한다.
        if _HTTP_SUCCESS_RE.search(text):
            return None
        return "http" + statuses[0]
    if len(statuses) > 1:
        return None
    match = _EXIT_CODE_RE.search(text)
    if match:
        return "exit" + match.group(1)
    return "other"


def is_dead_family_outcome(outcome) -> bool:
    """연속되면 재관측을 막아도 되는 결과인가."""
    return outcome in DEAD_FAMILY_OUTCOMES


def find_stale_targets(records, targets, limit: int) -> dict:
    """Targets already observed at least `limit` times.

    Returns {target: {"steps": [...], "outcome": ...}} where steps are the
    prior observation steps and outcome is the most recent outcome class.
    Harness-blocked results and ambiguous batches never count: they observed
    nothing attributable. Everything else counts — re-judging an old answer
    would require re-fetching it. Fresh checks use force=true.
    """
    seen: dict = {}
    for record in records or []:
        if record.get("tool") != "bash_exec" or not record.get("executed"):
            continue
        arguments = record.get("arguments") or {}
        command = arguments.get("command", "")
        if not isinstance(command, str):
            continue
        record_targets = extract_observation_targets(command)
        if not record_targets:
            continue
        outcome = classify_observation_outcome(
            str(record.get("result", "")), bool(record.get("failed"))
        )
        if outcome is None:
            continue
        step = record.get("step")
        for target in record_targets:
            seen.setdefault(target, []).append((step, outcome))
    stale = {}
    for target in targets or []:
        observations = seen.get(target, [])
        if len(observations) >= limit:
            steps = [
                step for step, _ in observations if isinstance(step, int)
            ]
            if steps:
                stale[target] = {
                    "steps": steps,
                    "outcome": observations[-1][1],
                }
    return stale


def check_observation_repeat(workspace, arguments, limit: int):
    """Decide whether a bash_exec call only re-observes known targets.

    Returns a {target: {"steps", "outcome"}} mapping when EVERY target in the
    command was already observed at least `limit` times, otherwise None.
    Refusing only fully-stale calls keeps bulk sweeps with at least one
    fresh target running. Never raises: any error means allow.
    running. Never raises: any error means allow.
    """
    try:
        args = arguments or {}
        command = args.get("command", "")
        targets = extract_observation_targets(command)
        if not targets:
            return None
        import trajectory

        records, _complete = trajectory.read_records(workspace)
        stale = find_stale_targets(records, targets, limit)
        if len(stale) == len(targets):
            return stale
        return None
    except Exception:
        return None
