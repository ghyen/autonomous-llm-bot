#!/usr/bin/env python3
"""Fail if a credential-shaped name is assigned a string literal in source.

The bot used to construct its LLM client with `api_key="not-needed"`. That is
harmless in itself, but an in-source default for a credential-named field is
exactly the pattern that later becomes a real key someone forgot to move to the
environment. Credentials come from configuration, so source keeps none.

Checked: assignment targets, annotated assignments, and keyword arguments whose
name looks like a credential and whose value is a string literal long enough to
be one. Test modules are skipped - they use deliberately invalid dummy values.

Usage: python tools/check_no_credential_defaults.py [path ...]
"""

import ast
import os
import sys

CREDENTIAL_MARKERS = (
    "token",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "access_key",
)

# Below this length a literal cannot plausibly be a working credential, which
# leaves room for placeholders like "-" that some SDKs require to be non-empty.
MIN_CREDENTIAL_LENGTH = 8

SKIP_DIRS = frozenset({".git", "venv", ".venv", "env", "__pycache__", "build", "dist"})


def looks_like_credential_name(name) -> bool:
    lowered = str(name or "").lower()
    return any(marker in lowered for marker in CREDENTIAL_MARKERS)


def suspicious_literal(node) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and len(node.value) >= MIN_CREDENTIAL_LENGTH
    )


def target_names(target):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Attribute):
        yield target.attr
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            for name in target_names(element):
                yield name


def scan_source(path) -> list:
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as error:
        return ["{0}:{1}: 구문 오류로 검사할 수 없습니다: {2}".format(path, error.lineno, error.msg)]

    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and suspicious_literal(node.value):
            for target in node.targets:
                for name in target_names(target):
                    if looks_like_credential_name(name):
                        findings.append((path, node.lineno, name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None and suspicious_literal(node.value):
            for name in target_names(node.target):
                if looks_like_credential_name(name):
                    findings.append((path, node.lineno, name))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg
                    and looks_like_credential_name(keyword.arg)
                    and suspicious_literal(keyword.value)
                ):
                    findings.append((path, keyword.value.lineno, keyword.arg))

    return [
        "{0}:{1}: '{2}'에 문자열 리터럴이 기본값으로 들어 있습니다. 설정에서 읽으세요.".format(*found)
        for found in findings
    ]


def python_sources(roots) -> list:
    found = []
    for root in roots:
        if os.path.isfile(root):
            found.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
            for filename in sorted(filenames):
                if filename.endswith(".py") and not filename.startswith("test_"):
                    found.append(os.path.join(dirpath, filename))
    return found


def main(argv) -> int:
    roots = argv[1:] or ["."]
    problems = []
    for path in python_sources(roots):
        problems.extend(scan_source(path))

    if problems:
        print("자격 증명 기본값이 소스에 있습니다:", file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1

    print("소스 내 자격 증명 기본값 없음.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
