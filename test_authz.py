import unittest

from authz import ACCESS, CONTROL, PURGE, authorize
from test_support import TEST_ADMIN_ID, TEST_OUTSIDER_ID, TEST_USER_ID

ALLOWED = frozenset({TEST_USER_ID, TEST_ADMIN_ID})
ADMINS = frozenset({TEST_ADMIN_ID})


def decide(action, user_id, **overrides):
    kwargs = {
        "allowed_user_ids": ALLOWED,
        "admin_user_ids": ADMINS,
        "tools_enabled": True,
    }
    kwargs.update(overrides)
    return authorize(action, user_id, **kwargs)


class AccessTest(unittest.TestCase):
    def test_allowlisted_caller_is_allowed(self):
        self.assertTrue(decide(ACCESS, TEST_USER_ID))

    def test_outsider_is_denied(self):
        decision = decide(ACCESS, TEST_OUTSIDER_ID)
        self.assertFalse(decision)
        self.assertIn("허용 목록", decision.reason)

    def test_unknown_identity_is_denied(self):
        self.assertFalse(decide(ACCESS, None))

    def test_unknown_action_is_denied(self):
        self.assertFalse(decide("delete_everything", TEST_ADMIN_ID))


class FailClosedTest(unittest.TestCase):
    def test_empty_allowlist_with_tools_enabled_refuses_rather_than_opens(self):
        decision = decide(ACCESS, TEST_USER_ID, allowed_user_ids=frozenset())
        self.assertFalse(decision)
        self.assertIn("도구가 활성화", decision.reason)

    def test_tool_free_mode_may_run_without_an_allowlist(self):
        self.assertTrue(
            decide(ACCESS, TEST_OUTSIDER_ID, allowed_user_ids=frozenset(), tools_enabled=False)
        )

    def test_tool_free_mode_still_refuses_purge_without_an_admin_list(self):
        self.assertFalse(
            decide(
                PURGE,
                TEST_OUTSIDER_ID,
                allowed_user_ids=frozenset(),
                admin_user_ids=frozenset(),
                tools_enabled=False,
                caller_can_manage_messages=True,
            )
        )


class ControlTest(unittest.TestCase):
    def test_any_allowed_caller_may_control_when_no_run_is_active(self):
        self.assertTrue(decide(CONTROL, TEST_USER_ID, run_owner_id=None))

    def test_owner_may_control_their_own_run(self):
        self.assertTrue(decide(CONTROL, TEST_USER_ID, run_owner_id=TEST_USER_ID))

    def test_non_owner_may_not_control_someone_elses_run(self):
        decision = decide(CONTROL, TEST_USER_ID, run_owner_id=TEST_ADMIN_ID)
        self.assertFalse(decision)
        self.assertIn("시작한 사용자", decision.reason)

    def test_admin_may_control_any_run(self):
        self.assertTrue(decide(CONTROL, TEST_ADMIN_ID, run_owner_id=TEST_USER_ID))

    def test_outsider_may_not_control_even_an_unowned_run(self):
        self.assertFalse(decide(CONTROL, TEST_OUTSIDER_ID, run_owner_id=None))


class PurgeTest(unittest.TestCase):
    def test_admin_with_the_discord_permission_is_allowed(self):
        self.assertTrue(decide(PURGE, TEST_ADMIN_ID, caller_can_manage_messages=True))

    def test_admin_without_the_discord_permission_is_denied(self):
        decision = decide(PURGE, TEST_ADMIN_ID, caller_can_manage_messages=False)
        self.assertFalse(decision)
        self.assertIn("메시지 관리", decision.reason)

    def test_allowed_non_admin_is_denied_when_an_admin_list_exists(self):
        decision = decide(PURGE, TEST_USER_ID, caller_can_manage_messages=True)
        self.assertFalse(decision)
        self.assertIn("DISCORD_ADMIN_USER_IDS", decision.reason)

    def test_allowed_caller_with_the_permission_passes_when_no_admin_list_is_set(self):
        self.assertTrue(
            decide(
                PURGE,
                TEST_USER_ID,
                admin_user_ids=frozenset(),
                caller_can_manage_messages=True,
            )
        )

    def test_outsider_is_denied_regardless_of_discord_permissions(self):
        self.assertFalse(decide(PURGE, TEST_OUTSIDER_ID, caller_can_manage_messages=True))


if __name__ == "__main__":
    unittest.main()
