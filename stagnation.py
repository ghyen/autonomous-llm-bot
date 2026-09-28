"""Stagnation index: repetition/novelty metrics over a trailing window.

Reported, never enforced. Measured across four production runs, no single
cheap signal separates a circling run from a healthy long run:

- result-text novelty: circling median 82 vs healthy 93 per 100 steps,
- new conclusions: a healthy run went 1,100 steps with 0 new conclusions,
- raw repetition: the healthy run re-read findings.md 84 times.

So this module does not judge and does not stop anything. It compresses
"what has this run been re-observing" into a few numbers that go into the
checkpoint report and the session log, where a human can tell circling from
progress. The mechanical waste bound lives in observation.py instead.
"""

import observation

INDEX_WINDOW_STEPS = 100
# 이 횟수까지의 반복은 허용분이다. 초과분만 데드엔드 반복으로 센다.
INDEX_DEAD_REPEAT_ALLOWANCE = 3
INDEX_TOP_REPEATS = 5


def _observation_pairs(records):
    """Yield ((target, outcome), step) for classifiable bash observations."""
    for record in records or []:
        if record.get("tool") != "bash_exec" or not record.get("executed"):
            continue
        arguments = record.get("arguments") or {}
        command = arguments.get("command", "")
        if not isinstance(command, str):
            continue
        targets = observation.extract_observation_targets(command)
        if not targets:
            continue
        outcome = observation.classify_observation_outcome(
            str(record.get("result", "")), bool(record.get("failed"))
        )
        if outcome is None:
            continue
        step = record.get("step")
        if not isinstance(step, int):
            continue
        for target in targets:
            yield (target, outcome), step


def compute_stagnation_index(records, end_step, window_steps=INDEX_WINDOW_STEPS):
    """Repetition/novelty metrics for the trailing window ending at end_step."""
    window_steps = max(1, int(window_steps or 0))
    start_step = end_step - window_steps
    prior = set()
    window_pairs = []
    for pair, step in _observation_pairs(records):
        if step <= start_step:
            prior.add(pair)
        elif step <= end_step:
            window_pairs.append((pair, step))
    novel_pairs = sum(1 for pair, _ in window_pairs if pair not in prior)
    reobserved = sum(1 for pair, _ in window_pairs if pair in prior)
    counts = {}
    for pair, _ in window_pairs:
        counts[pair] = counts.get(pair, 0) + 1
    dead_repeat_over = sum(
        count - INDEX_DEAD_REPEAT_ALLOWANCE
        for (target, outcome), count in counts.items()
        if outcome in observation.DEAD_FAMILY_OUTCOMES
        and count > INDEX_DEAD_REPEAT_ALLOWANCE
    )
    top_repeats = [
        {"target": target, "outcome": outcome, "count": count}
        for (target, outcome), count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0][0])
        )[:INDEX_TOP_REPEATS]
    ]
    total = len(window_pairs)
    return {
        "window_steps": window_steps,
        "end_step": int(end_step),
        "observations": total,
        "novel_pairs": novel_pairs,
        "dead_repeat_over": dead_repeat_over,
        "reobserve_pct": round(reobserved / total * 100) if total else 0,
        "top_repeats": top_repeats,
    }


def format_stagnation_lines(index) -> list:
    """Short Korean lines for the checkpoint report (4 lines max)."""
    lines = [
        f"> 📉 **정체 지수(최근 {index['window_steps']}스텝)**: "
        f"신규 관측 {index['novel_pairs']} · "
        f"재관측 {index['reobserve_pct']}% · "
        f"데드엔드 반복 초과 {index['dead_repeat_over']}"
    ]
    for item in (index.get("top_repeats") or [])[:3]:
        lines.append(
            f"> · `{item['target'][:60]}` → {item['outcome']} ×{item['count']}"
        )
    return lines
