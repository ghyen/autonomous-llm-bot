import os
import subprocess
import sys
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

    def test_bom_does_not_corrupt_the_first_key(self):
        # Mutation caught: reading as plain utf-8 leaves the BOM inside the first
        # key, so an editor-written file makes the token look unset - exactly the
        # silent-misconfiguration failure this loader exists to remove.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8-sig") as handle:
                handle.write("DISCORD_BOT_TOKEN=dummy-invalid-token\n")
            values = load_env_file(path)

        self.assertEqual(values, {"DISCORD_BOT_TOKEN": "dummy-invalid-token"})

    def test_trailing_comment_is_not_part_of_an_unquoted_value(self):
        # Mutation caught: keeping the comment made MODEL_NAME literally
        # "gpt-4 # the model", and a numeric field would fail with a message
        # blaming the operator's value rather than the stray comment.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "MODEL_NAME=gpt-4  # the model\n"
                    "LLM_API_KEY=secret#not-a-comment\n"
                    'DISCORD_BOT_TOKEN="quoted # stays"\n'
                )
            values = load_env_file(path)

        self.assertEqual(values["MODEL_NAME"], "gpt-4")
        self.assertEqual(values["LLM_API_KEY"], "secret#not-a-comment")
        self.assertEqual(values["DISCORD_BOT_TOKEN"], "quoted # stays")

    def test_unterminated_quote_is_refused_instead_of_truncated(self):
        # Mutation caught: the old length-2 quote check left the opening quote in
        # place and silently cut the value at the newline, so a multi-line value
        # became '"line one' and started the bot with a wrong secret.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('DISCORD_BOT_TOKEN="line one\nline two"\n')
            with self.assertRaises(ConfigError) as caught:
                load_env_file(path)

        self.assertIn("DISCORD_BOT_TOKEN", str(caught.exception))

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

    def test_tool_limits_have_bounded_defaults(self):
        config = load_config(env=env(), env_file=None)
        self.assertEqual(config.tool_cpu_seconds, 30.0)
        self.assertEqual(config.tool_memory_bytes, 268435456)
        self.assertEqual(config.tool_process_limit, 32)
        self.assertEqual(config.tool_thread_limit, 64)
        self.assertEqual(config.tool_open_files, 64)
        self.assertEqual(config.tool_file_bytes, 10485760)
        self.assertEqual(config.tool_output_bytes, 65536)
        self.assertEqual(config.tool_disk_bytes, 52428800)
        self.assertEqual(config.tool_network_allowlist, ())

    def test_network_allowlist_accepts_origins_and_rejects_ambiguous_values(self):
        config = load_config(
            env=env(TOOL_NETWORK_ALLOWLIST="https://example.com:443,http://127.0.0.1:8080"),
            env_file=None,
        )
        self.assertEqual(
            config.tool_network_allowlist,
            (("https", "example.com", 443), ("http", "127.0.0.1", 8080)),
        )
        for raw in (
            "example.com:443",
            "ftp://example.com",
            "https://user@example.com",
            "https://example.com/path",
            "https://example.com:0",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ConfigError):
                    load_config(env=env(TOOL_NETWORK_ALLOWLIST=raw), env_file=None)


class DiagnosticsTest(unittest.TestCase):
    def test_diagnostics_never_echo_the_token(self):
        secret = "dummy-invalid-token-abcdef123456"
        config = load_config(env=env(DISCORD_BOT_TOKEN=secret), env_file=None)
        text = "\n".join(startup_diagnostics(config))

        self.assertNotIn(secret, text)
        self.assertIn("allowed users: 1", text)
        self.assertIn("tools: enabled", text)

    def test_config_is_immutable(self):
        config = load_config(env=env(), env_file=None)
        with self.assertRaises(Exception):
            config.discord_token = "changed"
        self.assertIsInstance(config, BotConfig)


class DeadlineConfigTest(unittest.TestCase):
    # Mutation caught: changing any documented deadline default silently changes
    # operator-visible stop and timeout behavior.
    def test_defaults_match_the_documented_seconds(self):
        config = load_config(env=env(), env_file=None)
        self.assertEqual(
            (
                config.connect_timeout,
                config.idle_timeout,
                config.model_stage_timeout,
                config.tool_stage_timeout,
                config.bash_timeout,
            ),
            (15.0, 300.0, 600.0, 120.0, 60.0),
        )

    def test_overrides_are_applied(self):
        config = load_config(
            env=env(
                LLM_CONNECT_TIMEOUT_SECONDS="5",
                LLM_IDLE_TIMEOUT_SECONDS="30",
                MODEL_STAGE_TIMEOUT_SECONDS="90",
                TOOL_STAGE_TIMEOUT_SECONDS="45",
                BASH_TIMEOUT_SECONDS="10",
            ),
            env_file=None,
        )
        self.assertEqual(
            (
                config.connect_timeout,
                config.idle_timeout,
                config.model_stage_timeout,
                config.tool_stage_timeout,
                config.bash_timeout,
            ),
            (5.0, 30.0, 90.0, 45.0, 10.0),
        )

    # Mutation caught: hard-coding 60 seconds in the model-facing tool schema
    # misstates the actual shell deadline on an overridden deployment.
    def test_bash_tool_schema_reports_the_configured_timeout(self):
        child_env = dict(os.environ)
        child_env.update(MINIMAL_ENV)
        child_env.update({
            "BASH_TIMEOUT_SECONDS": "7.5",
            "DISCORD_ADMIN_USER_IDS": "",
            "LLM_BASE_URL": "http://127.0.0.1:18080/v1",
        })
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import bot; print(bot.TOOLS_SCHEMA[0]['function']['description'])",
            ],
            cwd=os.path.dirname(__file__),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("timeout은 7.5초", probe.stdout)

    def test_non_numeric_deadline_fails(self):
        with self.assertRaises(ConfigError) as caught:
            load_config(env=env(MODEL_STAGE_TIMEOUT_SECONDS="soon"), env_file=None)
        self.assertIn("MODEL_STAGE_TIMEOUT_SECONDS", str(caught.exception))

    def test_non_positive_deadline_fails(self):
        for bad in ("0", "-5"):
            with self.assertRaises(ConfigError):
                load_config(env=env(TOOL_STAGE_TIMEOUT_SECONDS=bad), env_file=None)

    # Mutation caught: validating only value <= 0 accepts NaN and infinity,
    # which cannot provide a meaningful finite stage budget.
    def test_non_finite_deadline_fails(self):
        for bad in ("nan", "inf", "-inf"):
            with self.subTest(value=bad), self.assertRaises(ConfigError):
                load_config(env=env(MODEL_STAGE_TIMEOUT_SECONDS=bad), env_file=None)

    def test_diagnostics_report_the_deadlines(self):
        text = "\n".join(startup_diagnostics(load_config(env=env(), env_file=None)))
        self.assertIn("deadlines(s):", text)



if __name__ == "__main__":
    unittest.main()
