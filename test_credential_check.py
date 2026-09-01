"""Checks for the credential-default CI gate, which shipped with none.

The gate is the only thing standing between a rewrite of the LLM client and a
reintroduced `api_key="not-needed"`, so it needs to fail on the shapes it claims
to catch and stay quiet on the ones it must not.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "tools"))

from check_no_credential_defaults import python_sources, scan_source


def findings_for(source: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sample.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        return scan_source(path)


class CredentialDefaultTest(unittest.TestCase):
    def test_flags_every_credential_shaped_assignment(self):
        for label, source in (
            ("plain assignment", 'api_key = "not-needed-here"\n'),
            ("keyword argument", 'client = Thing(api_key="sk-abcdefghijkl")\n'),
            ("annotated assignment", 'TOKEN: str = "abcdefghijklmnop"\n'),
            ("attribute assignment", 'obj.secret = "abcdefghijklmn"\n'),
        ):
            with self.subTest(label):
                self.assertEqual(len(findings_for(source)), 1)

    def test_leaves_alone_what_is_not_a_credential_default(self):
        for label, source in (
            # Too short to be a working credential; some SDKs need a non-empty value.
            ("short literal", 'token = "abc"\n'),
            ("non-credential name", 'greeting = "just a long string here"\n'),
            ("read from configuration", "api_key = os.environ['LLM_API_KEY']\n"),
            ("annotation without a value", "api_key: str\n"),
        ):
            with self.subTest(label):
                self.assertEqual(findings_for(source), [])

    def test_a_syntax_error_is_reported_rather_than_passed_over(self):
        findings = findings_for("def broken(:\n")
        self.assertEqual(len(findings), 1)
        self.assertIn("구문 오류", findings[0])

    def test_test_modules_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("bot.py", "test_bot.py"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                    handle.write("x = 1\n")
            os.mkdir(os.path.join(tmp, "__pycache__"))
            with open(os.path.join(tmp, "__pycache__", "cached.py"), "w") as handle:
                handle.write("x = 1\n")
            collected = {os.path.basename(path) for path in python_sources([tmp])}

        self.assertEqual(collected, {"bot.py"})


if __name__ == "__main__":
    unittest.main()
