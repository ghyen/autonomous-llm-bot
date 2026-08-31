import os
import tempfile
import unittest

from config import BotConfig, ConfigError, load_config, load_env_file, parse_id_set, startup_diagnostics

MINIMAL_ENV = {
    "DISCORD_BOT_TOKEN": "dummy-invalid-token",
    "DISCORD_ALLOWED_USER_IDS": "111111111111111111",
}


def env(**overrides):
    merged = dict(MINIMAL_ENV)
    merged.update(overrides)
    return {k: v for k, v in merged.items() if v is not None}


class ParseIdSetTest(unittest.TestCase):
    def test_parses_comma_separated_ids(self):
        self.assertEqual(
            parse_id_set("111111111111111111, 222222222222222222", "FIELD"),
            frozenset({111111111111111111, 222222222222222222}),
        )

    def test_empty_value_is_an_empty_set(self):
        self.assertEqual(parse_id_set("", "FIELD"), frozenset())
        self.assertEqual(parse_id_set(None, "FIELD"), frozenset())

    def test_trailing_separator_is_tolerated(self):
        self.assertEqual(parse_id_set("111111111111111111,", "FIELD"), frozenset({111111111111111111}))

    def test_malformed_entry_fails_instead_of_being_dropped(self):
        with self.assertRaises(ConfigError) as caught:
            parse_id_set("111111111111111111,not-an-id", "FIELD")
        self.assertIn("FIELD", str(caught.exception))
        self.assertIn("not-an-id", str(caught.exception))

    def test_negative_and_zero_ids_fail(self):
        for bad in ("-1", "0"):
            with self.assertRaises(ConfigError):
                parse_id_set(bad, "FIELD")


class EnvFileTest(unittest.TestCase):
    def test_parses_comments_quotes_and_export_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "# comment\n"
                    "\n"
                    "DISCORD_BOT_TOKEN=dummy-invalid-token\n"
                    'MODEL_NAME="quoted-model"\n'
                    "export LLM_BASE_URL='http://127.0.0.1:18080/v1'\n"
                    "MALFORMED_LINE_WITHOUT_EQUALS\n"
                )
            values = load_env_file(path)

        self.assertEqual(values["DISCORD_BOT_TOKEN"], "dummy-invalid-token")
        self.assertEqual(values["MODEL_NAME"], "quoted-model")
        self.assertEqual(values["LLM_BASE_URL"], "http://127.0.0.1:18080/v1")
        self.assertNotIn("MALFORMED_LINE_WITHOUT_EQUALS", values)

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(load_env_file("/nonexistent/.env"), {})

    def test_real_environment_wins_over_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("MODEL_NAME=from-file\nDISCORD_BOT_TOKEN=dummy-invalid-token\n")
                handle.write("DISCORD_ALLOWED_USER_IDS=111111111111111111\n")
            config = load_config(env={"MODEL_NAME": "from-environment"}, env_file=path)

        self.assertEqual(config.model_name, "from-environment")


class LoadConfigTest(unittest.TestCase):
    def test_minimal_valid_configuration(self):
        config = load_config(env=env(), env_file=None)
        self.assertEqual(config.discord_token, "dummy-invalid-token")
        self.assertEqual(config.allowed_user_ids, frozenset({111111111111111111}))
        self.assertTrue(config.tools_enabled)
        self.assertTrue(config.llm_is_local)

    def test_missing_token_fails(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(env=env(DISCORD_BOT_TOKEN=None), env_file=None)
        self.assertIn("DISCORD_BOT_TOKEN", str(caught.exception))

    def test_blank_token_fails(self):
        with self.assertRaises(ConfigError):
            load_config(env=env(DISCORD_BOT_TOKEN="   "), env_file=None)

    def test_tools_enabled_requires_a_user_policy(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(env=env(DISCORD_ALLOWED_USER_IDS=None), env_file=None)
        message = str(caught.exception)
        self.assertIn("DISCORD_ALLOWED_USER_IDS", message)
        self.assertIn("BOT_TOOLS_ENABLED", message)

    def test_tool_free_deployment_may_omit_the_user_policy(self):
        config = load_config(
            env=env(DISCORD_ALLOWED_USER_IDS=None, BOT_TOOLS_ENABLED="false"), env_file=None
        )
        self.assertFalse(config.tools_enabled)
        self.assertEqual(config.allowed_user_ids, frozenset())

    def test_malformed_channel_list_fails_instead_of_being_dropped(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(
                env=env(DISCORD_FREE_RESPONSE_CHANNELS="123456789012345678,oops"), env_file=None
            )
        self.assertIn("DISCORD_FREE_RESPONSE_CHANNELS", str(caught.exception))

    def test_admins_must_be_inside_the_allowlist(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(
                env=env(DISCORD_ADMIN_USER_IDS="222222222222222222"), env_file=None
            )
        self.assertIn("DISCORD_ADMIN_USER_IDS", str(caught.exception))

    def test_remote_llm_endpoint_requires_explicit_opt_in(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(env=env(LLM_BASE_URL="https://api.example.com/v1"), env_file=None)
        self.assertIn("LLM_ALLOW_REMOTE", str(caught.exception))

        config = load_config(
            env=env(LLM_BASE_URL="https://api.example.com/v1", LLM_ALLOW_REMOTE="true"),
            env_file=None,
        )
        self.assertFalse(config.llm_is_local)

    def test_unparsable_boolean_fails(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(env=env(BOT_TOOLS_ENABLED="yes-please"), env_file=None)
        self.assertIn("BOT_TOOLS_ENABLED", str(caught.exception))

    def test_load_config_creates_no_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = os.path.join(tmp, "workspace")
            logs = os.path.join(tmp, "logs")
            config = load_config(
                env=env(WORKSPACE_DIR=workspace, SYSTEM_LOG_DIR=logs), env_file=None
            )
            self.assertEqual(config.workspace_dir, workspace)
            self.assertFalse(os.path.exists(workspace))
            self.assertFalse(os.path.exists(logs))


class DiagnosticsTest(unittest.TestCase):
    def test_diagnostics_never_echo_the_token(self):
        secret = "dummy-invalid-token-abcdef123456"
        config = load_config(env=env(DISCORD_BOT_TOKEN=secret), env_file=None)
        text = "\n".join(startup_diagnostics(config))

        self.assertNotIn(secret, text)
        self.assertIn("allowed users: 1", text)
        self.assertIn("tools: enabled", text)

    def test_fingerprint_changes_with_the_policy(self):
        base = load_config(env=env(), env_file=None)
        widened = load_config(
            env=env(DISCORD_ALLOWED_USER_IDS="111111111111111111,222222222222222222"),
            env_file=None,
        )
        self.assertNotEqual(base.fingerprint(), widened.fingerprint())

    def test_config_is_immutable(self):
        config = load_config(env=env(), env_file=None)
        with self.assertRaises(Exception):
            config.discord_token = "changed"
        self.assertIsInstance(config, BotConfig)


if __name__ == "__main__":
    unittest.main()
