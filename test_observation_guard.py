"""Observation-repeat guard tests.

The exact-command loop guard only fires on byte-identical repeats. This guard
fires when the same *target* already yielded the same dead-end outcome,
however the command was spelled. Production evidence: `/en/profile/55142`
-> 404 was fetched ~99 times across 600 steps, each spelling different
enough to pass the fingerprint guard.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_support import FakeMessage  # sets required config env before bot imports

import bot
import observation
import trajectory


def _workspace(root):
    run = Path(root) / "run"
    run.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(root=str(run))


def _append(workspace, step, command, result, failed=False, executed=True):
    call_id = f"c{step}-{abs(hash(command)) % 10_000_000}"
    trajectory.append_tool_group(
        workspace,
        step,
        [{
            "id": call_id,
            "name": "bash_exec",
            "arguments": {"command": command},
            "failed": failed,
        }],
        [result],
        {call_id} if executed else set(),
    )


def _fetch(url, status=404, step_hint=""):
    return (
        f"[stdout]\n<!doctype html><title>Page Not Found</title> {status} body\n"
        f"[exit code: 0]"
    )


class NormalizeUrlTest(unittest.TestCase):
    def test_trailing_slash_and_case_fold(self):
        self.assertEqual(
            observation.normalize_url("https://BrownFeed.COM/en/profile/55142/"),
            observation.normalize_url("https://brownfeed.com/en/profile/55142"),
        )

    def test_fragment_is_dropped_query_is_kept(self):
        self.assertEqual(
            observation.normalize_url("https://x.test/feed?a=1#top"),
            "https://x.test/feed?a=1",
        )
        self.assertNotEqual(
            observation.normalize_url("https://x.test/feed?a=1"),
            observation.normalize_url("https://x.test/feed?a=2"),
        )

    def test_ids_stay_distinct(self):
        self.assertNotEqual(
            observation.normalize_url("https://brownfeed.com/en/profile/55142"),
            observation.normalize_url("https://brownfeed.com/en/profile/66"),
        )


class ExtractTargetsTest(unittest.TestCase):
    def test_single_url(self):
        targets = observation.extract_observation_targets(
            "curl -s https://brownfeed.com/en/profile/55142 | head -c 500"
        )
        self.assertEqual(targets, ["https://brownfeed.com/en/profile/55142"])

    def test_quoted_and_python_urls(self):
        targets = observation.extract_observation_targets(
            'python3 -c "import urllib.request; urllib.request.urlopen('
            "'https://brownfeed.com/api/v1/feed')\""
        )
        self.assertEqual(targets, ["https://brownfeed.com/api/v1/feed"])

    def test_no_url_returns_empty(self):
        self.assertEqual(observation.extract_observation_targets("ls -la /tmp"), [])
        self.assertEqual(observation.extract_observation_targets(""), [])
        self.assertEqual(observation.extract_observation_targets(None), [])

    def test_bulk_sweep_is_out_of_scope(self):
        command = " ".join(
            f"https://brownfeed.com/en/profile/{uid}" for uid in range(100)
        )
        self.assertEqual(observation.extract_observation_targets(command), [])

    def test_duplicates_collapse(self):
        targets = observation.extract_observation_targets(
            "curl https://x.test/a && curl https://x.test/a/"
        )
        self.assertEqual(targets, ["https://x.test/a"])


class ClassifyOutcomeTest(unittest.TestCase):
    def test_http_error_statuses(self):
        for code in ("400", "401", "403", "404", "429", "500", "503"):
            outcome = observation.classify_observation_outcome(
                f"[stdout]\npage {code} body\n[exit code: 0]", False
            )
            self.assertEqual(outcome, "http" + code)

    def test_mixed_statuses_are_ambiguous(self):
        self.assertIsNone(
            observation.classify_observation_outcome(
                "a 404 and b 200 here", False
            )
        )

    def test_blocked_is_not_an_observation(self):
        self.assertIsNone(
            observation.classify_observation_outcome(
                '{"blocked":true,"reason":"loop_guard_repeat"}', False
            )
        )

    def test_failed_flag(self):
        self.assertEqual(
            observation.classify_observation_outcome("boom", True), "failed"
        )

    def test_exit_codes(self):
        self.assertEqual(
            observation.classify_observation_outcome(
                "[stdout]\n[exit code: 0]", False
            ),
            "exit0",
        )

    def test_dead_family_set(self):
        self.assertTrue(observation.is_dead_family_outcome("http404"))
        self.assertTrue(observation.is_dead_family_outcome("http503"))
        self.assertTrue(observation.is_dead_family_outcome("failed"))
        for outcome in ("exit0", "exit1", "blocked", "other", None):
            self.assertFalse(observation.is_dead_family_outcome(outcome))


class FindStaleTargetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = _workspace(self._tmp.name)

    def test_three_identical_dead_ends_are_stale(self):
        url = "https://brownfeed.com/en/profile/55142"
        spellings = [
            f'curl -s "{url}" | head -c 500',
            "python3 -c \"import urllib.request; print(urllib.request.urlopen("
            f"'{url}').status)\"",
            f"curl -sL {url}/ -o /tmp/p.html -w '%{{http_code}}'",
        ]
        for step, command in enumerate(spellings, start=10):
            _append(self.workspace, step, command, _fetch(url))
        records, _ = trajectory.read_records(self.workspace)
        stale = observation.find_stale_targets(
            records, [url], bot.OBSERVATION_REPEAT_LIMIT
        )
        self.assertIn(url, stale)
        self.assertEqual(stale[url]["outcome"], "http404")
        self.assertEqual(stale[url]["steps"], [10, 11, 12])

    def test_two_observations_are_not_enough(self):
        url = "https://brownfeed.com/en/profile/55142"
        for step in (10, 11):
            _append(self.workspace, step, f"curl {url}", _fetch(url))
        records, _ = trajectory.read_records(self.workspace)
        self.assertEqual(
            observation.find_stale_targets(
                records, [url], bot.OBSERVATION_REPEAT_LIMIT
            ),
            {},
        )

    def test_mixed_outcomes_still_count_toward_the_limit(self):
        # v3: 같은 대상을 본 횟수만 센다. 옛 답이 유효한지 판단하려면 다시
        # 가져와야 해서 그 자체가 낭비다. 진짜 새 확인은 force=true로 명시한다.
        url = "https://brownfeed.com/api/v1/feed"
        for step in (10, 11):
            _append(self.workspace, step, f"curl {url}", _fetch(url, status=404))
        _append(
            self.workspace, 12, f"curl {url}",
            "[stdout]\n{\"life_events\": []}\n[exit code: 0]",
        )
        records, _ = trajectory.read_records(self.workspace)
        stale = observation.find_stale_targets(
            records, [url], bot.OBSERVATION_REPEAT_LIMIT
        )
        self.assertIn(url, stale)
        self.assertEqual(stale[url]["steps"], [10, 11, 12])

    def test_exit_zero_repeats_count_like_any_observation(self):
        # v3: 결과 종류와 무관하게 같은 대상을 limit회 보면 막는다. 피드가
        # 바뀌었는지 확인하려면 다시 가져와야 하고, 그 판단 자체가 반복의
        # 이유가 되므로 카운트에서 빼지 않는다. force=true가 탈출구다.
        url = "https://brownfeed.com/api/v1/feed"
        for step in (10, 11, 12, 13):
            _append(
                self.workspace, step, f"curl {url}",
                "[stdout]\n{\"life_events\": []}\n[exit code: 0]",
            )
        records, _ = trajectory.read_records(self.workspace)
        stale = observation.find_stale_targets(
            records, [url], bot.OBSERVATION_REPEAT_LIMIT
        )
        self.assertIn(url, stale)
        self.assertEqual(stale[url]["outcome"], "exit0")

    def test_unexecuted_records_do_not_count(self):
        url = "https://brownfeed.com/en/profile/55142"
        for step in (10, 11, 12):
            _append(
                self.workspace, step, f"curl {url}", _fetch(url), executed=False
            )
        records, _ = trajectory.read_records(self.workspace)
        self.assertEqual(
            observation.find_stale_targets(
                records, [url], bot.OBSERVATION_REPEAT_LIMIT
            ),
            {},
        )

    def test_ambiguous_batches_do_not_count(self):
        # 에러와 성공이 섞인 결과는 어느 대상의 것인지 특정할 수 없어서
        # 학습하지 않는다.
        url = "https://brownfeed.com/en/profile/55142"
        for step in (10, 11, 12):
            _append(
                self.workspace, step, f"curl {url}",
                "[stdout]\nuser 404 page 200 ok\n[exit code: 0]",
            )
        records, _ = trajectory.read_records(self.workspace)
        self.assertEqual(
            observation.find_stale_targets(
                records, [url], bot.OBSERVATION_REPEAT_LIMIT
            ),
            {},
        )

    def test_blocked_results_do_not_count(self):
        # 하네스 차단 결과는 관측이 아니라서 카운트하지 않는다.
        url = "https://brownfeed.com/en/profile/55142"
        for step in (10, 11, 12):
            _append(
                self.workspace, step, f"curl {url}",
                '{"blocked":true,"reason":"loop_guard_repeat"}',
            )
        records, _ = trajectory.read_records(self.workspace)
        self.assertEqual(
            observation.find_stale_targets(
                records, [url], bot.OBSERVATION_REPEAT_LIMIT
            ),
            {},
        )
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = _workspace(self._tmp.name)

    def test_all_stale_targets_returns_mapping(self):
        url = "https://brownfeed.com/en/profile/55142"
        for step in (10, 11, 12):
            _append(self.workspace, step, f"curl {url} #{step}", _fetch(url))
        stale = observation.check_observation_repeat(
            self.workspace,
            {"command": f"curl -sL {url}/"},
            bot.OBSERVATION_REPEAT_LIMIT,
        )
        self.assertIsNotNone(stale)
        self.assertEqual(stale[url]["outcome"], "http404")

    def test_one_fresh_target_keeps_the_call(self):
        dead = "https://brownfeed.com/en/profile/55142"
        fresh = "https://brownfeed.com/en/profile/99999"
        for step in (10, 11, 12):
            _append(self.workspace, step, f"curl {dead}", _fetch(dead))
        self.assertIsNone(
            observation.check_observation_repeat(
                self.workspace,
                {"command": f"curl {dead}; curl {fresh}"},
                bot.OBSERVATION_REPEAT_LIMIT,
            )
        )

    def test_no_targets_means_allow(self):
        self.assertIsNone(
            observation.check_observation_repeat(
                self.workspace, {"command": "ls -la /tmp"}, bot.OBSERVATION_REPEAT_LIMIT
            )
        )

    def test_broken_trajectory_fails_open(self):
        broken = Path(self.workspace.root) / trajectory.FILE_NAME
        if broken.exists():
            broken.unlink()
        broken.mkdir()
        self.assertIsNone(
            observation.check_observation_repeat(
                self.workspace,
                {"command": "curl https://brownfeed.com/en/profile/1"},
                bot.OBSERVATION_REPEAT_LIMIT,
            )
        )


class BlockedPayloadTest(unittest.TestCase):
    def test_observation_repeat_directive_names_the_dead_end(self):
        payload = json.loads(
            bot._blocked_tool_result(
                "observation_repeat", "bash_exec", 3, 5, first_step=926
            )
        )
        self.assertTrue(payload["blocked"])
        self.assertEqual(payload["reason"], "observation_repeat")
        self.assertEqual(payload["first_step"], 926)
        self.assertIn("force=true", payload["directive"])


if __name__ == "__main__":
    unittest.main()
