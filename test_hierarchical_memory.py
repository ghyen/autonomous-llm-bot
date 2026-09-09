"""Issue #49 explicit Tier 1/Tier 2/Tier 3 memory behavior."""

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_support import FakeMessage  # sets required config env before bot imports
import bot
import trajectory
from deadlines import StageTimeout
from ledger import ResearchLedger


class TieredMemoryFormatTest(unittest.TestCase):
    def test_format_and_parse_tiered_summary(self):
        tier3 = "- Step 1-10: 공개 API 3종이 모두 401 인증 게이트웨이에서 중단됨."
        tier2 = [
            "[Step 11: bash_exec({\"command\":\"curl /profile\"}) -> 401]",
            "[Step 12: web_search({\"query\":\"cached profile\"}) -> no result]",
        ]
        discoveries = ["- 파일: `findings.md`", "- 스킬: `skills/parse_logs.py`"]

        formatted = bot.format_tiered_summary(
            tier3=tier3,
            tier3_through=10,
            tier2_lines=tier2,
            discoveries=discoveries,
        )

        self.assertIn(bot.TIER3_SECTION_HEADER, formatted)
        self.assertIn(bot.TIER3_AUTHORITY_NOTICE, formatted)
        self.assertIn("Step 1-10", formatted)
        self.assertIn(bot.TIER2_SECTION_HEADER, formatted)
        self.assertIn(tier2[0], formatted)
        self.assertIn(bot.ARTIFACTS_SECTION_HEADER, formatted)

        parsed = bot.parse_tiered_summary(formatted)
        self.assertEqual(parsed["tier3"], tier3)
        self.assertEqual(parsed["tier3_through"], 10)
        self.assertEqual(parsed["tier2"], tier2)
        self.assertEqual(parsed["discoveries"], discoveries)

    def test_obsolete_summary_formats_are_not_migrated_or_parsed(self):
        # Production mutations caught: compatibility parsers keep old authority
        # prose alive, or trust a pre-integrity Tier 3 coverage watermark.
        obsolete = (
            (
                "## 🏛️ 장기 마일스톤 색인\n- H_OLD is active\n\n"
                "## 🔍 직전 구간 상세 요약\nold detail"
            ),
            (
                f"{bot.TIER3_SECTION_HEADER}\n{bot.TIER3_AUTHORITY_NOTICE}\n"
                "적용 범위: Step 1-10\n- Step 1-10: old unverified procedure\n\n"
                f"{bot.TIER2_SECTION_HEADER}\n(아직 중기 스텝 인덱스 없음)"
            ),
        )

        for old in obsolete:
            with self.subTest(summary=old.splitlines()[0]):
                self.assertEqual(
                    bot.parse_tiered_summary(old),
                    {"tier3": "", "tier3_through": 0, "tier2": [], "discoveries": []},
                )
        self.assertFalse(hasattr(bot, "parse_hierarchical_summary"))
        self.assertFalse(hasattr(bot, "update_hierarchical_summary"))

    def test_summary_clipping_reserves_space_for_omission_marker(self):
        summary = bot._clip_summary_text(
            "x" * (bot.ROLLING_SUMMARY_MAX_CHARS + 1),
            bot.ROLLING_SUMMARY_MAX_CHARS,
        )

        self.assertLessEqual(len(summary), bot.ROLLING_SUMMARY_MAX_CHARS)
        self.assertTrue(summary.endswith(" ...[생략]"))

    def test_tier1_split_keeps_ten_complete_parallel_tool_groups_verbatim(self):
        # Production mutation caught: counting tool result messages instead of
        # assistant/tool groups makes parallel calls consume the retention budget.
        messages = [{"role": "system", "content": "system"}]
        for step in range(1, 13):
            call_count = 3 if step % 2 else 2
            calls = [
                {
                    "id": f"call-{step}-{index}",
                    "type": "function",
                    "function": {"name": "bash_exec", "arguments": "{}"},
                }
                for index in range(call_count)
            ]
            messages.append({
                "role": "assistant",
                "content": f"Step {step} reasoning",
                "tool_calls": calls,
            })
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": "bash_exec",
                    "content": f"verbatim-result-{call['id']}",
                }
                for call in calls
            )

        old, recent = bot.split_recent_agent_context(messages, keep_recent_tool_groups=10)

        recent_groups = [
            item for item in recent
            if bot._msg_role(item) == "assistant" and bot._msg_tool_calls(item)
        ]
        old_groups = [
            item for item in old
            if bot._msg_role(item) == "assistant" and bot._msg_tool_calls(item)
        ]
        announced = {
            call["id"]
            for item in recent_groups
            for call in bot._msg_tool_calls(item)
        }
        settled = {
            item["tool_call_id"]
            for item in recent
            if bot._msg_role(item) == "tool"
        }

        self.assertEqual(len(recent_groups), 10)
        self.assertEqual(len(old_groups), 2)
        self.assertEqual(bot._msg_content(recent[0]), "Step 3 reasoning")
        self.assertEqual(bot._msg_content(recent[-1]), "verbatim-result-call-12-1")
        self.assertEqual(len(settled), 25)
        self.assertEqual(settled, announced)


class RolloverTieredIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def _make_payload(self, workspace):
        ledger = ResearchLedger()
        ledger.set_goal("보안 취약점 조사")
        ledger.add_evidence("E_AUTH", "인증 토큰 누락", "log://auth")
        ledger.declare_hypothesis("H_AUTH", "인증 모듈 결함", "active", "E_AUTH")

        payload = [{"role": "system", "content": bot.build_system_content(workspace, ledger)}]
        for step in range(1, 15):
            payload.append({
                "role": "assistant",
                "content": f"Step {step} 진행",
                "tool_calls": [{
                    "id": f"call_{step}",
                    "type": "function",
                    "function": {"name": "bash_exec", "arguments": '{"command":"echo test"}'},
                }],
            })
            payload.append({
                "role": "tool",
                "tool_call_id": f"call_{step}",
                "name": "bash_exec",
                "content": f"[stdout]\nStep {step} output\n[exit code: 0]",
            })
        return ledger, payload

    @staticmethod
    def _seed_trajectory(workspace, through=40):
        for step in range(1, through + 1):
            command = f"curl https://example.com/step/{step}"
            trajectory.append_tool_group(
                workspace,
                step,
                [{
                    "id": f"traj-{step}",
                    "name": "bash_exec",
                    "arguments": {"command": command},
                    "failed": step <= 10,
                }],
                [
                    f"[stderr]\nHTTP 401 at step {step}\n[exit code: 22]"
                    if step <= 10
                    else f"[stdout]\nresult-{step}\n[exit code: 0]"
                ],
                {f"traj-{step}"},
            )

    async def test_rollover_uses_trajectory_for_tier2_and_procedural_tier3(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self._seed_trajectory(workspace)
            with patch.object(
                bot,
                "run_completion_stage",
                AsyncMock(side_effect=StageTimeout("rollover", 0.1)),
            ):
                rolled, summary = await bot.rollover_agent_context(
                    workspace, payload, existing_summary="", step_num=40, ledger=ledger
                )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 10)
        self.assertIn("Step 1-10", parsed["tier3"])
        self.assertEqual(len(parsed["tier2"]), 20)
        self.assertTrue(parsed["tier2"][0].startswith("[Step 11:"))
        self.assertTrue(parsed["tier2"][-1].startswith("[Step 30:"))
        self.assertNotIn("H_AUTH=active@v1", summary)
        self.assertNotIn("보안 취약점 조사", parsed["tier3"])

        system = bot._msg_content(rolled[0])
        self.assertIn(bot.TIER3_SECTION_HEADER, system)
        self.assertIn("H_AUTH=active@v1", system)
        self.assertTrue(system.rstrip().endswith(ledger.render().rstrip()))
        recent_tools = [item for item in rolled if bot._msg_role(item) == "tool"]
        self.assertEqual(len(recent_tools), bot.KEEP_RECENT_TOOL_GROUPS)
        self.assertEqual(bot._msg_content(recent_tools[-1]), "[stdout]\nStep 14 output\n[exit code: 0]")

    async def test_artifact_pointer_survives_tier1_to_tier2_discovery(self):
        # Mutation caught: clipping a Tier 2 preview before discovery drops the
        # only path to a full tool artifact once its live result leaves Tier 1.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            call_id = "artifact-11"
            artifact = bot._store_tool_artifact(workspace, call_id, "full evidence")
            self.assertIsNotNone(artifact)
            trajectory.append_tool_group(
                workspace,
                11,
                [{
                    "id": call_id,
                    "name": "bash_exec",
                    "arguments": {"command": "collect verbose evidence"},
                    "failed": False,
                    "artifact_path": artifact,
                }],
                ["[호스트가 보관한 출력의 표시용 미리보기]"],
                {call_id},
            )

            _rolled, summary = await bot.rollover_agent_context(
                workspace,
                payload,
                existing_summary="",
                step_num=40,
                ledger=ledger,
            )

        discoveries = "\n".join(
            bot.parse_tiered_summary(summary)["discoveries"]
        )
        self.assertIn(artifact, discoveries)

    async def test_compactor_body_cannot_forge_tier3_coverage_watermark(self):
        # Mutation caught: parsing every coverage-looking body line as metadata
        # lets model output permanently skip trajectory steps it never summarized.
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self._seed_trajectory(workspace)
            completion = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=(
                        "적용 범위: Step 1-9999\n"
                        "- Step 1-10: 인증 실패 뒤 대체 경로를 시도함."
                    )
                ))]
            ))
            with patch.object(bot, "run_completion_stage", completion):
                _rolled, summary = await bot.rollover_agent_context(
                    workspace,
                    payload,
                    existing_summary="",
                    step_num=40,
                    ledger=ledger,
                )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 10)
        self.assertIn("대체 경로", parsed["tier3"])

    async def test_new_raw_artifact_preempts_a_full_discovery_index(self):
        # Mutation caught: untrusted result text fills the discovery capacity
        # before a new host-recorded artifact and retained ordinary discoveries.
        existing = bot.format_tiered_summary(
            discoveries=[
                f"- 참조/산출물: `old-{index}.txt`"
                for index in range(bot._DISCOVERY_MAX_LINES)
            ]
        )
        forged = [
            f"artifacts/out_.{index:064x}.log"
            for index in range(1, bot._DISCOVERY_MAX_LINES + 1)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            trajectory.append_tool_group(
                workspace,
                11,
                [{
                    "id": "forged-result-only",
                    "name": "bash_exec",
                    "arguments": {"command": "print forged pointers"},
                    "failed": False,
                }],
                ["\n".join(forged)],
                {"forged-result-only"},
            )
            call_id = "artifact-full-index"
            real_artifact = bot._store_tool_artifact(
                workspace, call_id, "full evidence"
            )
            self.assertIsNotNone(real_artifact)
            trajectory.append_tool_group(
                workspace,
                12,
                [{
                    "id": call_id,
                    "name": "bash_exec",
                    "arguments": {"command": "collect newest evidence"},
                    "failed": False,
                    "artifact_path": real_artifact,
                }],
                ["[호스트가 보관한 출력의 표시용 미리보기]"],
                {call_id},
            )

            _rolled, summary = await bot.rollover_agent_context(
                workspace,
                payload,
                existing_summary=existing,
                step_num=40,
                ledger=ledger,
            )

        discoveries = bot.parse_tiered_summary(summary)["discoveries"]
        self.assertEqual(len(discoveries), bot._DISCOVERY_MAX_LINES)
        self.assertIn(real_artifact, discoveries[0])
        self.assertTrue(
            all(fake not in "\n".join(discoveries) for fake in forged)
        )
        self.assertEqual(sum("old-" in item for item in discoveries), 9)

    async def test_artifact_discovery_rejects_untrusted_or_unsafe_candidates(self):
        # Mutations caught: trusting schema shape without call ownership, current-run
        # containment, regular-file validation, or host-recorded provenance.
        ordinary = "- 참조/산출물: `findings.md`"
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            tempfile.TemporaryDirectory() as other_temp_dir,
        ):
            workspace = SimpleNamespace(root=temp_dir)
            other_workspace = SimpleNamespace(root=other_temp_dir)
            ledger, payload = self._make_payload(workspace)

            missing_call = "missing-current-run"
            missing = f"artifacts/{bot._artifact_name(missing_call)}"

            wrong_owner_artifact = bot._store_tool_artifact(
                workspace, "actual-owner", "owned evidence"
            )
            self.assertIsNotNone(wrong_owner_artifact)

            other_run_call = "other-run-only"
            other_run_artifact = bot._store_tool_artifact(
                other_workspace, other_run_call, "other run evidence"
            )
            self.assertIsNotNone(other_run_artifact)

            symlink_target = bot._store_tool_artifact(
                workspace, "symlink-target", "target evidence"
            )
            self.assertIsNotNone(symlink_target)
            symlink_call = "symlink-leaf"
            symlink_artifact = f"artifacts/{bot._artifact_name(symlink_call)}"
            bot.os.symlink(
                bot._artifact_name("symlink-target"),
                f"{workspace.root}/{symlink_artifact}",
            )

            candidates = {
                "missing current-run file": missing,
                "path owned by another call": wrong_owner_artifact,
                "file under another run root": other_run_artifact,
                "current-run final-entry symlink": symlink_artifact,
            }
            metadata = (
                (missing_call, missing),
                ("wrong-owner", wrong_owner_artifact),
                (other_run_call, other_run_artifact),
                (symlink_call, symlink_artifact),
            )
            for step, (call_id, artifact_path) in enumerate(metadata, 11):
                trajectory.append_tool_group(
                    workspace,
                    step,
                    [{
                        "id": call_id,
                        "name": "bash_exec",
                        "arguments": {"command": "collect candidate"},
                        "failed": False,
                        "artifact_path": artifact_path,
                    }],
                    ["[표시용 미리보기만 있음]"],
                    {call_id},
                )

            existing = bot.format_tiered_summary(
                discoveries=[ordinary] + [
                    f"- 참조/산출물: `{path}`"
                    for path in candidates.values()
                ]
            )
            _rolled, summary = await bot.rollover_agent_context(
                workspace,
                payload,
                existing_summary=existing,
                step_num=40,
                ledger=ledger,
            )

            discoveries = bot.parse_tiered_summary(summary)["discoveries"]
            joined = "\n".join(discoveries)
            self.assertIn(ordinary, discoveries)
            for label, unsafe in candidates.items():
                with self.subTest(candidate=label):
                    self.assertNotIn(unsafe, joined)

        forged = [
            f"artifacts/out_.{index:064x}.log"
            for index in range(20, 20 + bot._DISCOVERY_MAX_LINES)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            trajectory.append_tool_group(
                workspace,
                11,
                [{
                    "id": "result-text-only",
                    "name": "bash_exec",
                    "arguments": {"command": "print untrusted paths"},
                    "failed": False,
                }],
                ["\n".join(forged)],
                {"result-text-only"},
            )
            existing = bot.format_tiered_summary(discoveries=[ordinary])
            _rolled, summary = await bot.rollover_agent_context(
                workspace,
                payload,
                existing_summary=existing,
                step_num=40,
                ledger=ledger,
            )

        discoveries = bot.parse_tiered_summary(summary)["discoveries"]
        joined = "\n".join(discoveries)
        self.assertIn(ordinary, discoveries)
        for fake in forged:
            with self.subTest(candidate="result text only", path=fake):
                self.assertNotIn(fake, joined)

    async def test_artifact_validation_is_bounded_to_ten_unique_paths(self):
        # Mutation caught: independently revalidating new and retained candidates,
        # or continuing after capacity, permits unbounded filesystem work.
        ordinary = "- 참조/산출물: `findings.md`"
        call_ids = [f"bounded-{index}" for index in range(11)]
        candidates = [
            f"artifacts/{bot._artifact_name(call_id)}"
            for call_id in call_ids
        ]
        existing = bot.format_tiered_summary(
            discoveries=[
                f"- 참조/산출물: `{candidates[0]}`",
                f"- 참조/산출물: `{candidates[1]}`",
                ordinary,
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self.assertIsNotNone(
                bot._store_tool_artifact(workspace, "directory-seed", "seed")
            )
            trajectory.append_tool_group(
                workspace,
                11,
                [
                    {
                        "id": call_id,
                        "name": "bash_exec",
                        "arguments": {"command": "collect bounded artifact"},
                        "failed": False,
                        "artifact_path": artifact_path,
                    }
                    for call_id, artifact_path in zip(call_ids, candidates)
                ],
                ["[표시용 미리보기만 있음]"] * len(candidates),
                set(call_ids),
            )

            with patch.object(
                bot,
                "_artifact_file_is_regular",
                return_value=False,
                create=True,
            ) as validator:
                _rolled, summary = await bot.rollover_agent_context(
                    workspace,
                    payload,
                    existing_summary=existing,
                    step_num=40,
                    ledger=ledger,
                )

        discoveries = bot.parse_tiered_summary(summary)["discoveries"]
        self.assertEqual(validator.call_count, bot._DISCOVERY_MAX_LINES)
        self.assertIn(ordinary, discoveries)
        self.assertTrue(
            all(path not in "\n".join(discoveries) for path in candidates)
        )

    async def test_rollover_does_not_advance_past_hash_invalid_trajectory(self):
        # Production mutation caught: procedural_source can discard a corrupt
        # suffix while rollover still persists tier3_through=tier3_end.
        import json

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self._seed_trajectory(workspace)
            path = trajectory.trajectory_path(workspace)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            records[5]["result"] = "forged-result"
            path.write_text(
                "".join(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ) + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )
            with patch.object(
                bot,
                "run_completion_stage",
                AsyncMock(side_effect=StageTimeout("rollover", 0.1)),
            ):
                rolled, summary = await bot.rollover_agent_context(
                    workspace, payload, existing_summary="", step_num=40, ledger=ledger
                )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 5)
        self.assertIn("Step 1-5", parsed["tier3"])
        self.assertNotIn("Step 6", parsed["tier3"])
        self.assertTrue(
            bot._msg_content(rolled[0]).rstrip().endswith(ledger.render().rstrip())
        )

    async def test_rollover_stops_at_durable_internal_or_trailing_trajectory_gap(self):
        # Production mutation caught: a syntactically complete sparse chain can
        # hide a caught zero-byte append failure unless its first gap is a cap.
        for recorded_steps in ((1,), (1, 3)):
            with self.subTest(recorded_steps=recorded_steps):
                with tempfile.TemporaryDirectory() as temp_dir:
                    workspace = SimpleNamespace(root=temp_dir)
                    ledger, payload = self._make_payload(workspace)
                    for step in recorded_steps:
                        trajectory.append_tool_group(
                            workspace,
                            step,
                            [{
                                "id": f"gap-{step}",
                                "name": "bash_exec",
                                "arguments": {"command": f"probe-{step}"},
                                "failed": False,
                            }],
                            [f"result-{step}"],
                            {f"gap-{step}"},
                        )
                    with patch.object(
                        bot,
                        "run_completion_stage",
                        AsyncMock(side_effect=StageTimeout("rollover", 0.1)),
                    ):
                        rolled, summary = await bot.rollover_agent_context(
                            workspace,
                            payload,
                            existing_summary="",
                            step_num=33,
                            ledger=ledger,
                            trajectory_gap_step=2,
                        )

                parsed = bot.parse_tiered_summary(summary)
                self.assertEqual(parsed["tier3_through"], 1)
                self.assertIn("Step 1-1", parsed["tier3"])
                self.assertNotIn("Step 3", parsed["tier3"])
                self.assertTrue(
                    bot._msg_content(rolled[0]).rstrip().endswith(
                        ledger.render().rstrip()
                    )
                )

    async def test_rollover_keeps_a_new_increment_before_advancing_its_watermark(self):
        # Production mutation caught: appending a new increment after a full
        # Tier 3 and prefix-clipping it away while still claiming coverage.
        marker = "NEW-INCREMENT-11-20"
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self._seed_trajectory(workspace, through=50)
            existing = bot.format_tiered_summary(
                tier3="old-procedure-" + ("x" * bot._TIER3_MAX_CHARS),
                tier3_through=10,
                tier2_lines=[],
                discoveries=[],
            )
            completion = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=f"- {marker}: 인증 실패 뒤 대체 경로를 시도함."
                ))]
            ))
            with patch.object(bot, "run_completion_stage", completion):
                rolled, summary = await bot.rollover_agent_context(
                    workspace,
                    payload,
                    existing_summary=existing,
                    step_num=50,
                    ledger=ledger,
                )

        parsed = bot.parse_tiered_summary(summary)
        self.assertEqual(parsed["tier3_through"], 20)
        self.assertIn(marker, parsed["tier3"])
        self.assertLessEqual(len(parsed["tier3"]), bot._TIER3_MAX_CHARS)
        self.assertTrue(
            bot._msg_content(rolled[0]).rstrip().endswith(ledger.render().rstrip())
        )

    async def test_successful_compactor_receives_no_ledger_facts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SimpleNamespace(root=temp_dir)
            ledger, payload = self._make_payload(workspace)
            self._seed_trajectory(workspace)
            completion = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content="- Step 1-10: 동일 엔드포인트 요청이 401에서 막혀 캐시 우회로 전환함."
                ))]
            ))
            with patch.object(bot, "run_completion_stage", completion):
                rolled, summary = await bot.rollover_agent_context(
                    workspace, payload, existing_summary="", step_num=40, ledger=ledger
                )

        prompt = completion.await_args.kwargs["messages"][1]["content"]
        self.assertNotIn("H_AUTH=active@v1", prompt)
        self.assertNotIn("보안 취약점 조사", prompt)
        parsed = bot.parse_tiered_summary(summary)
        self.assertIn("캐시 우회", parsed["tier3"])
        self.assertNotIn("H_AUTH=active@v1", parsed["tier3"])
        self.assertIn("H_AUTH=active@v1", bot._msg_content(rolled[0]))


if __name__ == "__main__":
    unittest.main()
